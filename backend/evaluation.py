"""Causal, incremental outcome evaluation and auditable paper execution.

Every decision gets outcomes (WAIT/leans as flagged counterfactuals on the score sign). Each cycle reads only
decisions after a stored cursor and aggregates tick paths in SQL, so the tick table is never loaded whole.
A tick counts for a horizon only if it was ingested by the horizon end (no lookahead).
"""
import math
from datetime import datetime, timedelta
from statistics import mean, median
from threading import RLock
from typing import Any, TypeGuard

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from backend import sessions
from backend.config import get_settings
from backend.database import RECORDS, Decision, Store, Tick, utcnow
from backend.engines import aware
from backend.sessions import ruleset
from backend.technicals import live_tick_filter

CURSOR_KEY = 'autolearn:evaluation-cursor'  # configuration_versions row holding evaluation progress
SHADOW_KEY = 'shadow-outcomes-v1'
COMPARABLES_KEY = 'comparables-v1'
DIMENSIONS = ('asset', 'event_type', 'regime', 'score_bucket', 'confidence_bucket', 'source_combination', 'setup')
SAMPLING = 'observed ticks ingested by the horizon end; extrema may miss between-tick moves'
_LOCK = RLock()


def config() -> dict:
    return ruleset()['autolearn']


def horizons() -> list[str]:
    cfg = config()
    return [*cfg['horizons'], *cfg['session_horizons']]


def asset_classes() -> dict[str, str]:
    return {symbol: kind for symbol, _, kind in ruleset()['assets']}


def horizon_end(label: str, at: datetime, symbol: str) -> datetime | None:
    fixed = config()['horizons']
    if label in fixed:
        return at + timedelta(seconds=fixed[label])
    kind = asset_classes().get(symbol)
    if kind is None:
        return None
    if label == 'close':
        return sessions.session_close(kind, at, symbol)
    if label == 'next_session':
        return sessions.next_session_close(kind, at, symbol)
    raise ValueError(f'Unknown horizon {label}')


def _number(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value: Any) -> TypeGuard[float]:
    return _number(value) and value > 0


def side_of(label: str | None) -> int:
    """+1/-1 only for actionable SETUP bands of the rules.json category table (category or legacy direction)."""
    for band in ruleset()['categories']['bands']:
        if band.get('setup'):
            for side, key in ((1, 'long'), (-1, 'short')):
                if label in (band[key]['category'], band[key]['direction']):
                    return side
    return 0


def decision_time(decision: Decision) -> datetime:
    """When the decision was actually known: never before its stamp, the explicit decided_at, or the quote it used.

    A cycle stamps created_at at its start while a published decision may re-read the quote after the AI roles;
    measuring from the earlier stamp would let the path benefit from information the decision already contained.
    """
    payload = decision.payload or {}
    quote = (payload.get('input_snapshot') or {}).get('quote') or {}
    moments = [aware(decision.created_at)]
    for value in (payload.get('decided_at'), quote.get('timestamp') if isinstance(quote, dict) else None):
        if not value:
            continue
        try:
            moments.append(aware(value))
        except (TypeError, ValueError):
            continue
    return max(moments)


def signal_side(direction: str, payload: dict, score: float) -> tuple[int, bool]:
    """(side, actionable): published side, else the counterfactual sign of the score."""
    published = side_of(payload.get('category') or direction)
    if published:
        return published, True
    return (1 if score > 0 else -1 if score < 0 else 0), False


def score_bucket(score: float) -> str:
    threshold = ruleset()['thresholds']
    size = abs(score)
    name = 'STRONG' if size >= threshold['strong'] else 'SETUP' if size >= threshold['setup'] else \
        'LEAN' if size >= threshold['lean'] else 'NEUTRAL'
    return name if name == 'NEUTRAL' else f"{name}{'+' if score > 0 else '-'}"


def confidence_bucket(value: Any) -> str:
    if not _number(value):
        return 'UNRECORDED'
    edges = config()['confidence_edges']
    lower = max((e for e in edges if value >= e), default=0)
    upper = min((e for e in edges if value < e), default=100)
    return f'{lower}-{upper}'


def regime_key(payload: dict) -> str:
    regime = payload.get('regime')
    posture = regime.get('posture') if isinstance(regime, dict) else None
    return str(posture) if posture else config()['comparable_regime_fallback']


def technical_setup(payload: dict) -> str:
    stored = payload.get('technical_setup') or payload.get('setup')
    if isinstance(stored, str) and stored:
        return stored
    levels = payload.get('levels') or {}
    price = ((payload.get('input_snapshot') or {}).get('quote') or {}).get('price')
    support, resistance = levels.get('support'), levels.get('resistance')
    if not (_positive(price) and _positive(support) and _positive(resistance)):
        return 'NO_LEVELS'
    return 'BREAKOUT_ABOVE_RESISTANCE' if price > resistance else 'BREAKDOWN_BELOW_SUPPORT' if price < support \
        else 'INSIDE_RANGE'


