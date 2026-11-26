"""The daily AI request budget (backend/ai_budget.py, config ai_runtime `budget`): role caps, the daily total, the reserve
share, the pace window, refusals that send nothing, counts read back from ai_runs (restarts), token counts and the
usage record (GET /api/ai/usage). No network: fake chat providers only."""
import copy
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import ai as ai_module
from backend import ai_budget, llm_limiter
from backend.ai import AIService, OllamaProvider
from backend.ai_budget import AIBudget
from backend.config import get_settings, reload_rules, rules
from backend.database import RECORDS, Store, uid
from backend.llm_limiter import LLMLimiter, QueueWaitExceeded
from backend.main import create_app
from backend.providers.base import ProviderUnavailable

PASS = '{"status":"PASS","reasons":[]}'
BUCHAREST_NOON = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)  # 12:00 in Bucharest (UTC+3)


def budget(**overrides) -> dict:
    config = {'daily_requests': 100, 'reset': 'Europe/Bucharest midnight',
              'spread': {'hourly_share_of_daily': 1.0, 'burst_hours': 2},
              'role_daily': {'auditor': 50, 'extractor': 50, 'research_planner': 50, 'market_interpreter': 50},
              'reserve': {'roles': ['market_interpreter', 'chief_analyst', 'auditor'], 'share': 0.0}}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = dict(config[key], **value)
        else:
            config[key] = value
    return config


class FakeProvider:
    """Counts chat requests (each would be one HTTP request); answers from a list, then repeats the last one."""

    def __init__(self, *outputs, raises: Exception | None = None, prompt_eval_count=None, eval_count=None):
        self.outputs = list(outputs) or [PASS]
        self.raises = raises
        self.calls: list[str] = []
        self.counts = (prompt_eval_count, eval_count)

    async def chat(self, model, messages, schema):
        self.calls.append(model)
        if self.raises is not None:
            raise self.raises
        content = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        reply = {'message': {'content': content}}
        if self.counts[0] is not None:
            reply['prompt_eval_count'] = self.counts[0]
        if self.counts[1] is not None:
            reply['eval_count'] = self.counts[1]
        return reply


def make_store(tmp_path, name='budget.db') -> Store:
    store = Store(f'sqlite:///{tmp_path}/{name}')
    store.initialize()
    for model in ('m1', 'm2'):
        store.put('ai_models', model, {'name': model, 'availability': True})
    for role in ('auditor', 'extractor', 'research_planner', 'market_interpreter', 'chief_analyst'):
        store.put('ai_role_assignments', role, {'primary': 'm1'})
    return store


def service(store: Store, provider, config: dict, clock=None) -> AIService:
    ai = AIService(store, provider)
    ai.budget = AIBudget(store, settings=lambda: config, **({'clock': clock} if clock else {}))
    return ai


def add_run(store: Store, role: str, at: datetime, **fields) -> None:
    """An ai_runs row stored at a given time (the budget counts rows by their storage time)."""
    payload = {'agent': role, 'model': 'm1', 'success': True, 'requests': 1, **fields}
    with store.session() as session:
        session.add(RECORDS['ai_runs'](id=uid(), created_at=at, payload=payload))
        session.commit()


# --- caps -------------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_role_at_its_cap_is_refused_without_a_request_and_the_refusal_is_stored(tmp_path):
    store = make_store(tmp_path)
    provider = FakeProvider()
    ai = service(store, provider, budget(role_daily={'auditor': 2}))
    assert await ai.run('auditor', {}) and await ai.run('auditor', {})
    assert await ai.run('auditor', {}) is None  # the caller's own fallback applies
    assert len(provider.calls) == 2  # the third call sent nothing
    refused = [r for r in store.list('ai_runs') if r.get('budget_refusal')]
    assert len(refused) == 1
    assert refused[0].items() >= {'success': False, 'error': 'AI daily budget reached', 'budget_refusal': 'role',
                                  'requests': 0, 'agent': 'auditor'}.items()
    assert ai.budget.refusal('extractor') is None  # the cap is per role


@pytest.mark.asyncio
async def test_the_daily_total_refuses_every_role_once_reached(tmp_path):
    store = make_store(tmp_path)
    provider = FakeProvider()
    ai = service(store, provider, budget(daily_requests=3))
    for _ in range(3):
        assert await ai.run('auditor', {})
    assert await ai.run('market_interpreter', {}) is None
    assert len(provider.calls) == 3
    assert store.list('ai_runs', 1)[0]['budget_refusal'] == 'daily'


