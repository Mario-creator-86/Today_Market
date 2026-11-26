import copy
from datetime import UTC, datetime, timedelta

import pytest

from backend.database import Decision, Store, Tick
from backend.evaluation import OutcomeTracker, performance


@pytest.fixture
def store():
    value = Store('sqlite://')
    value.initialize()
    return value


T = datetime(2025, 1, 1, tzinfo=UTC)


def tick(store, seconds, price, *, lag=0, quote=True):
    with store.session() as session:
        session.add(Tick(id=f'{seconds}:{lag}:{price}', symbol='BTC', provider='test', price=price,
                         timestamp=T + timedelta(seconds=seconds),
                         ingested_at=T + timedelta(seconds=seconds + lag),
                         payload={'bid': price - .01, 'ask': price + .01} if quote else {}))
        session.commit()


def decision(store, key='d1', seconds=0, audit='PASS', side='LONG', levels=None):
    with store.session() as session:
        session.add(Decision(id=key, symbol='BTC', direction=side, score=50,
                             created_at=T + timedelta(seconds=seconds),
                             payload={'audit_status': audit, 'levels': levels or {}}))
        session.commit()


def test_horizons_idempotence_and_no_lookahead(store):
    tick(store, 0, 100)
    decision(store)
    tick(store, 100, 105)
    tick(store, 200, 90, lag=200)  # Not available by the five minute deadline.
    tick(store, 300, 102)
    tracker = OutcomeTracker(store)
    result = tracker.evaluate(T + timedelta(seconds=310))
    outcome = store.get('signal_outcomes', 'd1:5m')
    assert outcome['return_pct'] == pytest.approx(2)
    assert outcome['mfe_pct'] == pytest.approx(5)
    assert outcome['mae_pct'] == 0
    assert result['new_outcomes'] == 1
    assert tracker.evaluate(T + timedelta(seconds=500))['new_outcomes'] == 0
    assert len(store.list('signal_outcomes')) == 1


@pytest.mark.parametrize('audit,quote,age', [('BLOCK', True, 0), ('PASS', False, 0), ('PASS', True, 60)])
def test_entry_requires_audit_freshness_and_quote(store, audit, quote, age):
    tick(store, -age, 100, quote=quote)
    decision(store, audit=audit)
    OutcomeTracker(store).evaluate(T)
    assert store.list('paper_trades') == []


def test_gapped_stop_fills_at_observed_bid_and_duplicate_prevention(store):
    tick(store, 0, 100)
    decision(store, levels={'support': 95, 'resistance': 110})
    tick(store, 1, 100)
    decision(store, key='d2', seconds=1)
    tick(store, 10, 90)
    tracker = OutcomeTracker(store)
    tracker.evaluate(T + timedelta(seconds=11))
    tracker.evaluate(T + timedelta(seconds=11))
    trades = store.list('paper_trades')
    assert len(trades) == 1
    assert trades[0]['exit_price'] < 90
    assert trades[0]['exit_reason'] == 'STOP'
    assert trades[0]['return_pct'] < -10
    result = performance(store)
    assert result['metrics']['paper']['profit_factor'] == 0
    assert result['metrics']['paper']['max_drawdown_return_points'] > 10


def test_missing_exit_quote_does_not_invent_fill(store):
    tick(store, 0, 100)
    decision(store, levels={'support': 95})
    tick(store, 10, 90, quote=False)
    OutcomeTracker(store).evaluate(T + timedelta(hours=5))
    assert store.get('paper_trades', 'd1')['status'] == 'OPEN'
    assert performance(store)['metrics']['paper']['mean_return_pct'] is None
    assert store.get('calibration_models', 'shadow-outcomes-v1')['promoted'] is False


def test_short_returns_and_stale_horizon(store):
    tick(store, 0, 100)
    decision(store, side='SHORT')
    tick(store, 300, 95)
    OutcomeTracker(store).evaluate(T + timedelta(hours=1))
    assert store.get('signal_outcomes', 'd1:5m')['return_pct'] == pytest.approx(5)
    assert store.get('signal_outcomes', 'd1:15m') is None
    assert performance(store)['probability'] is None


