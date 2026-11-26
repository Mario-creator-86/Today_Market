"""Descriptive derivatives positioning from Deribit book summaries.

Nothing here predicts direction: open interest, put/call and skew describe positioning, `directional_score` is always
0 and max pain is never computed. Crowding flags are deterministic, config-driven risk labels for scoring.
"""
import math
import operator
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from backend.config import rules
from backend.database import RECORDS, Store
from backend.engines import aware

OPERATORS = {'>': operator.gt, '<': operator.lt, '>=': operator.ge, '<=': operator.le}
SECONDS_PER_DAY = 86400
MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
EXPIRY = re.compile(r'(\d{1,2})([A-Z]{3})(\d{2})')
DESCRIPTIVE = 'Open interest, skew and funding describe positioning; they are not a direction or a price target.'


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def parse_expiry(code: str, hour: int | None = None) -> datetime | None:
    """Deribit '9OCT26' / '27DEC24'. Month names are fixed English, not the process locale (strptime %b is not).
    `hour` defaults to positioning.expiry_hour_utc; loops over a chain pass it once (each rules() read is a copy)."""
    match = EXPIRY.fullmatch(code)
    if not match or match[2] not in MONTHS:
        return None
    hour = rules()['positioning']['expiry_hour_utc'] if hour is None else hour
    try:
        return datetime(2000 + int(match[3]), MONTHS.index(match[2]) + 1, int(match[1]), hour, tzinfo=UTC)
    except ValueError:
        return None


def parse_option(name: str, hour: int | None = None) -> dict | None:
    """BTC-30OCT26-65000-P -> currency, expiry, strike, type. Deribit writes decimal strikes with 'd'."""
    parts = name.split('-')
    if len(parts) != 4 or parts[3] not in {'C', 'P'}:
        return None
    expiry = parse_expiry(parts[1], hour)
    try:
        strike = float(parts[2].replace('d', '.'))
    except ValueError:
        return None
    return {'currency': parts[0], 'expiry': expiry, 'strike': strike, 'type': parts[3]} if expiry else None


def black76(forward: float, strike: float, iv_pct: float, years: float) -> dict | None:
    """Delta and gamma from mark IV on the expiry's forward (Deribit quotes interest_rate 0)."""
    if min(forward, strike, iv_pct, years) <= 0:
        return None
    sigma = iv_pct / 100
    d1 = (math.log(forward / strike) + 0.5 * sigma**2 * years) / (sigma * math.sqrt(years))
    call_delta = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    gamma = math.exp(-d1**2 / 2) / math.sqrt(2 * math.pi) / (forward * sigma * math.sqrt(years))
    return {'call_delta': call_delta, 'put_delta': call_delta - 1, 'gamma': gamma}


def _skew(options: list[dict]) -> dict | None:
    config = rules()['positioning']
    target, tolerance = config['skew_target_delta'], config['skew_max_delta_error']
    calls = [o for o in options if o['type'] == 'C' and o['greeks']]
    puts = [o for o in options if o['type'] == 'P' and o['greeks']]
    if not calls or not puts:
        return None
    call = min(calls, key=lambda o: abs(o['greeks']['call_delta'] - target))
    put = min(puts, key=lambda o: abs(o['greeks']['put_delta'] + target))
    if abs(call['greeks']['call_delta'] - target) > tolerance or abs(put['greeks']['put_delta'] + target) > tolerance:
        return None
    return {'put_iv': put['iv'], 'call_iv': call['iv'], 'put_strike': put['strike'], 'call_strike': call['strike'],
            'put_delta': round(put['greeks']['put_delta'], 4), 'call_delta': round(call['greeks']['call_delta'], 4),
            'put_minus_call_iv': round(put['iv'] - call['iv'], 4),
            'basis': f'{int(target * 100)}-delta proxy: Black-76 delta from mark IV, nearest listed strikes'}


def _totals(options: list[dict]) -> dict:
    calls = sum(o['oi'] for o in options if o['type'] == 'C')
    puts = sum(o['oi'] for o in options if o['type'] == 'P')
    weighted = [(o['iv'], o['oi']) for o in options if o['iv'] is not None and o['oi'] > 0]
    total = sum(oi for _, oi in weighted)
    return {'call_oi': calls, 'put_oi': puts, 'total_oi': calls + puts, 'put_call_ratio': puts / calls if calls else None,
            'oi_weighted_iv': sum(iv * oi for iv, oi in weighted) / total if total else None}


def _concentrations(options: list[dict], top: int) -> list[tuple[float, float]]:
    strikes: dict[float, float] = {}
    for option in options:
        strikes[option['strike']] = strikes.get(option['strike'], 0.0) + option['oi']
    return sorted(((k, v) for k, v in strikes.items() if v > 0), key=lambda item: item[1], reverse=True)[:top]


