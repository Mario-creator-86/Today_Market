"""Directional publication must revalidate a shared cycle snapshot before storage and streaming."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_pipeline import hub as hub
from test_scoring import REGIME, hormuz

from backend import decision
from backend.config import rules
from backend.database import Decision, utcnow


def lean_context(hub, now):
    event = hormuz(first_seen_utc=(now - timedelta(hours=1)).isoformat(), last_updated_utc=now.isoformat())
    event['evidence'] = [{'id': 'evidence', 'weight': 0.97, 'relation': 'supports', 'source_id': 'reuters',
                          'url': 'https://www.reuters.com/test', 'published_at': (now - timedelta(hours=1)).isoformat()}]
    quote = {'price': 62000.0, 'timestamp': now.isoformat(), 'freshness_status': 'FRESH',
             'market_status': 'LIVE', 'provider': 'binance'}
    ctx = SimpleNamespace(hub=hub, now=now, assets=[{'symbol': 'BTC', 'asset_class': 'crypto', 'quote': dict(quote)}],
                          events=[deepcopy(event)], releases=[], reactions={}, regime=REGIME, context=None,
                          health={'binance': 'ONLINE', 'reuters': 'ONLINE'}, central_banks={},
                          weights={'weights': rules()['weights']}, bars={'BTC': []},
                          levels={'BTC': {'support': 61000.0, 'resistance': 63000.0, 'atr': 300.0}},
                          latest_basket=lambda symbol: (None, {}), positioning_summary=lambda symbol: None)
    return ctx, quote, event


@pytest.mark.asyncio
@pytest.mark.parametrize(('change', 'reason'), [
    ('expired', 'Decision expired; awaiting recalculation'),
    ('stale', 'Critical quote unavailable before publication'),
    ('missing', 'Critical quote unavailable before publication'),
    ('closed', 'Critical quote unavailable before publication'),
    ('non_live', 'Non-live quote cannot confirm a decision'),
    ('technical', 'Price confirmation changed before publication'),
    ('invalidated', 'Price invalidation crossed'),
    ('evidence', 'Evidence changed or expired'),
    ('evidence_expired', 'Evidence changed or expired'),
    ('provider', 'Critical market provider unavailable'),
])
async def test_a_lean_is_revalidated_before_persistence(hub, monkeypatch, change, reason):
    now = utcnow()
    ctx, latest, event = lean_context(hub, now)
    checked_at = now + timedelta(seconds=1)
    if change == 'expired':
        checked_at = now + timedelta(seconds=hub.settings.analysis_seconds + 1)
    latest['timestamp'] = checked_at.isoformat()
    if change == 'stale':
        latest['freshness_status'] = 'STALE'
    elif change == 'closed':
        latest['market_status'] = 'LAST CLOSE'
    elif change == 'non_live':
        latest['data_class'] = 'PROXY'
    elif change == 'technical':
        latest['price'] = 64000.0  # a break above resistance against the bearish lean: the confirmation reversed
    elif change == 'invalidated':
        latest['price'] = 63000.0
    elif change == 'evidence':
        event['last_updated_utc'] = checked_at.isoformat()
    elif change == 'evidence_expired':
        event['verification_status'] = 'STALE'
    elif change == 'provider':
        hub.providers['binance'].health['status'] = 'OFFLINE'
    monkeypatch.setattr(decision, 'utcnow', lambda: checked_at)
    monkeypatch.setattr(hub, 'quote', lambda *args, **kwargs: None if change == 'missing' else dict(latest))
    monkeypatch.setattr(hub, 'event', lambda *args, **kwargs: event)

    async def unexpected_ai(*args, **kwargs):
        pytest.fail('A lean must be revalidated without requesting an AI audit')

    monkeypatch.setattr(hub.ai, 'run_with_id', unexpected_ai)
    result = await decision.analyse(ctx, ctx.assets[0])
    assert result['direction'] == 'WAIT' and result['category'] == 'WAIT / DATA INSUFFICIENT'
    assert result['audit_status'] == 'BLOCK'
    assert reason in result['blocks']
    with hub.store.session() as session:
        assert session.get(Decision, result['id']).payload == result


@pytest.mark.asyncio
async def test_a_valid_lean_publishes_with_the_latest_quote(hub, monkeypatch):
    now = utcnow()
    ctx, latest, event = lean_context(hub, now)
    checked_at = now + timedelta(seconds=1)
    latest.update(price=62100.0, timestamp=checked_at.isoformat())
    monkeypatch.setattr(decision, 'utcnow', lambda: checked_at)
    monkeypatch.setattr(hub, 'quote', lambda *args, **kwargs: dict(latest))
    monkeypatch.setattr(hub, 'event', lambda *args, **kwargs: event)
    result = await decision.analyse(ctx, ctx.assets[0])
    assert result['category'] == 'BEARISH LEAN / WAIT' and result['audit_status'] == 'NOT_REQUIRED'
    assert not result['blocks']
    assert result['price'] == latest['price']
    assert result['input_snapshot']['quote'] == latest
    assert result['created_at'] == now.isoformat()
    with hub.store.session() as session:
        assert session.get(Decision, result['id']).payload == result


@pytest.mark.asyncio
async def test_an_expired_lean_is_streamed_as_wait(hub, monkeypatch):
    now = utcnow()
    ctx, latest, event = lean_context(hub, now)
    checked_at = now + timedelta(seconds=hub.settings.analysis_seconds + 1)
    latest['timestamp'] = checked_at.isoformat()
    monkeypatch.setattr(decision, 'CycleContext', lambda *args: ctx)
    monkeypatch.setattr(decision, 'utcnow', lambda: checked_at)
    monkeypatch.setattr(hub, 'quote', lambda *args, **kwargs: dict(latest))
    monkeypatch.setattr(hub, 'event', lambda *args, **kwargs: event)
    monkeypatch.setattr(hub, 'publish_briefs', lambda *args: [])
    messages = []
    monkeypatch.setattr(hub, 'publish', lambda stream, payload: messages.append((stream, payload)))
    result = await hub.recalculate()
    assert len(result) == 1 and result[0]['direction'] == 'WAIT'
    assert messages == [('decisions', {key: result[0][key]
                                      for key in ('id', 'symbol', 'category', 'direction', 'score', 'audit_status')})]


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['chief_analyst', 'auditor'])
@pytest.mark.parametrize('change', ['price', 'clock_only', 'unchanged'])
async def test_ai_publication_stays_bound_to_the_inputs_actually_analyzed(hub, monkeypatch, role, change):
    now = utcnow()
    ctx, latest, event = lean_context(hub, now)
    latest['price'] = ctx.assets[0]['quote']['price'] = 60000.0
    # asset_levels includes its original live price. Thus the old number remains traceable after a price move.
    ctx.levels['BTC']['price'] = 60000.0
    clock = [now]
    monkeypatch.setattr(decision, 'utcnow', lambda: clock[0])
    monkeypatch.setattr(hub, 'quote', lambda *args, **kwargs: dict(latest))
    monkeypatch.setattr(hub, 'event', lambda *args, **kwargs: event)
    sent = {}

    async def ai_run(current_role, inputs):
        sent[current_role] = deepcopy(inputs)
        if current_role == role:
            if change == 'price':
                latest['price'] = 59000.0
            elif change == 'clock_only':
                clock[0] += timedelta(seconds=1)
                latest['timestamp'] = clock[0].isoformat()
                latest['age_seconds'] = 0.0
        if current_role == 'chief_analyst':
            return {'reasons': ['The current BTC price is 60000.0.'], 'evidence_ids': ['evidence'],
                    'confirmation': inputs['confirmation']['confirmation'],
                    'invalidation': inputs['confirmation']['invalidation']}, None
        return {'status': 'PASS', 'reasons': [], 'checks': []}, None

    monkeypatch.setattr(hub.ai, 'run_with_id', ai_run)
    result = await decision.analyse(ctx, ctx.assets[0])
    sent_hash = decision.explanation_input_hash(sent['chief_analyst'])
    assert decision.explanation_input_hash(result['input_snapshot']) == sent_hash
    if change == 'price':
        assert result['category'] == 'WAIT / DATA INSUFFICIENT' and result['audit_status'] == 'BLOCK'
        assert result['audit']['status'] == 'BLOCK'
        assert 'Analysis inputs changed during AI generation' in result['blocks']
        assert not hub.decision_ai_reuse
        assert result['input_snapshot']['quote']['price'] == 60000.0
        if role == 'chief_analyst':
            assert 'auditor' not in sent
    else:
        assert result['category'] == 'SHORT SETUP' and result['audit_status'] == 'PASS'
        assert not result['blocks']
        entry = hub.decision_ai_reuse['BTC']
        assert entry['input_hash'] == sent_hash
        assert decision.reusable(hub, entry['key'], result['input_snapshot'], clock[0]) is entry
        assert result['input_snapshot']['quote'] == latest
    with hub.store.session() as session:
        assert session.get(Decision, result['id']).payload == result
