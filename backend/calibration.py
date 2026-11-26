"""Champion/challenger autolearn: chronological, purged calibration and shadow weight challengers.

Deterministic statistics only. Candidates never self-promote: promotion needs a champion-relative,
statistically meaningful out-of-sample improvement, and a live monitor rolls a champion back automatically.
"""
import math
from datetime import datetime, timedelta
from statistics import mean, stdev
from typing import Any

from sqlalchemy import or_, select

from backend import evaluation as ev
from backend.database import RECORDS, Decision, Store, uid, utcnow
from backend.engines import aware
from backend.sessions import ruleset

WEIGHTS_KEY = 'weights:champion'
SHADOW_CURSOR_KEY = 'autolearn:shadow-cursor'
BASE_RATE = 'BASE_RATE'
CHUNK = 500


def settings() -> dict:
    return ruleset()['autolearn']['calibration']


def sigmoid(value: float) -> float:
    value = max(-30, min(30, value))
    return 1 / (1 + math.exp(-value))


def predict(model: dict, score: float) -> float:
    """P(price up over the horizon | score) from a fitted logistic calibration."""
    return sigmoid(model['weight'] * score / 100 + model['bias'])


def fit(rows: list[dict]) -> dict:
    if not rows or any(not math.isfinite(r['score']) or r['target'] not in {0, 1} for r in rows):
        raise ValueError('Invalid calibration observations')
    cfg = settings()
    weight = bias = 0.0
    for _ in range(cfg['iterations']):
        errors = [(sigmoid(weight * r['score'] / 100 + bias) - r['target'], r['score'] / 100) for r in rows]
        weight -= cfg['learning_rate'] * (sum(error * x for error, x in errors) / len(rows) + cfg['l2'] * weight)
        bias -= cfg['learning_rate'] * sum(error for error, _ in errors) / len(rows)
    return {'weight': weight, 'bias': bias, 'baseline': mean(r['target'] for r in rows)}


def _setup_supported(score: float, raw: dict[str, float], need: dict) -> bool:
    """rules.json setup_requirements (as engines.decide): aligned components and one strong aligned driver."""
    aligned = all(name in raw and raw[name] * score > 0 for name in need['aligned'])
    anchor = need['min_abs_any']
    return aligned and any(name in raw and raw[name] * score > 0 and abs(raw[name]) >= anchor['value']
                           for name in anchor['components'])


def category(score: float, blocked: bool = False, raw: dict[str, float] | None = None) -> str:
    """Category from the rules.json table, mirroring engines.decide without its per-call config copy.

    Gated inputs give the insufficient label; a SETUP band without its setup requirements (checked on the
    measured raw components) is downgraded to the lean band exactly as live scoring does.
    """
    config = ruleset()
    table, thresholds = config['categories'], config['thresholds']
    if blocked:
        return table['insufficient']['category']
    bands = sorted(table['bands'], key=lambda b: -thresholds[b['threshold']])
    band = next((b for b in bands if abs(score) >= thresholds[b['threshold']]), None)
    if band is None:
        return table['neutral']['category']
    if band.get('setup') and not _setup_supported(score, raw or {}, table['setup_requirements']):
        band = next(b for b in table['bands'] if not b.get('setup'))
    return band['long' if score > 0 else 'short']['category']


def score_with(raw: dict[str, float], weights: dict[str, float]) -> float:
    return round(sum(raw.get(name, 0) * weight for name, weight in weights.items()), 2)


def raw_components(payload: dict) -> tuple[dict[str, float], list[str]]:
    """Unweighted component values. Missing components contribute 0 and are listed, never shown as measured."""
    detail = payload.get('component_detail')
    if isinstance(detail, dict) and detail:
        values = {k: float(v['value']) for k, v in detail.items()
                  if isinstance(v, dict) and v.get('status') == 'measured' and ev._number(v.get('value'))}
        return values, sorted(set(detail) - set(values))
    snapshot = (payload.get('input_snapshot') or {}).get('components') or {}
    values = {k: float(v) for k, v in snapshot.items() if ev._number(v)}
    return values, sorted(set(ruleset()['weights']) - set(values))