def option_positioning(rows: list[dict], now: datetime | None) -> dict:
    """Per-expiry OI, put/call, OI-weighted IV, skew proxy, strike and gamma concentrations. `now=None` keeps all
    expiries and skips greeks (no time to expiry)."""
    config = rules()['positioning']
    parsed = []
    for row in rows:
        meta = parse_option(str(row.get('instrument_name', '')), config['expiry_hour_utc'])
        oi = _number(row.get('open_interest'))
        if not meta or oi is None or oi < 0 or (now and meta['expiry'] <= aware(now)):
            continue
        iv = _number(row.get('mark_iv'))
        forward = _number(row.get('underlying_price'))
        years = (meta['expiry'] - aware(now)).total_seconds() / (config['days_per_year'] * SECONDS_PER_DAY) if now else 0
        greeks = black76(forward, meta['strike'], iv, years) if forward and iv and now else None
        parsed.append(dict(meta, oi=oi, iv=iv if iv is not None and iv >= 0 else None, forward=forward, greeks=greeks))
    expiries = []
    for expiry in sorted({o['expiry'] for o in parsed}):
        group = [o for o in parsed if o['expiry'] == expiry]
        gamma: dict[float, float] = {}
        for o in group:
            if o['greeks'] and o['forward'] and o['oi'] > 0:
                # Delta change per 1% move, in coin units; dealer sign is unknown, so this is magnitude only.
                gamma[o['strike']] = gamma.get(o['strike'], 0.0) + o['greeks']['gamma'] * o['forward'] * 0.01 * o['oi']
        expiries.append(dict(_totals(group), expiry=expiry.isoformat(),
                             days_to_expiry=round((expiry - aware(now)).total_seconds() / SECONDS_PER_DAY, 3) if now else None,
                             skew=_skew(group), strike_concentrations=_concentrations(group, config['top_strikes']),
                             gamma_concentrations=sorted(gamma.items(), key=lambda i: i[1], reverse=True)[:config['top_strikes']]))
    return {'totals': _totals(parsed), 'expiries': expiries,
            'largest_expiries': [{'expiry': e['expiry'], 'total_oi': e['total_oi'], 'put_call_ratio': e['put_call_ratio']}
                                 for e in sorted(expiries, key=lambda e: e['total_oi'], reverse=True)[:config['largest_expiries']]],
            'strike_concentrations': _concentrations(parsed, config['top_strikes']),
            'instruments': len(parsed), 'label': 'descriptive', 'directional_score': 0, 'reason': DESCRIPTIVE}


def futures_positioning(rows: list[dict], currency: str, now: datetime) -> dict:
    """Perpetual funding and premium, dated-future basis vs the index, and open interest (USD for inverse contracts)."""
    config = rules()['positioning']
    perpetual: dict[str, Any] | None = None
    dated: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get('instrument_name', ''))
        mark, index = _number(row.get('mark_price')), _number(row.get('estimated_delivery_price'))
        if not name.startswith(f'{currency}-') or not mark or not index:
            continue
        item = {'instrument': name, 'mark_price': mark, 'index_price': index, 'basis_pct': (mark / index - 1) * 100,
                'open_interest': _number(row.get('open_interest')), 'open_interest_unit': row.get('quote_currency')}
        if name == f'{currency}-PERPETUAL':
            perpetual = dict(item, funding_8h=_number(row.get('funding_8h')), current_funding=_number(row.get('current_funding')))
            continue
        expiry = parse_expiry(name.split('-')[1], config['expiry_hour_utc']) if name.count('-') == 1 else None
        if not expiry or expiry <= aware(now):
            continue
        days = (expiry - aware(now)).total_seconds() / SECONDS_PER_DAY
        dated.append(dict(item, expiry=expiry.isoformat(), days_to_expiry=round(days, 3),
                          annualized_basis_pct=item['basis_pct'] * config['days_per_year'] / days if days > 0 else None))
    dated.sort(key=lambda d: d['expiry'])
    open_interest = [i['open_interest'] for i in [*dated, *([perpetual] if perpetual else [])] if i['open_interest'] is not None]
    return {'perpetual': perpetual, 'dated': dated, 'total_open_interest': sum(open_interest) if open_interest else None,
            'label': 'descriptive', 'directional_score': 0, 'reason': DESCRIPTIVE}


def _metrics(options: dict | None, futures: dict | None, min_days: float) -> dict[str, float | None]:
    perp = (futures or {}).get('perpetual') or {}
    front = next((d for d in (futures or {}).get('dated', []) if d['days_to_expiry'] >= min_days), None)
    front_option = next((e for e in (options or {}).get('expiries', []) if e['skew'] and (e['days_to_expiry'] or 0) >= min_days), None)
    return {'funding_8h': perp.get('funding_8h'), 'perp_basis_pct': perp.get('basis_pct'),
            'front_annualized_basis_pct': front['annualized_basis_pct'] if front else None,
            'put_call_ratio': (options or {}).get('totals', {}).get('put_call_ratio'),
            'front_put_minus_call_iv': front_option['skew']['put_minus_call_iv'] if front_option else None}


