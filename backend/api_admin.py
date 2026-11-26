"""Administrative writes. Every change requires X-Admin-Token and leaves an audit_logs entry; configuration edits
write a new versioned fragment that must load cleanly, otherwise the previous file is restored (spec §13, §33, §37)."""
import copy
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import Field

from backend import ai_budget, calibration, provider_settings
from backend.config import CONFIG_DIR, get_settings, reload_rules
from backend.database import uid, utcnow
from backend.engines import surprise
from backend.providers.base import ProviderUnavailable
from backend.providers.news import SECProvider, x_source_for
from backend.schemas import ROLE_SCHEMAS, CalendarInput, DocumentInput, RoleAssignment, StrictModel
from backend.service import HubService

logger = logging.getLogger(__name__)

# Fragment -> (file, top-level key, editable leaf paths). Scoring weights must still sum to 1 (validated by decide).
EDITABLE: dict[str, tuple[str, str | None, set[str]]] = {
    'refresh': ('rules.d/refresh.json', 'refresh', {'cadences'}),
    'alerts': ('rules.d/alerts.json', 'alerts', {'priorities', 'cooldown_seconds', 'enabled_kinds', 'min_priority', 'channels'}),
    'scoring': ('rules.json', None, {'weights', 'thresholds'}),
    # The daily AI request budget (docs/AI_AGENTS.md): typed checks in backend/ai_budget.py instead of _shape_errors.
    'ai_budget': ('rules.d/ai_runtime.json', 'budget', set(ai_budget.EDITABLE_KEYS)),
}
# Fragments whose file keeps its version under another name than '<fragment>_version'.
VERSION_KEYS = {'ai_budget': 'ai_runtime_version'}


class SearchRequest(StrictModel):
    query: str = Field(min_length=3, max_length=512)
    priority: int = Field(default=3, ge=0, le=3)


class SourceOverride(StrictModel):
    tier: int | None = Field(default=None, ge=1, le=5)
    trust_weight: float | None = Field(default=None, ge=0, le=1)
    enabled: bool | None = None
    reason: str = Field(min_length=3, max_length=500)


class ProviderSwitch(StrictModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=200)


class BacktestRequest(StrictModel):
    start: datetime
    end: datetime
    step_seconds: int = Field(default=300, ge=60)
    symbols: list[str] | None = None
    weights: dict[str, float] | None = None
    horizons: list[str] | None = None


def _bump(version: str) -> str:
    parts = [int(p) for p in version.split('.')]
    parts[-1] += 1
    return '.'.join(str(p) for p in parts)


def _shape_errors(current: Any, proposed: Any, path: str = '') -> list[str]:
    """An edit may change values, never the structure: same keys, same JSON types (int/float interchangeable)."""
    if isinstance(current, dict):
        if not isinstance(proposed, dict):
            return [f'{path or "value"}: expected an object']
        errors = [f'{path}.{k}'.lstrip('.') + ': unknown key' for k in proposed if k not in current]
        return errors + [e for k, v in proposed.items() if k in current for e in _shape_errors(current[k], v, f'{path}.{k}')]
    if isinstance(current, bool) or isinstance(proposed, bool):
        return [] if isinstance(current, bool) and isinstance(proposed, bool) else [f'{path}: expected a boolean']
    if isinstance(current, (int, float)):
        return [] if isinstance(proposed, (int, float)) and proposed >= 0 else [f'{path}: expected a non-negative number']
    if isinstance(current, list):
        return [] if isinstance(proposed, list) else [f'{path}: expected a list']
    return [] if type(current) is type(proposed) else [f'{path}: expected {type(current).__name__}']


