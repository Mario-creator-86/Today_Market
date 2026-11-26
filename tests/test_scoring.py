from datetime import UTC, datetime, timedelta

import pytest

from backend import scoring
from backend.config import rules
from backend.engines import decide

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def hormuz(**changes):
    data = {'id': 'e1', 'summary_fact': 'Explosion near the Strait of Hormuz', 'event_type': 'GEOPOLITICAL_ENERGY',
            'verification_status': 'CONFIRMED', 'fact_confidence': 95, 'first_seen_utc': (NOW - timedelta(hours=1)).isoformat(),
            'evidence': [{'weight': 0.97, 'relation': 'supports'}]}
    data.update(changes)
    return data


REGIME = {'regimes': ['INFLATION_SHOCK'], 'posture': 'DEFENSIVE',
          'evaluated': [{'regime': 'INFLATION_SHOCK', 'state': 'ACTIVE', 'confidence': 80}],
          'features': {'BRENT': {'price': 90, 'change_pct': 2.8}, 'US2Y': {'price': 4.2, 'change_pct': 0.5},
                       'DXY': {'price': 104, 'change_pct': 0.3}, 'NASDAQ': {'price': 20000, 'change_pct': -0.8}}}


def test_no_inputs_means_every_component_missing_never_zero():
    result = scoring.score_asset('BTC', NOW)
    assert result['components_missing'] == list(rules()['weights'])
    assert all(c['value'] is None and c['status'] == 'missing' and c['basis'] for c in result['components'].values())
    decision = decide(result['components'], [])
    assert decision['score'] == 0 and decision['components_missing'] == list(rules()['weights'])


def test_event_component_applies_graph_and_factors():
    base = scoring.event_component('BTC', [hormuz()], NOW)
    assert base['status'] == 'measured' and base['value'] < 0
    entry = base['events'][0]
    assert {'novelty', 'priced_in', 'surprise', 'market_confirmation'} <= set(entry['unmeasured_factors'])
    assert entry['factors']['source_quality']['measured']
    stale_story = scoring.event_component('BTC', [hormuz(novelty=0.0)], NOW)['value']
    priced = scoring.event_component('BTC', [hormuz(priced_in=1.0)], NOW)['value']
    unconfirmed_market = scoring.event_component('BTC', [hormuz()], NOW, confirmations={'e1': 0.0})['value']
    surprise = scoring.event_component('BTC', [hormuz(surprise_z_score=3.0)], NOW)['value']
    assert abs(stale_story) < abs(base['value']) and abs(priced) < abs(base['value'])
    assert abs(unconfirmed_market) < abs(base['value']) < abs(surprise)
    older = scoring.event_component('BTC', [hormuz(first_seen_utc=(NOW - timedelta(hours=13)).isoformat())], NOW)['value']
    assert abs(older) < abs(base['value'])
    assert scoring.event_component('BTC', [hormuz(verification_status='UNCONFIRMED')], NOW)['status'] == 'missing'
    assert scoring.event_component('BTC', [hormuz(event_type='NEWS')], NOW)['status'] == 'missing'


def test_macro_component_uses_released_z_scores_through_triggers():
    release = {'name': 'CPI YoY', 'country': 'US', 'actual': 3.1, 'z_score': 2.1, 'scheduled_at': (NOW - timedelta(hours=1)).isoformat()}
    assert scoring.macro_component('US2Y', [release], NOW)['value'] > 0
    btc = scoring.macro_component('BTC', [release], NOW)
    assert btc['value'] < 0 and 'INFLATION_HOT' in btc['basis']
    assert scoring.macro_component('BTC', [dict(release, z_score=None)], NOW)['status'] == 'missing'
    assert scoring.macro_component('BTC', [dict(release, actual=None)], NOW)['status'] == 'missing'
    old = dict(release, scheduled_at=(NOW - timedelta(days=5)).isoformat())
    assert scoring.macro_component('BTC', [old], NOW)['status'] == 'missing'


def reaction(status, horizon='15m', symbol='BTC', expected=-1):
    return {'symbol': symbol, 'horizon': horizon, 'status': status, 'expected_direction': expected, 'change': -1.2, 'unit': 'pct'}


def test_reaction_component_from_measured_reactions_and_basket():
    assert scoring.reaction_component('BTC', [])['status'] == 'missing'
    confirmed = scoring.reaction_component('BTC', [reaction('CONFIRMED')])
    assert confirmed['value'] == -100
    blended = scoring.reaction_component('BTC', [reaction('CONFIRMED')], {'confirmation_pct': 50, 'horizon': '15m'})
    assert blended['value'] == pytest.approx(-80)  # half confirm; the other half do not imply the opposite direction
    diverged = scoring.reaction_component('BTC', [reaction('CONFIRMED', '5m'), reaction('REVERSED', '1h')])
    assert diverged['value'] == 100 and '1h' in diverged['basis']
    assert scoring.reaction_component('BTC', [reaction('NOT_EXPECTED', expected=0)])['status'] == 'missing'