def _ece(probabilities: list[float], targets: list[int]) -> float:
    bins = settings()['calibration_bins']
    cells: list[list[float]] = [[] for _ in range(bins)]
    for p, y in zip(probabilities, targets, strict=True):
        cells[min(int(p * bins), bins - 1)].append(p - y)
    return sum(abs(sum(cell)) for cell in cells) / len(targets)


def _calls(probabilities: list[float], rows: list[dict]) -> list[tuple[int, dict]]:
    cfg = settings()
    return [(1 if p >= cfg['call_up'] else -1, r) for p, r in zip(probabilities, rows, strict=True)
            if p >= cfg['call_up'] or p <= cfg['call_down']]


def _drawdown(values: list[float]) -> float:
    cumulative = peak = worst = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


def _call_stats(calls: list[tuple[int, dict]]) -> dict:
    cost = settings()['cost_bps'] / 100
    measured = [(side, r) for side, r in calls if ev._number(r.get('raw_return_pct'))]
    returns = [side * r['raw_return_pct'] - cost for side, r in measured]
    return {'calls': len(measured), 'returns': returns,
            'false_positive_rate': sum(v <= 0 for v in returns) / len(returns) if returns else None,
            'hit_rate': sum(v > 0 for v in returns) / len(returns) if returns else None,
            'drawdown': _drawdown(returns)}


def compare(candidate: list[float], champion: list[float], rows: list[dict], minimum: int,
            relative_calibration: bool = True) -> dict:
    """Paired OOS comparison of candidate vs champion probabilities on identical rows.

    A constant base-rate champion is calibrated by construction, so only a model champion sets a relative
    calibration bar; the absolute maximum calibration error always applies.
    """
    cfg = settings()
    if len(rows) < max(minimum, 2):
        return {'status': 'INSUFFICIENT_DATA', 'observations': len(rows)}
    targets = [r['target'] for r in rows]
    losses = [(p - y) ** 2 for p, y in zip(candidate, targets, strict=True)]
    reference = [(p - y) ** 2 for p, y in zip(champion, targets, strict=True)]
    improvements = [b - a for a, b in zip(losses, reference, strict=True)]
    lower = mean(improvements) - cfg['confidence_z'] * stdev(improvements) / math.sqrt(len(rows))
    error, champion_error = _ece(candidate, targets), _ece(champion, targets)
    mine = _call_stats(_calls(candidate, rows))
    theirs = _call_stats(_calls(champion, rows))
    benchmark = 'champion calls'
    if not theirs['calls']:
        # A constant base-rate champion makes no calls; compare against the live score-sign signals instead.
        theirs = _call_stats([(r['side'], r) for r in rows if r.get('side')])
        benchmark = 'score-sign signals'
    by_regime: dict[str, list[float]] = {}
    for row, value in zip(rows, improvements, strict=True):
        by_regime.setdefault(str(row.get('regime') or ruleset()['autolearn']['comparable_regime_fallback']), []).append(value)
    eligible = {k: mean(v) for k, v in by_regime.items() if len(v) >= cfg['min_regime_observations']}
    robust = len(eligible) >= cfg['min_regimes'] and all(v >= 0 for v in eligible.values())
    fpr_ok = mine['false_positive_rate'] is not None and (
        theirs['false_positive_rate'] is None or mine['false_positive_rate'] <= theirs['false_positive_rate'] + cfg['fpr_tolerance'])
    risk_ok = mine['calls'] > 0 and mine['drawdown'] <= theirs['drawdown'] * (1 + cfg['drawdown_tolerance'])
    calibrated = error <= cfg['max_calibration_error'] and (
        not relative_calibration or error <= champion_error + cfg['calibration_error_tolerance'])
    passed = lower > 0 and calibrated and fpr_ok and robust and risk_ok
    return {'status': 'PASS' if passed else 'REJECTED', 'observations': len(rows),
            'brier_score': mean(losses), 'champion_brier': mean(reference), 'baseline_brier': mean(reference),
            'improvement_lower_95': lower, 'calibration_error': error, 'champion_calibration_error': champion_error,
            'false_positive_rate': mine['false_positive_rate'], 'champion_false_positive_rate': theirs['false_positive_rate'],
            'baseline_false_positive_rate': theirs['false_positive_rate'], 'hit_rate': mine['hit_rate'],
            'champion_hit_rate': theirs['hit_rate'], 'regime_robustness': robust,
            'regime_improvement': {k: round(v, 6) for k, v in sorted(eligible.items())},
            'regime_observations': {k: len(v) for k, v in sorted(by_regime.items())},
            'risk_validation': risk_ok, 'cost_adjusted_trades': mine['calls'],
            'candidate_drawdown_points': mine['drawdown'] if mine['calls'] else None,
            'champion_drawdown_points': theirs['drawdown'] if theirs['calls'] else None, 'benchmark': benchmark,
            'checks': {'brier_improvement': lower > 0, 'calibration': calibrated, 'false_positive_rate': fpr_ok,
                       'regime_robustness': robust, 'drawdown': risk_ok},
            'limitations': 'Statistical diagnostic; overlapping horizons make observations dependent'}