def test_frozen_out_of_sample_forecast_scoring_never_promotes(store):
    tick(store, 0, 100)
    decision(store)
    with store.session() as session:
        row = session.get(Decision, 'd1')
        row.payload = dict(row.payload, calibration_forecast={
            'probability': .8, 'horizon': '5m', 'model_id': 'frozen-v1',
            'trained_through': (T - timedelta(days=2)).isoformat(),
            'generated_at': (T - timedelta(seconds=1)).isoformat()})
        session.commit()
    tick(store, 300, 105)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=301))
    record = store.get('calibration_models', 'shadow-outcomes-v1')
    assert record['brier_score'] == pytest.approx(.04)
    assert record['calibration_error'] == pytest.approx(.2)
    assert record['minimum_met'] is False
    assert record['promoted'] is False
    assert record['probability'] is None


def raw_decision(store, key, *, score, direction='WAIT', seconds=0, symbol='BTC', **payload):
    with store.session() as session:
        session.add(Decision(id=key, symbol=symbol, direction=direction, score=score,
                             created_at=T + timedelta(seconds=seconds), payload=payload))
        session.commit()


def test_wait_and_lean_decisions_get_flagged_counterfactual_outcomes(store):
    tick(store, 0, 100)
    raw_decision(store, 'wait', score=-20, direction='BEARISH LEAN / WAIT', audit_status='BLOCK')
    raw_decision(store, 'flat', score=0)
    tick(store, 150, 97)
    tick(store, 300, 98)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=301))
    lean = store.get('signal_outcomes', 'wait:5m')
    assert lean['counterfactual'] is True and lean['actionable'] is False and lean['side'] == -1
    assert lean['raw_return_pct'] == pytest.approx(-2)
    assert lean['return_pct'] == pytest.approx(2)
    assert lean['mfe_pct'] == pytest.approx(3) and lean['mae_pct'] == 0
    flat = store.get('signal_outcomes', 'flat:5m')
    assert flat['side'] == 0 and flat['return_pct'] is None and flat['raw_return_pct'] == pytest.approx(-2)
    assert store.list('paper_trades') == []  # counterfactuals never trade


def test_confirmation_and_invalidation_flags_follow_decision_levels(store):
    tick(store, 0, 100)
    raw_decision(store, 'd1', score=45, direction='LONG SETUP', category='LONG SETUP',
                 confirmation_level=103, invalidation_level=97)
    raw_decision(store, 'd2', score=45, direction='LONG SETUP', seconds=1)
    tick(store, 60, 96)
    tick(store, 120, 104)
    tick(store, 300, 101)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=301))
    outcome = store.get('signal_outcomes', 'd1:5m')
    assert outcome['confirmation_occurred'] is True and outcome['invalidation_occurred'] is True
    assert outcome['first_trigger'] == 'INVALIDATION'
    assert outcome['invalidation_at'] == (T + timedelta(seconds=60)).isoformat()
    missing = store.get('signal_outcomes', 'd2:5m')
    assert missing['confirmation_occurred'] is None and missing['confirmation_level'] is None  # never invented


def test_session_horizons_use_the_asset_calendar(store):
    tick(store, 0, 100)
    raw_decision(store, 'd1', score=40, direction='LONG')
    day = 86400
    tick(store, day - 5, 110)
    tick(store, 2 * day - 5, 90)
    OutcomeTracker(store).evaluate(T + timedelta(days=2, seconds=1))
    close = store.get('signal_outcomes', 'd1:close')
    following = store.get('signal_outcomes', 'd1:next_session')
    assert close['horizon_end'] == (T + timedelta(days=1)).isoformat()
    assert close['return_pct'] == pytest.approx(10)
    assert following['horizon_end'] == (T + timedelta(days=2)).isoformat()
    assert following['return_pct'] == pytest.approx(-10)