def test_technical_component_from_level_breaks():
    levels = {'support': 100.0, 'resistance': 110.0, 'atr': 2.0}
    assert scoring.technical_component('X', 111, levels)['value'] == pytest.approx(70)
    assert scoring.technical_component('X', 99, levels)['value'] == pytest.approx(-70)
    assert scoring.technical_component('X', 200, levels)['value'] == 100
    inside = scoring.technical_component('X', 105, levels)
    assert inside['status'] == 'measured' and inside['value'] == 0
    assert scoring.technical_component('X', 105, {})['status'] == 'missing'
    assert scoring.technical_component('X', None, levels)['status'] == 'missing'


def test_positioning_never_turns_calls_over_puts_into_bullish():
    assert scoring.positioning_component(None)['status'] == 'missing'
    calls = {'freshness_status': 'FRESH', 'options': {'totals': {'put_call_ratio': 0.2}}, 'futures': None,
             'crowding': [{'flag': 'CALL_HEAVY_OI', 'metric': 'put_call_ratio', 'value': 0.2, 'points': 0}]}
    assert scoring.positioning_component(calls)['value'] == 0
    crowded = dict(calls, crowding=[{'flag': 'PERP_FUNDING_HIGH', 'metric': 'funding_8h', 'value': 0.001, 'points': -20}])
    assert scoring.positioning_component(crowded)['value'] < 0
    assert scoring.positioning_component(dict(calls, freshness_status='STALE'))['status'] == 'missing'


def test_cross_asset_alignment_with_causal_paths():
    from backend import causal
    items = causal.paths('INFLATION_HOT')
    aligned = {'US2Y': {'change': 6.0, 'unit': 'bp'}, 'DXY': {'change': 0.4, 'unit': 'pct'}, 'NASDAQ': {'change': -1.0, 'unit': 'pct'}}
    result = scoring.cross_asset_component('BTC', items, aligned)
    assert result['value'] == -100 and set(result['aligned']) == {'US2Y', 'DXY', 'NASDAQ'}
    mixed = dict(aligned, DXY={'change': -0.4, 'unit': 'pct'})
    assert scoring.cross_asset_component('BTC', items, mixed)['value'] == pytest.approx(-100 / 3, abs=0.01)
    assert scoring.cross_asset_component('BTC', items, {'US2Y': aligned['US2Y']})['status'] == 'missing'
    assert scoring.cross_asset_component('BTC', [], aligned)['status'] == 'missing'
    moves = scoring.observed_moves({'US2Y': {'price': 4.0, 'change_pct': 1.0}, 'BTC': {'price': 1, 'change_pct': 2.0}})
    # 4.00% after a +1% move was 3.9604%: +3.96 bp, not the linearised 4.0.
    assert moves['US2Y'] == {'change': pytest.approx(400 / 101), 'unit': 'bp'} and moves['BTC']['unit'] == 'pct'


def test_regime_component():
    assert scoring.regime_component('BTC', None)['status'] == 'missing'
    assert scoring.regime_component('BTC', {'regimes': [], 'posture': 'INSUFFICIENT_DATA'})['status'] == 'missing'
    assert scoring.regime_component('BTC', {'regimes': ['MIXED'], 'posture': 'MIXED'})['value'] == 0
    risk_off = {'regimes': ['RISK_OFF'], 'posture': 'DEFENSIVE', 'evaluated': [{'regime': 'RISK_OFF', 'state': 'ACTIVE', 'confidence': 60}]}
    assert scoring.regime_component('BTC', risk_off, expected=-1)['value'] == -60
    assert 'aligned' in scoring.regime_component('BTC', risk_off, expected=-1)['basis']
    assert scoring.regime_component('GOLD', risk_off)['value'] == 60


def test_graded_confidence_confirmation_and_quality():
    event = {'status': 'measured', 'events': [{'contribution': -30, 'fact_confidence': 100}, {'contribution': -10, 'fact_confidence': 60}]}
    assert scoring.evidence_confidence(event) == 90
    assert scoring.evidence_confidence({'status': 'missing'}) is None
    reactions = [reaction('CONFIRMED', '15m'), reaction('PARTIAL', '1h')]
    baskets = [{'horizon': '15m', 'confirmation_pct': 80}, {'horizon': '1h', 'confirmation_pct': 40}]
    assert scoring.market_confirmation('BTC', reactions, baskets) == pytest.approx(((100 + 80) / 2 + (50 + 40) / 2) / 2)
    assert scoring.market_confirmation('BTC', [], []) is None
    quote = {'freshness_status': 'FRESH', 'timestamp': (NOW - timedelta(seconds=3)).isoformat()}
    components = {'event': {'status': 'measured'}, 'technical': {'status': 'measured'}}
    parts = scoring.data_quality_parts(quote, 'ONLINE', components, 10, NOW, 15)
    assert parts == {'freshness': 80.0, 'provider': 100.0, 'coverage': 45.0, 'history': 50.0}
    assert scoring.data_quality(quote, 'ONLINE', components, 10, NOW, 15) == pytest.approx(0.4 * 80 + 0.2 * 100 + 0.3 * 45 + 0.1 * 50)
    assert scoring.data_quality(None, 'OFFLINE', {}, 0, NOW, 15) == 0