def _write_atomic(path: Path, text: str) -> None:
    """Readers (the rules() cache) never see a half-written file."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


_CONFIG_LOCK = threading.Lock()


def _deep_update(target: dict, changes: dict) -> None:
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def admin_router(hub: HubService, admin) -> APIRouter:
    api = APIRouter(prefix='/admin', dependencies=[Depends(admin)])

    def audit(action: str, **fields: Any) -> None:
        hub.store.put('audit_logs', uid(), dict(action=action, timestamp=utcnow().isoformat(), **fields))

    async def analyse(operation: str) -> list[dict] | None:
        """One full analysis cycle. A failure is recorded in the dashboard 'analysis' status and logged with its type
        (never a stack trace in the response); None then."""
        try:
            return await hub.recalculate()
        except Exception as exc:
            logger.error('recalculate_failed', extra={'operation': operation, 'error_type': type(exc).__name__},
                         exc_info=True)
            return None

    @api.post('/documents')
    async def ingest(document: DocumentInput):
        try:
            result = hub.ingest_document(document)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit('document_import', source_id=document.source_id, url=str(document.url), event_id=result.get('id'))
        # The document is stored and audited either way: a failed analysis does not undo the import.
        if await analyse('document_import') is None:
            return dict(result, analysis_error='The document was imported, but the full analysis that followed failed; '
                                               'the previous decisions stay published. Details are in the server log.')
        return result

    @api.post('/recalculate')
    async def recalculate():
        audit('recalculate')
        decisions = await analyse('recalculate')
        if decisions is None:
            raise HTTPException(500, 'The full analysis failed; the previous decisions stay published. '
                                     'Details are in the server log.')
        return decisions

    @api.post('/models/discover')
    async def discover():
        audit('model_discovery')
        try:
            return await hub.ai.discover()
        except ProviderUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @api.post('/ollama/test')
    async def test_ollama():
        audit('ollama_test')
        # Queued as 'integration_probe' (ai_runtime.limiter): first in the LLM queue with a short wait, so the Settings
        # test answers within about half a minute even while the extraction backlog fills the queue.
        result, run_id = await hub.ai.run_with_id('auditor', {'integration_probe': True, 'evidence_ids': [],
                                                              'instruction_context': 'No market prices or verified facts were supplied. Publication must be blocked.'},
                                                  queue_role='integration_probe')
        success = bool(result and result.get('status') == 'BLOCK')
        # The probe runs the auditor role, so it counts against the auditor's daily cap; a refusal sent nothing and
        # says so ('role' | 'daily' | 'pace'), instead of reading as an invalid schema.
        refusal = (hub.store.get('ai_runs', run_id) or {}).get('budget_refusal') if run_id and result is None else None
        return {'success': success, 'schema_valid': None if refusal else result is not None,
                'publication_blocked': success, 'budget_refusal': refusal,
                'error': ai_budget.REFUSAL_MESSAGES.get(refusal) if refusal else None,
                'provider_status': hub.ollama.health['status'], 'checked_at': utcnow().isoformat()}

    @api.put('/roles/{role}')
    def assign(role: str, assignment: RoleAssignment):
        if role not in ROLE_SCHEMAS:
            raise HTTPException(404, 'Unknown AI role')
        for name in assignment.model_dump().values():
            if name and not (hub.store.get('ai_models', name) or {}).get('availability'):
                raise HTTPException(422, 'Model unavailable; discover models first')
        payload = dict(assignment.model_dump(), role=role)
        hub.store.put('ai_role_assignments', role, payload)
        audit('role_assignment', role=role, assignment=payload)
        return payload

    @api.put('/sources/{source_id}')
    def override_source(source_id: str, change: SourceOverride):
        """Trust changes are human decisions with a reason; the LLM has no path to this endpoint."""
        if not hub.store.get('sources', source_id):
            raise HTTPException(404, 'Unknown source')
        previous = hub.store.get('source_reliability', source_id) or {}
        updated = dict(previous, **{k: v for k, v in change.model_dump().items() if v is not None and k != 'reason'},
                       updated_at=utcnow().isoformat())
        hub.store.put('source_reliability', source_id, updated)
        audit('source_override', source_id=source_id, previous=previous, current=updated, reason=change.reason)
        return updated

    @api.put('/config/{fragment}')
    def edit_config(fragment: str, changes: dict):
        if fragment not in EDITABLE:
            raise HTTPException(404, f'Editable fragments: {sorted(EDITABLE)}')
        filename, key, allowed = EDITABLE[fragment]
        unknown = set(changes) - allowed
        if unknown:
            raise HTTPException(422, f'Not editable here: {sorted(unknown)}')
        path = CONFIG_DIR / filename
        with _CONFIG_LOCK:  # one edit at a time: read-modify-write of the same file
            original = path.read_text(encoding='utf-8')
            data = json.loads(original)
            section = data.get(key) if key else data
            if not isinstance(section, dict):  # e.g. an ai_runtime.json without its budget section
                raise HTTPException(422, f'Rejected: the configuration has no {key} section to edit')
            if fragment == 'ai_budget':
                # Typed limits: whole numbers for the daily budget (0-10000) and the known roles' caps (0-5000),
                # reserve share 0-0.9, burst window 1-24 hours; notes are never edited.
                proposed = copy.deepcopy(section)
                _deep_update(proposed, changes)
                errors = ai_budget.change_errors(changes, ROLE_SCHEMAS) or ai_budget.validate(proposed, ROLE_SCHEMAS)
            else:
                errors = _shape_errors({k: section.get(k) for k in changes}, changes)
            if errors:
                raise HTTPException(422, f'Rejected: {"; ".join(errors[:10])}')
            before = copy.deepcopy({k: section.get(k) for k in changes})
            _deep_update(section, changes)
            version_key = VERSION_KEYS.get(fragment) or (f'{fragment}_version' if key else 'version')
            data[version_key] = _bump(str(data.get(version_key, '1.0.0')))
            _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
            try:
                config = reload_rules()
                if fragment == 'scoring' and abs(sum(config['weights'].values()) - 1) > 1e-9:
                    raise ValueError('Scoring weights must sum to 1')
                if fragment == 'ai_budget' and (problems := ai_budget.validate(config.get('budget'), ROLE_SCHEMAS)):
                    raise ValueError('; '.join(problems))
            except (ValueError, KeyError) as exc:
                _write_atomic(path, original)
                reload_rules()
                raise HTTPException(422, f'Rejected, previous configuration restored: {exc}') from None
        hub.store.put('configuration_versions', f"{config['version']}:{config['content_hash'][:16]}",
                      dict(config, stored_at=utcnow().isoformat(), changed_fragment=fragment))
        audit('config_change', fragment=fragment, version=data[version_key], before=before, after=changes)
        return {'fragment': fragment, 'version': data[version_key], 'content_hash': config['content_hash']}

    # --- provider settings (docs/PROVIDER_SETTINGS.md): async so hub.providers is swapped on the event loop ----------

    @api.get('/provider-settings')
    async def provider_settings_view():
        return provider_settings.summary(hub)

    @api.put('/provider-settings/{group_id}')
    async def save_provider_settings(group_id: str, payload: dict[str, Any] = Body(...)):
        """Secrets are written to .env and never echoed: errors name fields only (the body is not a model, so a
        malformed value is never reflected by request validation). 422 also covers the writer's policies: a changed
        destination (base URL, SMTP host or port) without its stored secret in the same request, an SMTP login without
        SSL/TLS or STARTTLS to a remote server, and a value that would not read back exactly from .env."""
        spec = provider_settings.group(group_id)
        if spec is None:
            raise HTTPException(404, 'Unknown provider settings group')
        values = payload.get('values')
        if not isinstance(values, dict) or set(payload) - {'values'}:
            raise HTTPException(422, 'Expected {"values": {field: value or null}}')
        allowed = {f['name']: f for f in spec['fields']}
        unknown = sorted(set(values) - set(allowed))
        if unknown:
            raise HTTPException(422, f"Unknown fields for {group_id}: {', '.join(unknown)}")
        locked = sorted(name for name in values if provider_settings.in_environment(name))
        if locked:
            raise HTTPException(409, f"Set in the OS environment, which overrides .env (change it there and restart): "
                                     f"{', '.join(name.upper() for name in locked)}")
        try:
            for name, value in values.items():
                provider_settings.validate_value(allowed[name], value)
            changed, removed = provider_settings.write_values(values)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        get_settings.cache_clear()
        hub.apply_settings(get_settings(), changed + removed)
        audit('provider_settings_change', group=group_id, changed=changed, removed=removed)
        return provider_settings.summary(hub, only=group_id)['groups'][0]

    @api.put('/providers/{name}/enabled')
    async def switch_provider(name: str, change: ProviderSwitch):
        if name not in provider_settings.instances(hub):
            raise HTTPException(404, 'Unknown provider')
        hub.set_enabled(name, change.enabled, change.reason)
        audit('provider_enabled' if change.enabled else 'provider_disabled', provider=name, reason=change.reason)
        current = provider_settings.instances(hub).get(name)  # a stub can give way to the real adapter on rebuild
        status = provider_settings.status_of(current) if current is not None else 'NOT CONFIGURED'
        return {'name': name, 'enabled': change.enabled, 'status': status}

    @api.post('/providers/{target}/test')
    async def test_provider(target: str):
        if not provider_settings.known_target(hub, target):
            raise HTTPException(404, 'Unknown provider or channel')
        try:
            result = await provider_settings.run_test(hub, target)
        except provider_settings.RateLimited as exc:
            raise HTTPException(429, str(exc)) from None
        audit('provider_test', target=target, ok=result['ok'], status=result['status'])
        return result

    @api.post('/calendar')
    def add_calendar(item: CalendarInput):
        if not hub.store.get('sources', item.source_id):
            raise HTTPException(422, 'Unknown source')
        payload = item.model_dump(mode='json')
        now = utcnow().isoformat()
        payload.update(scheduled_utc=payload['scheduled_at'], ingested_at=now, provider='admin_import')
        if item.actual is not None:
            payload['actual_ingested_at'] = now
        if item.actual is not None and item.consensus is not None:
            payload.update(surprise(item.actual, item.consensus, item.historical_surprises))
        hub.store.put('macro_calendar', item.id, payload)
        audit('calendar_import', calendar_id=item.id)
        return payload

    @api.post('/research')
    async def research(request: SearchRequest):
        if not hub.search_budget.acquire(request.query, request.priority):
            raise HTTPException(429, 'Search budget or topic cooldown reached')
        audit('research_search', query=request.query, priority=request.priority)
        try:
            results = await hub.ollama.search(request.query)
        except ProviderUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        payload = {'query': request.query, 'priority': request.priority, 'results': results,
                   'verification_status': 'UNCONFIRMED', 'discovery_only': True, 'created_at': utcnow().isoformat()}
        hub.store.put('research_jobs', uid(), payload)
        return payload

    @api.post('/social/search')
    async def social(request: SearchRequest):
        provider = hub.providers.get('x') or hub.providers.get('x-stub')
        if provider is None or not provider.configured:
            raise HTTPException(503, 'X unavailable')
        audit('social_search', query=request.query, provider=provider.name)
        try:
            posts = await provider.search(request.query)
        except ProviderUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        result = []
        for post in posts:
            source_id = 'dev-stub' if post.get('stub') else x_source_for(post.get('author_username'))
            url = f"https://example.invalid/x/{post['id']}" if post.get('stub') else f"https://x.com/i/status/{post['id']}"
            event = hub.ingest_document(DocumentInput(source_id=source_id, title=post['text'][:1000], content=post['text'],
                                                      url=url, published_at=post['created_at'], event_type='SOCIAL_DISCOVERY',
                                                      discovery_only=source_id in {'x-unknown', 'dev-stub'}))
            hub.store.put('social_posts', post['id'], dict(post, source_id=source_id, event_id=event['id'], url=url))
            result.append(event)
        return result

    @api.get('/filings/{cik}')
    async def filings(cik: str):
        provider = hub.providers.get('sec_edgar')
        if not isinstance(provider, SECProvider):
            raise HTTPException(503, 'SEC unavailable')
        try:
            return await provider.filings(cik)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except ProviderUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @api.get('/ai-runs')
    def runs(limit: int = Query(100, ge=1, le=1000)):
        return hub.store.list('ai_runs', limit)

    @api.get('/audit-logs')
    def audit_logs(limit: int = Query(200, ge=1, le=2000)):
        return hub.store.list('audit_logs', limit)

    @api.post('/calibration/{symbol}/candidate')
    def calibration_candidate(symbol: str, horizon: str = '4h'):
        audit('calibration_candidate', symbol=symbol, horizon=horizon)
        try:
            return calibration.create_candidate(hub.store, symbol, horizon)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.post('/calibration/promote/{candidate_id}')
    def calibration_promote(candidate_id: str):
        try:
            result = calibration.promote(hub.store, candidate_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit('calibration_promote', candidate_id=candidate_id)
        return result

    @api.post('/calibration/{symbol}/rollback')
    def calibration_rollback(symbol: str, horizon: str = '4h'):
        audit('calibration_rollback', symbol=symbol, horizon=horizon)
        return calibration.rollback(hub.store, symbol, horizon)

    @api.post('/autolearn/challengers/{challenger_id}/evaluate')
    def evaluate_challenger(challenger_id: str, horizon: str | None = None):
        audit('challenger_evaluation', challenger_id=challenger_id, horizon=horizon)
        try:
            return calibration.evaluate_challenger(hub.store, challenger_id, horizon)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.post('/autolearn/weights/rollback')
    def weights_rollback(horizon: str | None = None):
        audit('weights_rollback', horizon=horizon)
        return calibration.rollback(hub.store, None, horizon, key='weights:champion')

    @api.post('/backtest')
    def backtest(request: BacktestRequest):
        from backend import backtest as replay
        try:
            result = replay.replay(hub.store, request.start, request.end, step_seconds=request.step_seconds,
                                   symbols=request.symbols, weights=request.weights, horizons=request.horizons)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit('backtest', start=request.start.isoformat(), end=request.end.isoformat())
        return result

    return api
