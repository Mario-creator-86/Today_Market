"""Deterministic market regime classification from observed market features.

Two bases (docs/MARKET_COVERAGE.md, Regime): 'live' uses FRESH intraday quotes only; 'last_close' is used only when the
live basis observes fewer than min_observed_features, and relies on close-to-close changes of official daily closes
plus configured proxy changes (direction only, never a level). Every feature records its basis, source and as-of time.
"""
import math
import operator
from datetime import date, datetime
from typing import TypeGuard

from backend.analytics import correlation, unit_for
from backend.config import rules
from backend.engines import aware
from backend.technicals import is_live_quote

OPERATORS = {'>': operator.gt, '<': operator.lt, '>=': operator.ge, '<=': operator.le}
ARROWS = {'up': '↑', 'down': '↓', 'flat': '→', 'unavailable': None}
MINUS = '−'
LIVE, LAST_CLOSE = 'live', 'last_close'


def _finite(value) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bp_change(values: dict[str, float]) -> float | None:
    """Yield change in bp from level and percent change of the level (previous = price / (1 + pct/100))."""
    if 'price' not in values or 'change_pct' not in values or values['change_pct'] <= -100:
        return None
    previous = values['price'] / (1 + values['change_pct'] / 100)
    return (values['price'] - previous) * rules()['market_units']['bp_per_unit']


def _quote_values(symbol: str, quote: dict) -> dict[str, float]:
    """price / change_pct, plus change_bp for yields (the provider's own bp change first, else derived)."""
    values = {k: float(quote[k]) for k in ('price', 'change_pct') if _finite(quote.get(k))}
    if values and unit_for(symbol) == 'bp':
        if _finite(quote.get('change_bp')):
            values['change_bp'] = float(quote['change_bp'])
        elif (bp := _bp_change(values)) is not None:
            values['change_bp'] = bp
    return values


def _source(quote: dict, basis: str, session: dict | None = None) -> dict:
    return {'basis': basis, 'source': quote.get('provider'), 'as_of': quote.get('timestamp'),
            'change_basis': quote.get('change_basis'), 'label': quote.get('label'),
            'close_date': quote.get('close_date'), 'close_status': quote.get('close_status'),
            'session': (session or quote.get('session') or {}).get('state') or quote.get('market_state')}


def live_quote(asset: dict) -> dict:
    """One eligibility rule for live levels and changes; freshness is supplied at read time."""
    quote = asset.get('quote') or {}
    state = ((quote.get('session') or asset.get('session') or {}).get('state')
             or quote.get('market_state') or quote.get('market_status'))
    return quote if (quote.get('freshness_status') == 'FRESH' and is_live_quote(quote)
                     and state in (None, 'LIVE')) else {}


def current_change(asset: dict) -> tuple[dict[str, float], dict]:
    """Fresh direction evidence only; an approved proxy never supplies the underlying's price level.

    Freshness is computed by HubService at read time using each feed's own age limit.
    A daily publication can be CURRENT without being a current-session observation.
    """
    symbol, quote = asset['symbol'], live_quote(asset)
    if quote:
        values = _quote_values(symbol, quote)
        if 'change_bp' in values or 'change_pct' in values:
            return values, _source(quote, LIVE, asset.get('session'))
    proxy = asset.get('proxy') or {}
    allowed = rules()['regime']['last_close']['proxy_features'].get(symbol, {})
    if (not proxy.get('stub') and proxy.get('freshness_status') == 'FRESH'
            and proxy.get('market_status') == 'LIVE' and proxy.get('symbol') in allowed
            and proxy.get('change_basis') in allowed[proxy['symbol']] and _finite(proxy.get('change_pct'))):
        return {'change_pct': float(proxy['change_pct'])}, dict(_source(proxy, 'proxy'), via=proxy['symbol'])
    return {}, {}