def group_keys(symbol: str, score: float, payload: dict) -> dict[str, list[str]]:
    events = (payload.get('input_snapshot') or {}).get('events') or []
    stored = payload.get('regime')
    regime: dict = stored if isinstance(stored, dict) else {}
    sources = sorted({str(e.get('source_id')) for event in events for e in event.get('evidence', [])
                      if e.get('source_id')})
    return {'asset': [symbol],
            'event_type': sorted({str(e.get('event_type')) for e in events if e.get('event_type')}) or ['NONE'],
            'regime': sorted(map(str, regime.get('regimes') or [])) or [config()['comparable_regime_fallback']],
            'score_bucket': [score_bucket(score)],
            'confidence_bucket': [confidence_bucket(payload.get('evidence_confidence'))],
            'source_combination': ['+'.join(sources) or 'NONE'],
            'setup': [technical_setup(payload)]}


def _usable(symbol: str, known_by: datetime) -> list[Any]:
    """Live ticks known by `known_by`; daily closes, proxies and stub fixtures never reach outcomes."""
    return [Tick.symbol == symbol, Tick.price > 0, Tick.ingested_at <= known_by,
            Tick.timestamp <= Tick.ingested_at, live_tick_filter()]


def entry_tick(session: Session, symbol: str, at: datetime, stale: float) -> Tick | None:
    tick = session.scalar(select(Tick).where(*_usable(symbol, at), Tick.timestamp <= at)
                          .order_by(Tick.timestamp.desc(), Tick.id.desc()).limit(1))
    return tick if tick and (at - aware(tick.timestamp)).total_seconds() <= stale else None


def _first_cross(session: Session, where: list[Any], above: bool, level: float) -> datetime | None:
    condition = Tick.price >= level if above else Tick.price <= level
    value = session.scalar(select(func.min(Tick.timestamp)).where(*where, condition))
    return aware(value) if value else None


def _levels(session: Session, where: list[Any], side: int, confirmation: Any, invalidation: Any) -> dict:
    result: dict[str, Any] = {'confirmation_level': confirmation if _positive(confirmation) else None,
                              'invalidation_level': invalidation if _positive(invalidation) else None,
                              'confirmation_occurred': None, 'confirmation_at': None,
                              'invalidation_occurred': None, 'invalidation_at': None, 'first_trigger': None}
    if not side:
        return result
    times = {}
    for name, level, above in (('confirmation', confirmation, side > 0), ('invalidation', invalidation, side < 0)):
        if _positive(level):
            hit = _first_cross(session, where, above, level)
            times[name] = hit
            result.update({f'{name}_occurred': hit is not None, f'{name}_at': hit.isoformat() if hit else None})
    hits = sorted((t, n) for n, t in times.items() if t)
    if hits:
        result['first_trigger'] = 'BOTH_SAME_TICK' if len(hits) == 2 and hits[0][0] == hits[1][0] else hits[0][1].upper()
    return result


def measure(session: Session, symbol: str, side: int, at: datetime, end: datetime, stale: float,
            entry: Tick | None = None, confirmation: Any = None, invalidation: Any = None) -> dict | None:
    """Return path statistics for [at, end] using only ticks ingested by `end`; None if unmeasurable."""
    entry = entry or entry_tick(session, symbol, at, stale)
    if entry is None:
        return None
    where = [*_usable(symbol, end), Tick.timestamp > at, Tick.timestamp <= end]
    count, high, low = session.execute(select(func.count(), func.max(Tick.price), func.min(Tick.price)).where(*where)).one()
    if not count:
        return None
    last = session.scalar(select(Tick).where(*where).order_by(Tick.timestamp.desc(), Tick.id.desc()).limit(1))
    if last is None or (end - aware(last.timestamp)).total_seconds() > stale:
        return None
    raw, up, down = ((value / entry.price - 1) * 100 for value in (last.price, high, low))
    favorable, adverse = (up, down) if side > 0 else (-down, -up)
    return {'entry_tick_id': entry.id, 'entry_price': entry.price, 'end_tick_id': last.id, 'end_price': last.price,
            'raw_return_pct': raw, 'raw_high_pct': up, 'raw_low_pct': down,
            'return_pct': side * raw if side else None, 'mfe_pct': max(0.0, favorable) if side else None,
            'mae_pct': min(0.0, adverse) if side else None, 'observations': count, 'sampling': SAMPLING,
            **_levels(session, where, side, confirmation, invalidation)}


