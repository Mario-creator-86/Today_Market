"""Interpretation responses expose the same configured age deadline the backend enforces."""
from datetime import timedelta

import pytest
from test_interpretation import NOW, make_store

from backend import interpretation


@pytest.fixture
def configured_freshness(monkeypatch):
    cfg = dict(interpretation.config(), active_seconds=70, idle_seconds=210, stale_factor=3,
               unchanged_max_age_seconds=650)
    monkeypatch.setattr(interpretation, 'config', lambda: cfg)
    return cfg


@pytest.mark.parametrize(('source', 'cadence', 'limit'), [
    ('deterministic', 70, 210), ('deterministic', 210, 630),
    ('ai', 70, 650), ('ai', 240, 720),
])
def test_stored_interpretation_exposes_its_effective_freshness_deadline(
        tmp_path, configured_freshness, source, cadence, limit):
    store = make_store(tmp_path)
    row = {'source': source, 'generated_at': NOW.isoformat(), 'basis_hash': 'snapshot',
           'output': {'ai_view': 'Stored market summary'}, 'cadence_seconds': cadence}
    store.put('interpretations', 'stored', row)
    at_deadline = interpretation.latest(store, NOW + timedelta(seconds=limit))
    assert at_deadline['stale_after_seconds'] == limit
    assert not at_deadline['stale']
    expired = interpretation.latest(store, NOW + timedelta(seconds=limit + 1))
    assert expired['stale_after_seconds'] == limit and expired['stale']
    assert store.get('interpretations', 'stored') == row


@pytest.mark.parametrize(('open_market', 'limit'), [(True, 210), (False, 630)])
def test_current_snapshot_summary_exposes_its_configured_session_deadline(
        configured_freshness, open_market, limit):
    snapshot = {'generated_at': NOW.isoformat(), 'markets_open': {
        'crypto': True, 'us_equities': open_market, 'us_rates': False, 'fx': False, 'commodities': False}}
    original = dict(snapshot, markets_open=dict(snapshot['markets_open']))
    result = interpretation.for_snapshot(None, snapshot, None, [], NOW)
    assert result['source'] == 'deterministic'
    assert result['generated_at'] == NOW.isoformat() and not result['stale']
    assert result['stale_after_seconds'] == limit
    assert snapshot == original


@pytest.mark.parametrize('traditional_open', [True, False])
def test_open_sessions_do_not_claim_live_observations(traditional_open):
    regime = {'posture': 'MIXED', 'regimes': ['MIXED'], 'basis': 'last_close',
              'basis_label': 'last close, markets closed',
              'current_drivers': [{'driver': name, 'direction': 'unavailable', 'inputs': []}
                                  for name in ('Rates', 'USD', 'Oil', 'Risk appetite')]}
    snapshot = {'generated_at': NOW.isoformat(), 'regime': regime, 'markets_open': {
        'crypto': True, 'us_equities': False, 'us_rates': traditional_open,
        'fx': traditional_open, 'commodities': traditional_open}}
    output = interpretation.deterministic_output(snapshot)
    assert 'historical context' in output['ai_view'].lower()
    assert 'there is no current observation to confirm it yet' in output['ai_view']
    # The sessions are said once, in the summary (the market view carries the regime and the event only), and an open
    # session is never described as live observations.
    assert 'closed' not in output['ai_view'] and 'interpreted live' not in output['ai_view'] + output['market_summary']
    assert output['market_summary'].count('cannot be confirmed until') == 1
    assert ('markets are open' if traditional_open else 'crypto market is open') in output['market_summary']
