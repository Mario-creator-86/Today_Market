"""Fewer AI requests at the source (docs/AI_AGENTS.md): research-planner attempts per event and per price anomaly (stored,
so restarts keep them), the search-budget topic cooldown, the interpretation cadence without fast retries, and the
extractor gate with its per-page dedupe. No network: fake providers only."""
import asyncio
import time
from datetime import timedelta

import pytest

from backend import analytics, notifications, refresh
from backend import runtime as runtime_module
from backend.ai import SearchBudgetManager
from backend.ai_budget import AIBudget
from backend.config import Settings, rules
from backend.database import Store, uid, utcnow
from backend.engines import aware
from backend.runtime import Runtime
from backend.schemas import DocumentInput
from backend.service import HubService, extraction_marker, worth_extracting

EXTRACTION = ('{"who":["Federal Reserve"],"what":"Rate decision","when":"today","where":"Washington",'
              '"direct_claims":[],"new_information":[],"background_information":[],"mentioned_assets":[],'
              '"mentioned_entities":[],"event_type":"CENTRAL_BANK","potential_importance":80}')


class FakeLLM:
    def __init__(self, *outputs):
        self.outputs = list(outputs) or ['not json']
        self.calls: list[str] = []

    async def chat(self, model, messages, schema):
        self.calls.append(model)
        return {'message': {'content': self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]}}