def _forecast(payload: dict, horizon: str, decision_at: datetime) -> dict | None:
    """Only frozen, dated forecasts made before the decision count; scores are never read as probabilities."""
    forecast = payload.get('calibration_forecast')
    if not isinstance(forecast, dict) or forecast.get('horizon') != horizon:
        return None
    probability = forecast.get('probability')
    if not _number(probability) or not 0 <= probability <= 1 or not forecast.get('model_id'):
        return None
    try:
        valid = aware(forecast['trained_through']) < aware(forecast['generated_at']) <= decision_at
    except (KeyError, TypeError, ValueError):
        return None
    return {'probability': probability, 'model_id': forecast['model_id']} if valid else None


def _quote(tick: Tick, at: datetime, stale: float, spread_limit: float) -> tuple[float, float] | None:
    bid, ask = tick.payload.get('bid'), tick.payload.get('ask')
    if not (_positive(bid) and _positive(ask) and bid <= ask):
        return None
    if not (aware(tick.timestamp) <= aware(tick.ingested_at) <= at):
        return None
    if (at - aware(tick.timestamp)).total_seconds() > stale:
        return None
    if _spread_bps(bid, ask) > spread_limit:
        return None
    return bid, ask


def _spread_bps(bid: float, ask: float) -> float:
    return (ask - bid) / ((ask + bid) / 2) * 10000


def _after(column_at: Any, column_id: Any, mark: list | None) -> Any:
    if not mark:
        return True
    at = aware(mark[0])
    return or_(column_at > at, and_(column_at == at, column_id > mark[1]))


class OutcomeTracker:
    def __init__(self, store: Store):
        self.store = store
        self.created: list[dict] = []
        self.unavailable = 0

    def evaluate(self, now: datetime | None = None) -> dict:
        now = aware(now or utcnow())
        thresholds, cfg = ruleset()['thresholds'], config()
        stale = get_settings().quote_stale_seconds
        if not 0 <= thresholds['slippage_bps'] < 10000 or thresholds['max_spread_bps'] < 0:
            raise ValueError('Invalid execution cost configuration')
        until = now - timedelta(seconds=cfg['decision_settle_seconds'])
        self.created = []
        with _LOCK:
            paper = self._evaluate(now, until, stale, cfg)
            _aggregate(self.store, self.created)  # under the lock so concurrent cycles cannot lose counts
        return {'new_outcomes': len(self.created), 'unavailable': self.unavailable, 'paper_opened': paper.opened,
                'paper_closed': paper.closed, 'evaluated_at': now.isoformat()}

    def _evaluate(self, now: datetime, until: datetime, stale: float, cfg: dict) -> 'Paper':
        with self.store.session() as session:
            cursor_row: Any = session.get(RECORDS['configuration_versions'], CURSOR_KEY)
            cursor = dict(cursor_row.payload) if cursor_row else {}
            marks = dict(cursor.get('outcomes', {}))
            budget, self.unavailable = cfg['max_decisions_per_cycle'], 0
            symbols = list(session.scalars(select(Decision.symbol).distinct()))
            for symbol in symbols:
                for label in horizons():
                    key = f'{symbol}|{label}'
                    rows = list(session.scalars(select(Decision).where(
                        Decision.symbol == symbol, Decision.created_at <= until,
                        _after(Decision.created_at, Decision.id, marks.get(key)))
                        .order_by(Decision.created_at, Decision.id).limit(max(budget, 0))))
                    for decision in rows:
                        at = decision_time(decision)
                        end = horizon_end(label, at, symbol)
                        if end is not None and end > now:
                            break
                        budget -= 1
                        marks[key] = [aware(decision.created_at).isoformat(), decision.id]
                        if end is None or not self._outcome(session, decision, label, at, end, now, stale):
                            self.unavailable += 1
            paper = Paper(session, dict(cursor.get('paper') or {}), now, stale).run(until)
            payload = dict(cursor, outcomes=marks, paper=paper.state, updated_at=now.isoformat())
            if cursor_row is None:
                session.add(RECORDS['configuration_versions'](id=CURSOR_KEY, payload=payload))
            else:
                cursor_row.payload = payload
            session.commit()
        return paper

    def _outcome(self, session: Session, decision: Decision, label: str, at: datetime, end: datetime,
                 now: datetime, stale: float) -> bool:
        key = f'{decision.id}:{label}'
        if session.get(RECORDS['signal_outcomes'], key) is not None:
            return True
        payload = decision.payload or {}
        side, actionable = signal_side(decision.direction, payload, decision.score)
        path = measure(session, decision.symbol, side, at, end, stale, confirmation=payload.get('confirmation_level'),
                       invalidation=payload.get('invalidation_level'))
        if path is None:
            return False
        forecast = _forecast(payload, label, at)
        outcome = dict(path, decision_id=decision.id, symbol=decision.symbol, horizon=label, horizon_end=end.isoformat(),
                       decision_at=at.isoformat(), evaluated_at=now.isoformat(), direction=decision.direction,
                       category=payload.get('category'), score=decision.score, side=side, actionable=actionable,
                       counterfactual=not actionable, regime_key=regime_key(payload),
                       groups=group_keys(decision.symbol, decision.score, payload),
                       forecast_probability=forecast['probability'] if forecast else None,
                       forecast_model_id=forecast['model_id'] if forecast else None,
                       rules_version=payload.get('rules_version'), autolearn_version=ruleset().get('autolearn_version'))
        session.add(RECORDS['signal_outcomes'](id=key, payload=outcome))
        self.created.append(outcome)
        return True


