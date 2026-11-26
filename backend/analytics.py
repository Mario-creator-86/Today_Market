"""Observed market diagnostics; none of these functions creates an order."""
import math
from bisect import bisect_right
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from statistics import mean, stdev
from typing import Any

from sqlalchemy import select

from backend.config import freshness_seconds, rules_view
from backend.database import RECORDS, Store, Tick, utcnow
from backend.engines import aware
from backend.positioning import option_positioning
from backend.technicals import live_tick_filter

ALIGNED = {'CONFIRMED', 'PARTIAL'}
AGAINST = {'DIVERGENT', 'REVERSED'}
# Session-bound windows (close, next_session) register a resolver: (t0, asset_class) -> window end or None.
WINDOW_RESOLVERS: dict[str, Callable[[datetime, str], datetime | None]] = {}


def _session_window(label: str) -> Callable[[datetime, str], datetime | None]:
    def resolve(t0: datetime, kind: str) -> datetime | None:
        from backend import sessions  # sessions -> engines; imported lazily to keep analytics import-light
        return (sessions.session_close if label == 'close' else sessions.next_session_close)(kind, t0)
    return resolve


# Session-based horizons (spec §18 'close' and 'next session') are part of the engine, not runtime state.
WINDOW_RESOLVERS.update(close=_session_window('close'), next_session=_session_window('next_session'))


def correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < rules_view()['thresholds']['min_history']:
        return None
    if not all(math.isfinite(v) for v in left + right):
        raise ValueError('Nonfinite correlation input')
    a, b = mean(left), mean(right)
    denominator = math.sqrt(sum((v-a)**2 for v in left) * sum((v-b)**2 for v in right))
    return sum((x-a)*(y-b) for x, y in zip(left, right, strict=True)) / denominator if denominator else None


def relative_policy(base: dict | None, other: dict | None) -> dict:
    required = {'rate', 'yield_2y', 'expected_next_rate', 'as_of', 'verified'}
    if not base or not other or not required.issubset(base) or not required.issubset(other):
        return {'bias': 'WAIT', 'reason': 'Both central banks, expectations and yields are required'}
    if not base['verified'] or not other['verified']:
        return {'bias': 'WAIT', 'reason': 'Unverified policy evidence'}
    yield_diff = base['yield_2y'] - other['yield_2y']
    repricing_diff = (base['expected_next_rate']-base['rate']) - (other['expected_next_rate']-other['rate'])
    return {'bias': 'WAIT', 'policy_differential': base['rate']-other['rate'],
            'yield_differential': yield_diff, 'repricing_differential': repricing_diff,
            'reason': 'Relative policy context; price, intervention risk and market reaction still required'}


def positioning(options: list[dict]) -> dict:
    """Legacy all-expiry summary; backend.positioning holds the per-expiry engine."""
    result = option_positioning(options, None)
    return dict(result['totals'], strike_concentrations=result['strike_concentrations'],
                directional_score=0, reason=result['reason'])


# --- units and tolerances ---------------------------------------------------------------

def asset_class(symbol: str) -> str | None:
    return next((kind for s, _, kind in rules_view()['assets'] if s == symbol), None)


def unit_for(symbol: str) -> str:
    config = rules_view()['market_units']
    return 'bp' if symbol in config['bp_symbols'] or asset_class(symbol) in config['bp_asset_classes'] else 'pct'


def change_in_unit(before: float, after: float, unit: str) -> float:
    if not math.isfinite(before) or not math.isfinite(after):
        raise ValueError('Nonfinite price')
    if unit == 'bp':
        return (after - before) * rules_view()['market_units']['bp_per_unit']
    if before <= 0 or after <= 0:
        raise ValueError('Percent change needs positive prices')
    return (after / before - 1) * 100


def tolerance(symbol: str) -> dict[str, float]:
    config = rules_view()['reaction']['tolerances']
    return (config['symbols'].get(symbol) or config['classes'].get(asset_class(symbol) or '')
            or config['default'][unit_for(symbol)])


