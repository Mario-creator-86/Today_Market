"""Spec §40 critical cases (docs/EVENT_AWARENESS.md §7), deterministic parts. The AI-specific halves of tests 3 and 4
(the MarketInterpretationOutput validator accepting or rejecting an assessment) live with the market_interpreter tests;
here the fake LLM classification only enters through the stored-interpretation shape the snapshot reads."""
import copy
from datetime import UTC, datetime, timedelta

from test_event_risk import (
    NOW,
    RISK_ON,
    asset,
    by_id,
    cpi,
    decision,
    earnings,
    fomc,
    make_hub,
    news,
    price_path,
    regime,
    snap,
    statement,
)

from backend import event_risk as er

FED_AT = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
RATE_FACTS = {'consensus': 4.5, 'actual': 4.5, 'previous': 4.25, 'unit': '%',
              'market_pricing': {'hike_probability': 0.95, 'hold_probability': 0.05}}


def interpretation(event_id: str, assessment: str, at: datetime, evidence_ids: list[str] | None = None,
                   source: str = 'ai') -> dict:
    """The dashboard 'interpretation' block shape with a fake LLM classification of the statement (citing the official
    documents in evidence_ids)."""
    item = {'event_id': event_id, 'surprise_assessment': assessment, 'evidence_ids': list(evidence_ids or [])}
    return {'source': source, 'model': 'fake-llm', 'generated_at': at.isoformat(), 'stale': False, 'basis_hash': 'x',
            'output': {'ai_view': '', 'market_summary': '', 'dominant_event': None, 'favored_assets': [],
                       'pressured_assets': [], 'non_confirming_assets': [], 'key_risks': [], 'trade_watch': [],
                       'next_events': [item]}}


def test_1_risk_on_btc_favorable_fed_in_30_minutes_waits_for_fed(tmp_path):
    now = FED_AT - timedelta(minutes=30)
    btc = decision('LONG SETUP', 44, market_confirmation=63, evidence_confidence=91, invalidation_level=60000.0)
    published = {'BTC': btc, 'NASDAQ': decision('BULLISH LEAN / WAIT', 22), 'WTI': decision('LONG SETUP', 35)}
    before = copy.deepcopy(published)
    assets = [asset(s, now, published.get(s), change=RISK_ON['features'].get(s, {}).get('change_pct'))
              for s in ('BTC', 'NASDAQ', 'WTI', 'DXY', 'GOLD')]
    result = snap(make_hub(tmp_path), now, [fomc(FED_AT)], assets=assets, regime_=RISK_ON)
    fed = result['next_event']
    assert fed['event_id'] == 'FED-20261028-FOMC' and fed['time_bucharest'] == '20:00' and fed['minutes_until_event'] == 30
    assert fed['relevance_level'] == 'CRITICAL' and result['dominant_event']['event_id'] == fed['event_id']
    assert result['mode']['active'] and result['mode']['event_id'] == fed['event_id']
    assert {'USD', 'Treasuries', 'Crypto', 'Gold', 'FX'} <= set(result['mode']['affected_groups'])
    assert 'BTC' in [i['symbol'] for i in result['favored']]  # BTC may remain favorable ...
    risk = result['asset_event_risk']['BTC']
    # ... but its event risk is very high: relevance x (floor + (1 - floor) x 'high' exposure 0.75)
    assert risk['event_id'] == fed['event_id'] and risk['level'] == 'VERY HIGH' and risk['score'] >= 75
    assert risk['trade_status'] == 'WAIT FOR FED' and risk['adjusted_confidence'] < 44
    assert result['asset_event_risk']['WTI']['trade_status'] == 'LONG'  # an unexposed setup is not blocked by the Fed
    card = next(c for c in result['trade_watch'] if c['asset'] == 'BTC')
    assert card['trade_status'] == 'WAIT FOR FED' and card['upcoming_event']['event_id'] == fed['event_id']
    assert 'after FED: Dovish' in card['confirmation'] and 'after FED: Hawkish' in card['invalidation']
    assert card['historical_probability'] is None and card['historical_probability_reason']
    assert any(item['kind'] == 'event' and 'Hawkish' in item['text'] for item in result['what_can_break'])
    assert any(item['text'] == 'BTC breaks below 60,000.00' for item in result['what_can_break'])
    assert published == before  # the published decisions are untouched


def test_2_risk_on_no_important_event_for_12_hours_btc_confirmed_is_actionable_long(tmp_path):
    now = FED_AT - timedelta(days=3)
    assets = [asset('BTC', now, decision('LONG SETUP', 44, market_confirmation=80))]
    result = snap(make_hub(tmp_path), now, [fomc(FED_AT)], assets=assets, regime_=RISK_ON)
    risk = result['asset_event_risk']['BTC']
    assert risk['trade_status'] == 'LONG' and risk['score'] == 0.0 and risk['adjusted_confidence'] == 44
    assert result['next_event']['event_id'] == 'FED-20261028-FOMC' and not result['mode']['active']