@pytest.fixture
def hub(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/volume.db')
    store.initialize()
    hub = HubService(store, Settings(enable_ingestion=False))
    hub.initialize()
    return hub


@pytest.fixture
def runtime(hub):
    return Runtime(hub)


def hot_event(event_id: str, evidence: int = 1) -> dict:
    return {'id': event_id, 'summary_fact': 'Explosion reported near the strait', 'event_type': 'NEWS',
            'verification_status': 'UNCONFIRMED', 'importance': 0.95, 'first_seen_utc': utcnow().isoformat(),
            'sources': [], 'topic': None, 'evidence': [{'id': f'{event_id}-{i}'} for i in range(evidence)]}


def age_attempt(hub: HubService, key: str, hours: float) -> None:
    state = hub.store.get('research_jobs', f'attempts:{key}')
    hub.store.put('research_jobs', f'attempts:{key}',
                  dict(state, last_attempt_at=(utcnow() - timedelta(hours=hours)).isoformat()))


# --- research planner -----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_event_is_researched_at_most_twice_and_again_early_only_with_new_evidence(runtime, hub, monkeypatch):
    events = [hot_event('ev1')]
    monkeypatch.setattr(hub, 'events', lambda limit=100: events)
    calls: list = []

    async def corroborate(event, priority, fresh_evidence=False):
        calls.append((event['id'], priority, fresh_evidence))
        return True
    monkeypatch.setattr(runtime, 'corroborate', corroborate)
    await runtime.research_hot_topics()
    await runtime.research_hot_topics()  # the next cycle: not again within recheck_hours
    assert calls == [('ev1', 0, False)]
    events[0] = hot_event('ev1', evidence=2)  # new evidence reached the event: one early re-check
    await runtime.research_hot_topics()
    assert calls[-1] == ('ev1', 0, True) and len(calls) == 2
    events[0] = hot_event('ev1', evidence=3)
    age_attempt(hub, 'ev1', 24)
    await runtime.research_hot_topics()  # max_attempts_per_event reached, whatever happens
    assert len(calls) == 2
    state = hub.store.get('research_jobs', 'attempts:ev1')
    assert state['attempts'] == rules()['refresh']['hot_topics']['max_attempts_per_event'] == 2
    assert state['evidence'] == 2 and state['kind'] == 'RESEARCH_ATTEMPTS'


@pytest.mark.asyncio
async def test_the_recheck_window_and_the_attempts_survive_a_restart(runtime, hub, monkeypatch):
    events = [hot_event('ev2')]
    monkeypatch.setattr(hub, 'events', lambda limit=100: events)
    calls: list = []

    async def corroborate(event, priority, fresh_evidence=False):
        calls.append(event['id'])
        return True
    monkeypatch.setattr(runtime, 'corroborate', corroborate)
    await runtime.research_hot_topics()
    restarted = Runtime(hub)  # a new process reads the stored attempts
    monkeypatch.setattr(restarted, 'corroborate', corroborate)
    await restarted.research_hot_topics()
    assert calls == ['ev2']
    age_attempt(hub, 'ev2', rules()['refresh']['hot_topics']['recheck_hours'] - 0.5)
    await restarted.research_hot_topics()
    assert calls == ['ev2']
    age_attempt(hub, 'ev2', rules()['refresh']['hot_topics']['recheck_hours'] + 0.5)
    await restarted.research_hot_topics()
    assert calls == ['ev2', 'ev2']


@pytest.mark.asyncio
async def test_a_request_that_did_not_start_is_not_an_attempt(runtime, hub, monkeypatch):
    events = [hot_event('ev3')]
    monkeypatch.setattr(hub, 'events', lambda limit=100: events)
    answers = [False, True]

    async def corroborate(event, priority, fresh_evidence=False):
        return answers.pop(0)
    monkeypatch.setattr(runtime, 'corroborate', corroborate)
    await runtime.research_hot_topics()
    assert hub.store.get('research_jobs', 'attempts:ev3') is None
    await runtime.research_hot_topics()
    assert hub.store.get('research_jobs', 'attempts:ev3')['attempts'] == 1


def test_the_hot_topic_cycle_is_fifteen_minutes():
    assert refresh.cadence('hot_topics')['seconds'] == 900


@pytest.mark.asyncio
async def test_an_exhausted_planner_budget_spends_no_search_slot(runtime, hub):
    hub.ollama.configured = True
    hub.store.put('ai_models', 'm1', {'name': 'm1', 'availability': True})
    hub.store.put('ai_role_assignments', 'research_planner', {'role': 'research_planner', 'primary': 'm1'})
    spent: list = []
    hub.search_budget.acquire = lambda topic, priority, **kwargs: spent.append(topic) or False
    assert hub.ai.allowed('research_planner')
    await runtime.corroborate(hot_event('ev4'), priority=0)
    assert spent == ['ev4']  # within the budget: the search budget is asked
    hub.ai.budget = AIBudget(hub.store, settings=lambda: {'daily_requests': 10, 'role_daily': {'research_planner': 1},
                                                          'spread': {'hourly_share_of_daily': 1, 'burst_hours': 1},
                                                          'reserve': {'roles': [], 'share': 0}})
    hub.store.put('ai_runs', 'r1', {'agent': 'research_planner', 'model': 'm1', 'success': True, 'requests': 1})
    assert not hub.ai.allowed('research_planner')
    assert await runtime.corroborate(hot_event('ev5'), priority=0) is False and spent == ['ev4']


def test_the_search_topic_cooldown_of_urgent_priorities_is_at_least_the_recheck_window():
    budget = SearchBudgetManager(60, 60)
    assert budget.acquire('topic', 0)
    budget.topics['topic'] = time.time() - 3600
    assert not budget.acquire('topic', 0) and not budget.acquire('topic', 1)
    assert budget.acquire('topic', 0, fresh_evidence=True)  # new evidence waives the cooldown, not the allowance
    budget.topics['topic'] = time.time() - (rules()['refresh']['hot_topics']['recheck_hours'] * 3600 + 1)
    assert budget.acquire('topic', 1)
    assert not SearchBudgetManager(0, 60).acquire('other', 0, fresh_evidence=True)


@pytest.mark.asyncio
async def test_an_unexplained_anomaly_is_researched_once_per_symbol_window_and_recheck(runtime, hub, monkeypatch):
    item = {'symbol': 'BTC', 'window_seconds': 300, 'triggered': True, 'research_recommended': True}
    monkeypatch.setattr(analytics, 'scan_anomalies', lambda *args, **kwargs: [item])
    monkeypatch.setattr(analytics, 'capture_reactions', lambda *args, **kwargs: None)
    monkeypatch.setattr(notifications, 'anomaly_alert', lambda *args, **kwargs: None)
    monkeypatch.setattr(notifications, 'market_alerts', lambda *args, **kwargs: [])
    monkeypatch.setattr(runtime, 'observe', lambda: None)
    calls: list = []

    async def corroborate(event, priority):
        calls.append((event['id'], priority))
        return True
    monkeypatch.setattr(runtime, 'corroborate', corroborate)
    await runtime.derived()
    await runtime.derived()
    assert calls == [('anomaly:BTC:300', 0)]
    assert Runtime(hub).research_due('anomaly:BTC:300', anomaly=True) == (False, False)  # stored: restarts keep it
    age_attempt(hub, 'anomaly:BTC:300', rules()['refresh']['hot_topics']['anomaly_recheck_hours'] + 0.1)
    await runtime.derived()
    assert len(calls) == 2


# --- market interpretation cadence -------------------------------------------------------------------------------------

def test_the_interpretation_cadence_and_debounce():
    config = refresh.interpretation_config()
    assert (config['active_seconds'], config['idle_seconds'], config['min_interval_seconds']) == (1800, 3600, 600)


@pytest.mark.asyncio
async def test_a_failed_interpretation_is_not_retried_before_the_next_tick_or_trigger(runtime, monkeypatch):
    runtime.analysis_started.set()
    monkeypatch.setattr(runtime_module.refresh, 'interpretation_interval', lambda now: 3600.0)
    real_cadence = runtime_module.refresh.cadence
    monkeypatch.setattr(runtime_module.refresh, 'cadence',
                        lambda kind: {'seconds': 0} if kind == 'derived' else real_cadence(kind))
    calls: list = []

    async def failing(reasons=None):
        calls.append(list(reasons or []))
        raise TimeoutError('provider timed out')
    monkeypatch.setattr(runtime, 'interpret_market', failing)
    for debounce in (None, 0):
        calls.clear()
        if debounce is not None:
            # Control: without the debounce the same trigger runs at once, so the wait came from the loop.
            runtime.interpret = refresh.RecalcRequests(min_interval=debounce,
                                                       accepted=refresh.interpretation_config()['triggers'])
        task = asyncio.create_task(runtime.interpretation_loop())
        try:
            await asyncio.sleep(0.2)
            assert len(calls) == 1  # the failed startup run; the next regular tick is an hour away
            runtime.interpret.request([{'trigger': 'EVENT_WINDOW_15', 'id': 'x'}])
            await asyncio.sleep(0.2)
            if debounce is None:
                assert len(calls) == 1  # a trigger right after the failure waits min_interval_seconds (600 s)
            else:
                assert len(calls) == 2 and calls[1][0]['trigger'] == 'EVENT_WINDOW_15'
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


# --- extractor gate ------------------------------------------------------------------------------------------------------

def document(source_id: str, url: str, title: str, content: str = 'Details of the announcement.') -> DocumentInput:
    return DocumentInput(source_id=source_id, title=title, content=content, url=url, published_at=utcnow())


def with_extractor(hub: HubService, *outputs) -> FakeLLM:
    provider = FakeLLM(*outputs)
    hub.ai.provider = provider
    hub.store.put('ai_models', 'm1', {'name': 'm1', 'availability': True})
    hub.store.put('ai_role_assignments', 'extractor', {'role': 'extractor', 'primary': 'm1'})
    return provider


@pytest.mark.asyncio
async def test_only_documents_worth_an_ai_read_go_to_the_extractor(hub):
    provider = with_extractor(hub, EXTRACTION)
    plain = hub.ingest_document(document('benzinga', 'https://www.benzinga.com/news/1', 'Company hosts its annual picnic'))
    await hub.enrich_event(plain['id'])
    assert provider.calls == []  # tier 4, no tracked asset, no causal rule: rule-based path only
    assert hub.event(plain['id'])['importance_basis'] == 'not assessed'
    assert hub.event(plain['id'])['importance'] is None
    named = hub.ingest_document(document('benzinga', 'https://www.benzinga.com/news/2', 'NVDA supplier raises guidance'))
    await hub.enrich_event(named['id'])
    assert provider.calls == ['m1']
    official = hub.ingest_document(document('fed', 'https://www.federalreserve.gov/newsevents/a.htm',
                                            'Federal Reserve issues statement'))
    await hub.enrich_event(official['id'])
    assert provider.calls == ['m1', 'm1'] and hub.event(official['id'])['ai_enriched']


def test_the_gate_rules():
    config = rules()['extraction']
    assert config['max_llm_tier'] == 2
    assert worth_extracting({'tier': 2, 'title': 'Anything at all'}, config)
    assert worth_extracting({'tier': 5, 'official': True, 'title': 'Anything at all'}, config)
    assert not worth_extracting({'tier': 4, 'title': 'A new strategy for the holidays'}, config)  # excluded name
    assert not worth_extracting({'tier': 4, 'title': 'The coin collector show opens'}, config)  # COIN only in capitals
    assert worth_extracting({'tier': 4, 'title': 'Shares of Tesla rise'}, config)
    assert worth_extracting({'tier': 5, 'title': 'BTC slides overnight'}, config)
    assert not worth_extracting({'tier': 4, 'title': 'Iran holds a trade fair'}, config)  # one keyword is not a rule
    assert worth_extracting({'tier': 4, 'title': 'Iran and the US agree a ceasefire around the strait of Hormuz'}, config)


@pytest.mark.asyncio
async def test_a_page_is_extracted_once_even_after_a_failure_or_a_restart(hub):
    provider = with_extractor(hub, 'not json')
    event = hub.ingest_document(document('fed', 'https://www.federalreserve.gov/newsevents/b.htm',
                                         'Federal Reserve issues statement on policy'))
    await hub.enrich_event(event['id'])
    assert provider.calls == ['m1', 'm1']  # one call and its schema correction, both invalid
    assert not hub.event(event['id']).get('ai_enriched')
    await hub.enrich_event(event['id'])  # the next feed poll brings the same page again
    assert len(provider.calls) == 2
    record = hub.store.get('research_jobs', extraction_marker('http://federalreserve.gov/newsevents/b.htm'))
    assert record['status'] == 'done' and record['success'] is False
    restarted = HubService(hub.store, Settings(enable_ingestion=False))
    restarted.ai.provider = provider
    await restarted.enrich_event(event['id'])
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_a_page_the_budget_refused_waits_for_another_budget_day(hub):
    provider = with_extractor(hub, EXTRACTION)
    hub.ai.budget = AIBudget(hub.store, settings=lambda: {'daily_requests': 10, 'role_daily': {'extractor': 0},
                                                          'spread': {'hourly_share_of_daily': 1, 'burst_hours': 1},
                                                          'reserve': {'roles': [], 'share': 0}})
    event = hub.ingest_document(document('fed', 'https://www.federalreserve.gov/newsevents/c.htm',
                                         'Federal Reserve announces a new facility'))
    await hub.enrich_event(event['id'])
    await hub.enrich_event(event['id'])
    refused = [r for r in hub.store.list('ai_runs') if r.get('agent') == 'extractor']
    assert provider.calls == [] and len(refused) == 1 and refused[0]['budget_refusal'] == 'role'
    key = extraction_marker(event['evidence'][0]['url'])
    assert hub.store.get('research_jobs', key)['status'] == 'refused'
    hub.store.put('research_jobs', key, dict(hub.store.get('research_jobs', key), day='2000-01-01'))
    hub.ai.budget = AIBudget(hub.store)
    hub._nothing_to_extract.clear()  # the in-memory note of this budget day (a new day clears it by itself)
    await hub.enrich_event(event['id'])
    assert provider.calls == ['m1'] and hub.event(event['id'])['ai_enriched']


def budget_settings(**caps) -> dict:
    return {'daily_requests': 100, 'role_daily': caps, 'spread': {'hourly_share_of_daily': 1, 'burst_hours': 2},
            'reserve': {'roles': [], 'share': 0}}


def stored_requests(hub: HubService, role: str, count: int) -> None:
    for _ in range(count):
        hub.store.put('ai_runs', uid(), {'agent': role, 'success': True, 'requests': 1})


@pytest.mark.asyncio
async def test_a_page_the_hourly_pace_refused_is_tried_again_after_the_pace_window(hub):
    provider = with_extractor(hub, EXTRACTION)
    paced = dict(budget_settings(extractor=50), spread={'hourly_share_of_daily': 0.01, 'burst_hours': 2})  # pace 2
    hub.ai.budget = AIBudget(hub.store, settings=lambda: paced)
    stored_requests(hub, 'auditor', 2)
    event = hub.ingest_document(document('fed', 'https://www.federalreserve.gov/newsevents/p.htm',
                                         'Federal Reserve announces a new lending facility'))
    await hub.enrich_event(event['id'])
    key = extraction_marker(event['evidence'][0]['url'])
    record = hub.store.get('research_jobs', key)
    assert record['status'] == 'refused' and record['refusal'] == 'pace' and record['first_refused_at']
    wait = (aware(record['retry_after']) - aware(record['at'])).total_seconds()
    assert wait == 2 * 3600  # budget.spread.burst_hours, not the rest of the day
    assert event['id'] not in hub._nothing_to_extract  # a page waiting for the pace is not "nothing to extract"
    await hub.enrich_event(event['id'])  # inside the wait: no request and no new refusal row
    assert len([r for r in hub.store.list('ai_runs') if r.get('agent') == 'extractor']) == 1
    hub.store.put('research_jobs', key, dict(record, retry_after=(utcnow() - timedelta(minutes=1)).isoformat()))
    hub.ai.budget = AIBudget(hub.store, settings=lambda: budget_settings(extractor=50))
    await hub.enrich_event(event['id'])
    assert provider.calls == ['m1'] and hub.event(event['id'])['ai_enriched']


@pytest.mark.asyncio
async def test_a_page_refused_for_the_day_is_dropped_once_older_than_max_page_age(hub):
    provider = with_extractor(hub, EXTRACTION)
    hub.ai.budget = AIBudget(hub.store, settings=lambda: budget_settings(extractor=0))
    hours = rules()['extraction']['max_page_age_hours']
    old = DocumentInput(source_id='fed', title='Federal Reserve announces a discount rate change',
                        content='Details of the announcement.', url='https://www.federalreserve.gov/newsevents/o.htm',
                        published_at=utcnow() - timedelta(hours=hours + 1))
    event = hub.ingest_document(old)
    await hub.enrich_event(event['id'])
    key = extraction_marker(event['evidence'][0]['url'])
    record = hub.store.get('research_jobs', key)
    assert record['status'] == 'refused' and record['refusal'] == 'role' and 'retry_after' not in record
    hub.store.put('research_jobs', key, dict(record, day='2000-01-01'))  # a later budget day
    hub.ai.budget = AIBudget(hub.store, settings=lambda: budget_settings(extractor=50))
    hub._nothing_to_extract.clear()
    await hub.enrich_event(event['id'])
    assert provider.calls == []  # yesterday's backlog stays on the rule-based path
    assert hub.store.get('research_jobs', key)['status'] == 'expired'


@pytest.mark.asyncio
async def test_once_half_the_extractor_cap_is_used_the_tier_alone_is_not_enough(hub):
    provider = with_extractor(hub, EXTRACTION)
    config = rules()['extraction']
    assert config['strict_after_cap_share'] == 0.5
    hub.ai.budget = AIBudget(hub.store, settings=lambda: budget_settings(extractor=4))
    stored_requests(hub, 'extractor', 1)
    assert not hub.extraction_strict(config)
    stored_requests(hub, 'extractor', 1)
    assert hub.extraction_strict(config)
    plain = {'tier': 1, 'official': True, 'title': 'Board meeting minutes are available'}
    assert worth_extracting(plain, config) and not worth_extracting(plain, config, strict=True)
    assert worth_extracting(dict(plain, title='NVDA supplier raises guidance'), config, strict=True)
    event = hub.ingest_document(document('fed', 'https://www.federalreserve.gov/newsevents/s.htm',
                                         'Board meeting minutes are available'))
    await hub.enrich_event(event['id'])
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_specialist_call_the_budget_refused_is_asked_again_later(hub, monkeypatch):
    provider = with_extractor(hub, EXTRACTION)
    monkeypatch.setattr(HubService, '_specialist', lambda self, event: 'macro')
    hub.store.put('ai_role_assignments', 'macro', {'role': 'macro', 'primary': 'm1'})
    hub.ai.budget = AIBudget(hub.store, settings=lambda: budget_settings(extractor=0, macro=0))
    event = hub.ingest_document(document('fed', 'https://www.federalreserve.gov/newsevents/m.htm',
                                         'Federal Reserve raises the policy rate by 25 basis points'))
    original = HubService.event
    monkeypatch.setattr(HubService, 'event', lambda self, event_id, context=None: dict(
        original(self, event_id, context), verification_status='CONFIRMED'))
    await hub.enrich_event(event['id'])
    assert hub.event(event['id']).get('analysed_status') != 'CONFIRMED' and provider.calls == []
    assert [r['budget_refusal'] for r in hub.store.list('ai_runs') if r.get('agent') == 'macro'] == ['role']
    hub.ai.budget = AIBudget(hub.store, settings=lambda: budget_settings(extractor=0, macro=5))
    await hub.enrich_event(event['id'])  # a later enrichment asks the specialist again
    assert provider.calls and set(provider.calls) == {'m1'}  # sent (this answer and its correction fail the schema)
    assert hub.event(event['id'])['analysed_status'] == 'CONFIRMED'  # a failed answer is not asked again


def test_the_setup_explanation_and_audit_are_reused_while_the_setup_is_unchanged():
    from types import SimpleNamespace

    from backend import decision
    hub = SimpleNamespace(decision_ai_reuse={})
    now = utcnow()
    candidate = {'category': 'LONG SETUP', 'direction': 'LONG'}
    key = decision.reuse_key('BTC', candidate, ['e2', 'e1'])
    assert key == decision.reuse_key('BTC', candidate, ['e1', 'e2'])
    inputs = {'quote': {'price': 64000.5}, 'levels': {'support': 63000}}
    explanation = {'reasons': ['Holds above 63000'], 'confirmation': 'A close above 64000.5', 'invalidation': ''}
    assert decision.reusable(hub, key, inputs, now) is None
    decision.remember(hub, key, now, explanation, None, {'chief_analyst': 'run-1', 'auditor': None}, None, inputs=inputs)
    entry = decision.reusable(hub, key, inputs, now + timedelta(minutes=5))
    assert entry and entry['explanation'] == explanation and entry['audit'] is None
    decision.remember(hub, key, now + timedelta(minutes=5), explanation, {'status': 'PASS'}, {'auditor': 'run-2'}, entry, inputs=inputs)
    again = decision.reusable(hub, key, inputs, now + timedelta(minutes=10))
    assert again['audit'] == {'status': 'PASS'} and again['runs'] == {'chief_analyst': 'run-1', 'auditor': 'run-2'}
    seconds = rules()['decision']['ai_reuse_seconds']
    assert decision.reusable(hub, key, inputs, now + timedelta(seconds=seconds)) is None  # expired
    moved = dict(inputs, quote={'price': 64100.0})  # the explanation cites a price no longer in the inputs
    assert decision.reusable(hub, key, moved, now + timedelta(minutes=10)) is None
    other = decision.reuse_key('BTC', dict(candidate, category='STRONG LONG SETUP'), ['e1', 'e2'])
    assert decision.reusable(hub, other, inputs, now + timedelta(minutes=10)) is None
    decision.remember(hub, key, now, None, None, {}, None, inputs=inputs)  # an unavailable explanation is never kept
    assert decision.reusable(hub, key, inputs, now + timedelta(minutes=10)) is not None
    assert decision.reusable(SimpleNamespace(), key, inputs, now) is None  # a hub without the cache never reuses


@pytest.mark.asyncio
async def test_the_most_important_hot_topics_are_researched_first(runtime, hub, monkeypatch):
    seen: list[str] = []

    async def research_event(event, priority):
        seen.append(event['id'])
        return True
    events = [dict(hot_event(name), importance=importance) for name, importance in (('a', 0.7), ('b', 0.95), ('c', 0.8))]
    monkeypatch.setattr(hub, 'events', lambda limit=100: events)
    monkeypatch.setattr(runtime, 'research_event', research_event)
    await runtime.research_hot_topics()
    assert seen == ['b', 'c', 'a']