@pytest.mark.asyncio
async def test_the_reserve_share_keeps_room_for_the_interpretation_the_chief_analyst_and_the_auditor(tmp_path):
    store = make_store(tmp_path)
    provider = FakeProvider()
    ai = service(store, provider, budget(daily_requests=4, reserve={'share': 0.5}))
    assert ai.budget.refusal('research_planner') is None
    for _ in range(2):  # (1 - 0.5) x 4 = 2 for all the non-reserve roles together
        await ai.run('research_planner', {})
    assert ai.budget.refusal('research_planner') == 'daily' and ai.budget.refusal('extractor') == 'daily'
    assert ai.budget.refusal('auditor') is None and ai.budget.refusal('market_interpreter') is None
    assert await ai.run('auditor', {}) and await ai.run('auditor', {})
    assert ai.budget.refusal('auditor') == 'daily'  # the whole day is used now


def test_the_pace_window_counts_only_the_last_burst_hours(tmp_path):
    store = make_store(tmp_path)
    # 48 a day x 1/24 x 2 hours = 4 requests per 2 hours.
    bounds = budget(daily_requests=48, spread={'hourly_share_of_daily': 1 / 24, 'burst_hours': 2})
    tracker = AIBudget(store, settings=lambda: bounds, clock=lambda: BUCHAREST_NOON)
    for minutes in (200, 150):  # outside the window: they count for the day only
        add_run(store, 'auditor', BUCHAREST_NOON - timedelta(minutes=minutes))
    for minutes in (100, 60, 30):
        add_run(store, 'auditor', BUCHAREST_NOON - timedelta(minutes=minutes))
    assert tracker.refusal('auditor') is None
    add_run(store, 'extractor', BUCHAREST_NOON - timedelta(minutes=5))
    assert tracker.refusal('auditor') == 'pace' and tracker.refusal('extractor') == 'pace'
    usage = tracker.usage()
    assert usage['pace'] == {'window_hours': 2.0, 'limit': 4, 'used': 4, 'shared_limit': 4, 'shared_used': 1}
    assert usage['used'] == 6
    # Two hours later the burst has left the window; the day's total still counts every request.
    later = AIBudget(store, settings=lambda: bounds, clock=lambda: BUCHAREST_NOON + timedelta(hours=2))
    assert later.refusal('auditor') is None and later.usage()['used'] == 6


def test_the_non_reserve_roles_get_their_share_of_the_pace_window_only(tmp_path):
    store = make_store(tmp_path)
    bounds = budget(daily_requests=48, spread={'hourly_share_of_daily': 1 / 24, 'burst_hours': 2}, reserve={'share': 0.5})
    tracker = AIBudget(store, settings=lambda: bounds, clock=lambda: BUCHAREST_NOON)
    for minutes in (40, 20):
        add_run(store, 'extractor', BUCHAREST_NOON - timedelta(minutes=minutes))
    assert tracker.refusal('extractor') == 'pace'  # 2 of floor(4 x 0.5) = 2
    assert tracker.refusal('market_interpreter') is None


def test_the_day_starts_at_midnight_in_bucharest(tmp_path):
    store = make_store(tmp_path)
    bounds = budget(daily_requests=2, spread={'hourly_share_of_daily': 1.0, 'burst_hours': 1})
    midnight = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)  # 00:00 on 28 September in Bucharest
    add_run(store, 'auditor', midnight - timedelta(minutes=90))
    add_run(store, 'auditor', midnight - timedelta(minutes=80))
    before = AIBudget(store, settings=lambda: bounds, clock=lambda: midnight - timedelta(minutes=1))
    after = AIBudget(store, settings=lambda: bounds, clock=lambda: midnight + timedelta(minutes=1))
    assert before.refusal('auditor') == 'daily' and after.refusal('auditor') is None
    usage = after.usage()
    assert usage['day'] == '2026-09-28' and usage['used'] == 0
    assert usage['resets_at'] == '2026-09-28T21:00:00+00:00'
    assert usage['history'][-2:] == [{'day': '2026-09-27', 'used': 2, 'tokens': None},
                                     {'day': '2026-09-28', 'used': 0, 'tokens': None}]


# --- what counts --------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_counts_come_from_the_stored_runs_so_a_restart_keeps_them(tmp_path):
    store = make_store(tmp_path)
    config = budget(role_daily={'auditor': 2})
    first = service(store, FakeProvider(), config)
    await first.run('auditor', {})
    await first.run('auditor', {})
    provider = FakeProvider()
    restarted = service(store, provider, config)  # a new process: nothing in memory
    assert await restarted.run('auditor', {}) is None and provider.calls == []