def test_incremental_cursor_skips_finished_decisions(store, monkeypatch):
    import backend.evaluation as evaluation
    tick(store, 0, 100)
    raw_decision(store, 'd1', score=40, direction='LONG')
    tick(store, 300, 101)
    calls = []
    original = evaluation.measure

    def counted(*args, **kwargs):
        calls.append(args[4])
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluation, 'measure', counted)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=301))
    assert len(calls) == 1
    OutcomeTracker(store).evaluate(T + timedelta(seconds=302))
    assert len(calls) == 1  # 5m already done and later horizons not due: nothing is measured again
    cursor = store.get('configuration_versions', evaluation.CURSOR_KEY)
    assert cursor['outcomes']['BTC|5m'][1] == 'd1'


def test_stub_ticks_never_reach_outcomes(store):
    with store.session() as session:
        for seconds in (0, 300):
            session.add(Tick(id=f'stub{seconds}', symbol='BTC', provider='massive-stub', price=100 + seconds,
                             timestamp=T + timedelta(seconds=seconds), ingested_at=T + timedelta(seconds=seconds),
                             payload={'bid': 99, 'ask': 101, 'stub': True}))
        session.commit()
    raw_decision(store, 'd1', score=40, direction='LONG', audit_status='PASS')
    OutcomeTracker(store).evaluate(T + timedelta(seconds=301))
    assert store.list('signal_outcomes') == [] and store.list('paper_trades') == []


def test_breakout_long_gets_reachable_atr_target_and_full_execution_record(store):
    tick(store, 0, 100)
    decision(store, levels={'support': 95, 'resistance': 99, 'atr': 2})
    tick(store, 30, 103)
    tick(store, 60, 98)
    tick(store, 90, 105)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=120))
    trade = store.get('paper_trades', 'd1')
    assert trade['target_basis'] == 'atr_multiple'
    assert trade['target'] == pytest.approx(trade['entry_price'] + 4)
    assert trade['status'] == 'CLOSED' and trade['exit_reason'] == 'TARGET'
    assert trade['entry_ask'] == pytest.approx(100.01) and trade['entry_bid'] == pytest.approx(99.99)
    assert trade['entry_spread_bps'] == pytest.approx(2, rel=1e-3)
    assert trade['exit_bid'] == pytest.approx(104.99) and trade['exit_spread_bps'] > 0
    assert trade['mae_pct'] < -1.9 and trade['mfe_pct'] > 4.9
    assert trade['fees_pct'] == pytest.approx(0.2)  # crypto: 10 bps per side
    assert trade['return_pct'] == pytest.approx(trade['gross_return_pct'] - 0.2)
    assert trade['pnl'] == pytest.approx(trade['sizing']['notional'] * trade['return_pct'] / 100)


def test_r_multiple_target_when_no_atr_and_short_side(store):
    tick(store, 0, 100)
    decision(store, side='SHORT', levels={'support': 101, 'resistance': 104})
    OutcomeTracker(store).evaluate(T)
    trade = store.get('paper_trades', 'd1')
    assert trade['stop'] == 104 and trade['target_basis'] == 'r_multiple'
    assert trade['target'] == pytest.approx(trade['entry_price'] - 2 * (104 - trade['entry_price']))


def test_fixed_risk_sizing_option(store, monkeypatch):
    import backend.evaluation as evaluation
    config = copy.deepcopy(evaluation.config())
    config['paper']['sizing'].update(mode='fixed_risk', risk_pct=1, max_notional_pct=100, initial_equity=10000)
    monkeypatch.setattr(evaluation, 'config', lambda: config)
    tick(store, 0, 100)
    decision(store, levels={'support': 90, 'resistance': 120})
    OutcomeTracker(store).evaluate(T)
    trade = store.get('paper_trades', 'd1')
    distance = (trade['entry_price'] - 90) / trade['entry_price']
    assert trade['sizing']['mode'] == 'fixed_risk'
    assert trade['sizing']['notional'] == pytest.approx(100 / distance)