def validate(model: dict, rows: list[dict], minimum: int, champion: dict | None = None) -> dict:
    """Candidate vs the active champion (or the training base rate when no champion exists)."""
    candidate = [predict(model, r['score']) for r in rows]
    reference = [predict(champion, r['score']) if champion else model['baseline'] for r in rows]
    return compare(candidate, reference, rows, minimum, relative_calibration=champion is not None)


def purged_split(rows: list[dict], horizon_seconds: int) -> tuple[list[dict], list[dict]]:
    """Chronological split; training labels must be known before the validation window opens."""
    ordered = sorted(rows, key=lambda r: aware(r['timestamp']))
    split = int(len(ordered) * settings()['train_fraction'])
    if split == 0 or split >= len(ordered):
        return [], []
    cutoff = aware(ordered[split]['timestamp'])
    training = [r for r in ordered[:split] if _label_time(r, horizon_seconds) < cutoff]
    return training, ordered[split:]


def _label_time(row: dict, horizon_seconds: int) -> datetime:
    return aware(row['end']) if row.get('end') else aware(row['timestamp']) + timedelta(seconds=horizon_seconds)


def _horizon_seconds(horizon: str) -> int:
    fixed = ruleset()['autolearn']['horizons']
    return fixed.get(horizon, max(fixed.values()))


def _outcomes(store: Store, decision_ids: list[str], horizon: str) -> dict[str, dict]:
    table = RECORDS['signal_outcomes']
    found: dict[str, dict] = {}
    with store.session() as session:
        for start in range(0, len(decision_ids), CHUNK):
            keys = [f'{d}:{horizon}' for d in decision_ids[start:start + CHUNK]]
            rows: list[Any] = list(session.scalars(select(table).where(table.id.in_(keys))))
            found.update({r.payload['decision_id']: r.payload for r in rows})
    return found


def _row(decision: Decision, outcome: dict, score: float) -> dict | None:
    raw = outcome.get('raw_return_pct')
    if raw is None:  # legacy outcome: only actionable decisions, signed by the published side
        side = ev.side_of(decision.direction)
        raw = outcome['return_pct'] * side if side else None
    if not ev._number(raw):
        return None
    at = aware(outcome.get('decision_at') or decision.created_at)
    return {'score': score, 'target': int(raw > 0), 'raw_return_pct': raw, 'timestamp': at.isoformat(),
            'end': outcome.get('horizon_end'), 'side': 1 if score > 0 else -1 if score < 0 else 0,
            'regime': outcome.get('regime_key') or ev.regime_key(decision.payload or {}), 'decision_id': decision.id}


def outcome_rows(store: Store, symbol: str, horizon: str) -> list[dict]:
    with store.session() as session:
        decisions = {d.id: d for d in session.scalars(select(Decision).where(Decision.symbol == symbol))}
    outcomes = _outcomes(store, sorted(decisions), horizon)
    rows = [_row(decisions[k], o, decisions[k].score) for k, o in outcomes.items()]
    return sorted((r for r in rows if r), key=lambda r: r['timestamp'])


def _pointer_key(candidate: dict) -> str:
    return WEIGHTS_KEY if candidate.get('kind') == 'weights' else f"calibration:{candidate['symbol']}:{candidate['horizon']}"