@pytest.mark.asyncio
async def test_the_backup_model_and_the_schema_correction_count_as_requests(tmp_path):
    store = make_store(tmp_path)
    store.put('ai_role_assignments', 'auditor', {'primary': 'm1', 'backup': 'm2'})
    provider = FakeProvider('not json', 'still not json', PASS)
    ai = service(store, provider, budget(role_daily={'auditor': 3}))
    assert await ai.run('auditor', {}) is not None
    assert provider.calls == ['m1', 'm1', 'm2']
    runs = {r['model']: r for r in store.list('ai_runs')}
    assert runs['m1']['requests'] == 2 and runs['m2']['requests'] == 1
    assert ai.budget.usage()['roles'][0].items() >= {'role': 'auditor', 'used': 3, 'cap': 3, 'succeeded': 1,
                                                      'failed': 1}.items()
    assert ai.budget.refusal('auditor') == 'role'


@pytest.mark.asyncio
async def test_a_refusal_before_the_backup_model_stops_the_run_and_the_correction_is_refused_too(tmp_path):
    store = make_store(tmp_path)
    store.put('ai_role_assignments', 'auditor', {'primary': 'm1', 'backup': 'm2'})
    provider = FakeProvider('not json')
    ai = service(store, provider, budget(role_daily={'auditor': 1}))
    assert await ai.run('auditor', {}) is None
    assert provider.calls == ['m1']  # the correction and the backup were refused: nothing more was sent
    row = store.list('ai_runs')[0]
    assert row['requests'] == 1 and row['budget_refusal'] == 'role' and row['error'] == 'AI daily budget reached'
    assert len(store.list('ai_runs')) == 1
    # The run sent one request and was then stopped by the budget: it is 'refused', never also 'failed'.
    auditor = next(r for r in ai.budget.usage()['roles'] if r['role'] == 'auditor')
    assert auditor.items() >= {'used': 1, 'refused': 1, 'failed': 0, 'succeeded': 0}.items()


@pytest.mark.asyncio
async def test_a_call_that_never_got_a_limiter_slot_is_not_counted(tmp_path):
    store = make_store(tmp_path)
    provider = FakeProvider(raises=QueueWaitExceeded('LLM queue wait exceeded 600 s'))
    ai = service(store, provider, budget(role_daily={'auditor': 1}))
    assert await ai.run('auditor', {}) is None
    assert store.list('ai_runs')[0]['requests'] == 0 and ai.budget.refusal('auditor') is None
    assert ai.budget.pending == {}  # the reservation was given back


@pytest.mark.asyncio
async def test_a_rate_limited_request_counts_and_shows_as_the_last_rate_limit(tmp_path):
    store = make_store(tmp_path)
    ai = service(store, FakeProvider(raises=ProviderUnavailable('Rate limited (HTTP 429)', 429)), budget())
    assert await ai.run('auditor', {}) is None
    usage = ai.budget.usage()
    assert usage['used'] == 1 and usage['last_rate_limited_at'] is not None
    assert next(r for r in usage['roles'] if r['role'] == 'auditor')['failed'] == 1


@pytest.mark.asyncio
async def test_prompt_and_completion_tokens_are_stored_and_summed(tmp_path):
    store = make_store(tmp_path)
    ai = service(store, FakeProvider(prompt_eval_count=1200, eval_count=80), budget())
    await ai.run('auditor', {})
    row = store.list('ai_runs')[0]
    assert row['prompt_tokens'] == 1200 and row['completion_tokens'] == 80 and row['tokens'] == 80
    usage = ai.budget.usage()
    assert next(r for r in usage['roles'] if r['role'] == 'auditor')['tokens'] == 1280
    assert usage['history'][-1]['tokens'] == 1280
    assert next(r for r in usage['roles'] if r['role'] == 'extractor')['tokens'] is None  # not reported, never 0


@pytest.mark.asyncio
async def test_a_refusal_is_logged_once_per_role_per_hour(tmp_path, caplog):
    store = make_store(tmp_path)
    ai = service(store, FakeProvider(), budget(role_daily={'auditor': 0, 'extractor': 0}))
    with caplog.at_level(logging.WARNING, logger='backend.ai_budget'):
        for _ in range(5):
            await ai.run('auditor', {})
        await ai.run('extractor', {})
    lines = [r for r in caplog.records if r.getMessage() == 'ai_budget_refused']
    assert [r.role for r in lines] == ['auditor', 'extractor']
    # ... and stored as one ai_runs row per role per hour: the later refusals are counted in it.
    rows = {r['agent']: r for r in store.list('ai_runs') if r.get('budget_refusal') == 'role'}
    assert len(store.list('ai_runs')) == 2 and rows['auditor']['suppressed'] == 4 and rows['extractor']['suppressed'] == 0
    usage = {r['role']: r for r in ai.budget.usage()['roles']}
    assert usage['auditor']['refused'] == 5 and usage['extractor']['refused'] == 1  # the usage record counts them all
    assert usage['auditor']['used'] == 0 and usage['auditor']['failed'] == 0