def current_context(assets: list[dict], historical: dict) -> dict:
    """Keep historical classification intact, and compare it with fresh observations on their own bases."""
    observed, sources, conflicts = {}, {}, []
    for asset in assets:
        values, source = current_change(asset)
        if values:
            observed[asset['symbol']], sources[asset['symbol']] = values, source
    for driver in rules()['regime']['drivers']:
        for spec in driver['inputs']:
            symbol, field = spec['feature'], spec['field']
            old = historical.get('features', {}).get(symbol, {}).get(field)
            new = observed.get(symbol, {}).get(field)
            if (not _finite(old) or not _finite(new) or min(abs(old), abs(new)) < spec['flat'] or old * new >= 0):
                continue
            source = sources[symbol]
            via = f" via {source['via']}" if source.get('via') else ''
            basis = '24h' if source.get('change_basis') == 'rolling_24h' else 'vs previous close'
            text = (f"{symbol} {_format(new, field)} ({basis}{via}) opposes the historical input "
                    f"{_format(old, field)}; the periods and instruments may differ.")
            conflicts.append({'feature': symbol, 'text': text, 'historical_value': old, 'current_value': new,
                              'source': source})
    return {'current_drivers': drivers(observed, sources), 'divergences': conflicts}


def _derived(result: dict[str, dict[str, float]], provenance: dict[str, dict], bars: dict[str, list[dict]] | None,
             credit: list[dict] | None, now: datetime | None = None, correlations: bool = True) -> None:
    """Curves and credit spreads (both bases), and correlations (live basis only), from what is already observed.
    A credit spread is a daily close: FRESH at poll time and, given `now`, at most last_close.max_age_days old.
    A correlation needs FRESH quotes for both legs: bars that stopped updating never describe the current regime."""
    config = rules()['regime']
    for curve in config['curves']:
        long, short = result.get(curve['long'], {}), result.get(curve['short'], {})
        if 'change_bp' in long and 'change_bp' in short:
            result[curve['name']] = {'change_bp': long['change_bp'] - short['change_bp']}
            if 'price' in long and 'price' in short:
                result[curve['name']]['level_bp'] = (long['price'] - short['price']) * rules()['market_units']['bp_per_unit']
            legs = [provenance.get(curve['long'], {}), provenance.get(curve['short'], {})]
            provenance[curve['name']] = {'basis': legs[0].get('basis'), 'source': 'computed from ' + ' and '.join(
                (curve['long'], curve['short'])), 'as_of': min((leg.get('as_of') or '' for leg in legs), default=None) or None}
    for item in credit or []:
        if item.get('name') in config['credit_features'] and item.get('freshness_status') == 'FRESH' and (
                now is None or _within(item.get('as_of'), now, config['last_close']['max_age_days'])):
            values = {k: float(item[k]) for k in ('value', 'change_bp') if _finite(item.get(k))}
            if values:
                result[f"CREDIT:{item['name']}"] = values
                provenance[f"CREDIT:{item['name']}"] = {
                    'basis': LAST_CLOSE, 'source': item.get('attribution') or 'credit feed', 'as_of': item.get('as_of'),
                    'provider': item.get('provider') or config.get('credit_providers', {}).get(item['name'])}
    if not correlations:
        return
    for left, right in config['correlation_pairs']:
        if any(leg not in result or provenance.get(leg, {}).get('basis') != LIVE for leg in (left, right)):
            continue
        a = {b['time']: b['close'] for b in (bars or {}).get(left, [])}
        b = {x['time']: x['close'] for x in (bars or {}).get(right, [])}
        times = sorted(a.keys() & b.keys())
        if len(times) < 2:
            continue
        ra = [a[t2] / a[t1] - 1 for t1, t2 in zip(times, times[1:], strict=False)]
        rb = [b[t2] / b[t1] - 1 for t1, t2 in zip(times, times[1:], strict=False)]
        value = correlation(ra, rb)
        if value is not None:
            result[f'CORR:{left}:{right}'] = {'value': value, 'observations': len(ra)}
            provenance[f'CORR:{left}:{right}'] = {'basis': LIVE, 'source': 'stored one-minute bars', 'as_of': times[-1]}