def _transition(candidate: dict, state: str, reason: str, **extra: Any) -> dict:
    history = list(candidate.get('transitions', []))
    history.append(dict(extra, **{'from': candidate.get('state'), 'to': state, 'at': utcnow().isoformat(), 'reason': reason}))
    return dict(candidate, state=state, transitions=history)


def _initial_state(validation: dict) -> str:
    return {'PASS': 'VALIDATED', 'REJECTED': 'REJECTED'}.get(validation.get('status', ''), 'SHADOW')


def _champion(store: Store, key: str) -> tuple[str, dict | None]:
    pointer = store.get('configuration_versions', key) or {}
    candidate = store.get('calibration_models', pointer['candidate_id']) if pointer.get('candidate_id') else None
    return (pointer['candidate_id'], candidate) if candidate else (BASE_RATE, None)


def _save_candidate(store: Store, record: dict) -> dict:
    config = ruleset()
    record.update(created_at=utcnow().isoformat(), rules_version=config['version'], content_hash=config['content_hash'],
                  autolearn_version=config.get('autolearn_version'))
    record = _transition(dict(record, state=None), _initial_state(record['validation']), 'created')
    store.put('calibration_models', record['id'], record)
    return record


def create_candidate(store: Store, symbol: str, horizon: str = '4h') -> dict:
    if horizon not in ev.horizons() or symbol not in {a[0] for a in ruleset()['assets']}:
        raise ValueError('Unsupported calibration asset or horizon')
    rows = outcome_rows(store, symbol, horizon)
    minimum = ruleset()['thresholds']['minimum_calibration']
    train, test = purged_split(rows, _horizon_seconds(horizon))
    champion_id, champion = _champion(store, f'calibration:{symbol}:{horizon}')
    model = fit(train) if len(train) >= minimum else None
    validation = validate(model, test, minimum, champion['model'] if champion else None) if model else \
        {'status': 'INSUFFICIENT_DATA', 'observations': len(test)}
    trained = max((_label_time(r, _horizon_seconds(horizon)) for r in train), default=None)
    return _save_candidate(store, {
        'id': uid(), 'kind': 'calibration', 'symbol': symbol, 'horizon': horizon, 'model': model,
        'validation': dict(validation, champion_id=champion_id), 'training_observations': len(train),
        'trained_through': trained.isoformat() if trained else None,
        'validation_from': test[0]['timestamp'] if test else None, 'data_ids': [r['decision_id'] for r in rows]})


def promote(store: Store, candidate_id: str) -> dict:
    candidate = store.get('calibration_models', candidate_id)
    if not candidate or candidate.get('validation', {}).get('status') != 'PASS':
        raise ValueError('Candidate lacks successful out-of-sample validation')
    if candidate.get('state') not in {None, 'VALIDATED'}:
        raise ValueError(f"Candidate in state {candidate.get('state')} cannot be promoted")
    key = _pointer_key(candidate)
    previous = store.get('configuration_versions', key)
    current = (previous or {}).get('candidate_id') or BASE_RATE
    if candidate['validation'].get('champion_id', BASE_RATE) != current:
        raise ValueError('Champion changed since validation; create a new candidate')
    now = utcnow().isoformat()
    if previous and previous.get('candidate_id'):
        retired = store.get('calibration_models', previous['candidate_id'])
        if retired:
            store.put('calibration_models', previous['candidate_id'], _transition(retired, 'RETIRED', f'replaced by {candidate_id}'))
    store.put('calibration_models', candidate_id, dict(_transition(candidate, 'CHAMPION', 'promoted'), activated_at=now, live={}))
    # Live scoring weights are untouched; the champion is a separately labelled, versioned activation.
    active = {'candidate_id': candidate_id, 'kind': candidate.get('kind', 'calibration'), 'previous': previous,
              'activated_at': now, 'symbol': candidate.get('symbol'), 'horizon': candidate.get('horizon'),
              'weights': candidate.get('weights'), 'challenger_id': candidate.get('challenger_id')}
    store.put('configuration_versions', key, active)
    store.put('audit_logs', uid(), {'action': f"{active['kind']}_promotion", 'candidate_id': candidate_id,
                                    'previous': previous, 'timestamp': now})
    return active