@pytest.mark.asyncio
async def test_refusals_are_stored_as_one_row_per_role_per_hour(tmp_path):
    """Measured: 150-250 budget-refused ai_runs rows a day, one per refused attempt (496 for one specialist role in
    three days)."""
    store = make_store(tmp_path)
    clock = {'now': BUCHAREST_NOON}
    config = budget(role_daily={'auditor': 0})
    provider = FakeProvider()
    ai = service(store, provider, config, clock=lambda: clock['now'])
    first = (await ai.run_with_id('auditor', {}))[1]
    for minutes in (5, 20, 59):
        clock['now'] = BUCHAREST_NOON + timedelta(minutes=minutes)
        assert await ai.run_with_id('auditor', {}) == (None, first)  # the caller reads the refusal from that row
    row = store.get('ai_runs', first)
    assert len(store.list('ai_runs')) == 1 and provider.calls == []
    assert row.items() >= {'budget_refusal': 'role', 'error': 'AI daily budget reached', 'suppressed': 3, 'requests': 0,
                           'success': False, 'refused_at': BUCHAREST_NOON.isoformat(),
                           'last_refused_at': (BUCHAREST_NOON + timedelta(minutes=59)).isoformat()}.items()
    # A restart keeps counting in the same row (the row is found in the store, not in memory).
    restarted = service(store, provider, config, clock=lambda: clock['now'])
    assert (await restarted.run_with_id('auditor', {}))[1] == first and store.get('ai_runs', first)['suppressed'] == 4
    # The row carries the latest kind of refusal, which callers read back to decide how long to wait.
    config['role_daily']['auditor'] = 50
    config['daily_requests'] = 0
    restarted.ready = lambda: True
    assert restarted.budget.refusal('auditor') == 'daily'
    assert (await restarted.run_with_id('auditor', {}))[1] == first
    assert store.get('ai_runs', first)['budget_refusal'] == 'daily' and store.get('ai_runs', first)['suppressed'] == 5
    # An hour after the row was written a new one starts.
    clock['now'] = BUCHAREST_NOON + timedelta(minutes=61)
    second = (await restarted.run_with_id('auditor', {}))[1]
    assert second != first and len(store.list('ai_runs')) == 2 and store.get('ai_runs', second)['suppressed'] == 0
    # Another role has its own row; a run that sent a request before the budget stopped it is stored on its own.
    config['role_daily']['extractor'] = 0
    third = (await restarted.run_with_id('extractor', {}))[1]
    assert third not in {first, second} and len(store.list('ai_runs')) == 3


@pytest.mark.asyncio
async def test_a_new_budget_day_starts_its_own_refusal_row(tmp_path):
    """A refusal at 00:10 is not counted in the row of 23:50: each day's usage counts its own refused attempts."""
    store = make_store(tmp_path)
    before_midnight = datetime(2026, 9, 28, 20, 50, tzinfo=UTC)  # 23:50 in Bucharest
    clock = {'now': before_midnight}
    ai = service(store, FakeProvider(), budget(role_daily={'auditor': 0}), clock=lambda: clock['now'])
    first = (await ai.run_with_id('auditor', {}))[1]
    clock['now'] = before_midnight + timedelta(minutes=5)
    assert (await ai.run_with_id('auditor', {}))[1] == first
    clock['now'] = before_midnight + timedelta(minutes=20)  # 00:10 the next day, 20 minutes after the row was written
    second = (await ai.run_with_id('auditor', {}))[1]
    assert second != first and store.get('ai_runs', first)['suppressed'] == 1 and store.get('ai_runs', second)['suppressed'] == 0


def test_a_cap_of_zero_makes_the_role_unavailable(tmp_path):
    store = make_store(tmp_path)
    ai = service(store, FakeProvider(), budget(role_daily={'research_planner': 0}))
    assert not ai.available('research_planner') and ai.available('auditor')
    ai.budget = AIBudget(store, settings=lambda: budget(daily_requests=0))
    assert not ai.available('auditor')