class Paper:
    """Virtual positions for audited, actionable decisions. Fills use observed bid/ask plus slippage and fees."""

    def __init__(self, session: Session, state: dict, now: datetime, stale: float):
        self.session, self.now, self.stale = session, now, stale
        self.cfg, self.thresholds = config()['paper'], ruleset()['thresholds']
        self.opened = self.closed = 0
        self.state = state if 'open' in state else self._bootstrap(state)

    def _bootstrap(self, state: dict) -> dict:
        rows: list[Any] = list(self.session.scalars(select(RECORDS['paper_trades'])))
        closed = [r.payload for r in rows if r.payload.get('status') == 'CLOSED']
        last: dict[str, str] = {}
        for trade in closed:
            last[trade['symbol']] = max(last.get(trade['symbol'], ''), trade['closed_at'])
        return dict(state, mark=None, open=[r.id for r in rows if r.payload.get('status') == 'OPEN'], last_closed=last,
                    equity=self.cfg['sizing']['initial_equity'] + sum(t.get('pnl') or 0 for t in closed))

    def run(self, until: datetime) -> 'Paper':
        rows = list(self.session.scalars(select(Decision).where(
            Decision.created_at <= until, _after(Decision.created_at, Decision.id, self.state['mark']))
            .order_by(Decision.created_at, Decision.id).limit(config()['max_decisions_per_cycle'])))
        for decision in rows:
            self.state['mark'] = [aware(decision.created_at).isoformat(), decision.id]
            self._consider(decision, decision_time(decision))
        for trade_id in list(self.state['open']):
            self._advance(trade_id, until)
        return self

    def _consider(self, decision: Decision, at: datetime) -> None:
        payload = decision.payload or {}
        side = side_of(payload.get('category') or decision.direction)
        if not side or payload.get('audit_status') != 'PASS':
            return
        if self.session.get(RECORDS['paper_trades'], decision.id) is not None:
            return
        for trade_id in list(self.state['open']):
            self._advance(trade_id, at, symbol=decision.symbol)
        busy = any(self._trade(t)['symbol'] == decision.symbol for t in self.state['open'])
        if busy or self.state['last_closed'].get(decision.symbol, '') > at.isoformat():
            return
        entry = entry_tick(self.session, decision.symbol, at, self.stale)
        quote = _quote(entry, at, self.stale, self.thresholds['max_spread_bps']) if entry else None
        if entry is None or quote is None:
            return
        slippage = self.thresholds['slippage_bps']
        fill = (quote[1] if side == 1 else quote[0]) * (1 + side * slippage / 10000)
        levels = payload.get('levels') or {}
        stop, stop_basis = _stop(side, fill, payload, levels)
        target, target_basis = _target(side, fill, stop, levels, self.cfg)
        kind = asset_classes().get(decision.symbol, 'default')
        trade = dict(decision_id=decision.id, symbol=decision.symbol, asset_class=kind, side=side, status='OPEN',
                     opened_at=at.isoformat(), expires_at=(at + timedelta(seconds=config()['horizons'][self.cfg['expiry_horizon']])).isoformat(),
                     entry_bid=quote[0], entry_ask=quote[1], entry_spread_bps=_spread_bps(*quote), entry_price=fill,
                     entry_tick_id=entry.id, stop=stop, stop_basis=stop_basis, target=target, target_basis=target_basis,
                     slippage_bps=slippage, fee_bps=self.cfg['fee_bps'].get(kind, self.cfg['fee_bps']['default']),
                     sizing=_size(self.cfg['sizing'], self.state['equity'], fill, stop), mfe_pct=0.0, mae_pct=0.0,
                     checked_through=None, rules_version=ruleset()['version'],
                     autolearn_version=ruleset().get('autolearn_version'), mode='PAPER')
        self.session.add(RECORDS['paper_trades'](id=decision.id, payload=trade))
        self.session.flush()
        self.state['open'].append(decision.id)
        self.opened += 1

    def _trade(self, trade_id: str) -> dict:
        row: Any = self.session.get(RECORDS['paper_trades'], trade_id)
        return row.payload

    def _advance(self, trade_id: str, until: datetime, symbol: str | None = None) -> None:
        row: Any = self.session.get(RECORDS['paper_trades'], trade_id)
        trade = self._complete(dict(row.payload))
        if symbol and trade['symbol'] != symbol:
            return
        side, opened = trade['side'], aware(trade['opened_at'])
        mark = trade.get('checked_through')
        # Ticks stamped more than `stale` before the mark are unusable quotes anyway; bound the index range.
        floor = max(opened, aware(mark[0]) - timedelta(seconds=self.stale)) if mark else opened
        ticks = list(self.session.scalars(select(Tick).where(
            *_usable(trade['symbol'], until), Tick.timestamp > floor, _after(Tick.ingested_at, Tick.id, mark))
            .order_by(Tick.ingested_at, Tick.id).limit(self.cfg['max_ticks_per_cycle'])))
        for tick in ticks:
            observed = aware(tick.ingested_at)
            trade['checked_through'] = [observed.isoformat(), tick.id]
            quote = _quote(tick, observed, self.stale, self.thresholds['max_spread_bps'])
            if not quote:
                continue
            price = quote[0] if side == 1 else quote[1]  # executable exit side
            excursion = side * (price / trade['entry_price'] - 1) * 100
            trade.update(mfe_pct=max(trade['mfe_pct'], excursion), mae_pct=min(trade['mae_pct'], excursion))
            stop, target = trade['stop'], trade['target']
            stopped = stop is not None and (price <= stop if side == 1 else price >= stop)
            targeted = target is not None and (price >= target if side == 1 else price <= target)
            if stopped or targeted or observed >= aware(trade['expires_at']):
                self._close(trade, tick, observed, quote, 'STOP' if stopped else 'TARGET' if targeted else 'TIME')
                break
        row.payload = trade

    def _complete(self, trade: dict) -> dict:
        """Trades opened before fees/sizing/excursions were tracked keep their fills; defaults are recorded as such."""
        if 'sizing' not in trade:
            trade.update(fee_bps=0, mfe_pct=0.0, mae_pct=0.0, excursion_basis='tracked from first evaluation after upgrade',
                         sizing=_size(dict(self.cfg['sizing'], mode='equal_notional'), self.state['equity'],
                                      trade['entry_price'], trade.get('stop')))
        return trade

    def _close(self, trade: dict, tick: Tick, observed: datetime, quote: tuple[float, float], reason: str) -> None:
        side = trade['side']
        fill = (quote[0] if side == 1 else quote[1]) * (1 - side * trade['slippage_bps'] / 10000)
        gross = side * (fill / trade['entry_price'] - 1) * 100
        net = gross - 2 * trade['fee_bps'] / 100
        pnl = trade['sizing']['notional'] * net / 100
        trade.update(status='CLOSED', closed_at=observed.isoformat(), exit_tick_id=tick.id, exit_bid=quote[0],
                     exit_ask=quote[1], exit_spread_bps=_spread_bps(*quote), exit_price=fill, exit_reason=reason,
                     gross_return_pct=gross, fees_pct=2 * trade['fee_bps'] / 100, return_pct=net, pnl=pnl)
        self.state['open'].remove(trade['decision_id'])
        self.state['last_closed'][trade['symbol']] = observed.isoformat()
        self.state['equity'] += pnl
        self.closed += 1