def features(assets: list[dict], bars: dict[str, list[dict]] | None = None,
             credit: list[dict] | None = None, provenance: dict[str, dict] | None = None,
             now: datetime | None = None) -> dict[str, dict[str, float]]:
    """Live basis: only FRESH quotes contribute; stale or closed-market values never describe the current regime.
    `credit` is the hook for spread feeds: [{name, value, change_bp, freshness_status, as_of}] with configured names
    (daily closes: supporting inputs labelled basis last_close, never counted as live, see live_count).
    `provenance`, when given, receives {feature: {basis, source, as_of, ...}}."""
    provenance = {} if provenance is None else provenance
    result: dict[str, dict[str, float]] = {}
    for asset in assets:
        quote = live_quote(asset)
        if not quote:
            continue
        values = _quote_values(asset['symbol'], quote)
        if values:
            result[asset['symbol']] = values
            provenance[asset['symbol']] = _source(quote, LIVE, asset.get('session'))
    _derived(result, provenance, bars, credit, now)
    return result


def live_count(observed: dict[str, dict[str, float]], provenance: dict[str, dict]) -> int:
    """Rule features observed on a live basis (FRESH quotes, curves of them, correlations of FRESH legs). Daily
    inputs mixed into the live classification (the credit spread) never make the live basis look sufficient."""
    config = rules()['regime']
    used = {c['feature'] for r in config['rules'] for c in r['required'] + r['supporting']}
    return sum(1 for name in used & observed.keys() if provenance.get(name, {}).get('basis') == LIVE)


def _within(timestamp: str | None, now: datetime, days: float) -> bool:
    try:
        age = (aware(now) - aware(timestamp)).total_seconds() if timestamp else None
    except ValueError:
        return False
    return age is not None and 0 <= age <= days * 86400 + 3600


def last_close_features(assets: list[dict], now: datetime, bars: dict[str, list[dict]] | None = None,
                        credit: list[dict] | None = None, provenance: dict[str, dict] | None = None,
                        excluded: list[dict] | None = None) -> dict[str, dict[str, float]]:
    """Last-close basis: close-to-close changes of daily closes (close_status CURRENT, or the market is closed) and,
    where configured, a proxy's change for a change feature only. Live-only changes (crypto rolling 24h) are left out
    and reported in `excluded`; live-only correlations are never computed here, so the two bases are never mixed."""
    config = rules()['regime']['last_close']
    provenance = {} if provenance is None else provenance
    classes = set(config['data_classes'])
    result: dict[str, dict[str, float]] = {}
    for asset in assets:
        symbol, quote, session = asset['symbol'], asset.get('quote') or {}, asset.get('session') or {}
        state = (quote.get('session') or session).get('state') or quote.get('market_state')
        # The latest expected publication always counts (EIA's weekly one can be 8 days old); an older close only while
        # its market is closed, and never beyond max_age_days.
        if (quote.get('data_class') in classes and not quote.get('stub')
                and (quote.get('close_status') == 'CURRENT'
                     or state != 'LIVE' and _within(quote.get('timestamp'), now, config['max_age_days']))):
            values = _quote_values(symbol, quote)
            if values:
                result[symbol] = values
                provenance[symbol] = _source(quote, LAST_CLOSE, session)
                continue
        proxy, allowed = asset.get('proxy') or {}, config['proxy_features'].get(symbol, {})
        if (proxy.get('symbol') in allowed and proxy.get('change_basis') in allowed[proxy['symbol']]
                and _finite(proxy.get('change_pct')) and _within(proxy.get('timestamp'), now, config['max_age_days'])):
            result[symbol] = {'change_pct': float(proxy['change_pct'])}  # direction only: never a level
            provenance[symbol] = dict(_source(proxy, 'proxy', session), via=proxy['symbol'])
            continue
        if quote and quote.get('data_class', 'LIVE') == 'LIVE' and quote.get('change_basis') == 'rolling_24h' \
                and excluded is not None:
            excluded.append({'feature': symbol, 'reason': config['excluded_note']})
    _derived(result, provenance, bars, credit, now, correlations=False)
    return result


def _format(value: float, field: str) -> str:
    sign = '+' if value > 0 else MINUS if value < 0 else ''
    if field == 'change_bp':
        text = f'{abs(value):.1f}'.rstrip('0').rstrip('.')
        return f'{sign}{text} bp'
    if field == 'change_pct':
        return f'{sign}{abs(value):.2f}%'
    return f'{value:g}'