def test_no_budget_section_means_no_limit(tmp_path):
    tracker = AIBudget(make_store(tmp_path), settings=lambda: {})
    assert tracker.refusal('auditor') is None and not tracker.disabled('auditor')


def test_the_shipped_budget_matches_the_contract():
    config = rules()['budget']
    assert config['daily_requests'] == 400 and config['reset'] == 'Europe/Bucharest midnight'
    assert config['spread'] == {'hourly_share_of_daily': 1 / 24, 'burst_hours': 2, 'role_pace': True}
    # market_interpreter 36 (was 72): one request per run, texts that still hold are reused, a rejected state is not
    # sent again; a normal day needs 8 requests when nothing changes and about 24 while minor facts keep changing, the
    # rest is for a release day.
    assert config['role_daily'] == {'market_interpreter': 36, 'chief_analyst': 60, 'auditor': 60, 'macro': 24, 'fx': 24,
                                    'crypto': 24, 'equity_credit': 24, 'geopolitical_energy': 24, 'fact_checker': 40,
                                    'research_planner': 40, 'extractor': 120}
    assert config['reserve'] == {'roles': ['market_interpreter', 'chief_analyst', 'auditor'], 'share': 0.25}
    bounds = ai_budget.limits(config)
    assert bounds.pace == 33 and bounds.shared_daily == 300 and bounds.role_pace
    assert ai_budget.validate(config, ai_budget.ROLE_LABELS) == []
    assert rules()['ai_runtime']['chat_attempts']['default'] == 1  # a timed-out generation is not paid for twice


# --- usage record -------------------------------------------------------------------------------------------------------

def test_usage_reports_the_day_roles_history_and_limiter(tmp_path):
    store = make_store(tmp_path)
    bounds = budget(daily_requests=10, spread={'hourly_share_of_daily': 0.5, 'burst_hours': 2})
    tracker = AIBudget(store, settings=lambda: bounds, clock=lambda: BUCHAREST_NOON)
    add_run(store, 'auditor', BUCHAREST_NOON - timedelta(minutes=10), prompt_tokens=100, completion_tokens=10)
    add_run(store, 'auditor', BUCHAREST_NOON - timedelta(minutes=9), success=False, requests=2, http_status=429)
    add_run(store, 'extractor', BUCHAREST_NOON - timedelta(minutes=8), success=False, requests=0,
            budget_refusal='role', error='AI daily budget reached')
    add_run(store, 'extractor', BUCHAREST_NOON - timedelta(days=3))
    add_run(store, 'extractor', BUCHAREST_NOON - timedelta(days=9))  # older than the 7-day history
    add_run(store, 'auditor', BUCHAREST_NOON - timedelta(minutes=7), requests=None)  # a row from before the budget
    limiter = LLMLimiter()
    usage = tracker.usage(limiter=limiter)
    assert usage['day'] == '2026-09-28' and usage['resets_at'] == '2026-09-28T21:00:00+00:00'
    # The row from before the budget counts in the history only (see the legacy-rows test).
    assert usage['daily_requests'] == 10 and usage['used'] == 3 and usage['remaining'] == 7
    assert usage['reserve_share'] == 0.0
    assert usage['pace'] == {'window_hours': 2.0, 'limit': 10, 'used': 3, 'shared_limit': 10, 'shared_used': 0}
    roles = {r['role']: r for r in usage['roles']}
    assert list(roles)[:4] == ['auditor', 'extractor', 'research_planner', 'market_interpreter']
    assert roles['auditor'] == {'role': 'auditor', 'label': 'Auditor', 'used': 3, 'cap': 50, 'tokens': 110,
                                'refused': 0, 'succeeded': 1, 'failed': 1}
    assert roles['extractor'] == {'role': 'extractor', 'label': 'News extraction', 'used': 0, 'cap': 50,
                                  'tokens': None, 'refused': 1, 'succeeded': 0, 'failed': 0}
    assert roles['research_planner']['label'] == 'Research planner'
    assert [d['day'] for d in usage['history']] == [f'2026-09-{day}' for day in range(22, 29)]
    assert {d['day']: d['used'] for d in usage['history']}['2026-09-25'] == 1
    assert usage['history'][-1] == {'day': '2026-09-28', 'used': 4, 'tokens': 110}
    tracker.pending['extractor'] = 2  # admitted, not stored yet: counted like the budget counts them
    pending = tracker.usage()
    assert pending['used'] == 5 and pending['pace']['used'] == 5 and pending['pace']['shared_used'] == 2
    assert next(r for r in pending['roles'] if r['role'] == 'extractor')['used'] == 2
    assert usage['last_rate_limited_at'] == (BUCHAREST_NOON - timedelta(minutes=9)).isoformat()
    assert set(usage['limiter']) == {'concurrency_limit', 'in_flight', 'queued', 'paused_until'}
    limiter.rate_limited_at = BUCHAREST_NOON - timedelta(minutes=1)
    assert tracker.usage(limiter=limiter)['last_rate_limited_at'] == limiter.rate_limited_at.isoformat()


