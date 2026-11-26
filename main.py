"""Start Market Intelligence locally with one command:  python main.py

Works from any Python interpreter and any working directory: it continues inside the
project's .venv, starts the FastAPI backend and the Next.js UI, and opens the browser.
Ctrl+C stops both. Use --api-only to run just the backend (for example under a debugger).
"""
import argparse
import atexit
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / 'frontend'
VENV = ROOT / '.venv'
VENV_PYTHON = VENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
HOST = '127.0.0.1'
DEFAULT_UI_PORT = 3000
DEFAULT_API_PORT = 8000
DEV_PORT_SCAN = range(3000, 3011)
UI_MARKER = '<title>Market Intelligence</title>'
API_TITLE = 'Market Intelligence & Decision Hub'
REQUIRED_MODULES = ('uvicorn', 'fastapi', 'sqlalchemy', 'pydantic_settings', 'exchange_calendars', 'websockets')
READY_TIMEOUT_SECONDS = 180
# Windows Proactor closes the listening socket when a client resets before accept (WinError 64) and the API then
# stops accepting connections; the selector loop retries instead. Elsewhere uvicorn's default is kept.
SERVER_LOOP = 'asyncio:SelectorEventLoop' if sys.platform == 'win32' else 'auto'
PIP_INSTALL = '.venv\\Scripts\\python -m pip install -e .[dev]'


def say(message: str) -> None:
    print(f'[launcher] {message}', flush=True)


def fail(message: str) -> None:
    print(f'\n[launcher] {message}\n', file=sys.stderr, flush=True)
    sys.exit(1)


def ensure_project_python() -> None:
    """Global Python or another IDE interpreter lacks the project's packages; hand over to .venv."""
    if Path(sys.prefix).resolve() == VENV.resolve():
        return
    if not VENV_PYTHON.exists():
        fail(f'The project environment .venv does not exist. Create it once:\n'
             f'  cd {ROOT}\n  python -m venv .venv\n  {PIP_INSTALL}')
    say(f'Current interpreter {sys.executable} is not the project environment; continuing with {VENV_PYTHON}')
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    child = subprocess.Popen([str(VENV_PYTHON), str(ROOT / 'main.py'), *sys.argv[1:]], cwd=ROOT)
    while child.poll() is None:
        try:
            time.sleep(0.5)
        except KeyboardInterrupt:
            # The child receives the same Ctrl+C / Ctrl+Break and shuts API and UI down itself.
            continue
    sys.exit(child.returncode)


def ensure_dependencies() -> None:
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        fail(f"Packages missing from .venv: {', '.join(missing)}. Install them once:\n  cd {ROOT}\n  {PIP_INSTALL}")
    if not (ROOT / '.env').exists():
        say('No .env file: providers without credentials will show NOT CONFIGURED. To configure them:\n'
            f'  cd {ROOT}\n  copy .env.example .env\n  .venv\\Scripts\\python -m scripts.init_local')


def listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, port)) == 0


def fetch(url: str, timeout: float = 3, limit: int = 200_000) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read(limit).decode('utf-8', 'replace')
    except (urllib.error.URLError, OSError, ValueError):
        return None


def is_project_api(port: int) -> bool:
    try:
        schema = json.loads(fetch(f'http://{HOST}:{port}/openapi.json', limit=10_000_000) or '')
        return isinstance(schema, dict) and schema.get('info', {}).get('title') == API_TITLE
    except (ValueError, AttributeError):
        return False


def cors_allows(api_port: int, ui_port: int) -> bool:
    origin = f'http://{HOST}:{ui_port}'
    request = urllib.request.Request(f'http://{HOST}:{api_port}/health', headers={'Origin': origin})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.headers.get('access-control-allow-origin') == origin
    except (urllib.error.URLError, OSError, ValueError):
        return False


def api_already_running(args: argparse.Namespace) -> bool:
    port = args.api_port
    if not listening(port):
        return False
    if not is_project_api(port):
        fail(f'Port {port} is used by another program. Close it, or start on another port: python main.py --api-port 8010')
    if not args.api_only and not cors_allows(port, args.ui_port):
        fail(f'The API already running on port {port} does not accept a UI on port {args.ui_port} (CORS).\n'
             'Stop that API, or start with the same ports it was started with.')
    say(f'The API is already running on port {port}; reusing it.')
    return True


def existing_dev_server() -> str | None:
    """Next.js allows one dev server per folder and records it in .next/dev/lock."""
    lock = FRONTEND / '.next' / 'dev' / 'lock'
    try:
        candidates = [str(json.loads(lock.read_text(encoding='utf-8'))['appUrl'])]
    except PermissionError:
        # Windows: a live dev server keeps the lock open exclusively; locate it by its page instead.
        found = [f'http://{HOST}:{port}' for port in DEV_PORT_SCAN
                 if listening(port) and UI_MARKER in (fetch(f'http://{HOST}:{port}/', timeout=60) or '')]
        return found[0] if found else f'a port outside {DEV_PORT_SCAN.start}-{DEV_PORT_SCAN.stop - 1}'
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return next((url for url in candidates if UI_MARKER in (fetch(url, timeout=60) or '')), None)