def drivers(observed: dict[str, dict[str, float]], provenance: dict[str, dict] | None = None,
            basis: str = LIVE) -> list[dict]:
    """Rates / USD / Oil / Risk appetite arrows: each observed input votes by its configured flat band. An input that
    is a daily close carries its close_status (CURRENT | STALE) and close_date (YYYY-MM-DD); both are null otherwise,
    so a STALE close that still votes (an older close while its market is closed) can be labelled as such."""
    provenance = provenance or {}
    result = []
    for driver in rules()['regime']['drivers']:
        inputs, missing = [], []
        for spec in driver['inputs']:
            value = observed.get(spec['feature'], {}).get(spec['field'])
            if value is None:
                missing.append(spec['feature'])
                continue
            vote = 0 if abs(value) < spec['flat'] else spec['sign'] * (1 if value > 0 else -1)
            source = provenance.get(spec['feature'], {})
            day = _day(source.get('close_date'))
            inputs.append({'feature': spec['feature'], 'field': spec['field'], 'value': value, 'vote': vote,
                           'basis': source.get('basis', basis), 'source': source.get('source'),
                           'as_of': source.get('as_of'), 'via': source.get('via'),
                           'close_status': source.get('close_status') or None,
                           'close_date': day.isoformat() if day else None})
        total = sum(i['vote'] for i in inputs)
        direction = 'unavailable' if not inputs else 'up' if total > 0 else 'down' if total < 0 else 'flat'
        text = ', '.join(f"{i['feature']} {_format(i['value'], i['field'])}" + (f" via {i['via']}" if i['via'] else '')
                         for i in inputs)
        result.append({'driver': driver['name'], 'direction': direction, 'arrow': ARROWS[direction],
                       'inputs': inputs, 'missing': missing, 'input': text or None, 'basis': basis if inputs else None})
    return result


def _check(condition: dict, observed: dict[str, dict[str, float]]) -> bool | None:
    value = observed.get(condition['feature'], {}).get(condition.get('field', 'change_pct'))
    return None if value is None else OPERATORS[condition['op']](value, condition['value'])


def _day(value: str | None) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]) if len(value) == 10 else aware(value).date()
    except ValueError:
        return None


def _label(day: date) -> str:
    return f'{day:%a} {day.day} {day:%b}'


def day_label(value: str | None) -> str | None:
    """'Fri 25 Sep' for an ISO date (a timestamp gives its UTC date); None when missing or unparseable."""
    day = _day(value)
    return _label(day) if day else None


def basis_label(basis: str, provenance: dict[str, dict]) -> str:
    """'live intraday' or e.g. 'last close Fri 25 Sep, markets closed' (dates of the closes used, proxies named)."""
    if basis == LIVE:
        return 'live intraday'
    closes = [p for p in provenance.values() if p.get('basis') == LAST_CLOSE and 'close_date' in p]  # quotes, not curves
    days = sorted({d for p in closes if (d := _day(p.get('close_date') or p.get('as_of')))})
    text = 'last close'
    if days:
        text += ' ' + (_label(days[-1]) if len(days) == 1 else f"{days[0].day} {days[0]:%b}–{_label(days[-1])}")
    proxies = sorted({p['via'] for p in provenance.values() if p.get('basis') == 'proxy' and p.get('via')})
    if proxies:
        text += f" + proxies ({', '.join(proxies)})"
    sessions = [p.get('session') for p in provenance.values() if p.get('basis') in {LAST_CLOSE, 'proxy'} and p.get('session')]
    if sessions and all(s != 'LIVE' for s in sessions):
        text += ', markets closed'
    return text