# --- API ----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/api.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    monkeypatch.setenv('ADMIN_TOKEN', 'test-admin-token')
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


HEADERS = {'X-Admin-Token': 'test-admin-token'}


def test_usage_endpoint_shape(client):
    add_run(client.app.state.hub.store, 'extractor', datetime.now(UTC), prompt_tokens=10, completion_tokens=5)
    body = client.get('/api/ai/usage').json()
    assert set(body) == {'day', 'resets_at', 'daily_requests', 'used', 'remaining', 'reserve_share', 'pace', 'roles',
                         'last_rate_limited_at', 'history', 'limiter'}
    assert body['daily_requests'] == rules()['budget']['daily_requests'] and body['used'] == 1
    assert body['remaining'] == body['daily_requests'] - 1 and body['reserve_share'] == 0.25
    assert body['pace'] == {'window_hours': 2.0, 'limit': 33, 'used': 1, 'shared_limit': 24, 'shared_used': 1}
    assert len(body['roles']) == 11 and len(body['history']) == 7
    extractor = next(r for r in body['roles'] if r['role'] == 'extractor')
    assert extractor == {'role': 'extractor', 'label': 'News extraction', 'used': 1, 'cap': 120, 'tokens': 15,
                         'refused': 0, 'succeeded': 1, 'failed': 0}
    assert set(body['limiter']) == {'concurrency_limit', 'in_flight', 'queued', 'paused_until'}


def test_ai_budget_edit_is_validated_audited_and_applies_without_restart(config_copy, client):
    path = config_copy / 'rules.d' / 'ai_runtime.json'  # a private copy: the owner's running app never sees it
    original = path.read_text(encoding='utf-8')
    for bad in ({'daily_requests': 10001}, {'daily_requests': -1}, {'daily_requests': 12.5}, {'daily_requests': '400'},
                {'daily_requests': True}, {'role_daily': {'extractor': 5001}}, {'role_daily': {'nobody': 3}},
                {'role_daily': {'extractor': 1.5}}, {'reserve': {'share': 0.95}}, {'spread': {'burst_hours': 0}},
                {'spread': {'burst_hours': 25}}, {'spread': {'note': 'x'}}, {'reset': 'UTC'}, {'note': 'x'},
                {'reserve': {'roles': ['nobody']}}, {'spread': {'role_pace': 'yes'}}):
        response = client.put('/api/admin/config/ai_budget', json=bad, headers=HEADERS)
        assert response.status_code == 422 and path.read_text(encoding='utf-8') == original, bad
    assert client.put('/api/admin/config/ai_budget', json={'daily_requests': 300}).status_code == 401
    ok = client.put('/api/admin/config/ai_budget', headers=HEADERS,
                    json={'daily_requests': 250, 'role_daily': {'extractor': 0, 'research_planner': 10}})
    assert ok.status_code == 200, ok.text
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['ai_runtime_version'] != json.loads(original)['ai_runtime_version']
    assert saved['budget']['role_daily']['extractor'] == 0 and saved['budget']['role_daily']['auditor'] == 60
    assert saved['budget']['role_daily_note'] == json.loads(original)['budget']['role_daily_note']
    # Applied at once: the usage record and the service read the new budget.
    body = client.get('/api/ai/usage').json()
    assert body['daily_requests'] == 250
    assert next(r for r in body['roles'] if r['role'] == 'extractor')['cap'] == 0
    assert not client.app.state.hub.ai.available('extractor')
    logs = client.get('/api/admin/audit-logs', headers=HEADERS).json()
    change = next(row for row in logs if row['action'] == 'config_change' and row['fragment'] == 'ai_budget')
    assert change['after'] == {'daily_requests': 250, 'role_daily': {'extractor': 0, 'research_planner': 10}}
    assert change['before']['daily_requests'] == 400 and change['before']['role_daily']['extractor'] == 120
    assert 'ai_budget' in client.get('/api/settings').json()['editable']
    assert client.put('/api/admin/config/ai_budget', headers=HEADERS,
                      json={'spread': {'role_pace': False}}).status_code == 200
    assert json.loads(path.read_text(encoding='utf-8'))['budget']['spread']['role_pace'] is False


