import random
from datetime import UTC, datetime, timedelta

import pytest

from backend import calibration
from backend.calibration import (
    calibration_forecast,
    create_candidate,
    evaluate_challenger,
    fit,
    historical_probability,
    monitor,
    observe_live,
    promote,
    purged_split,
    rollback,
    score_challengers,
    score_with,
    sigmoid,
    validate,
)
from backend.config import rules
from backend.database import RECORDS, Decision, Store
from backend.evaluation import _aggregate

START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    value = Store(f'sqlite:///{tmp_path}/calibration.db')
    value.initialize()
    return value


def seed(store, n=700, *, symbol='BTC', slope=3.0, seed_value=7, components=None, offset=0):
    """Chronological decisions with 4h outcomes whose up-probability rises with score (or a given component)."""
    rng, outcomes, weights = random.Random(seed_value), [], rules()['weights']
    with store.session() as session:
        for i in range(n):
            raw = components(rng) if components else None
            score = score_with(raw, weights) if raw else rng.choice([-80, -60, -40, -20, 20, 40, 60, 80])
            driver = raw['reaction'] if raw else score
            up = int(rng.random() < sigmoid(slope * driver / 100))
            at = START + timedelta(hours=i + offset)
            posture = 'DEFENSIVE' if i % 2 else 'CONSTRUCTIVE'
            key = f'{symbol}-{offset + i}'
            payload = {'regime': {'regimes': [], 'posture': posture}, 'input_snapshot': {'components': raw or {}}}
            session.add(Decision(id=key, symbol=symbol, direction='WAIT', score=score, created_at=at, payload=payload))
            outcome = {'decision_id': key, 'symbol': symbol, 'horizon': '4h', 'score': score, 'decision_at': at.isoformat(),
                       'horizon_end': (at + timedelta(hours=4)).isoformat(), 'raw_return_pct': .5 if up else -.5,
                       'side': 1 if score > 0 else -1 if score < 0 else 0, 'regime_key': posture}
            session.add(RECORDS['signal_outcomes'](id=f'{key}:4h', payload=outcome))
            outcomes.append(outcome)
        session.commit()
    _aggregate(store, outcomes)
    return outcomes


def test_purge_prevents_overlapping_forward_labels():
    rows = [{'timestamp': (START + timedelta(hours=i)).isoformat(), 'score': i, 'target': i % 2} for i in range(100)]
    train, test = purged_split(rows, 14400)
    assert datetime.fromisoformat(train[-1]['timestamp']) + timedelta(hours=4) < datetime.fromisoformat(test[0]['timestamp'])


def test_fit_uses_data_and_rejects_missing_regime_validation():
    rows = [{'score': 80 if i % 2 else -80, 'target': i % 2} for i in range(200)]
    model = fit(rows)
    assert model['weight'] > 0
    report = validate(model, rows, 100)
    assert report['brier_score'] < report['baseline_brier']
    assert report['status'] == 'REJECTED' and report['regime_robustness'] is False


def test_insufficient_candidate_cannot_promote(store):
    candidate = create_candidate(store, 'BTC')
    assert candidate['validation']['status'] == 'INSUFFICIENT_DATA' and candidate['state'] == 'SHADOW'
    with pytest.raises(ValueError):
        promote(store, candidate['id'])