def _stop(side: int, fill: float, payload: dict, levels: dict) -> tuple[float | None, str]:
    for value, basis in ((payload.get('invalidation_level'), 'invalidation_level'),
                         (levels.get('support') if side == 1 else levels.get('resistance'), 'opposite_level')):
        if _positive(value) and (value < fill if side == 1 else value > fill):
            return value, basis
    return None, 'none'


def _target(side: int, fill: float, stop: float | None, levels: dict, cfg: dict) -> tuple[float | None, str]:
    """Level target if still ahead of the fill; after a breakout use the ATR or R multiple beyond entry."""
    level = levels.get('resistance') if side == 1 else levels.get('support')
    if _positive(level) and (level > fill if side == 1 else level < fill):
        return level, 'level'
    atr = levels.get('atr')
    if _positive(atr):
        return fill + side * cfg['target_atr_multiple'] * atr, 'atr_multiple'
    if stop is not None:
        return fill + side * cfg['target_r_multiple'] * abs(fill - stop), 'r_multiple'
    return None, 'none'


def _size(cfg: dict, equity: float, fill: float, stop: float | None) -> dict:
    notional, basis = cfg['notional'], 'equal_notional'
    if cfg['mode'] == 'fixed_risk' and stop is not None:
        distance = abs(fill - stop) / fill
        notional = min(equity * cfg['risk_pct'] / 100 / distance, equity * cfg['max_notional_pct'] / 100)
        basis = 'fixed_risk'
    elif cfg['mode'] == 'fixed_risk':
        basis = 'equal_notional (no stop for risk sizing)'
    return {'mode': basis, 'notional': notional, 'quantity': notional / fill, 'equity_at_entry': equity}


