"""Guidance must stay valid as prices stream independently of full analyses."""
from datetime import timedelta

import pytest
from test_pipeline import hub as hub

from backend import audit, regime, scoring
from backend.database import Decision, Tick, utcnow
from backend.engines import aware


def test_confirmation_percentage_follows_the_selected_event():
    now = utcnow()
    old = {'event_id': 'old', 'symbol': 'BTC', 'expected_direction': -1, 'horizon': '1h',
           'baseline_timestamp': (now - timedelta(hours=2)).isoformat(), 'status': 'CONFIRMED'}
    new = dict(old, event_id='new', expected_direction=1, horizon='15m',
               baseline_timestamp=(now - timedelta(minutes=20)).isoformat(), status='NO_REACTION')
    basket = {'event_id': 'new', 'horizon': '15m', 'confirmation_pct': 0}
    for rows in ([old, new], [new, old]):
        assert scoring.reaction_component('BTC', rows, basket)['value'] == 0
        assert scoring.market_confirmation('BTC', rows, [basket]) == 0
        assert scoring.market_confirmation('BTC', rows, []) == 0


@pytest.mark.parametrize('metadata', [
    {'stub': True}, {'provider': 'feed-stub'}, {'data_class': 'PROXY'},
    {'data_class': 'UNRECOGNIZED'}, {'data_class': 'LIVE', 'daily_close': True},
])
def test_non_live_metadata_blocks_even_a_fresh_open_quote(metadata):
    quote = dict(price=100, timestamp=utcnow().isoformat(), provider='feed', freshness_status='FRESH', market_status='LIVE')
    quote.update(metadata)
    blocks = scoring.decision_gates('BTC', utcnow(), quote=quote, events=[], event={}, provider_health={})
    assert blocks, 'A non-live quote must also block leans, which do not pass through the AI auditor'
    checked = audit.pre_audit({'direction': 'WAIT'}, {'quote': quote}, utcnow(), 15, 30)
    assert not next(c for c in checked['checks'] if c['name'] == 'fresh_prices')['passed']
    assert regime.live_quote({'symbol': 'BTC', 'quote': quote}) == {}


def save_setup(hub, now):
    payload = {'id': 'setup', 'symbol': 'BTC', 'created_at': now.isoformat(), 'direction': 'LONG',
               'category': 'LONG SETUP', 'score': 50, 'audit_status': 'PASS', 'blocks': [], 'reasons': []}
    with hub.store.session() as session:
        session.add(Decision(id='setup', symbol='BTC', direction='LONG', score=50, created_at=now, payload=payload))
        session.commit()
    return payload


def save_tick(hub, now, metadata, provider='feed'):
    with hub.store.session() as session:
        session.add(Tick(id='q', symbol='BTC', price=100, timestamp=now, ingested_at=now, provider=provider, payload=metadata))
        session.commit()


@pytest.mark.parametrize(('metadata', 'provider'), [
    ({'stub': True}, 'feed'), ({}, 'feed-stub'), ({'data_class': 'PROXY'}, 'feed'),
    ({'data_class': 'UNRECOGNIZED'}, 'feed'),
])
def test_published_setups_cannot_survive_a_switch_to_non_live_data(hub, metadata, provider):
    now = utcnow()
    save_setup(hub, now)
    save_tick(hub, now, metadata, provider)
    current = hub.assets(now, event_overlay=False)[0]
    assert current['decision']['direction'] == 'WAIT'
    assert current['decision']['blocks']
    if provider.endswith('-stub'):
        assert current['quote']['stub'] is True
    if metadata.get('data_class'):
        assert current['quote']['data_class'] == metadata['data_class']


def test_published_decision_carries_its_configured_deadline_without_rewriting_history(hub):
    now = utcnow()
    original = save_setup(hub, now)
    save_tick(hub, now, {})
    current = hub.assets(now, event_overlay=False)[0]['decision']
    assert current['direction'] == 'LONG'
    assert aware(current['expires_at']) == now + timedelta(seconds=hub.settings.analysis_seconds)
    with hub.store.session() as session:
        assert session.get(Decision, 'setup').payload == original
