import asyncio
import json
import logging
import re
import secrets
import sys
from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import IO, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import SecretStr
from sqlalchemy import text

from backend import config, metrics, telemetry
from backend.api import router
from backend.config import Settings, get_settings, rules
from backend.database import Store, utcnow
from backend.runtime import Runtime
from backend.service import HubService
from backend.streams import Fanout, create_bus

# LogRecord attributes that are not caller-supplied extras (color_message is uvicorn's duplicate message).
STANDARD_ATTRIBUTES = set(vars(logging.LogRecord('', 0, '', 0, '', None, None))) | {'message', 'asctime', 'color_message',
                                                                                    'taskName'}
REDACTIONS = (
    (re.compile(r'([?&;](?:api[_-]?key|apikey|key|token|access_token|secret|password|signature|sig)=)[^&\s"\'<>]+',
                re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'(\b(?:api[_-]?key|apikey|access_token|token|secret|password)\s*[=:]\s*)[^&\s"\'<>,;)]+',
                re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'(Bearer\s+)[A-Za-z0-9._~+/=-]+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'/bot\d+:[A-Za-z0-9_-]+'), '/bot[REDACTED]'),
)
QUIET_LOGGERS = ('httpx', 'httpcore', 'hpack', 'websockets.client')
UVICORN_LOGGERS = ('uvicorn', 'uvicorn.error', 'uvicorn.access')
_known_secrets: list[str] = []


def redact(value: str) -> str:
    for pattern, replacement in REDACTIONS:
        value = pattern.sub(replacement, value)
    for secret in _known_secrets:
        value = value.replace(secret, '[REDACTED]')
    return value


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value if isinstance(value, (int, float, bool)) or value is None else redact(str(value))


class JsonFormatter(logging.Formatter):
    """One JSON object per line with every caller extra, request/trace ids and redacted secrets."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {'timestamp': utcnow().isoformat(), 'level': record.levelname, 'logger': record.name,
                                 'message': redact(record.getMessage()),
                                 'request_id': telemetry.request_id_var.get(), 'trace_id': telemetry.trace_id_var.get()}
        entry.update({k: _clean(v) for k, v in vars(record).items() if k not in STANDARD_ATTRIBUTES})
        if record.exc_info and record.exc_info[0] is not None:
            entry['exc_type'] = record.exc_info[0].__name__
            entry['exception'] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


class RedactingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def secret_values(*sources: Any) -> list[str]:
    """Non-empty SecretStr values of settings objects; short values would redact ordinary words."""
    found = []
    for source in sources:
        for name in type(source).model_fields:
            value = getattr(source, name)
            if isinstance(value, SecretStr) and len(value.get_secret_value()) >= 6:
                found.append(value.get_secret_value())
    return sorted(set(found), key=len, reverse=True)


def configure_logging(config: telemetry.TelemetrySettings | None = None, secret_list: Iterable[str] = (),
                      stream: IO[str] | None = None) -> None:
    """JSON (or redacted text) logs on the root logger; uvicorn loggers propagate to it; httpx URL logs are
    silenced because some providers pass API keys as query parameters."""
    config = config or telemetry.TelemetrySettings()
    _known_secrets[:] = sorted(set(secret_list), key=len, reverse=True)
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, 'hub_handler', False)]:
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.hub_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter() if config.log_format == 'json' else
                         RedactingTextFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    root.addHandler(handler)
    root.setLevel(config.log_level.upper())
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers, logger.propagate = [], True


def uvicorn_log_config() -> dict:
    """Pass as uvicorn log_config so uvicorn does not install its own text handlers."""
    return {'version': 1, 'disable_existing_loggers': False,
            'loggers': {name: {'handlers': [], 'propagate': True, 'level': 'INFO'} for name in UVICORN_LOGGERS}}


class RequestContextMiddleware:
    """Pure ASGI (safe for WebSockets and streaming): accepts or creates X-Request-ID, echoes it, and traces
    the request. Invalid inbound ids are replaced, never reflected."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope['type'] not in {'http', 'websocket'}:
            await self.app(scope, receive, send)
            return
        headers = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope.get('headers', [])}
        request_id = telemetry.incoming_request_id(headers)
        token = telemetry.request_id_var.set(request_id)
        status: dict[str, int] = {}

        async def send_with_id(message: dict) -> None:
            if message['type'] == 'http.response.start':
                status['code'] = message['status']
                message = dict(message, headers=[*message.get('headers', []), (b'x-request-id', request_id.encode())])
            await send(message)
        try:
            with telemetry.span('http.request', method=scope.get('method', 'WEBSOCKET'), path=scope.get('path', ''),
                                request_id=request_id) as current:
                await self.app(scope, receive, send_with_id)
                if 'code' in status:
                    current.set_attribute('http.status_code', status['code'])
        finally:
            telemetry.request_id_var.reset(token)


class BodySizeLimitMiddleware:
    """Pure ASGI: rejects request bodies above max_request_bytes (declared Content-Length or streamed) with 413."""

    def __init__(self, app: Any, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get('headers', [])).get(b'content-length')
        if declared is not None and (not declared.isdigit() or int(declared) > self.limit):
            await self._reject(send)
            return
        received = 0

        async def limited() -> dict:
            nonlocal received
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > self.limit:
                    raise RequestTooLarge
            return message
        try:
            await self.app(scope, limited, send)
        except RequestTooLarge:
            await self._reject(send)

    async def _reject(self, send: Any) -> None:
        body = b'{"detail":"Request body too large"}'
        await send({'type': 'http.response.start', 'status': 413,
                    'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': body})


class RequestTooLarge(Exception):
    pass


LOOPBACK_HOSTS = {'127.0.0.1', '::1', 'localhost'}


def local_request(request: Request, allowed_origins: list[str]) -> bool:
    """A request made on this computer to this computer: loopback client, loopback Host header (a DNS-rebinding page
    keeps its own host name) and, when a browser sends one, an allowed Origin."""
    client = request.client.host if request.client else ''
    if not (client in LOOPBACK_HOSTS or client.startswith('127.')):
        return False
    host = (urlsplit(f"//{request.headers.get('host', '')}").hostname or '').lower()
    if host not in LOOPBACK_HOSTS and not host.startswith('127.'):
        return False
    origin = request.headers.get('origin')
    return origin is None or origin in allowed_origins


BENIGN_DISCONNECTS = (ConnectionResetError, ConnectionAbortedError)


def install_disconnect_filter(loop: asyncio.AbstractEventLoop) -> None:
    """Windows Proactor logs a traceback when it shuts down a socket the browser already reset
    (page reload, closed tab). The request itself completed; hide only that case."""
    previous = loop.get_exception_handler()

    def handler(current: asyncio.AbstractEventLoop, context: dict) -> None:
        exception = context.get('exception')
        where = f"{context.get('message', '')} {context.get('handle', '')}"
        if isinstance(exception, BENIGN_DISCONNECTS) and '_call_connection_lost' in where:
            logging.getLogger(__name__).debug('client_disconnected_abruptly')
            return
        if previous is not None:
            previous(current, context)
        else:
            current.default_exception_handler(context)

    loop.set_exception_handler(handler)


def create_app() -> FastAPI:
    settings: Settings = get_settings()
    from backend.notifications import NotificationSettings

    def notification_settings() -> NotificationSettings:
        return NotificationSettings(_env_file=config.ENV_FILE)
    configure_logging(secret_list=secret_values(settings, notification_settings()))
    telemetry.setup_tracing()
    telemetry.remove_span_listener(metrics.span_listener)
    telemetry.add_span_listener(metrics.span_listener)
    store = Store(settings.database_url)
    hub = HubService(store, settings)

    def refresh_redaction(saved: Settings) -> None:
        """Secrets saved from Settings -> Data providers are redacted from logs by value from now on."""
        _known_secrets[:] = secret_values(saved, notification_settings())
    hub.settings_listeners.append(refresh_redaction)
    bus = create_bus(settings.redis_url)
    hub.bus = bus  # service and runtime publish quotes/events/decisions/alerts/briefs on one shared bus
    runtime = Runtime(hub, bus)
    fanout = Fanout(bus, snapshot=hub.dashboard)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        install_disconnect_filter(asyncio.get_running_loop())
        store.initialize()
        hub.initialize()
        fanout.start()
        if settings.enable_ingestion:
            await runtime.start()
        yield
        await runtime.stop()
        await fanout.stop()
        await bus.close()
        store.engine.dispose()
    app = FastAPI(title='Market Intelligence & Decision Hub', version='0.1.0', lifespan=lifespan)
    app.state.store = store
    app.state.hub = hub
    app.state.bus = bus
    app.state.fanout = fanout
    # Innermost: JSON payloads of several hundred KB (dashboard, asset detail) go out gzip-compressed when accepted.
    compression = rules()['ui']['http_compression']
    app.add_middleware(GZipMiddleware, minimum_size=compression['minimum_bytes'], compresslevel=compression['level'])
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=['GET', 'POST', 'PUT'],
                       allow_headers=['Content-Type', 'X-Admin-Token', 'X-Local-Admin', 'X-Request-ID'],
                       expose_headers=['X-Request-ID'])
    app.add_middleware(BodySizeLimitMiddleware, limit=settings.max_request_bytes)
    app.add_middleware(RequestContextMiddleware)

    def admin(request: Request, x_admin_token: str = Header(default=''), x_local_admin: str = Header(default='')) -> None:
        """ADMIN_AUTH=local (default, single user on this computer): no password; the request must come from this
        machine to a loopback Host (no DNS rebinding), from an allowed Origin, and carry X-Local-Admin (a custom header
        forces a CORS preflight, so other websites cannot send it). ADMIN_AUTH=token: X-Admin-Token is required."""
        expected = settings.admin_token.get_secret_value()
        if expected and x_admin_token and secrets.compare_digest(expected.encode(), x_admin_token.encode()):
            return
        if settings.admin_auth == 'local':
            if x_local_admin == '1' and local_request(request, settings.cors_origins):
                return
            raise HTTPException(403, 'Administration is only available from this computer (open the app at 127.0.0.1)')
        if not expected:
            raise HTTPException(503, 'Administrative writes disabled: configure ADMIN_TOKEN')
        raise HTTPException(401, 'Invalid administrative token')
    app.state.admin = admin

    @app.get('/health')
    def health():
        return {'status': 'ok', 'time': utcnow().isoformat()}

    @app.get('/health/ready')
    async def ready():
        try:
            with store.engine.connect() as connection:
                connection.execute(text('SELECT 1'))
            if settings.redis_url:
                from redis.asyncio import Redis
                client = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
                try:
                    await client.ping()
                finally:
                    await client.aclose()
        except Exception:
            raise HTTPException(503, 'Infrastructure unavailable') from None
        return {'status': 'ready', 'database': store.engine.dialect.name,
                'redis': 'configured' if settings.redis_url else 'local mode', 'event_bus': bus.backend}

    def observed() -> tuple[list[dict], list[dict]]:
        runs = store.list('ai_runs', rules()['refresh']['metrics']['ai_run_window'])
        return metrics.provider_rows(hub.providers), metrics.ai_model_rows(runs)

    @app.get('/metrics', response_class=PlainTextResponse)
    def prometheus():
        return PlainTextResponse(metrics.render_prometheus(*observed()), media_type='text/plain; version=0.0.4')

    @app.get('/metrics/json')
    def metrics_json():
        return metrics.render_json(*observed())

    @app.websocket('/ws')
    async def stream(websocket: WebSocket):
        origin = websocket.headers.get('origin')
        if origin and origin not in settings.cors_origins:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = fanout.subscribe()
        try:
            while True:
                await websocket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError, *BENIGN_DISCONNECTS):
            pass
        finally:
            fanout.unsubscribe(queue)
    app.include_router(router(hub, admin))
    return app


app = create_app()