def rollback(store: Store, symbol: str | None, horizon: str | None, reason: str = 'manual',
             metrics: dict | None = None, key: str | None = None) -> dict:
    key = key or f'calibration:{symbol}:{horizon}'
    active = store.get('configuration_versions', key) or {}
    if active.get('candidate_id'):
        current = store.get('calibration_models', active['candidate_id'])
        if current:
            store.put('calibration_models', active['candidate_id'], _transition(current, 'ROLLED_BACK', reason, metrics=metrics))
    previous = active.get('previous') or {'candidate_id': None}
    if previous.get('candidate_id'):
        restored = store.get('calibration_models', previous['candidate_id'])
        if restored:
            store.put('calibration_models', previous['candidate_id'],
                      dict(_transition(restored, 'CHAMPION', f'restored by {reason} rollback'), live={},
                           activated_at=utcnow().isoformat()))
        previous = dict(previous, activated_at=utcnow().isoformat())
    store.put('configuration_versions', key, previous)
    store.put('audit_logs', uid(), {'action': f"{active.get('kind', 'calibration')}_rollback", 'key': key, 'symbol': symbol,
                                    'horizon': horizon, 'reason': reason, 'metrics': metrics,
                                    'rolled_back': active.get('candidate_id'), 'timestamp': utcnow().isoformat()})
    return previous


def active(store: Store, symbol: str, horizon: str) -> dict | None:
    return _champion(store, f'calibration:{symbol}:{horizon}')[1]


def _quality(error: float | None) -> str:
    cfg = settings()
    if error is None:
        return 'unknown'
    return 'good' if error <= cfg['quality_good'] else 'fair' if error <= cfg['quality_fair'] else 'poor'


# What historical_probability's n counts (it differs from the asset comparables list, which counts signed outcomes):
# the regime's own count, else the any-regime count of the same score bucket when that regime has none.
N_BASIS = 'past decisions for this asset and horizon in the same score bucket and regime'
N_BASIS_ANY_REGIME = 'past decisions for this asset and horizon in the same score bucket (any regime)'


def historical_probability(store: Store, symbol: str, score: float, regime: Any, horizon: str,
                           now: datetime) -> dict:
    """Directional probability from the promoted champion, with comparable n; otherwise NOT ENOUGH DATA."""
    if isinstance(regime, dict):
        posture = ev.regime_key({'regime': regime})
    else:
        posture = regime if isinstance(regime, str) and regime else ruleset()['autolearn']['comparable_regime_fallback']
    counts = (store.get('calibration_models', ev.COMPARABLES_KEY) or {}).get('counts', {})
    bucket = ev.score_bucket(score)
    comparable, basis = counts.get(f'{symbol}|{horizon}|{bucket}|{posture}'), N_BASIS
    if not comparable and counts.get(f'{symbol}|{horizon}|{bucket}|*'):
        comparable, basis = counts[f'{symbol}|{horizon}|{bucket}|*'], N_BASIS_ANY_REGIME
    n = (comparable or {}).get('n', 0)
    champion = active(store, symbol, horizon)
    minimum = ruleset()['autolearn']['min_comparable']
    reason = None
    if champion is None or not champion.get('model'):
        reason = 'No promoted calibration for this asset and horizon'
    elif not champion.get('trained_through') or aware(champion['trained_through']) >= aware(now):
        reason = 'Champion was trained on information not available at this time'
    elif n < minimum:
        reason = f'Comparable observations below minimum ({n} < {minimum})'
    if reason:
        return {'status': 'NOT ENOUGH DATA', 'n': n, 'n_basis': basis, 'horizon': horizon, 'minimum': minimum,
                'reason': reason}
    assert champion is not None
    up = predict(champion['model'], score)
    validation = champion.get('validation', {})
    return {'p': round(up if score >= 0 else 1 - up, 4), 'direction': 'UP' if score >= 0 else 'DOWN', 'n': n,
            'n_basis': basis, 'horizon': horizon, 'calibration': {
                'model_id': champion['id'], 'quality': _quality(validation.get('calibration_error')),
                'calibration_error': validation.get('calibration_error'), 'brier_score': validation.get('brier_score'),
                'validation_observations': validation.get('observations'), 'trained_through': champion['trained_through'],
                'basis': 'Promoted out-of-sample logistic calibration of the direction score'}}