def test_validated_candidate_promotes_and_serves_historical_probability(store):
    seed(store)
    now = START + timedelta(days=60)
    assert historical_probability(store, 'BTC', 60, {'posture': 'DEFENSIVE'}, '4h', now)['status'] == 'NOT ENOUGH DATA'
    candidate = create_candidate(store, 'BTC')
    assert candidate['validation']['status'] == 'PASS', candidate['validation']['checks']
    assert candidate['state'] == 'VALIDATED' and candidate['validation']['champion_id'] == 'BASE_RATE'
    active = promote(store, candidate['id'])
    assert active['candidate_id'] == candidate['id']
    assert store.get('calibration_models', candidate['id'])['state'] == 'CHAMPION'
    assert any(log['action'] == 'calibration_promotion' for log in store.list('audit_logs'))
    upside = historical_probability(store, 'BTC', 60, {'posture': 'DEFENSIVE'}, '4h', now)
    downside = historical_probability(store, 'BTC', -60, 'CONSTRUCTIVE', '4h', now)
    assert upside['p'] > .6 and upside['direction'] == 'UP' and upside['n'] >= 30 and upside['horizon'] == '4h'
    assert downside['p'] > .6 and downside['direction'] == 'DOWN'
    assert upside['calibration']['model_id'] == candidate['id'] and upside['calibration']['quality'] in {'good', 'fair'}
    early = historical_probability(store, 'BTC', 60, None, '4h', START)
    assert early['status'] == 'NOT ENOUGH DATA' and 'not available' in early['reason']
    assert historical_probability(store, 'ETH', 60, None, '4h', now)['status'] == 'NOT ENOUGH DATA'
    forecast = calibration_forecast(store, 'BTC', 60, '4h', now)
    assert forecast['model_id'] == candidate['id'] and 0 < forecast['probability'] < 1
    assert calibration_forecast(store, 'BTC', 60, '4h', START) is None


def test_new_candidate_is_judged_against_the_champion(store):
    seed(store)
    first = create_candidate(store, 'BTC')
    stale = create_candidate(store, 'BTC')
    promote(store, first['id'])
    with pytest.raises(ValueError, match='Champion changed'):
        promote(store, stale['id'])
    rival = create_candidate(store, 'BTC')
    assert rival['validation']['champion_id'] == first['id']
    assert rival['validation']['status'] == 'REJECTED'  # identical data: no significant paired improvement
    assert rival['validation']['checks']['brier_improvement'] is False
    with pytest.raises(ValueError):
        promote(store, rival['id'])


def test_manual_rollback_restores_previous_champion(store):
    seed(store)
    first = create_candidate(store, 'BTC')
    promote(store, first['id'])
    second = dict(create_candidate(store, 'BTC'), state='VALIDATED')
    second['validation'] = dict(second['validation'], status='PASS', champion_id=first['id'])
    store.put('calibration_models', second['id'], second)
    promote(store, second['id'])
    assert store.get('calibration_models', first['id'])['state'] == 'RETIRED'
    restored = rollback(store, 'BTC', '4h')
    assert restored['candidate_id'] == first['id']
    assert store.get('calibration_models', second['id'])['state'] == 'ROLLED_BACK'
    assert store.get('calibration_models', first['id'])['state'] == 'CHAMPION'
    transitions = [t['to'] for t in store.get('calibration_models', second['id'])['transitions']]
    assert transitions[-2:] == ['CHAMPION', 'ROLLED_BACK']


def test_automatic_rollback_when_live_performance_degrades(store):
    seed(store)
    candidate = create_candidate(store, 'BTC')
    active = promote(store, candidate['id'])
    after = datetime.fromisoformat(active['activated_at']) + timedelta(minutes=1)
    limit = rules()['autolearn']['rollback']['min_observations']
    reversed_outcomes = [{'decision_id': f'live{i}', 'symbol': 'BTC', 'horizon': '4h', 'score': 80,
                          'decision_at': after.isoformat(), 'raw_return_pct': -.5} for i in range(limit)]
    assert observe_live(store, reversed_outcomes[:-1]) == limit - 1
    assert monitor(store, after) == []  # below the configured minimum sample
    observe_live(store, reversed_outcomes[-1:])
    actions = monitor(store, after)
    assert actions and set(actions[0]['breaches']) >= {'brier_score', 'false_positive_rate'}
    assert store.get('configuration_versions', 'calibration:BTC:4h')['candidate_id'] is None
    assert store.get('calibration_models', candidate['id'])['state'] == 'ROLLED_BACK'
    log = next(x for x in store.list('audit_logs') if x['action'] == 'calibration_rollback')
    assert log['reason'] == 'automatic' and log['metrics']['observations'] == limit