def test_3_priced_hike_as_expected_with_press_conference_pending_is_not_a_hawkish_surprise(tmp_path):
    now = FED_AT + timedelta(minutes=5)
    row = fomc(FED_AT, **RATE_FACTS)
    fake = interpretation('FED-20261028-FOMC', 'HAWKISH_SURPRISE', now)  # a model output that must not be used
    assets = [asset('BTC', now, decision('LONG SETUP', 44, market_confirmation=70))]
    result = snap(make_hub(tmp_path), now, [row], assets=assets, regime_=RISK_ON, interpretation=fake)
    fed = by_id(result['events'], 'FED-')
    assert fed['phase'] == 'IN_PROGRESS' and fed['sub_events'][0]['phase'] in {'APPROACHING', 'IMMINENT'}
    basis = fed['surprise_basis']
    assert basis['headline_priced'] is True and basis['guidance_pending'] is True
    assert basis['allowed_assessments'] == ['UNDETERMINED', 'NEUTRAL_PRICED']
    post = result['post_event'][0]
    assert post['resolved_scenario'] is None and 'guidance pending' in post['resolution_basis']
    assert post['confirmation'] is None and post['divergence'] is False
    assert result['asset_event_risk']['BTC']['trade_status'] == 'WAIT FOR FED'  # the event is not finished
    assert 'next risk: press conference at 20:30 Bucharest' in fed['status_note']


def test_4_hike_expected_but_dovish_press_conference_applies_dovish_expectations(tmp_path):
    hub = make_hub(tmp_path)
    now = FED_AT + timedelta(minutes=100)  # press conference 18:30-19:30 UTC is over
    price_path(hub.store, 'US2Y', FED_AT, 3.90, {1: 3.89, 5: 3.88, 15: 3.86, 30: 3.84, 60: 3.82})
    price_path(hub.store, 'DXY', FED_AT, 100.0, {1: 99.95, 5: 99.9, 15: 99.7, 30: 99.6, 60: 99.5})
    price_path(hub.store, 'NASDAQ', FED_AT, 20000.0, {1: 20010.0, 5: 20040.0, 15: 20100.0, 30: 20150.0, 60: 20200.0})
    # The statement and the press-conference transcript are official Fed documents the AI received and cites.
    documents = [statement('fomc-st', 'Federal Reserve issues FOMC statement', FED_AT + timedelta(minutes=1)),
                 statement('fomc-pc', 'Transcript of the Chair press conference', FED_AT + timedelta(minutes=95))]
    fake = interpretation('FED-20261028-FOMC', 'DOVISH_SURPRISE', now - timedelta(minutes=2), ['ev-fomc-st', 'ev-fomc-pc'])
    result = snap(hub, now, [fomc(FED_AT, **RATE_FACTS)], documents, regime_=RISK_ON, interpretation=fake)
    fed = by_id(result['events'], 'FED-')
    assert fed['phase'] == 'POST_EVENT' and fed['surprise_basis']['guidance_pending'] is False
    assert 'DOVISH_SURPRISE' in fed['surprise_basis']['allowed_assessments']
    assert [d['kind'] for d in fed['official_documents']] == ['STATEMENT', 'PRESS_CONFERENCE_TRANSCRIPT']
    assert fed['surprise'] == {'assessment': 'DOVISH_SURPRISE', 'source': 'ai', 'evidence_ids': ['ev-fomc-st', 'ev-fomc-pc'],
                               'validated': True}
    post = result['post_event'][0]
    assert post['resolved_scenario'] == 'C' and 'DOVISH_SURPRISE' in post['resolution_basis']
    assert post['expected']['US2Y'] == -1 and post['expected']['DXY'] == -1 and post['expected']['NASDAQ'] == 1
    assert post['expected']['BTC'] == 1 and post['expected']['GOLD'] == 1
    assert post['horizon'] == '1h' and post['confirmation'] == 100.0 and post['divergence'] is False
    cell = post['reactions']['US2Y']['1h']
    assert cell['change'] == -8.0 and cell['unit'] == 'bp' and set(cell) == {'change', 'unit', 'status'}
    assert post['reactions']['NASDAQ']['1h']['unit'] == 'pct'  # yields in bp, the rest in percent