def crowding_flags(options: dict | None, futures: dict | None) -> list[dict]:
    """Config-driven labels; `points` is the contrarian crowding contribution, 0 for descriptive-only flags."""
    metrics = _metrics(options, futures, rules()['positioning']['front_min_days'])
    flags = []
    for rule in rules()['positioning']['crowding']:
        value = metrics.get(rule['metric'])
        if value is not None and OPERATORS[rule['op']](value, rule['value']):
            flags.append({'flag': rule['flag'], 'metric': rule['metric'], 'value': value, 'threshold': rule['value'],
                          'side': rule['side'], 'points': rule['points']})
    return flags


def summarize(currency: str, option_rows: list[dict], future_rows: list[dict], now: datetime,
              as_of: datetime | None) -> dict:
    """Full positioning snapshot; stale or absent inputs are reported as such, never as a measured neutral. A snapshot
    written after the caller took `now` (a cycle or page read in progress) is at most future_tolerance_seconds ahead
    and counts as age 0; one further ahead is a clock problem and never fresh."""
    config = rules()['positioning']
    age = (aware(now) - aware(as_of)).total_seconds() if as_of else None
    if age is not None and -config['future_tolerance_seconds'] <= age < 0:
        age = 0.0
    fresh = age is not None and 0 <= age <= config['max_age_seconds']
    options = option_positioning(option_rows, now) if option_rows else None
    futures = futures_positioning(future_rows, currency, now) if future_rows else None
    return {'currency': currency, 'as_of': aware(as_of).isoformat() if as_of else None, 'age_seconds': age,
            'freshness_status': 'FRESH' if fresh else 'STALE' if as_of else 'MISSING', 'options': options,
            'futures': futures, 'crowding': crowding_flags(options, futures), 'directional_score': 0,
            'label': 'descriptive', 'reason': DESCRIPTIVE}


def positioning_component(summary: dict | None) -> dict:
    """Decision component {value, status, basis}: contrarian crowding points only, from fresh data only."""
    if not summary or summary.get('freshness_status') != 'FRESH' or not (summary.get('options') or summary.get('futures')):
        return {'value': None, 'status': 'missing', 'basis': 'Fresh derivatives positioning unavailable'}
    cap = rules()['positioning']['component_cap']
    scored = [f for f in summary['crowding'] if f['points']]
    value = max(-cap, min(cap, sum(f['points'] for f in scored)))
    basis = ', '.join(f"{f['flag']} ({f['metric']} {f['value']:.6g})" for f in scored) or 'No crowding threshold crossed'
    return {'value': value, 'status': 'measured', 'basis': f'Crowding (contrarian): {basis}; calls vs puts never set direction'}


def option_strike_levels(summary: dict | None, top: int | None = None) -> list[dict]:
    """Largest OI strikes per the nearest expiries, for the technical level engine."""
    options = (summary or {}).get('options') or {}
    top = top or rules()['technical']['options_top_strikes']
    by_strike: dict = {}
    for expiry in options.get('expiries', [])[:2]:
        for strike, oi in expiry['strike_concentrations']:
            item = by_strike.setdefault(strike, {'strike': strike, 'open_interest': 0.0, 'expiries': []})
            item['open_interest'] += oi  # one level per strike, OI summed across the nearest expiries
            item['expiries'].append(expiry['expiry'])
    levels = [dict(item, expiry=item['expiries'][0]) for item in by_strike.values()]
    return sorted(levels, key=lambda level: level['open_interest'], reverse=True)[:top]


def stored_rows(store: Store, currency: str) -> tuple[list[dict], list[dict], datetime | None]:
    """derivative_metrics rows for one currency split into options and futures, each limited to its latest snapshot
    (rows ingested within snapshot_window_seconds of that group's newest row). Rows left over from delisted or
    expired instruments are dropped rather than mixed in. `as_of` is the older of the two snapshot times."""
    cls = RECORDS['derivative_metrics']
    with store.session() as session:
        rows: list[Any] = list(session.scalars(select(cls).where(cls.id.startswith(f'{currency}-', autoescape=True))))
        payloads = [row.payload for row in rows if (row.payload or {}).get('ingested_at')]
    config = rules()['positioning']
    window, hour = config['snapshot_window_seconds'], config['expiry_hour_utc']
    groups: tuple[list[dict], list[dict]] = ([], [])
    for payload in payloads:
        groups[0 if parse_option(str(payload.get('instrument_name', '')), hour) else 1].append(payload)
    snapshots: list[list[dict]] = []
    latest: list[datetime] = []
    for group in groups:
        newest = max((aware(p['ingested_at']) for p in group), default=None)
        snapshots.append([p for p in group if newest and (newest - aware(p['ingested_at'])).total_seconds() <= window])
        latest += [newest] if newest else []
    return snapshots[0], snapshots[1], min(latest) if latest else None


def expired_instruments(rows: list[dict], now: datetime) -> list[str]:
    """Instrument names whose expiry has passed, for pruning derivative_metrics."""
    names, hour = [], rules()['positioning']['expiry_hour_utc']
    for row in rows:
        name = str(row.get('instrument_name', ''))
        parts = name.split('-')
        expiry = parse_expiry(parts[1], hour) if len(parts) >= 2 and not name.endswith('PERPETUAL') else None
        if expiry and expiry <= aware(now):
            names.append(name)
    return names
