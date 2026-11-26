"""Event-aware recalculation triggers and the market-interpretation cadence (docs/EVENT_AWARENESS.md §5).
event_risk.trigger_state and interpretation.refresh are faked here: the contract shapes are
{'phases': {id: phase}, 'windows': {id: 'W60'|'W15'|None}, 'risk_mode', 'dominant_event_id', 'posture', 'critical_ids'}."""
import asyncio
import sys
import types
from datetime import UTC, datetime, timedelta

import pytest

import backend
from backend import refresh
from backend import runtime as runtime_module
from backend.config import Settings
from backend.database import Store
from backend.runtime import Runtime
from backend.service import HubService

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)  # Wednesday 13:00 New York: NYSE open, FOMC decision at 18:00 UTC


def event_state(phases=None, windows=None, critical=(), posture='RISK_ON', risk_mode=False, dominant=None, divergences=()):
    return {'phases': dict(phases or {}), 'windows': dict(windows or {}), 'risk_mode': risk_mode,
            'dominant_event_id': dominant, 'posture': posture, 'critical_ids': list(critical),
            'divergences': list(divergences), 'markets_open': {'crypto': True}}


def state(at, rows=(), events=None, scores=None, sessions=None):
    return refresh.trigger_state([], list(rows), now=at, event_state=events, scores=scores, sessions=sessions)


def found(before, after):
    return [(t['trigger'], t['id']) for t in refresh.detect_triggers(before, after)]


def test_window_entries_critical_events_releases_and_regime_changes_fire_once():
    fomc = 'FED-20261028-FOMC'
    base = state(NOW, events=event_state({fomc: 'APPROACHING'}, {fomc: None}))
    w60 = state(NOW + timedelta(seconds=5), events=event_state({fomc: 'APPROACHING'}, {fomc: 'W60'}, critical=[fomc]))
    assert set(found(base, w60)) == {('EVENT_WINDOW_60', fomc), ('EVENT_CRITICAL_NEW', fomc)}
    assert found(w60, w60) == []  # edge-triggered
    w15 = state(NOW + timedelta(minutes=45), events=event_state({fomc: 'IMMINENT'}, {fomc: 'W15'}, critical=[fomc]))
    assert found(w60, w15) == [('EVENT_WINDOW_15', fomc)]  # 60 -> 15 is the 15-minute entry only
    assert found(base, w15) == [('EVENT_CRITICAL_NEW', fomc), ('EVENT_WINDOW_15', fomc)]  # skipped straight into 15
    press = f'{fomc}:PRESS_CONFERENCE'
    released = state(NOW + timedelta(minutes=61), events=event_state({fomc: 'RELEASED', press: 'IMMINENT'}, {fomc: None},
                                                                    critical=[fomc]))
    assert found(w15, released) == [('EVENT_RELEASED', fomc)]
    running = state(NOW + timedelta(minutes=62), events=event_state({fomc: 'IN_PROGRESS', press: 'IMMINENT'}, {fomc: None},
                                                                   critical=[fomc]))
    assert found(released, running) == []  # RELEASED -> IN_PROGRESS is not a second release
    talking = state(NOW + timedelta(minutes=91), events=event_state({fomc: 'IN_PROGRESS', press: 'IN_PROGRESS'},
                                                                   {fomc: None}, critical=[fomc]))
    starts = refresh.detect_triggers(running, talking)
    assert [(t['trigger'], t['id'], t['event_id']) for t in starts] == [('PRESS_CONFERENCE_START', press, fomc)]
    diverging = state(NOW + timedelta(minutes=92), events=event_state({fomc: 'IN_PROGRESS', press: 'IN_PROGRESS'},
                                                                     {fomc: None}, critical=[fomc], divergences=[fomc]))
    assert found(talking, diverging) == [('CROSS_ASSET_DIVERGENCE', f'MARKET_DIVERGENCE:{fomc}')]
    off = state(NOW + timedelta(minutes=63), events=event_state({fomc: 'IN_PROGRESS'}, {fomc: None}, critical=[fomc],
                                                               posture='RISK_OFF'))
    change = refresh.detect_triggers(running, off)
    assert [(t['trigger'], t['from'], t['to']) for t in change] == [('REGIME_CHANGE', 'RISK_ON', 'RISK_OFF')]