def test_5_hot_cpi_with_falling_yields_weaker_dollar_and_nasdaq_rally_is_market_divergence(tmp_path):
    hub = make_hub(tmp_path)
    t0 = NOW - timedelta(minutes=20)
    row = cpi(t0, consensus=0.3, actual=0.5, previous=0.2, unit='%')  # consensus injected by the test
    price_path(hub.store, 'US2Y', t0, 3.90, {1: 3.89, 5: 3.87, 15: 3.84})
    price_path(hub.store, 'US10Y', t0, 4.20, {1: 4.19, 5: 4.17, 15: 4.15})
    price_path(hub.store, 'DXY', t0, 100.0, {1: 99.9, 5: 99.7, 15: 99.6})
    price_path(hub.store, 'NASDAQ', t0, 20000.0, {1: 20050.0, 5: 20120.0, 15: 20200.0})
    assets = [asset('NASDAQ', NOW, decision('SHORT SETUP', -40))]
    result = snap(hub, NOW, [row], assets=assets, regime_=RISK_ON)
    post = result['post_event'][0]
    assert post['resolved_scenario'] == 'B' and 'above consensus' in post['resolution_basis']
    assert post['horizon'] == '15m' and post['measured'] == 4 and set(post['diverging']) == {'US2Y', 'US10Y', 'DXY', 'NASDAQ'}
    assert post['divergence'] is True and post['confirmation'] == 0.0 and post['research_recommended'] is True
    assert any(f.startswith('MARKET DIVERGENCE') for f in post['summary_facts'])
    nasdaq = result['asset_event_risk']['NASDAQ']
    assert nasdaq['trade_status'] != 'SHORT' and nasdaq['trade_status'].startswith('CONDITIONAL')  # no automatic short
    assert 'MARKET DIVERGENCE' in nasdaq['status_reason']
    assert assets[0]['decision']['category'] == 'SHORT SETUP'  # the decision itself stays published as it was
    assert post['event_id'] in er.trigger_state(result)['divergences']


def test_6_weekend_hormuz_escalation_btc_live_traditional_markets_await_reopen(tmp_path):
    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    hormuz = news('hormuz-1', 'Tanker attacked in the Strait of Hormuz; shipping halted', 'CONFIRMED',
                  saturday - timedelta(hours=2), trigger='GEOPOLITICAL_ENERGY', event_type='GEOPOLITICAL_ENERGY')
    assets = [asset(s, saturday) for s in ('BTC', 'NASDAQ', 'US10Y', 'EURUSD', 'BRENT')]
    result = snap(make_hub(tmp_path), saturday, [], [hormuz], assets=assets,
                  regime_=regime(['GEOPOLITICAL_SHOCK'], 'DEFENSIVE'))
    assert result['markets_open'] == {'crypto': True, 'us_equities': False, 'us_rates': False, 'fx': False,
                                      'commodities': False}
    active = result['active_events'][0]
    assert active['event_class'] == 'GEO_ENERGY' and active['timing'] == 'ANYTIME' and active['status'] == 'UNRESOLVED'
    assert 'BTC' in active['live_assets'] and {'NASDAQ', 'US10Y', 'EURUSD'} <= set(active['awaiting_reopen'])
    assert active['consequences']['BRENT'] == 1 and active['consequences']['NASDAQ'] == -1
    per_asset = result['asset_event_risk']
    assert per_asset['BTC']['awaiting_reopen'] is False and per_asset['BTC']['score'] > 0  # interpreted live
    assert all(per_asset[s]['awaiting_reopen'] is True for s in ('NASDAQ', 'US10Y', 'EURUSD'))


def test_7_unverified_x_missile_post_is_only_a_potential_event(tmp_path):
    hub = make_hub(tmp_path)
    assets = [asset(s, NOW, decision('LONG SETUP', 40, market_confirmation=70)) for s in ('BTC', 'BRENT', 'NASDAQ')]
    rows = [fomc(FED_AT + timedelta(days=1))]
    post = news('x-1', 'Missile strike reported on a tanker near Hormuz', 'UNCONFIRMED', NOW - timedelta(minutes=10),
                trigger='GEOPOLITICAL_ENERGY', event_type='SOCIAL_DISCOVERY', sources=['X (unknown account)'])
    without = snap(hub, NOW, rows, [], assets=copy.deepcopy(assets), regime_=RISK_ON)
    with_post = snap(hub, NOW, rows, [post], assets=copy.deepcopy(assets), regime_=RISK_ON)
    assert with_post['active_events'] == [] and len(with_post['potential_events']) == 1
    potential = with_post['potential_events'][0]
    assert potential['status'] == 'POTENTIAL EVENT · UNCONFIRMED' and potential['verification_status'] == 'UNCONFIRMED'
    assert potential['dominance_score'] == 0.0 and 'under verification' in potential['status_note']
    for key in ('asset_event_risk', 'mode', 'dominant_event', 'favored', 'pressured', 'non_confirming'):
        assert with_post[key] == without[key], key
    assert [c['trade_status'] for c in with_post['trade_watch']] == [c['trade_status'] for c in without['trade_watch']]