def calibration_forecast(store: Store, symbol: str, score: float, horizon: str, generated_at: datetime) -> dict | None:
    """Frozen P(up) forecast to store on the decision; evaluation scores it once the horizon closes."""
    champion = active(store, symbol, horizon)
    if not champion or not champion.get('model') or not champion.get('trained_through'):
        return None
    if aware(champion['trained_through']) >= aware(generated_at):
        return None
    return {'probability': predict(champion['model'], score), 'horizon': horizon, 'model_id': champion['id'],
            'trained_through': champion['trained_through'], 'generated_at': aware(generated_at).isoformat()}


# Challenger weight sets (shadow only) ---------------------------------------------------------------------------

def challengers() -> list[dict]:
    return list(ruleset()['autolearn']['challengers'])


def shadow_decision(decision: Decision, challenger: dict) -> dict:
    payload = decision.payload or {}
    raw, missing = raw_components(payload)
    score = score_with(raw, challenger['weights'])
    # Data/session gates are independent of the weights; setup downgrades are re-derived for the challenger score.
    gates = payload['blocks'] if 'blocks' in payload else payload.get('reasons')
    blocked = bool(gates) or payload.get('audit_status') == 'BLOCK'
    label = category(score, blocked, raw)
    return {'kind': 'SHADOW_DECISION', 'published': False, 'challenger_id': challenger['id'],
            'challenger_version': challenger['version'], 'weights': challenger['weights'], 'decision_id': decision.id,
            'symbol': decision.symbol, 'created_at': aware(decision.created_at).isoformat(), 'score': score,
            'category': label, 'side': 1 if score > 0 else -1 if score < 0 else 0, 'actionable': ev.side_of(label) != 0,
            'blocked': blocked, 'champion_score': decision.score, 'champion_direction': decision.direction,
            'components_used': raw, 'components_missing': missing}


def score_challengers(store: Store, now: datetime) -> int:
    """Re-score new decisions with every configured challenger; results are stored, never published."""
    table, created = RECORDS['decision_components'], 0
    with store.session() as session:
        cursor: Any = session.get(RECORDS['configuration_versions'], SHADOW_CURSOR_KEY)
        mark = cursor.payload.get('mark') if cursor else None
        decisions = list(session.scalars(select(Decision).where(
            Decision.created_at <= now, ev._after(Decision.created_at, Decision.id, mark))
            .order_by(Decision.created_at, Decision.id).limit(ruleset()['autolearn']['max_decisions_per_cycle'])))
        for decision in decisions:
            for challenger in challengers():
                key = f"shadow:{challenger['id']}:{decision.id}"
                if session.get(table, key) is None:
                    session.add(table(id=key, payload=shadow_decision(decision, challenger)))
                    created += 1
            mark = [aware(decision.created_at).isoformat(), decision.id]
        state = {'mark': mark, 'updated_at': aware(now).isoformat()}
        if cursor is None:
            session.add(RECORDS['configuration_versions'](id=SHADOW_CURSOR_KEY, payload=state))
        else:
            cursor.payload = state
        session.commit()
    return created


def _shadow_rows(store: Store, challenger_id: str) -> dict[str, dict]:
    table = RECORDS['decision_components']
    with store.session() as session:
        rows: list[Any] = list(session.scalars(select(table).where(table.id.like(f'shadow:{challenger_id}:%'))))
        return {r.payload['decision_id']: r.payload for r in rows}