def test_performance_breakdowns_and_metrics(store):
    tick(store, 0, 100)
    snapshot = {'quote': {'price': 100}, 'events': [
        {'event_type': 'INFLATION_HOT', 'evidence': [{'source_id': 'bls'}, {'source_id': 'fed'}]}]}
    raw_decision(store, 'a', score=65, direction='STRONG LONG', audit_status='PASS', evidence_confidence=92,
                 regime={'regimes': ['RISK_ON'], 'posture': 'CONSTRUCTIVE'}, input_snapshot=snapshot,
                 levels={'support': 90, 'resistance': 99})
    raw_decision(store, 'b', score=-20, seconds=1)
    tick(store, 100, 104)
    tick(store, 14400, 103)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=14401))
    report = performance(store)
    assert report['breakdown_horizon'] == '4h'
    groups = report['breakdowns']
    assert groups['asset']['BTC']['observations'] == 2
    assert groups['event_type']['INFLATION_HOT']['observations'] == 1
    assert groups['event_type']['NONE']['counterfactual'] == 1
    assert groups['regime']['RISK_ON']['hit_rate'] == 1
    assert set(groups['score_bucket']) == {'STRONG+', 'LEAN-'}
    assert groups['confidence_bucket']['90-100']['observations'] == 1
    assert groups['source_combination']['bls+fed']['median_return_pct'] == pytest.approx(3)
    assert groups['setup']['BREAKOUT_ABOVE_RESISTANCE']['mean_mfe_pct'] == pytest.approx(4)
    lean = groups['score_bucket']['LEAN-']
    # Counterfactual failures are not false positives of published signals; they are reported separately.
    assert lean['false_positive_rate'] is None and lean['counterfactual_failure_rate'] == 1
    assert lean['max_drawdown_return_points'] == pytest.approx(3)
    assert groups['score_bucket']['STRONG+']['false_positive_rate'] == 0
    assert performance(store, population='actionable')['breakdowns']['asset']['BTC']['observations'] == 1
    assert report['metrics']['4h']['median_return_pct'] == pytest.approx(0)
    with pytest.raises(ValueError):
        performance(store, population='everything')
    with pytest.raises(ValueError, match='Unknown group_by'):
        performance(store, group_by=['asset', 'moon_phase'])
    assert list(performance(store, group_by=['asset'])['breakdowns']) == ['asset']


def test_side_of_follows_the_category_table():
    from backend.evaluation import side_of
    assert side_of('LONG SETUP') == 1 and side_of('STRONG SHORT SETUP') == -1
    assert side_of('STRONG LONG') == 1 and side_of('SHORT') == -1  # legacy directions of setup bands
    assert side_of('BULLISH LEAN / WAIT') == 0 and side_of('WAIT / DATA INSUFFICIENT') == 0 and side_of(None) == 0


def test_outcome_starts_when_the_decision_was_known_not_at_the_cycle_stamp(store):
    """A cycle stamps created_at at its start; a decision that re-read a later quote is measured from that quote."""
    tick(store, 0, 100)
    tick(store, 120, 110)  # the quote the published decision actually used, after the AI roles ran
    raw_decision(store, 'late', score=40, direction='LONG', input_snapshot={'quote': {
        'price': 110, 'timestamp': (T + timedelta(seconds=120)).isoformat()}})
    tick(store, 300, 111)
    tick(store, 420, 112)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=421))
    outcome = store.get('signal_outcomes', 'late:5m')
    assert outcome['decision_at'] == (T + timedelta(seconds=120)).isoformat()
    assert outcome['entry_price'] == 110 and outcome['return_pct'] == pytest.approx(200 / 110)
    assert outcome['horizon_end'] == (T + timedelta(seconds=420)).isoformat()