# --- reaction engine --------------------------------------------------------------------

def classify_reaction(change: float, expected: int, noise: float, confirm: float, aligned_earlier: bool = False) -> str:
    """REVERSED: an earlier window moved with the expectation and this one sits beyond baseline against it."""
    if expected not in {-1, 0, 1}:
        raise ValueError('Expected direction must be -1, 0 or 1')
    if expected == 0:
        return 'NOT_EXPECTED'
    if abs(change) < noise:
        return 'NO_REACTION'
    if change * expected > 0:
        return 'CONFIRMED' if abs(change) >= confirm else 'PARTIAL'
    return 'REVERSED' if aligned_earlier else 'DIVERGENT'


def measure_reaction(event_id: str, symbol: str, horizon: str, baseline: tuple[float, datetime],
                     observed: tuple[float, datetime], expected: int, aligned_earlier: bool = False,
                     confirm_scale: float = 1.0) -> dict:
    unit, bands = unit_for(symbol), tolerance(symbol)
    change = change_in_unit(baseline[0], observed[0], unit)
    status = classify_reaction(change, expected, bands['noise'], bands['confirm'] * confirm_scale, aligned_earlier)
    row = {'event_id': event_id, 'symbol': symbol, 'horizon': horizon, 'baseline_price': baseline[0],
           'observed_price': observed[0], 'change': round(change, 6), 'unit': unit, 'expected_direction': expected,
           'status': status, 'baseline_timestamp': aware(baseline[1]).isoformat(),
           'observed_timestamp': aware(observed[1]).isoformat(),
           'tolerance': {'noise': bands['noise'], 'confirm': bands['confirm'] * confirm_scale}}
    if unit == 'pct':
        row['change_pct'] = row['change']
    return row


def expected_effects(event: dict) -> dict[str, int]:
    """Net sign per symbol: caller-supplied effects, else active causal paths, else every matching causal rule.
    Paths carrying `contributions` (backend.causal) are netted on them, so regime scaling and inverting exceptions
    count; paths that were supplied but are all inactive (suppressed) mean no expectation, never a rule fallback.
    A symbol whose competing paths nearly cancel (causal conflict_net_ratio) has sign 0 (unresolved_effects)."""
    if isinstance(event.get('expected_effects'), dict):
        return {s: int(v) for s, v in event['expected_effects'].items() if v in {-1, 0, 1}}
    if event.get('causal_paths') is not None:
        paths = [p for p in event['causal_paths'] if p.get('active', True)]
    else:
        trigger = event.get('trigger') or event.get('event_type')
        paths = [r for r in rules_view().get('causal_rules', []) if r['trigger'] == trigger]
    net, gross = _netted(paths)
    ratio = _conflict_ratio()
    # Competing paths that nearly cancel leave the asset unresolved (the causal engine's own conflict rule, which the
    # decision gate uses): its sign is 0, so no reaction is graded and no scenario is written against a direction the
    # graph does not have (weak payrolls: lower yields lift equities, weaker growth pressures them).
    return {s: 0 if _conflicting(v, gross[s], ratio) else (v > 0) - (v < 0) for s, v in net.items()}