def test_missing_event_state_never_fires_event_triggers():
    with_state = state(NOW, events=event_state(posture='RISK_ON'))
    without = state(NOW + timedelta(seconds=5), events=None)
    assert found(with_state, without) == [] and found(without, with_state) == []


def test_press_conference_start_and_release_crossings_from_timed_calendar_rows():
    decision = {'id': 'fomc-decision', 'name': 'FOMC rate decision', 'impact': 10, 'sub_event': 'DECISION',
                'time_known': True, 'scheduled_at': '2026-10-28T18:00:00+00:00'}
    press = {'id': 'fomc-press', 'name': 'FOMC press conference', 'impact': 9, 'sub_event': 'PRESS_CONFERENCE',
             'parent_event_id': 'fomc-decision', 'time_known': True, 'scheduled_at': '2026-10-28T18:30:00+00:00'}
    auction = {'id': 'auction', 'name': '10-Year Note auction', 'impact': 7, 'time_known': False,
               'scheduled_at': '2026-10-28T18:10:00+00:00'}
    rows = [decision, press, auction]
    at = datetime(2026, 10, 28, 17, 59, 58, tzinfo=UTC)
    first, second = state(at, rows), state(at + timedelta(seconds=5), rows)
    # Without an event_risk state the calendar crossing is the release trigger; date-only rows never cross.
    assert found(first, second) == [('EVENT_RELEASED', 'fomc-decision')]
    later = state(datetime(2026, 10, 28, 18, 30, 1, tzinfo=UTC), rows)
    assert found(second, later) == [('PRESS_CONFERENCE_START', 'fomc-press')]
    # With an event_risk state its phases are the authority (no second trigger from the rows).
    events = event_state({'FED': 'IMMINENT'})
    assert found(state(at, rows, events), state(at + timedelta(seconds=5), rows, events)) == []
    assert found(state(at, rows, events), state(datetime(2026, 10, 28, 18, 30, 1, tzinfo=UTC), rows, events)) == []


def test_large_score_changes_and_market_open_close():
    threshold = refresh._config()['triggers']['large_score_change']
    before = state(NOW, scores={'BTC': 10.0, 'ETH': 5.0}, sessions={'equity': False, 'fx': True, 'bond': None})
    after = state(NOW + timedelta(seconds=5), scores={'BTC': 10.0 + threshold, 'ETH': 5.0 + threshold - 1, 'NVDA': 40.0},
                  sessions={'equity': True, 'fx': True, 'bond': True})
    triggers = refresh.detect_triggers(before, after)
    assert [(t['trigger'], t['id']) for t in triggers] == [('LARGE_SCORE_CHANGE', 'BTC'), ('MARKET_OPEN_CLOSE', 'equity')]
    assert triggers[1]['state'] == 'OPEN'  # an unknown session (bond: None) never fires


def test_analysis_and_interpretation_accept_different_triggers():
    analysis = refresh.RecalcRequests(min_interval=0)
    interpretation = refresh.RecalcRequests(min_interval=0, accepted=refresh.interpretation_config()['triggers'])
    triggers = [{'trigger': name, 'id': 'x'} for name in ('EVENT_WINDOW_15', 'LARGE_SCORE_CHANGE', 'LARGE_MOVE',
                                                          'MARKET_OPEN_CLOSE', 'MACRO_RELEASE', 'UNKNOWN')]
    analysis.request(triggers)
    interpretation.request(triggers)
    # Score/window triggers never re-run decisions (a score change would feed itself).
    assert [t['trigger'] for t in analysis.pending] == ['LARGE_MOVE', 'MARKET_OPEN_CLOSE', 'MACRO_RELEASE']
    assert [t['trigger'] for t in interpretation.pending] == ['EVENT_WINDOW_15', 'LARGE_SCORE_CHANGE', 'MARKET_OPEN_CLOSE',
                                                              'MACRO_RELEASE']
    assert refresh.interpretation_config()['min_interval_seconds'] == 600  # at most one run per 10 minutes (AI budget)