def test_hormuz_scenario_end_to_end_with_texts_and_drivers():
    levels = {'support': 61000.0, 'resistance': 63000.0, 'atr': 300.0}
    result = scoring.score_asset('BTC', NOW, events=[hormuz()], price=60000, levels=levels, regime=REGIME)
    components = result['components']
    assert result['components_missing'] == ['macro', 'reaction', 'positioning']
    decision = decide(components, result['gates'])
    assert decision['category'] == 'SHORT SETUP' and decision['direction'] == 'SHORT'
    why = scoring.why_bullets('BTC', components)
    assert why[0].startswith('Explosion near the Strait of Hormuz (fact confirmed, 95%). Rule reading:') and 'BTC lower' in why[0]
    assert any(b.startswith('Macro (unavailable)') for b in why)
    assert any(b.startswith('Reaction (unavailable)') for b in why)
    assert any(b.startswith('Positioning (unavailable)') for b in why)
    drivers = scoring.driver_contributions(components)
    assert [r['driver'] for r in drivers['rows']] == ['Rates', 'USD', 'Geopolitics/Energy', 'Technical', 'Positioning', 'Other']
    assert drivers['net'] == pytest.approx(decision['score'], abs=0.02)
    rows = {r['driver']: r['value'] for r in drivers['rows']}
    assert rows['Geopolitics/Energy'] < 0 and rows['Technical'] == -15 and rows['Positioning'] == 0
    assert drivers['unmeasured'] == ['macro', 'reaction', 'positioning']
    text = scoring.confirmation_text('BTC', decision['score'], levels, result['paths'], {'NASDAQ': {'support': 19800, 'resistance': 20500}})
    assert text['confirmation_level'] == 61000 and text['invalidation_level'] == 63000
    assert '61,000.00' in text['confirmation'] and 'NASDAQ below 19,800.00' in text['confirmation']
    assert '63,000.00' in text['invalidation'] and 'NASDAQ reclaims 20,500.00' in text['invalidation']
    conflicted = scoring.score_asset('US10Y', NOW, events=[hormuz()], regime=REGIME)
    assert conflicted['conflict']['conflicting'] and conflicted['gates']
    assert decide(conflicted['components'], conflicted['gates'])['category'] == 'WAIT / DATA INSUFFICIENT'


def test_confirmation_text_long_neutral_and_missing_levels():
    from backend import causal
    items = causal.paths('FED_DOVISH')
    long = scoring.confirmation_text('EURUSD', 40, {'support': 1.081, 'resistance': 1.0925}, items, {'DXY': {'support': 103.1, 'resistance': 104.2}})
    assert long['confirmation'].startswith('EURUSD holds above 1.0925 (resistance)') and 'DXY below 103.10' in long['confirmation']
    assert 'DXY reclaims 104.20' in long['invalidation'] and 'US2Y up' in long['invalidation']
    assert scoring.confirmation_text('EURUSD', 0, None, items)['confirmation_level'] is None
    assert 'Await measured support/resistance' in scoring.confirmation_text('EURUSD', -40, None, items)['confirmation']