def _aggregate(store: Store, outcomes: list[dict]) -> None:
    """Incremental forecast scoring and comparable counts; never rescans stored outcomes."""
    minimum = ruleset()['thresholds']['minimum_calibration']
    bins = config()['calibration']['calibration_bins']
    shadow = store.get('calibration_models', SHADOW_KEY) or {}
    table = shadow.get('bins') or [[0, 0.0, 0.0] for _ in range(bins)]
    total, brier = shadow.get('forecast_observations', 0), shadow.get('brier_sum', 0.0)
    counts = (store.get('calibration_models', COMPARABLES_KEY) or {}).get('counts', {})
    for outcome in outcomes:
        up = int(outcome['raw_return_pct'] > 0)
        bucket = score_bucket(outcome['score'])
        for scope in (outcome['regime_key'], '*'):
            entry = counts.setdefault(f"{outcome['symbol']}|{outcome['horizon']}|{bucket}|{scope}", {'n': 0, 'up': 0})
            entry['n'] += 1
            entry['up'] += up
        probability = outcome.get('forecast_probability')
        if probability is not None:
            cell = table[min(int(probability * bins), bins - 1)]
            cell[0], cell[1], cell[2] = cell[0] + 1, cell[1] + probability, cell[2] + up
            total, brier = total + 1, brier + (probability - up) ** 2
    observations = shadow.get('observations', 0) + len({o['decision_id'] for o in outcomes})
    store.put('calibration_models', SHADOW_KEY, {
        'status': 'SHADOW', 'promoted': False, 'observations': observations, 'minimum_observations': minimum,
        'probability': None, 'bins': table, 'brier_sum': brier, 'forecast_observations': total,
        'brier_score': brier / total if total else None,
        'calibration_error': sum(abs(c[1] - c[2]) for c in table) / total if total else None,
        'minimum_met': total >= minimum,
        'reason': 'Scores frozen, dated forecasts against realised outcomes; promotion needs champion-relative OOS validation.'})
    if outcomes:
        store.put('calibration_models', COMPARABLES_KEY, {'counts': counts, 'updated_at': utcnow().isoformat()})


def evaluate_pending(store: Store, now: datetime | None = None) -> dict:
    """One autolearn cycle: outcomes and paper trades, then shadow challengers and the rollback monitor."""
    from backend import calibration
    tracker = OutcomeTracker(store)
    result = tracker.evaluate(now)
    result['autolearn'] = calibration.cycle(store, tracker.created, aware(now or utcnow()))
    return result


def _normalized(rows: list[dict], store: Store) -> list[dict]:
    """Legacy outcomes predate side/groups; derive them from their decisions instead of guessing."""
    missing = {r['decision_id'] for r in rows if 'groups' not in r or 'side' not in r}
    decisions: dict[str, Decision] = {}
    if missing:
        with store.session() as session:
            ids = sorted(missing)
            for start in range(0, len(ids), 500):
                decisions.update({d.id: d for d in session.scalars(select(Decision).where(Decision.id.in_(ids[start:start + 500])))})
    result = []
    for row in rows:
        decision = decisions.get(row['decision_id'])
        if row['decision_id'] in missing:
            if decision is None:
                continue
            side, actionable = signal_side(decision.direction, decision.payload or {}, decision.score)
            raw = row['return_pct'] * side if side else None
            row = dict(row, side=side, actionable=actionable, counterfactual=not actionable, score=decision.score,
                       raw_return_pct=row.get('raw_return_pct', raw), regime_key=regime_key(decision.payload or {}),
                       groups=group_keys(decision.symbol, decision.score, decision.payload or {}))
        result.append(row)
    return result


def _drawdown(values: list[float]) -> float:
    cumulative = peak = worst = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


def _average(values: list[float], method: Any) -> float | None:
    return method(values) if values else None


def _hit_rate(rows: list[dict]) -> float | None:
    return sum(r['return_pct'] > 0 for r in rows) / len(rows) if rows else None