def classify(observed: dict[str, dict[str, float]], confirmed_events: list[dict], now: datetime,
             basis: str = LIVE, provenance: dict[str, dict] | None = None) -> dict:
    """Allows several simultaneous regimes. Confidence is rule coverage, never a probability. When no rule has all of
    its required inputs, the posture is INSUFFICIENT_DATA (never a MIXED that nothing was evaluated for)."""
    config = rules()['regime']
    provenance = provenance or {}
    max_age = rules()['thresholds']['max_evidence_age_hours'] * 3600
    event_types = {e['event_type'] for e in confirmed_events
                   if e.get('verification_status') == 'CONFIRMED'
                   and 0 <= (aware(now) - aware(e['first_seen_utc'])).total_seconds() <= max_age}
    used = {c['feature'] for r in config['rules'] for c in r['required'] + r['supporting']}
    active, evaluated, missing_required = [], [], set()
    for rule in config['rules']:
        required = [_check(c, observed) for c in rule['required']]
        missing_required |= {c['feature'] for c, v in zip(rule['required'], required, strict=True) if v is None}
        supporting = [_check(c, observed) for c in rule['supporting']]
        supporting += [t in event_types for t in rule.get('confirmed_events', [])]
        missing = sorted({c['feature'] for c in rule['required'] + rule['supporting'] if c['feature'] not in observed})
        total = len(required) + len(supporting)
        satisfied = sum(v is True for v in required + supporting)
        state = 'ACTIVE' if all(v is True for v in required) else 'UNAVAILABLE' if any(v is None for v in required) else 'INACTIVE'
        item = {'regime': rule['regime'], 'state': state, 'confidence': round(100 * satisfied / total, 1) if total else 0.0,
                'missing_inputs': missing}
        evaluated.append(item)
        if state == 'ACTIVE':
            active.append(item)
    observed_count = len(used & observed.keys())
    common = {'coverage_pct': round(100 * observed_count / len(used), 1) if used else 0.0, 'evaluated': evaluated,
              'features': observed, 'feature_sources': {k: v for k, v in provenance.items() if k in observed},
              'drivers': drivers(observed, provenance, basis), 'basis': basis,
              'basis_label': basis_label(basis, {k: v for k, v in provenance.items() if k in observed}),
              'missing_required': sorted(missing_required)}
    if observed_count < config['min_observed_features']:
        return dict(common, regimes=[], posture='INSUFFICIENT_DATA', confidence=None,
                    method='Observed cross-asset features are insufficient')
    if not any(item['state'] != 'UNAVAILABLE' for item in evaluated):
        return dict(common, regimes=[], posture='INSUFFICIENT_DATA', confidence=None,
                    method='No regime rule has all of its required inputs')
    names = [a['regime'] for a in active] or ['MIXED']
    defensive = [n for n in names if n in config['defensive']]
    constructive = [n for n in names if n in config['constructive']]
    posture = 'DEFENSIVE' if defensive and not constructive else 'CONSTRUCTIVE' if constructive and not defensive else 'MIXED'
    return dict(common, regimes=names, posture=posture,
                confidence=round(sum(a['confidence'] for a in active) / len(active), 1) if active else None,
                method='Rule coverage across observed features; not a calibrated probability')


def evaluate(assets: list[dict], events: list[dict], now: datetime, bars: dict[str, list[dict]] | None = None,
             credit: list[dict] | None = None, last_close: bool = False) -> dict:
    """Live basis first. With `last_close`, the last-close basis replaces it when the live basis observes fewer than
    min_observed_features live features (live_count: a daily credit spread or a correlation of bars without FRESH
    quotes never counts) and the closes observe more (display only: decisions call this without it)."""
    config = rules()['regime']
    used = {c['feature'] for r in config['rules'] for c in r['required'] + r['supporting']}
    live_sources: dict[str, dict] = {}
    live = features(assets, bars, credit, live_sources, now)
    observed_live = live_count(live, live_sources)
    if not last_close or observed_live >= config['min_observed_features']:
        return classify(live, events, now, LIVE, live_sources)
    close_sources: dict[str, dict] = {}
    excluded: list[dict] = []
    closes = last_close_features(assets, now, bars, credit, close_sources, excluded)
    if len(used & closes.keys()) <= observed_live:
        return classify(live, events, now, LIVE, live_sources)
    result = classify(closes, events, now, LAST_CLOSE, close_sources)
    return dict(result, excluded=[e for e in excluded if e['feature'] in used], **current_context(assets, result))