def test_8_apple_earnings_tonight_high_aapl_risk_moderate_nasdaq_low_oil(tmp_path):
    now = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
    row = earnings('AAPL', datetime(2026, 10, 28, 20, 0, tzinfo=UTC), estimates={'eps': 2.02, 'label': 'Finnhub estimate'})
    assets = [asset(s, now) for s in ('AAPL', 'NASDAQ', 'WTI')]
    result = snap(make_hub(tmp_path), now, [row], assets=assets, regime_=RISK_ON)
    event = by_id(result['events'], 'EARN-')
    assert event['event_id'] == 'EARN-20261028-AAPL-EARNINGS' and event['time_known'] is False
    # never 'Street consensus' (docs/EVENT_AWARENESS.md section 8 shape)
    assert event['consensus'] == {'label': 'Finnhub estimate', 'values': [{'name': 'EPS', 'value': 2.02, 'unit': None}],
                                  'source': 'finnhub_earnings'}
    risk = result['asset_event_risk']
    assert risk['AAPL']['level'] in {'HIGH', 'VERY HIGH', 'CRITICAL'}
    assert risk['NASDAQ']['level'] in {'HIGH', 'MODERATE'} and risk['NASDAQ']['score'] < risk['AAPL']['score']
    assert risk['WTI']['score'] == 0.0 and risk['WTI']['level'] == 'LOW'
    assert not result['mode']['active']  # a date-only row never triggers EVENT RISK MODE


def test_9_risk_off_from_a_growth_scare_favors_treasuries(tmp_path):
    growth = regime(['RISK_OFF', 'GROWTH_SCARE'], 'DEFENSIVE', {'Rates': 'down', 'Risk appetite': 'down'},
                    {'US10Y': {'price': 4.1, 'change_bp': -8.0}, 'US2Y': {'price': 3.8, 'change_bp': -6.0},
                     'SP500': {'price': 6000.0, 'change_pct': -1.5}, 'GOLD': {'price': 2500.0, 'change_pct': 0.8}})
    assets = [asset(s, NOW, change=growth['features'].get(s, {}).get(
        'change_bp' if s.startswith('US') else 'change_pct')) for s in ('US10Y', 'US2Y', 'SP500', 'GOLD', 'NASDAQ')]
    result = snap(make_hub(tmp_path), NOW, assets=assets, regime_=growth)
    favored = {i['symbol']: i for i in result['favored']}
    assert {'US10Y', 'US2Y'} <= set(favored) and favored['US10Y']['expected_direction'] == -1
    assert 'Treasuries ↑' in favored['US10Y']['display'] and result['regime']['cause']['id'] == 'RISK_OFF_GROWTH'
    assert 'SP500' in [i['symbol'] for i in result['pressured']]


def test_10_risk_off_from_an_inflation_or_oil_shock_does_not_favor_treasuries(tmp_path):
    shock = regime(['RISK_OFF', 'INFLATION_SHOCK'], 'DEFENSIVE', {'Rates': 'up', 'Oil': 'up', 'Risk appetite': 'down'},
                   {'US10Y': {'price': 4.4, 'change_bp': 6.0}, 'US2Y': {'price': 4.0, 'change_bp': 8.0},
                    'BRENT': {'price': 90.0, 'change_pct': 3.0}, 'DXY': {'price': 104.0, 'change_pct': 0.5}})
    assets = [asset(s, NOW, change=shock['features'].get(s, {}).get(
        'change_bp' if s.startswith('US') else 'change_pct')) for s in ('US10Y', 'US2Y', 'DXY', 'GOLD', 'NASDAQ')]
    hub = make_hub(tmp_path)
    result = snap(hub, NOW, assets=assets, regime_=shock)
    favored = [i['symbol'] for i in result['favored']]
    assert 'US10Y' not in favored and 'US2Y' not in favored and 'DXY' in favored
    assert {'US10Y', 'US2Y'} <= {i['symbol'] for i in result['pressured']}  # yields up: bonds pressured
    rally = copy.deepcopy(shock)
    rally['features']['US10Y']['change_bp'] = -6.0  # even if yields fall, Treasuries are not automatically favored
    next(a for a in assets if a['symbol'] == 'US10Y')['quote']['change_bp'] = -6.0
    again = snap(hub, NOW, assets=assets, regime_=rally)
    assert 'US10Y' not in [i['symbol'] for i in again['favored']]
    assert 'US10Y' in [i['symbol'] for i in again['non_confirming']]
