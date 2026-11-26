"""Guidance regressions: real calculations and isolated storage, never provider calls."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_pipeline import hub as hub
from test_pipeline import quote

from backend import analytics, decision, evaluation, scoring, technicals
from backend.config import rules
from backend.database import Decision, Tick, utcnow

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


def minute_bars(count=40):
    """Contiguous one-minute bars ending now: enough for the session range to be a level (technical.session_min_bars)
    and for the levels to count (technical.min_fresh_bars)."""
    return [{'time': (NOW - timedelta(minutes=count - i)).isoformat(),
             'open': 100.0, 'high': 110.0, 'low': 90.0, 'close': 100.0, 'volume': None}
            for i in range(count)]


@pytest.mark.parametrize(('price', 'sign'), [(115.0, 1), (85.0, -1)])
def test_actual_level_pipeline_can_observe_a_breakout(hub, price, sign):
    bars = minute_bars()
    before = technicals.asset_levels(hub.store, 'BTC', bars, NOW, price=100.0)
    after = technicals.asset_levels(hub.store, 'BTC', bars, NOW, price=price)
    assert (after['support'], after['resistance']) == (before['support'], before['resistance'])
    assert after['price'] == price
    assert scoring.technical_component('BTC', price, after)['value'] * sign > 0


@pytest.mark.parametrize(('close', 'price', 'boundary'), [(110.0, 115.0, 'resistance'), (90.0, 85.0, 'support')])
def test_a_level_touched_by_the_last_close_is_still_a_breakout_boundary(hub, close, price, boundary):
    bars = minute_bars()
    bars[-1]['close'] = close
    levels = technicals.asset_levels(hub.store, 'BTC', bars, NOW, price=price)
    assert levels[boundary] == close
    assert scoring.technical_component('BTC', price, levels)['value'] * (price - close) > 0


def test_forming_bars_do_not_define_breakout_levels_and_completed_bars_are_retained(hub):
    bars = minute_bars()
    baseline = technicals.asset_levels(hub.store, 'BTC', bars, NOW, price=115)
    forming = {'time': NOW.isoformat(), 'open': 100, 'high': 500, 'low': 1, 'close': 115, 'volume': None}
    current = technicals.asset_levels(hub.store, 'BTC', [*bars, forming], NOW, price=115)
    assert current == baseline
    assert current['observations'] == len(bars)


@pytest.mark.parametrize(('price', 'sign'), [(120.0, 1), (80.0, -1)])
def test_daily_only_history_anchors_levels_to_the_previous_completed_close(hub, price, sign):
    from backend.database import Bar

    with hub.store.session() as session:
        session.add(Bar(symbol='NVDA', interval_seconds=86400, start=NOW - timedelta(days=1),
                        open=100, high=110, low=90, close=100, volume=None, ticks=1, provider='alpaca'))
        session.commit()
    levels = technicals.asset_levels(hub.store, 'NVDA', [], NOW, price=price)
    assert (levels['support'], levels['resistance']) == (90, 110)
    assert scoring.technical_component('NVDA', price, levels)['value'] * sign > 0


def reaction_row(event_id, baseline, expected, horizon='5m', status='CONFIRMED'):
    return {'event_id': event_id, 'symbol': 'BTC', 'expected_direction': expected, 'horizon': horizon,
            'baseline_timestamp': baseline.isoformat(), 'status': status}


def test_recent_event_reaction_wins_over_an_older_longer_window():
    old = reaction_row('old', NOW - timedelta(hours=5), -1, '4h')
    new = reaction_row('new', NOW - timedelta(minutes=5), 1)
    for rows in ([old, new], [new, old]):
        assert scoring.reaction_component('BTC', rows)['value'] > 0


def test_basket_and_own_reaction_must_belong_to_the_same_event():
    old = reaction_row('old', NOW - timedelta(hours=5), -1, '4h')
    new = reaction_row('new', NOW - timedelta(minutes=5), 1)
    basket = {'event_id': 'new', 'horizon': '5m', 'confirmation_pct': 100}
    assert scoring.reaction_component('BTC', [old, new], basket)['value'] == 100
    assert scoring.reaction_component('BTC', [old], basket)['status'] == 'missing'


@pytest.mark.parametrize('expected', [-1, 1])
def test_no_market_reaction_is_neutral_instead_of_opposite_direction(expected):
    row = reaction_row('flat', NOW - timedelta(minutes=5), expected, status='NO_REACTION')
    basket = analytics.basket_confirmation('flat', '5m', [row], {'BTC': expected})
    assert scoring.reaction_component('BTC', [row], basket)['value'] == 0


@pytest.mark.parametrize('category', ['BULLISH LEAN / WAIT', 'BEARISH LEAN / WAIT'])
def test_directional_leans_expire_even_when_the_quote_is_fresh(hub, category):
    now = utcnow()
    hub.ingest_quote(quote(timestamp=now))
    at = now - timedelta(seconds=hub.settings.analysis_seconds + 1)
    payload = {'id': 'lean', 'symbol': 'BTC', 'created_at': at.isoformat(), 'direction': category,
               'category': category, 'score': 20, 'audit_status': 'NOT_REQUIRED', 'blocks': [], 'reasons': []}
    with hub.store.session() as session:
        session.add(Decision(id='lean', symbol='BTC', direction=category, score=20, created_at=at, payload=payload))
        session.commit()
    current = hub.assets(now, event_overlay=False)[0]['decision']
    assert current['direction'] == 'WAIT'
    assert 'Decision expired; awaiting recalculation' in current['blocks']
    with hub.store.session() as session:
        assert session.get(Decision, 'lean').payload == payload


def test_read_time_invalidation_is_explained_in_blocks(hub):
    payload = {'direction': 'LONG', 'category': 'LONG SETUP', 'audit_status': 'PASS',
               'blocks': [], 'reasons': [], 'why': ['Earlier evidence was bullish']}
    hub._downgrade(payload, ['Price invalidation crossed'])
    assert 'Price invalidation crossed' in payload['blocks']


@pytest.mark.parametrize('metadata', [
    {'stub': True}, {'daily_close': True}, {'data_class': 'DAILY_CLOSE'},
    {'data_class': 'DERIVED_DAILY'}, {'data_class': 'PROXY'},
])
def test_non_live_ticks_never_feed_reactions_anomalies_or_outcomes(hub, metadata):
    with hub.store.session() as session:
        for i, price in [(0, 100.0), (60, 110.0)]:
            at = NOW + timedelta(seconds=i)
            session.add(Tick(id=f'tick-{i}', symbol='BTC', price=price, timestamp=at, ingested_at=at,
                             provider='feed', payload=metadata))
        session.commit()
        assert analytics._tick_at(session, 'BTC', NOW, 30) is None
        from sqlalchemy import select
        assert session.scalars(select(Tick).where(*evaluation._usable('BTC', NOW + timedelta(minutes=2)))).all() == []
    assert analytics.recent_prices(hub.store, 'BTC', NOW + timedelta(minutes=2), 300) == []
    assert analytics.scheduled_reactions(hub.store, 'e', NOW, ['BTC'], {'BTC': 1}, NOW + timedelta(minutes=2),
                                         labels=['1m'], baseline_seconds=30, gap_seconds=30) == []


@pytest.mark.parametrize('metadata', [{}, {'data_class': 'LIVE'}, {'stub': False, 'daily_close': False}])
def test_legitimate_ticks_keep_reaction_and_evaluation_coverage(hub, metadata):
    with hub.store.session() as session:
        session.add(Tick(id='live', symbol='BTC', price=100, timestamp=NOW, ingested_at=NOW,
                         provider='feed', payload=metadata))
        session.commit()
        assert analytics._tick_at(session, 'BTC', NOW, 30) is not None
        from sqlalchemy import select
        assert len(session.scalars(select(Tick).where(*evaluation._usable('BTC', NOW))).all()) == 1
    assert analytics.recent_prices(hub.store, 'BTC', NOW, 60) == [(NOW, 100)]


def ai_inputs():
    return {'asset': 'BTC', 'quote': {'price': 110, 'timestamp': NOW.isoformat()},
            'levels': {'support': 90, 'resistance': 100},
            'components': {'event': 30, 'technical': 60},
            'component_status': {'event': 'measured', 'technical': 'measured'},
            'events': [{'id': 'e', 'trigger': 'FED_DOVISH', 'verification_status': 'CONFIRMED'}],
            'regime': {'regimes': ['RISK_ON'], 'posture': 'CONSTRUCTIVE'}}


@pytest.mark.parametrize('change', ['level_role', 'regime', 'event_direction'])
def test_setup_ai_reuse_requires_the_same_meaning_not_just_the_same_numbers(change):
    cached = SimpleNamespace(decision_ai_reuse={})
    candidate = {'category': 'LONG SETUP', 'direction': 'LONG'}
    key = decision.reuse_key('BTC', candidate, ['e'])
    inputs = ai_inputs()
    explanation = {'reasons': ['RISK_ON, dovish Fed and support at 90'],
                   'confirmation': 'Above 100', 'invalidation': 'Below 90'}
    decision.remember(cached, key, NOW, explanation, {'status': 'PASS'}, {}, None, inputs=inputs)
    assert decision.reusable(cached, key, inputs, NOW + timedelta(minutes=1))
    changed = deepcopy(inputs)
    if change == 'level_role':
        changed['levels'] = {'support': 80, 'resistance': 90, 'previous_day_low': 100}
    elif change == 'regime':
        changed['regime'] = {'regimes': ['RISK_OFF'], 'posture': 'DEFENSIVE'}
    else:
        changed['events'][0]['trigger'] = 'FED_HAWKISH'
    assert decision.reusable(cached, key, changed, NOW + timedelta(minutes=1)) is None


def test_setup_ai_reuse_ignores_quote_clock_refresh_only():
    cached = SimpleNamespace(decision_ai_reuse={})
    key = decision.reuse_key('BTC', {'category': 'LONG SETUP', 'direction': 'LONG'}, [])
    inputs = ai_inputs()
    explanation = {'reasons': ['Support at 90'], 'confirmation': 'Above 100', 'invalidation': 'Below 90'}
    decision.remember(cached, key, NOW, explanation, {'status': 'PASS'}, {}, None, inputs=inputs)
    updated = deepcopy(inputs)
    updated['quote']['timestamp'] = (NOW + timedelta(seconds=5)).isoformat()
    assert decision.reusable(cached, key, updated, NOW + timedelta(minutes=1))


@pytest.mark.asyncio
@pytest.mark.parametrize('during_audit', ['unchanged', 'stale', 'invalidated', 'evidence_changed'])
async def test_publication_rechecks_inputs_after_the_last_ai_await(hub, monkeypatch, during_audit):
    from test_scoring import REGIME, hormuz

    clock = [utcnow()]
    monkeypatch.setattr(decision, 'utcnow', lambda: clock[0])
    now = clock[0]
    event = hormuz(first_seen_utc=(now - timedelta(hours=1)).isoformat(), last_updated_utc=now.isoformat())
    event['evidence'] = [{'id': 'evidence', 'weight': 0.97, 'relation': 'supports', 'source_id': 'reuters',
                          'url': 'https://www.reuters.com/test', 'published_at': (now - timedelta(hours=1)).isoformat()}]
    current_quote = {'price': 60000.0, 'timestamp': now.isoformat(), 'freshness_status': 'FRESH',
                     'market_status': 'LIVE', 'provider': 'binance'}
    monkeypatch.setattr(hub, 'quote', lambda *args, **kwargs: dict(current_quote))
    monkeypatch.setattr(hub, 'event', lambda *args, **kwargs: event)

    async def ai_run(role, inputs):
        if role == 'chief_analyst':
            return {'reasons': ['Verified catalyst and technical breakdown'], 'evidence_ids': ['evidence'],
                    'confirmation': inputs['confirmation']['confirmation'],
                    'invalidation': inputs['confirmation']['invalidation']}, None
        assert role == 'auditor'
        if during_audit == 'stale':
            clock[0] += timedelta(seconds=hub.settings.quote_stale_seconds + 1)
            current_quote['freshness_status'] = 'STALE'
        elif during_audit == 'invalidated':
            current_quote['price'] = 64000.0
        elif during_audit == 'evidence_changed':
            event['last_updated_utc'] = (now + timedelta(seconds=1)).isoformat()
        return {'status': 'PASS', 'reasons': [], 'checks': []}, None

    monkeypatch.setattr(hub.ai, 'run_with_id', ai_run)
    ctx = SimpleNamespace(hub=hub, now=now, events=[deepcopy(event)], releases=[], reactions={}, regime=REGIME,
                          context=None, health={'binance': 'ONLINE', 'reuters': 'ONLINE'}, central_banks={},
                          weights={'weights': rules()['weights']}, bars={'BTC': minute_bars()},
                          levels={'BTC': {'support': 61000.0, 'resistance': 63000.0, 'atr': 300.0}},
                          latest_basket=lambda symbol: (None, {}), positioning_summary=lambda symbol: None)
    result = await decision.analyse(ctx, {'symbol': 'BTC', 'asset_class': 'crypto', 'quote': dict(current_quote)})
    if during_audit == 'unchanged':
        assert result['category'] == 'SHORT SETUP' and result['audit_status'] == 'PASS'
    else:
        assert result['category'] == 'WAIT / DATA INSUFFICIENT'
        assert result['audit_status'] == 'BLOCK' and result['blocks']
        assert result['audit']['status'] == 'BLOCK'