def test_interpretation_cadence_follows_traditional_markets():
    config = refresh.interpretation_config()
    assert refresh.interpretation_interval(NOW) == config['active_seconds'] == 1800
    saturday = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)
    assert not refresh.traditional_markets_open(saturday)
    assert refresh.interpretation_interval(saturday) == config['idle_seconds'] == 3600
    assert refresh.market_sessions(saturday, ['crypto', 'equity']) == {'crypto': True, 'equity': False}


# --- runtime --------------------------------------------------------------------------------------------------------------


@pytest.fixture
def runtime(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/triggers.db')
    store.initialize()
    hub = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    hub.initialize()
    return Runtime(hub)


def install(monkeypatch, name, module):
    """Make `from backend import <name>` return `module` (or fail with ImportError when module is None)."""
    monkeypatch.setitem(sys.modules, f'backend.{name}', module)
    if module is None:
        monkeypatch.delattr(backend, name, raising=False)
    else:
        monkeypatch.setattr(backend, name, module, raising=False)


def fake_event_risk(states: list):
    module = types.ModuleType('backend.event_risk')
    calls = []

    def snapshot(hub, now=None):
        calls.append(now)
        return {'generated_at': (now or NOW).isoformat()}

    def trigger_state(snap):
        return states[min(len(calls), len(states)) - 1]
    module.snapshot, module.trigger_state, module.calls = snapshot, trigger_state, calls
    return module


def test_observe_routes_event_triggers_to_the_interpretation_only(runtime, monkeypatch):
    fomc = 'FED-20261028-FOMC'
    module = fake_event_risk([event_state({fomc: 'APPROACHING'}, {fomc: None}),
                              event_state({fomc: 'APPROACHING'}, {fomc: 'W60'}, critical=[fomc])])
    install(monkeypatch, 'event_risk', module)
    runtime.observe()  # baseline
    runtime.event_state_at = None  # past interpretation.event_state_seconds
    runtime.observe()
    assert [t['trigger'] for t in runtime.interpret.pending] == ['EVENT_CRITICAL_NEW', 'EVENT_WINDOW_60']
    assert runtime.recalc.pending == []
    runtime.interpret.pending.clear()
    runtime.observe()  # within event_state_seconds: the cached state is reused, nothing re-fires
    assert len(module.calls) == 2 and runtime.interpret.pending == []


def test_event_engine_failures_keep_the_last_state_and_absence_is_quiet(runtime, monkeypatch, caplog):
    install(monkeypatch, 'event_risk', None)
    runtime.observe()
    runtime.observe()
    assert runtime.event_state is None and runtime.interpret.pending == []
    good = event_state(posture='RISK_ON')
    install(monkeypatch, 'event_risk', fake_event_risk([good]))
    runtime.event_state_at = None
    assert runtime.event_trigger_state(NOW) == good
    broken = types.ModuleType('backend.event_risk')

    def snapshot(hub, now=None):
        raise RuntimeError('engine bug')
    broken.snapshot, broken.trigger_state = snapshot, lambda snap: snap
    install(monkeypatch, 'event_risk', broken)
    runtime.event_state_at = None
    assert runtime.event_trigger_state(NOW) == good  # last good state kept
    assert 'event_state_failed' in caplog.text


@pytest.mark.asyncio
async def test_inside_the_event_loop_the_state_is_built_in_a_worker_thread(runtime, monkeypatch):
    good = event_state(posture='RISK_OFF')
    install(monkeypatch, 'event_risk', fake_event_risk([good]))
    assert runtime.event_trigger_state(NOW) is None  # previous (none yet) returned at once; the build runs aside
    await asyncio.wait_for(runtime.event_state_task, 2)
    runtime.event_state_at = None
    assert runtime.event_trigger_state(NOW) == good
    await runtime.stop()


CPI_ID = 'BLS-20261014-US_CPI'


def diverging_event_risk(entries: list[dict], divergences=None):
    """Fake engine whose snapshot carries post_event entries; its trigger_state lists the diverging ids."""
    module = types.ModuleType('backend.event_risk')

    def snapshot(hub, now=None):
        return {'post_event': [dict(e) for e in entries]}

    def trigger_state(snap):
        ids = divergences if divergences is not None else [p['event_id'] for p in snap['post_event'] if p.get('divergence')]
        return event_state(divergences=ids)
    module.snapshot, module.trigger_state = snapshot, trigger_state
    return module


DIVERGING = {'event_id': CPI_ID, 'name': 'US CPI', 'event_class': 'US_CPI', 'divergence': True, 'research_recommended': True,
             'summary_facts': ['CPI: actual 0.4 above consensus 0.3',
                               'MARKET DIVERGENCE: 3 of 3 measured exposed assets moved against the expected direction']}


@pytest.mark.asyncio
async def test_market_divergence_starts_one_research_planner_request_per_event(runtime, monkeypatch):
    """Review finding: post-event MARKET DIVERGENCE (research_recommended) never reached the research planner."""
    install(monkeypatch, 'event_risk', diverging_event_risk([DIVERGING, dict(DIVERGING, event_id='OTHER', divergence=False,
                                                                                research_recommended=False)]))
    runtime.build_event_state(NOW)
    calls: list = []
    answers = [False, True]  # first attempt: no planner model / no budget; the retry starts

    async def corroborate(event, priority):
        calls.append((event, priority))
        return answers.pop(0)
    monkeypatch.setattr(runtime, 'corroborate', corroborate)
    assert runtime.pending_divergences() == [CPI_ID]
    assert await runtime.research_divergences() == [] and len(calls) == 1
    assert await runtime.research_divergences() == []  # retried only after divergence_research.retry_seconds
    runtime.divergence_attempts[CPI_ID] -= refresh.divergence_config()['retry_seconds']
    assert await runtime.research_divergences() == [CPI_ID]
    event, priority = calls[-1]
    assert event['id'] == f'divergence:{CPI_ID}' and event['event_type'] == 'MARKET_DIVERGENCE'
    assert event['summary_fact'].startswith('MARKET DIVERGENCE after US CPI: CPI: actual 0.4 above consensus 0.3')
    assert priority == refresh.divergence_config()['priority'] and event['topic'] == 'US_CPI'
    runtime.divergence_attempts.clear()
    assert await runtime.research_divergences() == [] and len(calls) == 2  # once per event
    assert runtime.pending_divergences() == []
    # ... also after a restart (or in another worker process): the started request is recorded in research_jobs.
    restarted = Runtime(runtime.hub)
    restarted.build_event_state(NOW)
    monkeypatch.setattr(restarted, 'corroborate', corroborate)
    assert await restarted.research_divergences() == [] and len(calls) == 2
    assert runtime.hub.store.get('research_jobs', f'divergence:{CPI_ID}')['kind'] == 'DIVERGENCE_RESEARCH'


@pytest.mark.asyncio
async def test_divergence_research_respects_the_planner_guard_and_runs_from_observe(runtime, monkeypatch):
    install(monkeypatch, 'event_risk', diverging_event_risk([], divergences=[CPI_ID]))  # a trigger_state id alone suffices
    spent: list = []
    monkeypatch.setattr(runtime.hub.search_budget, 'acquire', lambda topic, priority: spent.append(topic) or True)
    runtime.build_event_state(NOW)
    # No planner model: the real corroborate refuses before spending a search-budget slot; the event stays pending.
    assert await runtime.research_divergences() == [] and spent == [] and runtime.pending_divergences() == []
    runtime.divergence_attempts.clear()
    assert runtime.pending_divergences() == [CPI_ID]
    started: list = []

    async def corroborate(event, priority):
        started.append(event['id'])
        return True
    monkeypatch.setattr(runtime, 'corroborate', corroborate)
    runtime.observe()  # inside the event loop: the request runs as a background task
    assert runtime.divergence_task is not None
    await asyncio.wait_for(runtime.divergence_task, 2)
    if runtime.event_state_task is not None:
        await asyncio.wait_for(runtime.event_state_task, 2)
    assert started == [f'divergence:{CPI_ID}']
    runtime.observe()
    assert runtime.divergence_task.done() and started == [f'divergence:{CPI_ID}']
    await runtime.stop()


def test_observe_reads_assets_without_the_event_overlay(runtime, monkeypatch):
    install(monkeypatch, 'event_risk', None)
    flags: list = []

    def assets(now=None, *, event_overlay=True):
        flags.append(event_overlay)
        return []
    monkeypatch.setattr(runtime.hub, 'assets', assets)
    runtime.observe()
    assert flags == [False]

    def legacy_assets():  # a hub without the flag still works
        flags.append('legacy')
        return []
    monkeypatch.setattr(runtime.hub, 'assets', legacy_assets)
    assert runtime.plain_assets() == [] and flags[-1] == 'legacy'


@pytest.mark.asyncio
async def test_derived_market_alerts_read_assets_without_the_event_overlay(runtime, monkeypatch):
    install(monkeypatch, 'event_risk', None)
    flags: list = []
    real = runtime.hub.assets

    def assets(now=None, *, event_overlay=True):
        flags.append(event_overlay)
        return real(now, event_overlay=False)
    monkeypatch.setattr(runtime.hub, 'assets', assets)
    await runtime.derived()
    assert flags and not any(flags)


@pytest.mark.asyncio
async def test_interpret_market_calls_the_interpretation_with_reasons(runtime, monkeypatch):
    install(monkeypatch, 'interpretation', None)
    assert await runtime.interpret_market([{'trigger': 'EVENT_WINDOW_15', 'id': 'x'}]) is None
    calls = []
    module = types.ModuleType('backend.interpretation')

    async def refresh_interpretation(hub, reasons, now=None):
        calls.append((hub, reasons, now))
        return {'source': 'deterministic'}
    module.refresh = refresh_interpretation
    install(monkeypatch, 'interpretation', module)
    published = []
    monkeypatch.setattr(runtime, 'publish', lambda stream, payload: published.append((stream, payload)))
    result = await runtime.interpret_market([{'trigger': 'EVENT_WINDOW_15', 'id': 'x'}])
    assert result == {'source': 'deterministic'} and calls[0][0] is runtime.hub
    assert calls[0][1] == [{'trigger': 'EVENT_WINDOW_15', 'id': 'x'}] and calls[0][2].tzinfo is not None
    assert published[0][0] == 'recalculations' and published[0][1]['scope'] == 'interpretation'


@pytest.mark.asyncio
async def test_interpretation_loop_runs_at_start_then_on_triggers_and_cadence(runtime, monkeypatch):
    runs: list = []
    done = asyncio.Event()

    async def interpret(reasons=None):
        runs.append(list(reasons or []))
        if len(runs) >= 3:
            done.set()
    monkeypatch.setattr(runtime, 'interpret_market', interpret)
    monkeypatch.setattr(runtime_module.refresh, 'interpretation_interval', lambda now, markets_open=None: 0.05)
    runtime.interpret = refresh.RecalcRequests(min_interval=0, accepted=refresh.interpretation_config()['triggers'])
    runtime.analysis_started.set()
    task = asyncio.create_task(runtime.interpretation_loop())
    try:
        await asyncio.sleep(0.01)
        runtime.interpret.request([{'trigger': 'EVENT_WINDOW_15', 'id': 'FED'}])
        await asyncio.wait_for(done.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert runs[0] == [] and [t['trigger'] for t in runs[1]] == ['EVENT_WINDOW_15'] and runs[2] == []