def evaluate_challenger(store: Store, challenger_id: str, horizon: str | None = None) -> dict:
    """OOS champion-vs-challenger test on the same decisions, each score calibrated on the training window."""
    horizon = horizon or ruleset()['autolearn']['report_horizon']
    spec = next((c for c in challengers() if c['id'] == challenger_id), None)
    if spec is None or horizon not in ev.horizons():
        raise ValueError('Unknown challenger or horizon')
    shadow = _shadow_rows(store, challenger_id)
    champion_id, champion = _champion(store, WEIGHTS_KEY)
    reference = _shadow_rows(store, champion['challenger_id']) if champion else {}
    outcomes = _outcomes(store, sorted(shadow), horizon)
    with store.session() as session:
        decisions = {d.id: d for start in range(0, len(outcomes), CHUNK) for d in session.scalars(
            select(Decision).where(Decision.id.in_(sorted(outcomes)[start:start + CHUNK])))}
    rows = []
    for key, outcome in outcomes.items():
        if key not in decisions or (champion and key not in reference):
            continue  # paired test: both scores must exist for the same decision, never a mixed champion
        base = reference[key]['score'] if champion else decisions[key].score
        row = _row(decisions[key], outcome, base)
        if row:
            rows.append(dict(row, challenger_score=shadow[key]['score']))
    minimum = ruleset()['thresholds']['minimum_calibration']
    train, test = purged_split(rows, _horizon_seconds(horizon))
    if len(train) >= minimum:
        champion_model = fit(train)
        model = fit([dict(r, score=r['challenger_score']) for r in train])
        validation = compare([predict(model, r['challenger_score']) for r in test],
                             [predict(champion_model, r['score']) for r in test], test, minimum)
    else:
        model, validation = None, {'status': 'INSUFFICIENT_DATA', 'observations': len(test)}
    trained = max((_label_time(r, _horizon_seconds(horizon)) for r in train), default=None)
    return _save_candidate(store, {
        'id': uid(), 'kind': 'weights', 'challenger_id': challenger_id, 'challenger_version': spec['version'],
        'weights': spec['weights'], 'horizon': horizon, 'symbol': None, 'model': model, 'score_field': 'challenger',
        'validation': dict(validation, champion_id=champion_id), 'training_observations': len(train),
        'trained_through': trained.isoformat() if trained else None,
        'validation_from': test[0]['timestamp'] if test else None})


def champion_weights(store: Store) -> dict:
    """Weights the scoring engine should use: rules.json unless a validated weights challenger was promoted."""
    pointer = store.get('configuration_versions', WEIGHTS_KEY) or {}
    if pointer.get('candidate_id') and pointer.get('weights'):
        return {'weights': pointer['weights'], 'source': 'autolearn', 'candidate_id': pointer['candidate_id'],
                'challenger_id': pointer.get('challenger_id'), 'activated_at': pointer.get('activated_at')}
    return {'weights': dict(ruleset()['weights']), 'source': 'rules', 'candidate_id': None, 'version': ruleset()['version']}


# Live monitoring and automatic rollback -------------------------------------------------------------------------

def _pointers(store: Store) -> dict[str, dict]:
    table = RECORDS['configuration_versions']
    with store.session() as session:
        rows: list[Any] = list(session.scalars(select(table).where(
            or_(table.id == WEIGHTS_KEY, table.id.like('calibration:%')))))
        return {r.id: r.payload for r in rows if r.payload.get('candidate_id')}


def _accumulate(store: Store, candidate: dict, outcomes: list[dict], since: datetime, field: str) -> int:
    """Add forecast-vs-outcome stats for outcomes of decisions made at or after `since`."""
    cfg, observed = settings(), 0
    stats = dict({'n': 0, 'brier_sum': 0.0, 'calls': 0, 'false_calls': 0, 'hits': 0}, **candidate.get(field, {}))
    weights = candidate.get('kind') == 'weights'
    for outcome in outcomes:
        if outcome['horizon'] != candidate['horizon'] or aware(outcome['decision_at']) < since:
            continue
        if not weights and outcome['symbol'] != candidate['symbol']:
            continue
        score = outcome['score']
        if weights:
            shadow = store.get('decision_components', f"shadow:{candidate['challenger_id']}:{outcome['decision_id']}")
            if not shadow:
                continue
            score = shadow['score']
        p, up = predict(candidate['model'], score), int(outcome['raw_return_pct'] > 0)
        stats['n'] += 1
        stats['brier_sum'] += (p - up) ** 2
        if p >= cfg['call_up'] or p <= cfg['call_down']:
            signed = (1 if p >= cfg['call_up'] else -1) * outcome['raw_return_pct'] - cfg['cost_bps'] / 100
            stats['calls'] += 1
            stats['hits'] += signed > 0
            stats['false_calls'] += signed <= 0
        observed += 1
    store.put('calibration_models', candidate['id'], dict(candidate, **{field: stats}))
    return observed