def test_decision_gates_are_scoped_to_the_feeds_and_facts_that_score():
    quote = {'freshness_status': 'FRESH', 'market_status': 'LIVE', 'provider': 'binance'}
    confirmed = hormuz(evidence=[{'weight': 0.97, 'relation': 'supports', 'source_id': 'reuters'}])
    event = scoring.event_component('BTC', [confirmed], NOW)
    health = {'binance': 'ONLINE', 'reuters': 'ONLINE', 'fed': 'OFFLINE'}
    assert scoring.decision_gates('BTC', NOW, quote=quote, events=[confirmed], event=event, provider_health=health) == []
    disputed = dict(confirmed, ai_disputed_evidence=['ev-1'])
    assert scoring.decision_gates('BTC', NOW, quote=quote, events=[disputed], event=scoring.event_component('BTC', [disputed], NOW),
                                  provider_health=health) == [f"Fact-checker disputes evidence: {confirmed['summary_fact']}"]
    offline = scoring.decision_gates('BTC', NOW, quote=quote, events=[confirmed], event=event, provider_health=dict(health, reuters='OFFLINE'))
    assert offline == ['Major provider unavailable: Reuters (licensed news), offline']  # a label, never an id
    limited = scoring.decision_gates('BTC', NOW, quote=quote, events=[confirmed], event=event,
                                     provider_health=dict(health, reuters='RATE_LIMITED', binance='AUTH_ERROR'))
    assert limited == ['Major provider unavailable: Binance (crypto spot), access rejected; '
                       'Reuters (licensed news), rate limited']
    assert 'Critical data missing or stale' in scoring.decision_gates('BTC', NOW, quote=None, events=[], event={}, provider_health={})
    closed = scoring.decision_gates('BTC', NOW, quote=dict(quote, market_status='LAST CLOSE'), events=[], event={}, provider_health={})
    assert closed == ['Market closed or price unavailable']
    # An unverified fact blocks when its importance was assessed as high, or - not yet assessed - when its source is
    # authoritative (tier weight >= 0.9); a missing rating alone never blocks (DEC-6).
    rumor = hormuz(id='e2', verification_status='UNCONFIRMED', summary_fact='Unverified tanker attack', tier_weight=0.9)
    old_rumor = dict(rumor, first_seen_utc=(NOW - timedelta(hours=12)).isoformat())
    unrelated = hormuz(id='e3', event_type='CRYPTO_REGULATION', verification_status='UNCONFIRMED')
    gates = scoring.decision_gates('BTC', NOW, quote=quote, events=[confirmed, rumor, old_rumor], event=event, provider_health=health)
    assert gates == ['Important fact unverified: Unverified tanker attack']
    assert scoring.decision_gates('BRENT', NOW, quote=quote, events=[unrelated], event={}, provider_health=health) == []
    minor = dict(rumor, importance=0.1)
    assert scoring.decision_gates('BTC', NOW, quote=quote, events=[confirmed, minor], event=event, provider_health=health) == []
    for unassessed in (dict(rumor, tier_weight=0.5), dict(rumor, tier_weight=0.2), {k: v for k, v in rumor.items() if k != 'tier_weight'}):
        assert scoring.decision_gates('BTC', NOW, quote=quote, events=[confirmed, dict(unassessed, importance=None)],
                                      event=event, provider_health=health) == []
    rated = dict(rumor, tier_weight=0.5, importance=0.5)  # a tier-4 item the extractor rated 100
    assert scoring.decision_gates('BTC', NOW, quote=quote, events=[confirmed, rated], event=event,
                                  provider_health=health) == ['Important fact unverified: Unverified tanker attack']
    contradicted = dict(rumor, verification_status='CONTRADICTED')
    assert any(g.startswith('High-quality sources contradict') for g in
               scoring.decision_gates('BTC', NOW, quote=quote, events=[contradicted], event={}, provider_health=health))
    social = hormuz(evidence=[{'weight': 0.2, 'relation': 'supports', 'source_id': 'x-unknown'}])
    social_event = scoring.event_component('BTC', [social], NOW)
    assert 'Score based mainly on social rumors' in scoring.decision_gates(
        'BTC', NOW, quote=quote, events=[social], event=social_event, provider_health=health)


def test_events_first_seen_after_now_never_score():
    future = hormuz(first_seen_utc=(NOW + timedelta(minutes=5)).isoformat())
    assert scoring.event_component('BTC', [future], NOW)['status'] == 'missing'
    assert scoring.score_asset('BTC', NOW, events=[future])['paths'] == []


def test_no_causal_link_gives_no_cross_asset_conditions():
    from backend import causal
    items = causal.paths('CRYPTO_REGULATION')
    text = scoring.confirmation_text('EURUSD', 40, {'support': 1.081, 'resistance': 1.0925}, items, {'DXY': {'support': 103.1}})
    assert text['confirmation'] == 'EURUSD holds above 1.0925 (resistance)'
    assert text['invalidation'] == 'EURUSD breaks below 1.0810 (support)'


def test_reaction_without_change_or_basket_horizon_and_rumor_kind_on_evidence():
    partial = {'symbol': 'BTC', 'horizon': '15m', 'expected_direction': -1, 'status': 'CONFIRMED', 'change': None}
    result = scoring.reaction_component('BTC', [partial], {'confirmation_pct': 75})
    assert result['status'] == 'measured' and result['value'] < 0 and 'latest window' in result['basis']
    quote = {'freshness_status': 'FRESH', 'market_status': 'LIVE', 'provider': 'binance'}
    social = hormuz(evidence=[{'weight': 0.2, 'relation': 'supports', 'source_id': 'some-new-account', 'kind': 'social'}])
    event = scoring.event_component('BTC', [social], NOW)
    assert 'Score based mainly on social rumors' in scoring.decision_gates(
        'BTC', NOW, quote=quote, events=[social], event=event, provider_health={})