def summarize(rows: list[dict]) -> dict:
    """Outcome metrics of one set of outcome rows. Hit rates never mix populations (AFP-03): `hit_rate` is the SIGNAL
    hit rate, over published (actionable) signals only, and None while no published signal has a measured outcome;
    `counterfactual_hit_rate` is what following the score sign of WAIT and lean decisions would have returned.
    `hit_rate_basis` names the population behind hit_rate ('published', else None) and `counterfactual_only` is true
    when every measured outcome of the set is counterfactual. The return / MFE / MAE / drawdown figures cover every
    directional row of the set (the `actionable` and `counterfactual` counts say how many of each)."""
    rows = sorted(rows, key=lambda r: r['decision_at'])
    directional = [r for r in rows if r.get('side') and _number(r.get('return_pct'))]
    signals = [r for r in directional if r.get('actionable')]
    shadow = [r for r in directional if not r.get('actionable')]
    returns = [r['return_pct'] for r in directional]
    mfe = [r['mfe_pct'] for r in directional if _number(r.get('mfe_pct'))]
    mae = [r['mae_pct'] for r in directional if _number(r.get('mae_pct'))]
    pairs = [(r['forecast_probability'], int(r['raw_return_pct'] > 0)) for r in rows
             if _number(r.get('forecast_probability')) and _number(r.get('raw_return_pct'))]
    bins = config()['calibration']['calibration_bins']
    cells = [[(p, y) for p, y in pairs if min(int(p * bins), bins - 1) == i] for i in range(bins)]
    failed = [r['return_pct'] <= 0 or r.get('first_trigger') == 'INVALIDATION' for r in directional]
    # A false positive is a published (actionable) signal that failed; counterfactual failures are reported apart.
    published = [bad for bad, r in zip(failed, directional, strict=True) if r.get('actionable')]
    shadowed = [bad for bad, r in zip(failed, directional, strict=True) if not r.get('actionable')]
    confirmed = [r for r in directional if r.get('confirmation_occurred') is not None]
    invalidated = [r for r in directional if r.get('invalidation_occurred') is not None]
    return {'observations': len(returns), 'actionable': len(signals), 'counterfactual': len(shadow),
            'counterfactual_only': bool(shadow) and not signals,
            'hit_rate': _hit_rate(signals), 'hit_rate_basis': 'published' if signals else None,
            'counterfactual_hit_rate': _hit_rate(shadow),
            'mean_return_pct': _average(returns, mean), 'median_return_pct': _average(returns, median),
            'mean_mfe_pct': _average(mfe, mean), 'median_mfe_pct': _average(mfe, median),
            'mean_mae_pct': _average(mae, mean), 'median_mae_pct': _average(mae, median),
            'forecast_observations': len(pairs),
            'brier_score': sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None,
            'calibration_error': sum(abs(sum(p - y for p, y in c)) for c in cells) / len(pairs) if pairs else None,
            'false_positive_rate': sum(published) / len(published) if published else None,
            'counterfactual_failure_rate': sum(shadowed) / len(shadowed) if shadowed else None,
            'confirmation_rate': sum(bool(r['confirmation_occurred']) for r in confirmed) / len(confirmed) if confirmed else None,
            'invalidation_rate': sum(bool(r['invalidation_occurred']) for r in invalidated) / len(invalidated) if invalidated else None,
            'max_drawdown_return_points': _drawdown(returns) if returns else None}


def _paper_metrics(trades: list[dict]) -> dict:
    closed = sorted((t for t in trades if t['status'] == 'CLOSED'), key=lambda t: t['closed_at'])
    values = [t['return_pct'] for t in closed]
    gains, losses = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    pnl = [t['pnl'] for t in closed if _number(t.get('pnl'))]
    initial = config()['paper']['sizing']['initial_equity']
    equity, peak, worst = initial, initial, 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak * 100 if peak > 0 else 0.0)
    return {'closed_trades': len(values), 'open_trades': len(trades) - len(values),
            'hit_rate': sum(v > 0 for v in values) / len(values) if values else None,
            'mean_return_pct': _average(values, mean), 'median_return_pct': _average(values, median),
            'profit_factor': gains / losses if losses else None,
            'max_drawdown_return_points': _drawdown(values) if values else None,
            'mean_mfe_pct': _average([t['mfe_pct'] for t in closed if _number(t.get('mfe_pct'))], mean),
            'mean_mae_pct': _average([t['mae_pct'] for t in closed if _number(t.get('mae_pct'))], mean),
            'total_pnl': sum(pnl) if pnl else None, 'equity': equity if pnl else None,
            'max_drawdown_equity_pct': worst if pnl else None,
            'basis': f"Net of configured fees and slippage; {config()['paper']['sizing']['mode'].replace('_', '-')} "
                     f"sizing on {initial:,.0f} initial virtual equity"}