def observe_live(store: Store, outcomes: list[dict]) -> int:
    """Accumulate post-activation performance for every active champion from newly created outcomes."""
    observed = 0
    for pointer in _pointers(store).values():
        candidate = store.get('calibration_models', pointer['candidate_id'])
        if candidate and candidate.get('model'):
            observed += _accumulate(store, candidate, outcomes, aware(pointer['activated_at']), 'live')
    return observed


def observe_shadow(store: Store, outcomes: list[dict]) -> int:
    """Validated candidates keep a forward shadow track record before any promotion (never published)."""
    table, observed = RECORDS['calibration_models'], 0
    with store.session() as session:
        rows: list[Any] = list(session.scalars(select(table).where(table.id.notin_([ev.SHADOW_KEY, ev.COMPARABLES_KEY]))))
        waiting = [r.payload for r in rows if r.payload.get('state') == 'VALIDATED' and r.payload.get('model')]
    for candidate in waiting:
        observed += _accumulate(store, candidate, outcomes, aware(candidate['created_at']), 'shadow_live')
    return observed


def monitor(store: Store, now: datetime) -> list[dict]:
    """Roll back any champion whose live performance breaches configured tolerances vs its validation."""
    limits, actions = ruleset()['autolearn']['rollback'], []
    for key, pointer in _pointers(store).items():
        candidate = store.get('calibration_models', pointer['candidate_id']) or {}
        live, validation = candidate.get('live') or {}, candidate.get('validation') or {}
        if live.get('n', 0) < limits['min_observations']:
            continue
        metrics = {'observations': live['n'], 'brier_score': live['brier_sum'] / live['n'],
                   'false_positive_rate': live['false_calls'] / live['calls'] if live['calls'] else None,
                   'hit_rate': live['hits'] / live['calls'] if live['calls'] else None,
                   'validation': {k: validation.get(k) for k in ('brier_score', 'false_positive_rate', 'hit_rate')}}
        breaches = []
        if validation.get('brier_score') is not None and metrics['brier_score'] > validation['brier_score'] + limits['brier_tolerance']:
            breaches.append('brier_score')
        if None not in (metrics['false_positive_rate'], validation.get('false_positive_rate')) and \
                metrics['false_positive_rate'] > validation['false_positive_rate'] + limits['fpr_tolerance']:
            breaches.append('false_positive_rate')
        if None not in (metrics['hit_rate'], validation.get('hit_rate')) and \
                metrics['hit_rate'] < validation['hit_rate'] - limits['hit_rate_tolerance']:
            breaches.append('hit_rate')
        if breaches:
            rollback(store, pointer.get('symbol'), pointer.get('horizon'), reason='automatic',
                     metrics=dict(metrics, breaches=breaches, checked_at=aware(now).isoformat()), key=key)
            actions.append({'key': key, 'candidate_id': pointer['candidate_id'], 'breaches': breaches})
    return actions


def cycle(store: Store, outcomes: list[dict], now: datetime) -> dict:
    return {'shadow_decisions': score_challengers(store, now), 'live_observations': observe_live(store, outcomes),
            'shadow_observations': observe_shadow(store, outcomes), 'rollbacks': monitor(store, now)}


def versions(store: Store) -> dict:
    """Inspectable autolearn state: active champions, every candidate and its transitions, configured challengers."""
    table = RECORDS['calibration_models']
    with store.session() as session:
        rows: list[Any] = list(session.scalars(select(table).order_by(table.created_at.desc())))
    keep = ('id', 'kind', 'symbol', 'horizon', 'state', 'transitions', 'validation', 'created_at', 'trained_through',
            'challenger_id', 'challenger_version', 'rules_version', 'content_hash', 'live', 'shadow_live', 'activated_at')
    candidates = [{k: r.payload.get(k) for k in keep} for r in rows if r.payload.get('kind') in {'calibration', 'weights'}]
    return {'active': _pointers(store), 'candidates': candidates, 'challengers': challengers(),
            'champion_weights': champion_weights(store)}