def test_ai_budget_edit_uses_the_local_admin_header_in_local_mode(config_copy, tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/local.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    monkeypatch.setenv('ADMIN_AUTH', 'local')
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), base_url='http://127.0.0.1:8000', client=('127.0.0.1', 50123)) as c:
            assert c.put('/api/admin/config/ai_budget', json={'daily_requests': 399}).status_code == 403
            ok = c.put('/api/admin/config/ai_budget', json={'daily_requests': 399}, headers={'X-Local-Admin': '1'})
            assert ok.status_code == 200 and c.get('/api/ai/usage').json()['daily_requests'] == 399
        assert json.loads((config_copy / 'rules.d' / 'ai_runtime.json').read_text(encoding='utf-8'))[
            'budget']['daily_requests'] == 399
    finally:
        get_settings.cache_clear()


def test_ai_budget_edit_without_a_budget_section_is_a_422_not_a_500(config_copy, client):
    path = config_copy / 'rules.d' / 'ai_runtime.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data.pop('budget')
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')
    reload_rules()
    body = client.get('/api/ai/usage').json()
    # No budget section means no limit: nothing is reported as a budget of 0.
    assert body['daily_requests'] is None and body['remaining'] is None and body['pace']['limit'] is None
    response = client.put('/api/admin/config/ai_budget', json={'daily_requests': 300}, headers=HEADERS)
    assert response.status_code == 422 and 'no budget section' in response.json()['detail']


def test_the_ollama_test_reports_a_budget_refusal_instead_of_an_invalid_schema(client):
    hub = client.app.state.hub
    hub.store.put('ai_models', 'm1', {'name': 'm1', 'availability': True})
    hub.store.put('ai_role_assignments', 'auditor', {'primary': 'm1'})
    hub.ai.provider = provider = FakeProvider()
    hub.ai.budget = AIBudget(hub.store, settings=lambda: budget(role_daily={'auditor': 0}))
    body = client.post('/api/admin/ollama/test', headers=HEADERS).json()
    assert provider.calls == []  # the probe counts against the auditor's cap and sent nothing
    assert body['success'] is False and body['budget_refusal'] == 'role' and body['schema_valid'] is None
    assert body['error'] == 'AI daily budget reached'


# --- LLM limiter ----------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_429_is_retried_once_only():
    assert rules()['ai_runtime']['limiter']['rate_limit_retries'] == 1
    config = copy.deepcopy(dict(llm_limiter.limiter_settings()))
    config.update(rate_limit_backoff_seconds=0.01, rate_limit_backoff_min_seconds=0.001,
                  rate_limit_backoff_max_seconds=0.02)
    limiter = LLMLimiter(lambda: config)
    sent = []

    async def send():
        sent.append(1)
        raise ProviderUnavailable('Rate limited (HTTP 429)', 429)
    with pytest.raises(ProviderUnavailable):
        await limiter.run('extractor', send)
    assert len(sent) == 2  # the call and one retry


# --- rows from before the budget, per-role pace, HTTP requests actually sent ---------------------------------------------

def test_rows_from_before_the_budget_never_count_against_the_day(tmp_path):
    store = make_store(tmp_path)
    # 10 a day, pace floor(10 x 0.1 x 2) = 2: thirty older rows would refuse every role until midnight.
    bounds = budget(daily_requests=10, spread={'hourly_share_of_daily': 0.1, 'burst_hours': 2})
    tracker = AIBudget(store, settings=lambda: bounds, clock=lambda: BUCHAREST_NOON)
    for minutes in range(1, 31):
        add_run(store, 'auditor', BUCHAREST_NOON - timedelta(minutes=minutes), requests=None)
    assert tracker.refusal('auditor') is None and tracker.refusal('extractor') is None
    assert tracker.role_used('auditor') == (0, 50)
    usage = tracker.usage()
    assert usage['used'] == 0 and usage['remaining'] == 10 and usage['pace']['used'] == 0
    assert next(r for r in usage['roles'] if r['role'] == 'auditor').items() >= {'used': 0, 'succeeded': 0}.items()
    assert usage['history'][-1]['used'] == 30  # the history still shows what was sent