def test_comparables_respects_limit(store):
    from backend.evaluation import comparables
    for i in range(5):
        raw_decision(store, f'd{i}', score=40, direction='LONG', seconds=i)
    raw_decision(store, 'w', score=-40, direction='SHORT', seconds=10)
    items = comparables(store, 'BTC', side=1, limit=2)['items']
    assert [i['decision_id'] for i in items] == ['d4', 'd3']
    with pytest.raises(ValueError):
        comparables(store, 'BTC', limit=0)


def test_comparables_lists_past_outcomes(store):
    from backend.evaluation import comparables
    tick(store, 0, 100)
    raw_decision(store, 'd1', score=40, direction='LONG')
    tick(store, 300, 102)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=301))
    result = comparables(store, 'BTC', side=1)
    assert result['items'][0]['outcomes']['5m']['return_pct'] == pytest.approx(2)
    assert result['by_horizon']['5m']['observations'] == 1
    assert comparables(store, 'BTC', side=-1)['items'] == []


def test_legacy_outcomes_are_normalized_from_their_decisions(store):
    raw_decision(store, 'old', score=-35, direction='SHORT')
    store.put('signal_outcomes', 'old:4h', {'decision_id': 'old', 'symbol': 'BTC', 'horizon': '4h', 'return_pct': 1.5,
                                            'mfe_pct': 2, 'mae_pct': -1, 'decision_at': T.isoformat()})
    report = performance(store)
    bucket = report['breakdowns']['score_bucket']['SETUP-']
    assert bucket['observations'] == 1 and bucket['actionable'] == 1 and bucket['hit_rate'] == 1


def test_evaluate_pending_runs_shadow_challengers(store):
    from backend.evaluation import evaluate_pending
    tick(store, 0, 100)
    raw_decision(store, 'd1', score=40, direction='LONG', input_snapshot={'components': {'event': 100, 'technical': 100}})
    result = evaluate_pending(store, T + timedelta(seconds=1))
    assert result['autolearn']['shadow_decisions'] == 2
    shadow = store.get('decision_components', 'shadow:reaction-weighted:d1')
    assert shadow['published'] is False and shadow['score'] == pytest.approx(45)
    assert evaluate_pending(store, T + timedelta(seconds=2))['autolearn']['shadow_decisions'] == 0


def test_performance_counts_directional_observations_apart_from_recorded_decisions(store):
    """Regression (UI audit PERF-1): 676 recorded WAIT outcomes were reported as 676 'observations' next to
    'Not enough data' at every horizon, because the count included outcomes without a side."""
    tick(store, 0, 100)
    raw_decision(store, 'flat', score=0)
    tick(store, 300, 98)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=301))
    report = performance(store)
    assert report['observations'] == 0 and report['decisions_recorded'] == 1
    assert report['status'] == 'INSUFFICIENT_DATA'  # nothing directional was measured
    assert all(report['metrics'][h]['observations'] == 0 for h in report['horizons'])
    raw_decision(store, 'lean', score=-20, direction='BEARISH LEAN / WAIT', seconds=1)
    tick(store, 302, 97)
    OutcomeTracker(store).evaluate(T + timedelta(seconds=302))
    report = performance(store)
    assert report['observations'] == 1 and report['decisions_recorded'] == 2 and report['status'] == 'UNCALIBRATED'


def test_performance_names_its_default_horizon_and_a_readable_paper_basis(store):
    """Regression (UI audit PERF-3): the default horizon echoed the requested one, and the paper basis printed the raw
    sizing id and an unformatted number."""
    from backend.evaluation import config
    report = performance(store, horizon='5m')
    assert report['breakdown_horizon'] == '5m' and report['default_horizon'] == config()['report_horizon']
    basis = report['metrics']['paper']['basis']
    initial = config()['paper']['sizing']['initial_equity']
    assert '_' not in basis and f'{initial:,.0f}' in basis
    assert basis.startswith('Net of configured fees and slippage; ')