def performance(store: Store, group_by: list[str] | None = None, population: str = 'all',
                horizon: str | None = None) -> dict:
    """Breakdowns per dimension for one horizon (default: the configured report horizon), plus per-horizon totals."""
    if population not in {'all', 'actionable', 'counterfactual'}:
        raise ValueError('population must be all, actionable or counterfactual')
    dimensions = list(group_by or DIMENSIONS)
    unknown = sorted(set(dimensions) - set(DIMENSIONS))
    if unknown:
        raise ValueError(f'Unknown group_by dimensions {unknown}; use {list(DIMENSIONS)}')
    focus = horizon or config()['report_horizon']
    if focus not in horizons():
        raise ValueError('Unsupported horizon')
    with store.session() as session:
        outcome_rows: list[Any] = list(session.scalars(select(RECORDS['signal_outcomes'])))
        trade_rows: list[Any] = list(session.scalars(select(RECORDS['paper_trades'])))
        outcomes = [r.payload for r in outcome_rows]
        trades = [dict(r.payload, id=r.id) for r in trade_rows]
    outcomes = _normalized(outcomes, store)
    if population != 'all':
        outcomes = [o for o in outcomes if bool(o.get('actionable')) == (population == 'actionable')]
    metrics: dict[str, Any] = {h: summarize([o for o in outcomes if o['horizon'] == h]) for h in horizons()}
    metrics['paper'] = _paper_metrics(trades)
    breakdowns: dict[str, dict[str, dict]] = {}
    focused = [o for o in outcomes if o['horizon'] == focus]
    for dimension in dimensions:
        groups: dict[str, list[dict]] = {}
        for outcome in focused:
            for key in outcome['groups'].get(dimension, []):
                groups.setdefault(key, []).append(outcome)
        breakdowns[dimension] = {key: summarize(rows) for key, rows in sorted(groups.items())}
    # observations counts decisions with a measured directional outcome (the rows every metric is computed from);
    # decisions_recorded also counts no-side (WAIT) decisions whose raw move was measured but has no signed return.
    measured = [o for o in outcomes if o.get('side') and _number(o.get('return_pct'))]
    directional = len({o['decision_id'] for o in measured})
    recorded = len({o['decision_id'] for o in outcomes})
    published = len({o['decision_id'] for o in measured if o.get('actionable')})
    shadow = len({o['decision_id'] for o in measured if not o.get('actionable')})
    return {'observations': directional, 'directional_observations': directional, 'decisions_recorded': recorded,
            # Which is which (AFP-03): decisions measured as published signals vs as counterfactuals (WAIT and lean
            # decisions on the sign of their score). Every hit_rate below is over published signals only.
            'published_signals': published, 'counterfactual_decisions': shadow,
            'hit_rate_basis': 'published signals only; counterfactual outcomes are reported in counterfactual_hit_rate',
            'probability': None,
            'status': 'UNCALIBRATED' if directional else 'INSUFFICIENT_DATA', 'population': population,
            'default_horizon': config()['report_horizon'], 'breakdown_horizon': focus, 'dimensions': dimensions,
            'horizons': horizons(), 'paper_trades': trades, 'metrics': metrics, 'breakdowns': breakdowns}


def comparables(store: Store, symbol: str, side: int | None = None, limit: int = 50) -> dict:
    """Past decisions for one asset with their measured outcomes (index lookups only)."""
    with store.session() as session:
        query = select(Decision).where(Decision.symbol == symbol).order_by(Decision.created_at.desc())
        if limit < 1:
            raise ValueError('limit must be positive')
        decisions: list[Decision] = []
        result = session.scalars(query.execution_options(yield_per=limit))
        for d in result:  # stream newest first; stop as soon as enough matching decisions are found
            if side is None or signal_side(d.direction, d.payload or {}, d.score)[0] == side:
                decisions.append(d)
                if len(decisions) >= limit:
                    break
        result.close()
        keys = [f'{d.id}:{h}' for d in decisions for h in horizons()]
        found: list[Any] = list(session.scalars(select(RECORDS['signal_outcomes']).where(RECORDS['signal_outcomes'].id.in_(keys)))) if keys else []
    by_decision: dict[str, dict] = {}
    for row in found:
        by_decision.setdefault(row.payload['decision_id'], {})[row.payload['horizon']] = row.payload
    items = [{'decision_id': d.id, 'created_at': aware(d.created_at).isoformat(), 'direction': d.direction,
              'category': (d.payload or {}).get('category'), 'score': d.score,
              'outcomes': {h: {k: o.get(k) for k in ('return_pct', 'raw_return_pct', 'mfe_pct', 'mae_pct', 'actionable',
                                                   'confirmation_occurred', 'invalidation_occurred')}
                           for h, o in by_decision.get(d.id, {}).items()}} for d in decisions]
    minimum = ruleset()['thresholds']['minimum_calibration']
    return {'symbol': symbol, 'items': items, 'minimum_observations': minimum,
            'by_horizon': {h: summarize([o[h] for o in by_decision.values() if h in o]) for h in horizons()}}