def start_ui(ui_port: int, api_port: int) -> subprocess.Popen | None:
    if listening(ui_port):
        page = fetch(f'http://{HOST}:{ui_port}/', timeout=60)
        if page and UI_MARKER in page:
            if api_port != DEFAULT_API_PORT:
                # A running Next.js server keeps the API address it was started with.
                fail(f'A UI is already running on port {ui_port} and cannot be switched to API port {api_port}.\n'
                     'Stop it, or choose another UI port: python main.py --ui-port 3001')
            say(f'The UI is already running on port {ui_port}; reusing it.')
            return None
        fail(f'Port {ui_port} is used by another program. Close it, or start on another port: python main.py --ui-port 3001')
    running = existing_dev_server()
    if running:
        fail(f'A UI dev server for this project is already running on {running}, and Next.js allows only one per folder.\n'
             'Close the window where it runs (or stop its node.exe process), then start again.')
    node = shutil.which('node')
    if not node:
        fail('Node.js 20.9+ is required for the UI: https://nodejs.org\n'
             'The backend alone can be started with: python main.py --api-only')
    next_cli = FRONTEND / 'node_modules' / 'next' / 'dist' / 'bin' / 'next'
    if not next_cli.exists():
        fail(f'UI packages are not installed. Install them once:\n  cd {FRONTEND}\n  npm ci')
    say(f'Starting the UI on port {ui_port} (the first page load compiles and can take ~30 s).')
    # Own process group: Ctrl+C reaches only the launcher, which then stops the whole UI tree.
    return subprocess.Popen([str(node), str(next_cli), 'dev', '--hostname', HOST, '--port', str(ui_port)],
                            cwd=FRONTEND, env=dict(os.environ, NEXT_PUBLIC_API_URL=f'http://{HOST}:{api_port}/api'),
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
                            start_new_session=os.name != 'nt')


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, check=False)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


class Supervisor:
    """Reports readiness once, then stops the API if the UI it started dies."""

    def __init__(self, args: argparse.Namespace, ui: subprocess.Popen | None, server: Any):
        self.args, self.ui, self.server = args, ui, server
        self.ui_port = None if args.api_only else args.ui_port
        self.stopping = threading.Event()
        self.failed = threading.Event()

    def ui_died(self) -> bool:
        if self.failed.is_set():
            return True
        if self.ui is None or self.ui.poll() is None or self.stopping.is_set():
            return False
        say(f'The UI stopped unexpectedly (exit code {self.ui.returncode}); see its messages above. Shutting down.')
        self.failed.set()
        if self.server is not None:
            self.server.should_exit = True
        return True

    def ready(self) -> bool:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        api_ok, ui_ok = False, self.ui_port is None
        while not (api_ok and ui_ok):
            # Probing a server that is closing races its accept loop on Windows; stop probing on shutdown.
            if self.ui_died() or self.stopping.is_set() or getattr(self.server, 'should_exit', False):
                return False
            if time.monotonic() > deadline:
                say('Startup is taking longer than expected; check the messages above.')
                return False
            api_ok = api_ok or fetch(f'http://{HOST}:{self.args.api_port}/health/ready') is not None
            ui_ok = ui_ok or UI_MARKER in (fetch(f'http://{HOST}:{self.ui_port}/', timeout=90) or '')
            time.sleep(1)
        return True

    def watch(self) -> None:
        if self.ready():
            api = f'http://{HOST}:{self.args.api_port}'
            ui = f'http://{HOST}:{self.ui_port}' if self.ui_port else None
            lines = ['', '  Market Intelligence is running', *([f'  UI:        {ui}'] if ui else []),
                     f'  API docs:  {api}/docs', '  Stop:      Ctrl+C in this window', '']
            print('\n'.join(lines), flush=True)
            if not self.args.no_browser:
                webbrowser.open(ui or f'{api}/docs')
        while self.ui is not None and not self.stopping.is_set() and not self.ui_died():
            time.sleep(2)


def parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Start the Market Intelligence API and UI.')
    parser.add_argument('--api-only', action='store_true', help='start only the FastAPI backend')
    parser.add_argument('--no-browser', action='store_true', help='do not open the browser')
    parser.add_argument('--api-port', type=int, default=DEFAULT_API_PORT)
    parser.add_argument('--ui-port', type=int, default=DEFAULT_UI_PORT)
    return parser.parse_args()


def main() -> None:
    args = parse()
    # .env and the default SQLite URL (sqlite:///./market_hub.db) are relative to the project root.
    os.chdir(ROOT)
    ensure_project_python()
    if hasattr(signal, 'SIGBREAK'):
        # uvicorn re-raises Ctrl+Break after shutdown; the CRT default would exit(3) and skip the UI cleanup.
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    ensure_dependencies()
    if args.ui_port != DEFAULT_UI_PORT and 'CORS_ORIGINS' not in os.environ:
        os.environ['CORS_ORIGINS'] = json.dumps([f'http://localhost:{args.ui_port}', f'http://{HOST}:{args.ui_port}'])
    server = None
    if not api_already_running(args):
        import uvicorn
        server = uvicorn.Server(uvicorn.Config('backend.main:app', host=HOST, port=args.api_port, loop=SERVER_LOOP))
    ui = None if args.api_only else start_ui(args.ui_port, args.api_port)
    atexit.register(stop, ui)
    supervisor = Supervisor(args, ui, server)
    watcher = threading.Thread(target=supervisor.watch, daemon=True)
    watcher.start()
    try:
        if server is not None:
            server.run()
        elif ui is not None:
            # Popen.wait() cannot be interrupted by Ctrl+C on Windows; poll instead.
            while ui.poll() is None:
                time.sleep(0.5)
            supervisor.ui_died()
        else:
            watcher.join()  # API and UI already running: report the URLs, open the browser, then exit
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.stopping.set()
        stop(ui)
    if supervisor.failed.is_set():
        sys.exit(1)


if __name__ == '__main__':
    main()