def test_the_role_pace_spreads_each_cap_over_the_day(tmp_path):
    store = make_store(tmp_path)
    spread = {'hourly_share_of_daily': 1.0, 'burst_hours': 2, 'role_pace': True}
    bounds = budget(daily_requests=1000, spread=spread, role_daily={'extractor': 24})
    one_am = datetime(2026, 9, 27, 22, 0, tzinfo=UTC)  # 01:00 in Bucharest: 24 x (1 + 2) / 24 = 3 so far
    tracker = AIBudget(store, settings=lambda: bounds, clock=lambda: one_am)
    for minutes in (50, 40):
        add_run(store, 'extractor', one_am - timedelta(minutes=minutes))
    assert tracker.refusal('extractor') is None
    add_run(store, 'extractor', one_am - timedelta(minutes=30))
    assert tracker.refusal('extractor') == 'pace' and tracker.refusal('auditor') is None
    noon = AIBudget(store, settings=lambda: bounds, clock=lambda: BUCHAREST_NOON)  # 24 x 14 / 24 = 14 by noon
    assert noon.refusal('extractor') is None
    without = dict(bounds, spread={'hourly_share_of_daily': 1.0, 'burst_hours': 2})
    assert AIBudget(store, settings=lambda: without, clock=lambda: one_am).refusal('extractor') is None
    assert [ai_budget.role_allowance(24, 2, hours) for hours in (0, 1, 12, 22, 23.9)] == [2, 3, 14, 24, 24]
    assert ai_budget.role_allowance(0, 2, 5) == 0 and ai_budget.role_allowance(72, 2, 0) == 6


async def _no_sleep(_delay):
    return None


def fast_limiter(monkeypatch) -> LLMLimiter:
    config = copy.deepcopy(dict(llm_limiter.limiter_settings()))
    config.update(rate_limit_backoff_seconds=0.001, rate_limit_backoff_min_seconds=0.001,
                  rate_limit_backoff_max_seconds=0.01)
    limiter = LLMLimiter(lambda: config)
    monkeypatch.setattr(llm_limiter, 'LIMITER', limiter)
    monkeypatch.setattr('backend.providers.base.asyncio.sleep', _no_sleep)
    return limiter


def answering(monkeypatch, answers: list) -> list:
    """httpx answers in order: an HTTP status, or 'timeout' for a read timeout. Returns the list of sent requests."""
    sent: list[str] = []

    async def request(self, method, url, **kwargs):
        sent.append(url)
        answer = answers.pop(0)
        if answer == 'timeout':
            raise httpx.ReadTimeout('slow generation', request=httpx.Request(method, url))
        body = {'done': True, 'message': {'content': PASS}} if answer == 200 else {}
        return httpx.Response(answer, json=body, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    return sent


@pytest.mark.asyncio
async def test_every_http_request_sent_counts_the_limiter_retry_included(tmp_path, monkeypatch):
    fast_limiter(monkeypatch)
    sent = answering(monkeypatch, [503, 'timeout'])
    store = make_store(tmp_path)
    ai = service(store, OllamaProvider('https://ollama.invalid/api', 'test-key'), budget())
    assert await ai.run('auditor', {}) is None
    assert len(sent) == 2  # the 503 was sent again once by the limiter, then timed out
    assert store.list('ai_runs')[0]['requests'] == 2 and ai.budget.usage()['used'] == 2


@pytest.mark.asyncio
async def test_a_transport_retry_counts_as_a_second_request(tmp_path, monkeypatch):
    fast_limiter(monkeypatch)
    sent = answering(monkeypatch, ['timeout', 200])
    monkeypatch.setattr(ai_module, 'chat_options', lambda provider, role, queue_role=None: {'attempts': 2, 'role': role})
    store = make_store(tmp_path)
    ai = service(store, OllamaProvider('https://ollama.invalid/api', 'test-key'), budget())
    assert await ai.run('auditor', {}) is not None
    assert len(sent) == 2 and store.list('ai_runs')[0]['requests'] == 2


@pytest.mark.asyncio
async def test_a_call_refused_before_sending_counts_nothing(tmp_path, monkeypatch):
    fast_limiter(monkeypatch)
    sent = answering(monkeypatch, [200])
    provider = OllamaProvider('https://ollama.invalid/api', 'test-key')
    provider.next_attempt = time.monotonic() + 600  # the circuit is open after a 401: nothing is sent
    store = make_store(tmp_path)
    ai = service(store, provider, budget())
    assert await ai.run('auditor', {}) is None
    row = store.list('ai_runs')[0]
    assert sent == [] and row['requests'] == 0 and row['error'] == 'ProviderUnavailable'
    assert next(r for r in ai.budget.usage()['roles'] if r['role'] == 'auditor')['failed'] == 0