def test_shadow_challenger_is_stored_never_published_and_needs_oos_win(store, monkeypatch):
    sharp = {'id': 'reaction-only', 'version': '1.0.0', 'weights': {'reaction': 1.0}}
    monkeypatch.setattr(calibration, 'challengers', lambda: [sharp])

    def components(rng):
        return {'event': rng.choice([-100, 100]), 'reaction': rng.choice([-40, 40])}  # live score follows noise

    seed(store, 1000, components=components)
    before = {d.id: (d.direction, d.score) for d in _decisions(store)}
    assert score_challengers(store, START + timedelta(days=90)) == 1000
    assert {d.id: (d.direction, d.score) for d in _decisions(store)} == before  # live decisions untouched
    shadow = store.get('decision_components', 'shadow:reaction-only:BTC-0')
    assert shadow['published'] is False and shadow['kind'] == 'SHADOW_DECISION'
    candidate = evaluate_challenger(store, 'reaction-only', '4h')
    assert candidate['kind'] == 'weights' and candidate['validation']['champion_id'] == 'BASE_RATE'
    assert candidate['validation']['status'] == 'PASS', candidate['validation']['checks']
    assert calibration.champion_weights(store)['source'] == 'rules'
    promote(store, candidate['id'])
    assert calibration.champion_weights(store)['weights'] == {'reaction': 1.0}
    assert calibration.versions(store)['active']['weights:champion']['challenger_id'] == 'reaction-only'


def test_worse_challenger_is_rejected(store, monkeypatch):
    noise = {'id': 'event-only', 'version': '1.0.0', 'weights': {'event': 1.0}}
    monkeypatch.setattr(calibration, 'challengers', lambda: [noise])
    seed(store, 1000, components=lambda rng: {'event': rng.choice([-100, 100]), 'reaction': rng.choice([-80, 80])})
    score_challengers(store, START + timedelta(days=90))
    candidate = evaluate_challenger(store, 'event-only', '4h')
    assert candidate['validation']['status'] == 'REJECTED' and candidate['state'] == 'REJECTED'
    with pytest.raises(ValueError):
        promote(store, candidate['id'])


def test_shadow_category_applies_setup_requirements_and_gates():
    table = rules()['categories']
    lean = next(b for b in table['bands'] if not b['setup'])['long']['category']
    strong = calibration.category(80, raw={'event': 100, 'technical': 100})
    assert strong == next(b for b in table['bands'] if b['threshold'] == 'strong')['long']['category']
    assert calibration.category(80, raw={'event': 100}) == lean  # no aligned technical: downgraded like engines.decide
    assert calibration.category(80, blocked=True, raw={'event': 100, 'technical': 100}) == table['insufficient']['category']
    assert calibration.category(1) == table['neutral']['category']


def test_shadow_decision_treats_downgrades_as_unblocked():
    decision = Decision(id='x', symbol='BTC', direction='BULLISH LEAN / WAIT', score=40, created_at=START, payload={
        'reasons': ['Technical confirmation absent or opposed'], 'blocks': [], 'audit_status': 'PASS',
        'component_detail': {'event': {'value': 100, 'status': 'measured'}, 'technical': {'value': 100, 'status': 'measured'},
                             'macro': {'value': None, 'status': 'missing'}}})
    shadow = calibration.shadow_decision(decision, {'id': 'c', 'version': '1', 'weights': {'event': .5, 'technical': .5}})
    assert shadow['blocked'] is False and shadow['score'] == 100 and shadow['actionable'] is True
    assert shadow['components_missing'] == ['macro']


def _decisions(store):
    with store.session() as session:
        return list(session.query(Decision))


def test_validated_candidate_keeps_forward_shadow_record_without_publication(store):
    seed(store)
    candidate = create_candidate(store, 'BTC')
    after = datetime.fromisoformat(candidate['created_at']) + timedelta(minutes=1)
    fresh = [{'decision_id': f'new{i}', 'symbol': 'BTC', 'horizon': '4h', 'score': 60, 'decision_at': after.isoformat(),
              'raw_return_pct': .5} for i in range(5)]
    result = calibration.cycle(store, fresh, after)
    assert result['shadow_observations'] == 5 and result['live_observations'] == 0
    record = store.get('calibration_models', candidate['id'])
    assert record['state'] == 'VALIDATED' and record['shadow_live']['n'] == 5
    assert store.get('configuration_versions', 'calibration:BTC:4h') is None
    assert historical_probability(store, 'BTC', 60, None, '4h', after)['status'] == 'NOT ENOUGH DATA'
