"""Regressions from the live dashboard: historical context must not confirm today's move."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from test_regime_last_close import close

from backend import event_risk as er
from backend import interpretation
from backend.regime import evaluate

NOW = datetime(2026, 9, 28, 16, tzinfo=UTC)


def markets():
    assets = [close('SP500', 7743.41, provider='fred', state='LIVE', change_pct=0.51),
              close('NASDAQ', 27068.72, provider='fred', state='LIVE', change_pct=0.48),
              close('DXY', 101.09, state='LIVE', change_pct=0.11),
              close('US2Y', 4.81, state='LIVE', change_bp=-6),
              close('BRENT', 114.89, state='LIVE', change_pct=-1.08)]
    for asset, via, change in [(assets[0], 'SPY', -0.8), (assets[1], 'QQQ', -1.2), (assets[4], 'BNO', 2.9)]:
        asset['proxy'] = {'symbol': via, 'change_pct': change, 'change_basis': 'previous_close',
                          'provider': 'finnhub', 'timestamp': NOW.isoformat(), 'freshness_status': 'FRESH',
                          'market_status': 'LIVE', 'relation': 'direction_only'}
    return assets


def historical(assets):
    return evaluate(assets, [], NOW, last_close=True)


def test_fresh_proxy_contradiction_cannot_be_called_favored():
    assets = markets()
    regime = historical(assets)
    assert regime['regimes'] == ['RISK_ON']  # the old close remains historical context
    lists = er.market_view(assets, regime, {})
    assert not {'SP500', 'NASDAQ'} & {i['symbol'] for i in lists['favored']}
    item = next(i for i in lists['non_confirming'] if i['symbol'] == 'NASDAQ')
    assert any('-1.20%' in f and 'QQQ' in f for f in item['reason_facts'])


@pytest.mark.parametrize('bad', [{'freshness_status': 'STALE'}, {'stub': True},
                                 {'market_status': 'CLOSED'}, {'change_basis': 'unknown'},
                                 {'change_pct': float('nan')}])
def test_unusable_proxy_does_not_confirm_old_close_in_open_market(bad):
    assets = markets()
    assets[1]['proxy'].update({'change_pct': 1.2, **bad})
    lists = er.market_view(assets, historical(assets), {})
    assert 'NASDAQ' not in {i['symbol'] for i in lists['favored']}


def test_fresh_direct_quote_has_priority_and_opposing_move_vetoes_old_decision():
    assets = markets()
    assets[1]['quote'] = {'price': 26000, 'change_pct': -2, 'freshness_status': 'FRESH',
                          'data_class': 'LIVE', 'timestamp': NOW.isoformat(), 'session': {'state': 'LIVE'}}
    assets[1]['proxy']['change_pct'] = 1.2
    assets[1]['decision'] = {'category': 'LONG', 'score': 70}
    regime = historical(markets())
    lists = er.market_view(assets, regime, {})
    assert 'NASDAQ' not in {i['symbol'] for i in lists['favored']}
    assert any('-2.00%' in f for i in lists['non_confirming'] if i['symbol'] == 'NASDAQ' for f in i['reason_facts'])


def test_historical_regime_exposes_current_driver_conflicts_without_changing_decisions():
    assets = markets()
    regime = historical(assets)
    assert {d['feature'] for d in regime['divergences']} == {'SP500', 'NASDAQ', 'BRENT'}
    assert any('BNO' in d['text'] and '2.90%' in d['text'] for d in regime['divergences'])
    assert {d['driver']: d['direction'] for d in regime['current_drivers']}['Oil'] == 'up'
    assert evaluate(assets, [], NOW)['posture'] == 'INSUFFICIENT_DATA'


def test_breaks_only_reverse_observations_that_support_active_regime_rules():
    regime = historical(markets())
    breaks = er.what_can_break(None, {'favored': [], 'pressured': []}, regime, [])
    assert not any('DXY reversal lower' in b['text'] for b in breaks)
    assert any('SP500 reversal lower' in b['text'] for b in breaks)
    assert not any('US2Y' in b['text'] or 'BRENT' in b['text'] for b in breaks)


def test_snapshot_summary_rebuilds_deterministic_text_without_writing_or_calling_ai():
    assets = markets()
    regime = historical(assets)
    risk = dict(er.market_view(assets, regime, {}), generated_at=NOW.isoformat(), regime=regime)
    stored = {'source': 'deterministic', 'generated_at': (NOW - timedelta(hours=1)).isoformat(),
              'stale': True, 'basis_hash': 'old', 'output': {'ai_view': 'NASDAQ is favored'}, 'assessments': {}}
    original = deepcopy(stored)
    current = interpretation.for_snapshot(stored, risk, regime, [], NOW)
    assert current['source'] == 'deterministic' and not current['stale']
    assert current['generated_at'] == NOW.isoformat()
    assert 'historical context' in current['output']['ai_view'].lower()
    assert 'not confirming' in current['output']['ai_view']
    assert 'QQQ' in current['output']['market_summary']
    assert stored == original


def test_stale_or_incompatible_ai_is_replaced_but_matching_fresh_ai_is_retained():
    regime = historical(markets())
    risk = dict(er.market_view(markets(), regime, {}), generated_at=NOW.isoformat(), regime=regime)
    digest = interpretation.basis_hash(interpretation.bundle(risk, regime, [], NOW))
    stored = {'source': 'ai', 'stale': False, 'basis_hash': digest, 'output': {'ai_view': 'Useful AI text'},
              'assessments': {'event': {'validated': True}}}
    assert interpretation.for_snapshot(stored, risk, regime, [], NOW) == stored
    for changed in [dict(stored, stale=True), dict(stored, basis_hash='different')]:
        current = interpretation.for_snapshot(changed, risk, regime, [], NOW)
        assert current['source'] == 'deterministic'
        assert current['assessments'] == stored['assessments']


def test_calendar_gets_existing_conditional_scenarios_without_fabricating_consensus():
    from backend.providers.calendars import calendar_row
    row = calendar_row('US personal income & outlays (PCE)', 'US', 'MACRO', NOW + timedelta(days=2),
                       9, 'bea_release_calendar', time_known=True)
    before = deepcopy(row)
    enriched = er.calendar_context([row], NOW)[0]
    assert enriched['scenarios']
    assert all(s['likelihood'] == 'possible' for s in enriched['scenarios'])
    assert any('NASDAQ' in s['implications'] for s in enriched['scenarios'])
    assert enriched.get('consensus') == row.get('consensus')
    assert enriched.get('actual') is None and enriched.get('previous') is None
    assert row == before
