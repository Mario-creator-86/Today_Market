from datetime import UTC, datetime, timedelta

import pytest

from backend.config import Settings
from backend.database import Store, utcnow
from backend.runtime import Runtime
from backend.service import HubService


@pytest.fixture
def runtime(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/loops.db')
    store.initialize()
    hub = HubService(store, Settings(enable_ingestion=False))
    hub.initialize()
    return Runtime(hub)


class Fake:
    configured = True

    def __init__(self, name, **methods):
        self.name, self.health = name, {'name': name, 'status': 'ONLINE'}
        for key, value in methods.items():
            setattr(self, key, value)


def returns(value):
    async def call(*args, **kwargs):
        return value
    return call




@pytest.mark.asyncio
async def test_positioning_records_are_stored_descriptively(runtime):
    runtime.hub.providers = {'cot': Fake('cftc_cot', positioning=returns([
        {'provider': 'cftc_cot', 'contract_code': '088691', 'report': 'disaggregated', 'as_of_date': '2026-09-22',
         'symbol': 'GOLD', 'directional_score': 0, 'label': 'descriptive'}]))}
    await runtime.poll_positioning()
    rows = runtime.hub.store.list('positioning_reports')
    assert rows[0]['id'] == 'cftc_cot:088691:disaggregated:2026-09-22' and rows[0]['directional_score'] == 0


def official_providers(fred_configured=False, fred_rows=()):
    ecb = Fake('ecb_data', quotes=returns([]), policy_rates=returns([]), yields=returns([]), hicp=returns([]))
    fred = Fake('fred', latest=returns([]), observations=returns(list(fred_rows)))
    fred.configured = fred_configured
    return {'ecb_data': ecb, 'fred': fred, 'eurostat': Fake('eurostat', hicp_flash=returns([])),
            'bls_data': Fake('bls_data', series=returns([{'provider': 'bls', 'series': 'CUUR0000SA0', 'period': '2026-08', 'value': 334.9}])),
            'treasury_fiscal': Fake('treasury_fiscal', tga=returns([])), 'eia_wpsr': Fake('eia_wpsr', inventories=returns([])),
            'nyfed_rates': Fake('nyfed_rates', policy_rate=returns({'bank': 'fed', 'currency': 'USD', 'policy_rate': 3.875, 'rate': 3.875,
                                                                    'as_of': '2026-09-24T00:00:00+00:00', 'verified': True,
                                                                    'source': 'Source: Federal Reserve Bank of New York'}))}


@pytest.mark.asyncio
async def test_official_loop_fills_fed_rate_from_new_york_fed_without_fred(runtime, monkeypatch):
    runtime.hub.providers = official_providers()

    async def state(ecb, fred, quote):
        return {'USD': {'bank': 'fed', 'currency': 'USD', 'missing': ['policy_rate', 'rate', 'as_of', 'verified', 'expected_next_rate'],
                        'reason': 'FRED not configured'},
                'EUR': {'bank': 'ecb', 'currency': 'EUR', 'rate': 2.5, 'as_of': '2026-09-15T22:00:00Z', 'verified': True, 'missing': []}}
    monkeypatch.setattr('backend.providers.official_data.build_central_bank_state', state)
    await runtime.poll_official()
    fed = runtime.hub.store.get('central_bank_state', 'fed')
    assert fed['rate'] == 3.875 and fed['missing'] == ['expected_next_rate'] and 'reason' not in fed
    assert runtime.hub.store.get('official_series', 'bls:CUUR0000SA0:2026-08')['value'] == 334.9
    assert runtime.hub.store.get('credit_spreads', 'BAA10Y') is None  # no FRED key: no credit input is invented


@pytest.mark.asyncio
async def test_credit_spread_change_comes_from_the_last_two_fred_observations(runtime, monkeypatch):
    now = utcnow()
    rows = [{'value': 1.80, 'as_of': (now - timedelta(days=2)).isoformat(), 'attribution': 'Source: FRED'},
            {'value': 1.86, 'as_of': (now - timedelta(days=1)).isoformat(), 'attribution': 'Source: FRED', 'delay': 'EOD'}]
    runtime.hub.providers = official_providers(fred_configured=True, fred_rows=rows)

    async def state(ecb, fred, quote):
        return {'USD': {'bank': 'fed', 'rate': 3.875, 'missing': []}}
    monkeypatch.setattr('backend.providers.official_data.build_central_bank_state', state)
    await runtime.poll_official()
    credit = runtime.hub.store.get('credit_spreads', 'BAA10Y')
    assert credit['change_bp'] == pytest.approx(6) and credit['freshness_status'] == 'FRESH'


@pytest.mark.asyncio
async def test_discovery_keeps_gdelt_out_of_evidence_and_ingests_ofac_documents(runtime):
    candidate = {'candidate_id': 'g1', 'title': 'Clash reported near strait', 'discovery_only': True}
    ofac_doc = {'source_id': 'ofac', 'title': 'Treasury sanctions shipping network', 'url': 'https://ofac.treasury.gov/recent-actions/1',
                'published_at': utcnow().isoformat(), 'content': 'Official action', 'event_type': 'SANCTIONS', 'assets': ['BRENT']}
    runtime.hub.providers = {'gdelt_events': Fake('gdelt_events', batch=returns({'status': 'OK', 'batch_id': 'b1', 'candidates': [candidate],
                                                                                'intensity': {'IR': {'events': 3}}})),
                             'ofac_actions': Fake('ofac_actions', documents=returns([ofac_doc]))}
    await runtime.poll_discovery()
    assert runtime.hub.store.get('discovery_items', 'g1')['discovery_only']
    events = runtime.hub.events()
    assert len(events) == 1 and events[0]['verification_status'] == 'CONFIRMED' and 'Clash' not in events[0]['summary_fact']


@pytest.mark.asyncio
async def test_calendar_keeps_first_ingestion_times_and_computes_surprise(runtime):
    scheduled = datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
    row = {'name': 'CPI YoY', 'country': 'US', 'category': 'MACRO', 'scheduled_at': scheduled.isoformat(), 'impact': 10,
           'consensus': 3.0, 'actual': None, 'provider': 'calendar', 'id': 'cpi'}

    class Calendar(Fake):
        async def calendar(self, start, end):
            return [dict(self.row)]
    provider = Calendar('calendar')
    provider.row = row
    runtime.hub.providers = {'calendar': provider}
    await runtime.refresh_calendar()
    first = runtime.hub.store.get('macro_calendar', 'cpi')
    provider.row = dict(row, actual=3.3)
    await runtime.refresh_calendar()
    released = runtime.hub.store.get('macro_calendar', 'cpi')
    assert released['ingested_at'] == first['ingested_at'] and released['actual_ingested_at']
    assert released['raw_surprise'] == pytest.approx(0.3) and 'z_score' in released
    await runtime.refresh_calendar()
    assert runtime.hub.store.get('macro_calendar', 'cpi')['actual_ingested_at'] == released['actual_ingested_at']


@pytest.mark.asyncio
async def test_x_official_account_is_official_statement_and_unknown_account_is_discovery(runtime):
    posts = [{'id': '11', 'text': 'Federal Reserve issues FOMC statement on the policy rate', 'author_username': 'federalreserve',
              'created_at': utcnow().isoformat()},
             {'id': '12', 'text': 'Explosion reported near Hormuz, unconfirmed', 'author_username': 'someone',
              'created_at': utcnow().isoformat()}]
    runtime.hub.providers = {'x': Fake('x', search=returns(posts))}
    researched = []

    async def corroborate(event, priority):
        researched.append(event['id'])
    runtime.corroborate = corroborate
    await runtime.social()
    official = runtime.hub.store.get('social_posts', '11')
    unknown = runtime.hub.store.get('social_posts', '12')
    assert official['source_id'] == 'x-federalreserve' and official['official']
    assert runtime.hub.event(official['event_id'])['verification_status'] == 'CONFIRMED'
    assert unknown['source_id'] == 'x-unknown' and runtime.hub.event(unknown['event_id'])['verification_status'] != 'CONFIRMED'
    assert researched == [unknown['event_id']]


def test_a_new_cross_asset_divergence_requests_one_recalculation(runtime):
    runtime.observe()  # baseline
    runtime.divergences = ['DIVERGENCE:BTC:btc_vs_drivers:relative_strength']
    runtime.observe()
    assert [t['trigger'] for t in runtime.recalc.pending] == ['CROSS_ASSET_DIVERGENCE']
    runtime.recalc.pending.clear()
    runtime.observe()  # other loops observing again must not re-trigger the same divergence
    assert runtime.recalc.pending == []


@pytest.mark.asyncio
async def test_corroboration_does_not_spend_search_budget_without_a_planner_model(runtime):
    runtime.hub.ollama.configured = True
    spent = []
    runtime.hub.search_budget.acquire = lambda topic, priority: spent.append(topic) or True
    await runtime.corroborate({'id': 'ev-1', 'summary_fact': 'Claim', 'event_type': 'NEWS', 'verification_status': 'UNCONFIRMED',
                               'sources': [], 'topic': None}, priority=1)
    assert spent == [] and not runtime.hub.ai.available('research_planner')
    runtime.hub.store.put('ai_models', 'm1', {'name': 'm1', 'availability': True, 'preferred_roles': []})
    runtime.hub.store.put('ai_role_assignments', 'research_planner', {'role': 'research_planner', 'primary': 'm1'})
    assert runtime.hub.ai.available('research_planner')


@pytest.mark.asyncio
async def test_in_process_calibration_runs_daily_and_survives_restarts(runtime, monkeypatch):
    from backend import workers
    calls = []
    monkeypatch.setattr(workers, 'calibrate_all', lambda store, now: calls.append(now) or {})
    await runtime.calibrate()
    await runtime.calibrate()  # e.g. a restart right after: not due yet
    assert len(calls) == 1
    runtime.hub.store.put('recalc_triggers', 'calibration:last_run', {'at': (utcnow() - timedelta(days=2)).isoformat()})
    await runtime.calibrate()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_stub_x_posts_are_stored_without_a_live_looking_link(runtime):
    """Regression (UI audit EV-23): development-stub posts kept an https://x.com/.../status/stub-1 link."""
    posts = [{'id': 'stub-1', 'text': 'Synthetic development post about the policy rate', 'author_username': 'federalreserve',
              'created_at': utcnow().isoformat(), 'stub': True}]
    runtime.hub.providers = {'x': Fake('x', search=returns(posts))}

    async def corroborate(event, priority):
        return False
    runtime.corroborate = corroborate
    await runtime.social()
    assert runtime.hub.store.get('social_posts', 'stub-1')['url'] is None


@pytest.mark.asyncio
async def test_central_bank_state_keeps_the_display_name_beside_the_bank_id(runtime, monkeypatch):
    """Regression (UI audit FX-4): the stored row replaced the display name 'ECB' with the id 'ecb'."""
    runtime.hub.providers = official_providers()

    async def state(ecb, fred, quote):
        return {'EUR': {'bank': 'ECB', 'currency': 'EUR', 'rate': 2.5, 'basis': 'ECB deposit facility rate', 'missing': []}}
    monkeypatch.setattr('backend.providers.official_data.build_central_bank_state', state)
    await runtime.poll_official()
    row = runtime.hub.store.get('central_bank_state', 'ecb')
    assert row['bank'] == 'ecb' and row['bank_name'] == 'ECB' and row['currency'] == 'EUR'