def _netted(paths: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    net: dict[str, float] = {}
    gross: dict[str, float] = {}
    for path in paths:
        weights = path.get('contributions') or {s: e * path.get('strength', 1) for s, e in path.get('effects', {}).items()}
        for symbol, value in weights.items():
            net[symbol] = net.get(symbol, 0.0) + value
            gross[symbol] = gross.get(symbol, 0.0) + abs(value)
    return net, gross


def _conflict_ratio() -> float:
    from backend import causal
    return float(causal.graph().conflict_net_ratio)


def _conflicting(net: float, gross: float, ratio: float) -> bool:
    """Paths disagree (something cancelled) and the net is a small share of the gross: backend.causal.conflict."""
    return gross > 0 and gross - abs(net) > 1e-9 and abs(net) / gross < ratio


def unresolved_effects(event: dict) -> list[str]:
    """Symbols the event's active causal paths reach from both sides without a resolved direction (expected_effects
    gives them sign 0): the text to show is 'competing paths, no direction', never an arrow."""
    if isinstance(event.get('expected_effects'), dict):
        return []
    if event.get('causal_paths') is not None:
        paths = [p for p in event['causal_paths'] if p.get('active', True)]
    else:
        trigger = event.get('trigger') or event.get('event_type')
        paths = [r for r in rules_view().get('causal_rules', []) if r['trigger'] == trigger]
    net, gross = _netted(paths)
    ratio = _conflict_ratio()
    return [s for s, v in net.items() if _conflicting(v, gross[s], ratio)]


def reaction_windows() -> list[dict]:
    """Fixed-offset windows from config plus any registered session-bound resolvers, in order."""
    return [dict(w) for w in rules_view()['reaction']['windows']] + [{'label': k, 'resolver': k} for k in WINDOW_RESOLVERS]


def window_end(window: dict, t0: datetime, kind: str) -> datetime | None:
    if 'seconds' in window:
        return t0 + timedelta(seconds=window['seconds'])
    end = WINDOW_RESOLVERS[window['resolver']](t0, kind)
    return aware(end) if end else None


def basket_confirmation(event_id: str, horizon: str, reactions: Iterable[dict], expected: dict[str, int]) -> dict:
    """Event-level confirmation across the expected basket; missing assets never count as confirming."""
    config = rules_view()['reaction']
    by_symbol = {r['symbol']: r for r in reactions if r['horizon'] == horizon}
    wanted = sorted(s for s, d in expected.items() if d)
    status = {s: by_symbol[s]['status'] for s in wanted if s in by_symbol}
    confirming = [s for s, v in status.items() if v == 'CONFIRMED']
    partial = [s for s, v in status.items() if v == 'PARTIAL']
    diverging = [s for s, v in status.items() if v in AGAINST]
    missing = [s for s in wanted if s not in status]
    pct = round(100 * (len(confirming) + config['partial_weight'] * len(partial)) / len(status), 1) if status else None
    aligned = len(confirming) + len(partial)
    levels = config['confirmation_levels']
    # One or two measured assets are too thin to grade a basket: the percentage stays, the level does not.
    level = None if pct is None or len(status) < config['min_measured_for_level'] \
        else 'HIGH' if pct >= levels['high'] else 'MEDIUM' if pct >= levels['medium'] else 'LOW'
    return {'event_id': event_id, 'horizon': horizon, 'confirmation_pct': pct, 'confirmation_level': level,
            'confirming': confirming, 'partial': partial, 'diverging': diverging, 'missing': missing,
            'no_reaction': [s for s, v in status.items() if v == 'NO_REACTION'],
            'divergence': bool(diverging) and aligned >= config['divergence_min_aligned'] and aligned > len(diverging),
            'measured': len(status), 'expected': len(wanted), 'expected_symbols': wanted}


def live_ticks() -> Any:
    """Only live observations: daily closes, proxies and development fixtures never measure an intraday reaction."""
    return live_tick_filter()


def _tick_at(session: Any, symbol: str, at: datetime, stale_seconds: float) -> Tick | None:
    """Last live tick known at `at` (timestamp and ingestion both <= at) and no older than the freshness limit of its
    provider (config.freshness_seconds; stale_seconds is the default of the unlisted, streamed providers). With one
    15 s limit for every feed, a REST-polled equity had no usable tick at most window ends."""
    tick = session.scalar(select(Tick).where(Tick.symbol == symbol, Tick.timestamp <= at, Tick.ingested_at <= at,
                                             live_ticks()).order_by(Tick.timestamp.desc()).limit(1))
    if tick is None or (aware(at) - aware(tick.timestamp)).total_seconds() > freshness_seconds(tick.provider, stale_seconds):
        return None
    return tick


def _stored(store: Store, prefix: str) -> dict[str, dict]:
    cls = RECORDS['event_relationships']
    with store.session() as session:
        rows: list[Any] = list(session.scalars(select(cls).where(cls.id.startswith(prefix, autoescape=True))))
        return {row.id: row.payload for row in rows}


def publication_lag_seconds(event: dict) -> float | None:
    """Seconds from the event's publication time (event_time) to when it was first seen; None without a usable
    publication time."""
    published = event.get('event_time')
    if not published:
        return None
    try:
        return (aware(event['first_seen_utc']) - aware(published)).total_seconds()
    except (TypeError, ValueError):
        return None


def _eligible(event: dict, now: datetime) -> bool:
    """Recent enough and important enough to measure, and first seen close to its publication: a release ingested
    long after it was published (a backfill) is never measured, because the move after ingestion is not its reaction."""
    config = rules_view()['reaction']
    importance = event.get('importance')
    age = (now - aware(event['first_seen_utc'])).total_seconds()
    lag = publication_lag_seconds(event)
    return (0 <= age <= config['max_event_age_seconds'] and (importance is None or importance >= config['min_importance'])
            and (lag is None or lag <= config['max_publication_lag_seconds']))


def capture_reactions(store: Store, events: list[dict], stale_seconds: float, now: datetime | None = None,
                      basket: list[str] | None = None) -> int:
    """Measures every basket asset per window and stores reactions plus the event-level basket per horizon."""
    now = aware(now or utcnow())
    symbols = basket or rules_view()['baskets']['default']
    windows = reaction_windows()
    added = 0
    for event in events:
        done_key = f'reactions-complete:{event["id"]}'
        if not _eligible(event, now) or store.get('event_relationships', done_key):
            continue
        t0, expected = aware(event['first_seen_utc']), expected_effects(event)
        stored = _stored(store, f'reaction:{event["id"]}:')
        fresh: dict[str, dict] = {}
        pending = False
        ends: dict[tuple[str, str], datetime | None] = {}  # a window ends at the same time for every asset of a class
        with store.session() as session:
            for symbol in list(dict.fromkeys([*symbols, *expected])):
                kind = asset_class(symbol) or ''
                baseline, baseline_read = None, False  # read only when a window is due: most runs measure nothing new
                aligned_earlier = False
                for window in windows:
                    key = f'reaction:{event["id"]}:{symbol}:{window["label"]}'
                    if key in stored:
                        aligned_earlier |= stored[key]['status'] in ALIGNED
                        continue
                    if (window['label'], kind) not in ends:
                        ends[window['label'], kind] = window_end(window, t0, kind)
                    end = ends[window['label'], kind]
                    if end is None:
                        continue
                    if end > now:
                        pending = True
                        continue
                    if not baseline_read:
                        baseline, baseline_read = _tick_at(session, symbol, t0, stale_seconds), True
                    tick = _tick_at(session, symbol, end, stale_seconds) if baseline else None
                    if baseline is None or tick is None:
                        continue
                    row = measure_reaction(event['id'], symbol, window['label'], (baseline.price, baseline.timestamp),
                                           (tick.price, tick.timestamp), expected.get(symbol, 0), aligned_earlier,
                                           window.get('confirm_scale', 1.0))
                    fresh[key] = row
                    aligned_earlier |= row['status'] in ALIGNED
        rows = list({**stored, **fresh}.values())
        baskets = {f'basket:{event["id"]}:{w["label"]}': basket_confirmation(event['id'], w['label'], rows, expected)
                   for w in windows if any(r['horizon'] == w['label'] for r in fresh.values())}
        store.put_many('event_relationships', {**fresh, **baskets})
        if not pending:
            store.put('event_relationships', done_key, {'event_id': event['id'], 'completed_at': now.isoformat()})
        added += len(fresh)
    return added


def scheduled_reactions(store: Store, event_id: str, t0: datetime, symbols: Iterable[str], expected: dict[str, int],
                        now: datetime, *, labels: Iterable[str], baseline_seconds: float, gap_seconds: float) -> list[dict]:
    """Reactions to a scheduled release (docs/EVENT_AWARENESS.md §3 post-event): the baseline is the last live tick at or
    before the scheduled time (at most baseline_seconds old), each window the last live tick at or before t0 + window (at
    most gap_seconds old and after the baseline). Configured fixed windows only, one query for every symbol; a window that
    has not ended yet, or an asset without both ticks, is simply not measured."""
    from backend import sessions  # shared read-only config view: rules_view() deep-copies on every call
    config = sessions.ruleset()
    t0, now = aware(t0), aware(now)
    wanted = set(labels)
    windows = [w for w in config['reaction']['windows'] if w['label'] in wanted and 'seconds' in w]
    symbols = list(dict.fromkeys(symbols))
    if not windows or not symbols or now <= t0:
        return []
    kinds = {s: kind for s, _, kind in config['assets']}
    units, bands_config, per_bp = config['market_units'], config['reaction']['tolerances'], config['market_units']['bp_per_unit']
    end = min(now, t0 + timedelta(seconds=max(w['seconds'] for w in windows)))
    with store.session() as session:
        rows = session.execute(select(Tick.symbol, Tick.timestamp, Tick.price).where(
            Tick.symbol.in_(symbols), Tick.timestamp >= t0 - timedelta(seconds=baseline_seconds), Tick.timestamp <= end,
            Tick.ingested_at <= now, live_ticks()).order_by(Tick.timestamp)).all()
    series: dict[str, list[tuple[datetime, float]]] = {}
    for symbol, stamp, price in rows:
        if math.isfinite(price):
            series.setdefault(symbol, []).append((aware(stamp), price))
    result = []
    for symbol in symbols:
        points = series.get(symbol, [])
        base = _price_at(points, t0, baseline_seconds)
        if base is None:
            continue
        unit = 'bp' if symbol in units['bp_symbols'] or kinds.get(symbol) in units['bp_asset_classes'] else 'pct'
        bands = (bands_config['symbols'].get(symbol) or bands_config['classes'].get(kinds.get(symbol) or '')
                 or bands_config['default'][unit])
        aligned_earlier = False
        for window in windows:
            at = t0 + timedelta(seconds=window['seconds'])
            if at > now:
                break
            seen = _price_at(points, at, gap_seconds)
            if seen is None or seen[0] <= base[0] or (unit == 'pct' and min(base[1], seen[1]) <= 0):
                continue
            # Same row as measure_reaction, with the unit and bands resolved once per symbol.
            change = (seen[1] - base[1]) * per_bp if unit == 'bp' else (seen[1] / base[1] - 1) * 100
            confirm = bands['confirm'] * window.get('confirm_scale', 1.0)
            direction = expected.get(symbol, 0)
            status = classify_reaction(change, direction, bands['noise'], confirm, aligned_earlier)
            row = {'event_id': event_id, 'symbol': symbol, 'horizon': window['label'], 'baseline_price': base[1],
                   'observed_price': seen[1], 'change': round(change, 6), 'unit': unit, 'expected_direction': direction,
                   'status': status, 'baseline_timestamp': base[0].isoformat(), 'observed_timestamp': seen[0].isoformat(),
                   'tolerance': {'noise': bands['noise'], 'confirm': confirm}}
            if unit == 'pct':
                row['change_pct'] = row['change']
            aligned_earlier |= status in ALIGNED
            result.append(row)
    return result


def stored_relationships(store: Store, prefix: str) -> dict[str, dict]:
    """event_relationships rows whose key starts with `prefix` (one query)."""
    return _stored(store, prefix)


def event_reactions(store: Store, event_id: str) -> dict:
    """Stored per-asset reactions and event-level baskets for one event (for Event detail and scoring)."""
    order = {w['label']: i for i, w in enumerate(reaction_windows())}
    reactions = sorted(_stored(store, f'reaction:{event_id}:').values(),
                       key=lambda r: (order.get(r['horizon'], len(order)), r['symbol']))
    baskets = sorted(_stored(store, f'basket:{event_id}:').values(), key=lambda b: order.get(b['horizon'], len(order)))
    return {'event_id': event_id, 'reactions': reactions, 'baskets': baskets}


# --- anomaly detector -------------------------------------------------------------------

def anomaly_rules(symbol: str) -> list[dict]:
    config = rules_view()['anomaly']
    own = [r for r in config['rules'] if r['symbol'] == symbol]
    return own or [r for r in config['class_defaults'] if r['asset_class'] == asset_class(symbol)]


def _price_at(points: list[tuple[datetime, float]], at: datetime, max_gap: float) -> tuple[datetime, float] | None:
    index = bisect_right(points, at, key=lambda p: p[0])
    return points[index-1] if index and (at - points[index-1][0]).total_seconds() <= max_gap else None


def window_volatility(points: list[tuple[datetime, float]], now: datetime, unit: str) -> float | None:
    """Std of fixed-interval changes over the lookback, sampled on a time grid (not per bar)."""
    config = rules_view()['anomaly']['volatility']
    step, gap = config['sample_seconds'], rules_view()['anomaly']['max_baseline_gap_seconds']
    start = now - timedelta(seconds=config['lookback_seconds'])
    grid = [start + timedelta(seconds=step * i) for i in range(int(config['lookback_seconds'] // step) + 1)]
    sampled = [_price_at(points, t, gap) for t in grid]
    # A tick carried across two grid points is not an observed zero change, so such pairs are skipped.
    changes = [change_in_unit(a[1], b[1], unit) for a, b in zip(sampled, sampled[1:], strict=False)
               if a and b and a[0] != b[0]]
    return stdev(changes) if len(changes) >= config['min_returns'] else None


def detect_anomaly(symbol: str, points: list[tuple[datetime, float]], now: datetime, rule: dict,
                   mode: str | None = None) -> dict | None:
    """Move over a clock window: last observed price vs the price known at window start."""
    config = rules_view()['anomaly']
    points = sorted((aware(t), p) for t, p in points if math.isfinite(p))
    now, unit = aware(now), unit_for(symbol)
    gap = config['max_baseline_gap_seconds']
    last = _price_at(points, now, gap)
    start = now - timedelta(seconds=rule['window_seconds'])
    first = _price_at(points, start, gap)
    if last is None or first is None:
        return None
    change = change_in_unit(first[1], last[1], unit)
    threshold, basis = float(rule['threshold']), 'fixed'
    volatility = None
    if (mode or config['mode']) == 'volatility_adjusted':
        volatility = window_volatility(points, now, unit)
        if volatility is not None:
            scaled = volatility * math.sqrt(rule['window_seconds'] / config['volatility']['sample_seconds'])
            threshold = max(threshold * config['volatility']['floor_ratio'], config['volatility']['multiplier'] * scaled)
            basis = 'volatility_adjusted'
        else:
            basis = 'fixed (insufficient volatility history)'
    return {'symbol': symbol, 'window_seconds': rule['window_seconds'], 'change': round(change, 6), 'unit': unit,
            'threshold': round(threshold, 6), 'threshold_basis': basis, 'sample_volatility': volatility,
            'triggered': abs(change) >= threshold, 'direction': (change > 0) - (change < 0),
            'start_price': first[1], 'end_price': last[1], 'start_timestamp': first[0].isoformat(),
            'end_timestamp': last[0].isoformat(), 'observations': sum(start <= t <= now for t, _ in points)}


def correlate_events(anomaly: dict, events: list[dict], releases: list[dict]) -> list[dict]:
    """Candidate explanations near the move. Candidates are leads for research, never asserted causes."""
    config = rules_view()['anomaly']['correlation']
    end = aware(anomaly['end_timestamp'])
    start = end - timedelta(seconds=anomaly['window_seconds'])
    low, high = start - timedelta(seconds=config['lookback_seconds']), end + timedelta(seconds=config['lookahead_seconds'])
    candidates: list[dict[str, Any]] = []
    for kind, items, stamp in (('event', events, 'first_seen_utc'), ('release', releases, 'scheduled_at')):
        for item in items:
            at = item.get('released_at') or item.get(stamp)
            if not at or not low <= aware(at) <= high:
                continue
            assets = set(item.get('affected_assets') or []) | set(item.get('assets') or [])
            expected = expected_effects(item).get(anomaly['symbol'], 0) if kind == 'event' else 0
            candidates.append({'kind': kind, 'id': item.get('id'), 'title': item.get('summary_fact') or item.get('name')
                               or item.get('title'), 'timestamp': aware(at).isoformat(),
                               'lag_seconds': round((start - aware(at)).total_seconds(), 1),
                               'verification_status': item.get('verification_status'),
                               'mentions_symbol': anomaly['symbol'] in assets, 'expected_direction': expected,
                               'direction_consistent': None if not expected else expected == anomaly['direction']})
    candidates.sort(key=lambda c: (not c['mentions_symbol'], c['verification_status'] != 'CONFIRMED',
                                   c['direction_consistent'] is False, abs(c['lag_seconds'])))
    return candidates[:config['max_candidates']]


def scan_anomalies(series: dict[str, list[tuple[datetime, float]]], now: datetime, events: list[dict] | None = None,
                   releases: list[dict] | None = None, mode: str | None = None) -> list[dict]:
    """Every configured window per instrument; triggered moves carry event correlation for the research planner."""
    found = []
    for symbol, points in series.items():
        for rule in anomaly_rules(symbol):
            result = detect_anomaly(symbol, points, now, rule, mode)
            if not result or not result['triggered']:
                continue
            candidates = correlate_events(result, events or [], releases or [])
            explained = any(c['mentions_symbol'] and c['verification_status'] == 'CONFIRMED'
                            and c['direction_consistent'] is not False for c in candidates)
            found.append(dict(result, candidates=candidates, explained_by_confirmed_event=explained,
                              research_recommended=not explained))
    return found


def recent_prices(store: Store, symbol: str, now: datetime, seconds: int) -> list[tuple[datetime, float]]:
    """Ticks known by `now` over a clock window (plus the baseline gap), oldest first."""
    since = aware(now) - timedelta(seconds=seconds + rules_view()['anomaly']['max_baseline_gap_seconds'])
    with store.session() as session:
        rows = session.execute(select(Tick.timestamp, Tick.price).where(Tick.symbol == symbol, Tick.timestamp >= since,
                               Tick.timestamp <= aware(now), Tick.ingested_at <= aware(now), live_ticks())
                               .order_by(Tick.timestamp))
        return [(aware(t), p) for t, p in rows]


def rolling_anomaly(prices: list[float], threshold_pct: float) -> dict | None:
    """Legacy bar-count check kept for callers not yet on scan_anomalies."""
    if len(prices) < 2 or any(p <= 0 or not math.isfinite(p) for p in prices):
        return None
    change = (prices[-1]/prices[0]-1)*100
    returns = [(b/a-1)*100 for a, b in zip(prices, prices[1:], strict=False)]
    volatility = stdev(returns) if len(returns) >= rules_view()['thresholds']['min_history'] else None
    return {'change_pct': change, 'threshold_pct': threshold_pct, 'triggered': abs(change) >= threshold_pct,
            'sample_volatility': volatility, 'observations': len(prices)}
