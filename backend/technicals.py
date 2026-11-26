"""Calculated technical levels. The LLM may explain these levels; it never invents them.

Bars are dicts {time, timestamp, open, high, low, close, volume}. `volume` is traded quantity, or None when the feed
carries no trade volume; it is never invented, and volume-based levels are then absent.
"""
import math
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select

from backend import sessions
from backend.config import freshness_seconds, rules, rules_view
from backend.database import Store, Tick
from backend.engines import aware

MINUTE, HOUR, DAY = 60, 3600, 86400
DAILY_CLASSES = ('DAILY_CLOSE', 'DERIVED_DAILY')  # tick data classes of official (or derived) daily closes


def _floor(at: datetime, seconds: int) -> datetime:
    at = aware(at)
    return datetime.fromtimestamp(at.timestamp() // seconds * seconds, tz=at.tzinfo)


def _bar(start: datetime, price: float, volume: float | None) -> dict:
    stamp = start.isoformat()
    return {'time': stamp, 'timestamp': stamp, 'open': price, 'high': price, 'low': price, 'close': price, 'volume': volume}


def _add_volume(total: float | None, volume: float | None) -> float | None:
    return total if volume is None else (total or 0.0) + volume


def aggregate_ticks(ticks: list[dict], seconds: int = MINUTE) -> list[dict]:
    """Ticks {timestamp, price, quantity|None} -> bars. Volume sums traded quantities only (None if none were traded)."""
    buckets: dict[int, dict] = {}
    for at, tick in sorted(((aware(t['timestamp']), t) for t in ticks), key=lambda pair: pair[0]):
        price, quantity = tick['price'], tick.get('quantity')
        if not isinstance(price, (int, float)) or not math.isfinite(price):
            continue
        key = int(at.timestamp() // seconds)
        bar = buckets.get(key)
        if bar is None:
            buckets[key] = _bar(datetime.fromtimestamp(key * seconds, tz=at.tzinfo), price, quantity)
            continue
        if price > bar['high']:
            bar['high'] = price
        elif price < bar['low']:
            bar['low'] = price
        bar['close'] = price
        bar['volume'] = _add_volume(bar['volume'], quantity)
    return list(buckets.values())


def resample(bars: list[dict], seconds: int) -> list[dict]:
    """1m -> 1h -> 1d on UTC boundaries."""
    buckets: dict[datetime, dict] = {}
    for bar in sorted(bars, key=lambda b: aware(b['time'])):
        start = _floor(bar['time'], seconds)
        out = buckets.get(start)
        if out is None:
            buckets[start] = dict(_bar(start, bar['open'], bar.get('volume')), high=bar['high'], low=bar['low'], close=bar['close'])
            continue
        out.update(high=max(out['high'], bar['high']), low=min(out['low'], bar['low']), close=bar['close'],
                   volume=_add_volume(out['volume'], bar.get('volume')))
    return list(buckets.values())


def completed(bars: list[dict], seconds: int, now: datetime) -> list[dict]:
    return [b for b in bars if aware(b['time']) + timedelta(seconds=seconds) <= aware(now)]


def high_low(bars: list[dict]) -> dict | None:
    return {'high': max(b['high'] for b in bars), 'low': min(b['low'] for b in bars), 'bars': len(bars)} if bars else None


def previous_day(daily: list[dict], now: datetime) -> dict | None:
    today = _floor(now, DAY)
    return high_low([b for b in daily if aware(b['time']) == today - timedelta(days=1)])


def previous_week(daily: list[dict], now: datetime, covered_from: datetime | None = None) -> dict | None:
    """Monday-to-Sunday UTC week before the current one; None when history starts inside that week."""
    today = _floor(now, DAY)
    this_week = today - timedelta(days=today.weekday())
    if covered_from and aware(covered_from) > this_week - timedelta(days=7):
        return None
    return high_low([b for b in daily if this_week - timedelta(days=7) <= aware(b['time']) < this_week])


def full_days(bars: list[dict], day_minutes: Any = None) -> list[dict]:
    """Daily bars aggregated from intraday bars, without the first day when history starts after its open.
    `day_minutes(day_start)` gives the one-minute bars a full day of the venue holds (None = unknown, no check): a day
    with fewer than technical.min_day_coverage of them is dropped, because the high and low of 76 recorded minutes are
    not that day's high and low."""
    first = min(aware(b['time']) for b in bars)
    days = [d for d in resample(bars, DAY) if aware(d['time']) >= first]
    if day_minutes is None:
        return days
    share = rules_view()['technical']['min_day_coverage']
    counts: dict[datetime, int] = {}
    for bar in bars:
        start = _floor(bar['time'], DAY)
        counts[start] = counts.get(start, 0) + 1
    kept = []
    for day in days:
        expected = day_minutes(aware(day['time']))
        if expected is None or counts.get(aware(day['time']), 0) >= share * expected:
            kept.append(day)
    return kept


def session_bars(bars: list[dict], start: datetime, now: datetime) -> list[dict]:
    return [b for b in bars if aware(start) <= aware(b['time']) <= aware(now)]


def vwap(bars: list[dict]) -> float | None:
    """Volume-weighted typical price; None unless traded volume exists."""
    volume = sum(b['volume'] for b in bars if b.get('volume'))
    if not volume:
        return None
    return sum((b['high'] + b['low'] + b['close']) / 3 * b['volume'] for b in bars if b.get('volume')) / volume


def current_run(bars: list[dict], max_gap: float) -> list[dict]:
    """The trailing bars without a hole: each one starts at most `max_gap` seconds after the bar before it. Bars on the
    far side of a hole (the app was stopped, the feed was down, the venue was closed) are another stretch of data."""
    if not bars:
        return []
    first = len(bars) - 1
    while first > 0 and (aware(bars[first]['time']) - aware(bars[first - 1]['time'])).total_seconds() <= max_gap:
        first -= 1
    return bars[first:]


def atr(bars: list[dict], period: int, max_gap: float | None = None) -> float | None:
    """Mean true range of the last `period` bar pairs. With `max_gap` (intraday bars) only the current run counts
    (current_run): a true range measured across a hole would book the whole move made while no bar was recorded as one
    bar's range, so after a restart the value is None until the run holds period + 1 bars."""
    if max_gap is not None:
        bars = current_run(bars, max_gap)
    ranges = [max(b['high'] - b['low'], abs(b['high'] - p['close']), abs(b['low'] - p['close']))
              for p, b in zip(bars, bars[1:], strict=False)]
    return sum(ranges[-period:]) / len(ranges[-period:]) if len(ranges) >= period else None


def swings(bars: list[dict], lookback: int, keep: int) -> dict[str, list[dict]]:
    """Fractal pivots: a high (low) strictly above (below) `lookback` bars on each side."""
    highs, lows = [], []
    for i in range(lookback, len(bars) - lookback):
        around = bars[i - lookback:i] + bars[i + 1:i + 1 + lookback]
        if all(bars[i]['high'] > b['high'] for b in around):
            highs.append({'price': bars[i]['high'], 'time': bars[i]['time']})
        if all(bars[i]['low'] < b['low'] for b in around):
            lows.append({'price': bars[i]['low'], 'time': bars[i]['time']})
    return {'highs': highs[-keep:], 'lows': lows[-keep:]}


def volume_profile(bars: list[dict], bins: int, value_area_pct: float) -> dict | None:
    """Traded volume by typical-price bin: point of control and the value area around it."""
    traded = [b for b in bars if b.get('volume')]
    if not traded:
        return None
    low, high = min(b['low'] for b in traded), max(b['high'] for b in traded)
    width = (high - low) / bins if high > low else 0
    histogram = [0.0] * bins
    for bar in traded:
        typical = (bar['high'] + bar['low'] + bar['close']) / 3
        histogram[min(bins - 1, int((typical - low) / width)) if width else 0] += bar['volume']
    total, poc = sum(histogram), max(range(bins), key=lambda i: histogram[i])
    left = right = poc
    covered = histogram[poc]
    while covered < total * value_area_pct / 100 and (left > 0 or right < bins - 1):
        below = histogram[left - 1] if left > 0 else -1
        above = histogram[right + 1] if right < bins - 1 else -1
        if above >= below:
            right += 1
            covered += above
        else:
            left -= 1
            covered += below
    center = low + (poc + 0.5) * width
    return {'poc': center, 'value_area_low': low + left * width, 'value_area_high': low + (right + 1) * width,
            'value_area_pct': value_area_pct, 'bins': bins, 'total_volume': total}


def _nearest(candidates: list[tuple[str, float]], price: float) -> dict[str, Any]:
    below = [(n, v) for n, v in candidates if v < price]
    above = [(n, v) for n, v in candidates if v > price]
    touched = [(n, v) for n, v in candidates if v == price]
    below = below or touched
    above = above or touched
    support = max(below, key=lambda c: c[1]) if below else None
    resistance = min(above, key=lambda c: c[1]) if above else None
    return {'support': support[1] if support else None, 'support_basis': support[0] if support else None,
            'resistance': resistance[1] if resistance else None, 'resistance_basis': resistance[0] if resistance else None}


def reference_session(now: datetime, zone: ZoneInfo) -> date:
    """The session date levels are computed for: today in the venue's timezone, or the coming Monday on a weekend
    (so on Saturday the previous day is Friday and the previous week is the one just ended)."""
    today = aware(now).astimezone(zone).date()
    return today + timedelta(days=7 - today.weekday()) if today.weekday() >= 5 else today


def stored_daily(store: Store, symbol: str, now: datetime) -> list[dict]:
    """Stored daily bars (ohlc_bars interval 86400) keyed by their NY session date. The date is read at start + 12 h,
    which gives the same session date whether the row was aligned on New York or on UTC midnight."""
    config = rules()['technical']['daily_bars']
    zone = ZoneInfo(config['timezone'])
    rows = store.bars(symbol, DAY, aware(now) - timedelta(days=config['lookback_days']), aware(now))
    return [dict(row, session_date=(aware(row['start']) + timedelta(hours=12)).astimezone(zone).date().isoformat())
            for row in rows]


def daily_basis(daily: list[dict]) -> str:
    """'daily bars: Alpaca IEX' from the bars' provider column (config labels), else 'stored daily bars'."""
    labels = rules()['technical']['daily_bars']['labels']
    providers = sorted({labels.get(b.get('provider') or '', b.get('provider') or '') for b in daily} - {''})
    return f"daily bars: {', '.join(providers)}" if providers else 'stored daily bars'


def session_levels(daily: list[dict], now: datetime) -> dict[str, Any]:
    """Previous day (high/low/close), previous Monday-Friday week, 20/50-day ranges, daily ATR and SMA20/SMA50 from
    stored daily bars, using only sessions before the reference session (a forming day never defines a level)."""
    config = rules()['technical']
    zone = ZoneInfo(config['daily_bars']['timezone'])
    ref = reference_session(now, zone)
    done = sorted((b for b in daily if b['session_date'] < ref.isoformat()), key=lambda b: b['session_date'])
    week_start = (ref - timedelta(days=ref.weekday() + 7)).isoformat()
    week_end = (ref - timedelta(days=ref.weekday())).isoformat()
    week = [b for b in done if week_start <= b['session_date'] < week_end]
    partial = bool(done) and done[0]['session_date'] > week_start  # history starts inside that week
    result: dict[str, Any] = {
        'previous_day': dict(high_low(done[-1:]) or {}, close=done[-1]['close'], date=done[-1]['session_date'])
        if done else None,
        'previous_week': dict(high_low(week) or {}, start=week[0]['session_date'], end=week[-1]['session_date'])
        if week and not partial else None,
        'atr_daily': atr(done, config['atr_period']), 'daily_basis': daily_basis(done) if done else None,
        'daily_sessions': len(done), 'reference_session': ref.isoformat()}
    for days in config['range_days']:
        if len(done) >= days:
            result[f'range_{days}d'] = high_low(done[-days:])
    for days in config['daily_bars']['sma_days']:
        result[f'sma_{days}'] = sum(b['close'] for b in done[-days:]) / days if len(done) >= days else None
    return result


def levels(bars: list[dict], now: datetime, price: float | None = None, daily: list[dict] | None = None,
           session_start: datetime | None = None, option_strikes: list[dict] | None = None,
           day_minutes: Any = None) -> dict:
    """All computable levels from 1m bars (plus optional daily bars and option strikes). Absent inputs mean absent
    levels. Support/resistance are the nearest computed levels below/above price, not window extremes.
    Stored daily bars (rows with a session_date, see stored_daily) are keyed by NY session date and add SMA20/SMA50;
    other daily bars keep the UTC-day behaviour.
    Bars separated by a hole longer than technical.max_gap_seconds are never read as one series: the intraday ATR and
    the session range, VWAP and volume profile use the current run only (current_run), the session levels become
    support/resistance candidates from technical.session_min_bars bars on, and a day aggregated from intraday bars
    counts only with enough of its minutes (full_days, `day_minutes`)."""
    config = rules()['technical']
    bars = sorted(bars, key=lambda b: aware(b['time']))
    stored = bool(daily) and all('session_date' in b for b in daily or [])
    if not bars and not stored:
        return {}
    now = aware(now)
    result: dict[str, Any]
    if stored:
        by_session = session_levels(daily or [], now)
        basis = by_session['daily_basis'] or 'stored daily bars (none completed)'
        result = dict(by_session, scope=f'{len(bars)} one-minute bars; daily from {basis}')
        last_close = by_session['previous_day']['close'] if by_session['previous_day'] else None
        price = price if price is not None else bars[-1]['close'] if bars else last_close
    else:
        price = price if price is not None else bars[-1]['close']
        daily_source = 'daily bars' if daily else 'aggregated intraday bars (partial first day dropped)'
        covered_from: datetime | None = None
        if not daily:
            daily = full_days(bars, day_minutes)
            covered_from = aware(daily[0]['time']) if daily else now
        daily = completed(sorted(daily, key=lambda b: aware(b['time'])), DAY, now)
        result = {'scope': f'{len(bars)} one-minute bars; daily from {daily_source}',
                  'previous_day': previous_day(daily, now), 'previous_week': previous_week(daily, now, covered_from),
                  'atr_daily': atr(daily, config['atr_period'])}
        for days in config['range_days']:
            if len(daily) >= days:
                result[f'range_{days}d'] = high_low(daily[-days:])
    start = aware(session_start) if session_start else _floor(now, DAY)
    gap = config['max_gap_seconds']
    in_window = session_bars(bars, start, now)
    # Bars before a hole are not this session's range: the high and low start again where the data resumed.
    session = current_run(in_window, gap)
    formed = len(session) >= config['session_min_bars']
    swing = swings(resample(bars, config['swing_bar_seconds']), config['swing_lookback'], config['swing_max'])
    result.update({
        'price': price, 'observations': len(bars),
        'session': dict(high_low(session) or {}, start=start.isoformat(), anchor='caller session' if session_start else 'UTC day',
                        since=session[0]['time'], bars_before_gap=len(in_window) - len(session), formed=formed)
        if session else None,
        'vwap': vwap(session), 'vwap_anchor': start.isoformat(),
        'atr': atr(bars, config['atr_period'], gap) if len(bars) >= config['min_bars'] else None,
        'swings': swing, 'volume_profile': volume_profile(session, config['profile_bins'], config['value_area_pct']),
        'options': option_strikes or [], 'volume_available': any(b.get('volume') for b in bars)})
    if price is None:
        return result
    candidates: list[tuple[str, float]] = []
    for name in ['previous_day', 'previous_week', 'session', *[f'range_{d}d' for d in config['range_days']]]:
        if name == 'session' and not formed:
            continue  # the high and low of a few minutes are not a session range
        if result.get(name) and 'high' in result[name]:
            candidates += [(f'{name}_high', result[name]['high']), (f'{name}_low', result[name]['low'])]
    candidates += [('swing_high', s['price']) for s in swing['highs']] + [('swing_low', s['price']) for s in swing['lows']]
    candidates += [(f'sma_{d}', result[f'sma_{d}']) for d in config['daily_bars']['sma_days'] if result.get(f'sma_{d}') is not None]
    if result['vwap'] is not None and formed:
        candidates.append(('vwap', result['vwap']))
    if result['volume_profile'] and formed:
        profile = result['volume_profile']
        candidates += [('poc', profile['poc']), ('value_area_low', profile['value_area_low']),
                       ('value_area_high', profile['value_area_high'])]
    candidates += [('option_strike', float(s['strike'])) for s in result['options']]
    return dict(result, **_nearest(candidates, price))


BOUNDARIES = ('support', 'support_basis', 'resistance', 'resistance_basis')


def _span(seconds: float) -> str:
    return f'{seconds / 3600:.1f} h' if seconds >= 3600 else f'{seconds / 60:.0f} min'


def levels_state(bars: list[dict], now: datetime) -> dict:
    """Whether completed one-minute bars (oldest first) are current enough to test a live price against levels anchored
    to their last close: the last bar ended at most technical.max_gap_seconds ago and the current run (current_run)
    holds at least technical.min_fresh_bars bars. After a restart, a feed outage or a session open the answer is no
    until that many current bars exist: {ready, reason, fresh_bars, min_fresh_bars, reference_age_seconds,
    max_gap_seconds}."""
    config = rules_view()['technical']
    gap, needed = config['max_gap_seconds'], config['min_fresh_bars']
    state: dict[str, Any] = {'ready': False, 'reason': None, 'fresh_bars': 0, 'min_fresh_bars': needed,
                             'reference_age_seconds': None, 'max_gap_seconds': gap}
    if not bars:
        return dict(state, reason='No one-minute bars: no current reference to test a price against')
    age = (aware(now) - aware(bars[-1]['time'])).total_seconds() - MINUTE
    state['reference_age_seconds'] = round(max(0.0, age), 1)
    if age > gap:
        return dict(state, reason=f'No current minute bar: the last one ended {_span(age)} ago '
                                  f'(limit {_span(gap)}); levels wait for {needed} current bars')
    run = len(current_run(bars, gap))
    state['fresh_bars'] = run
    if run < needed:
        return dict(state, reason=f'Levels rebuilding after a data gap: {run} of {needed} current minute bars')
    return dict(state, ready=True)


def withhold(levels_: dict, reason: str, **detail: Any) -> dict:
    """The same levels without a support/resistance pair: nothing may test a price against them (the technical
    component reads 'missing' with this reason; no level-break alert and no lean can come from them). The withheld pair
    is kept under state.withheld for inspection."""
    kept = {key: levels_.get(key) for key in BOUNDARIES}
    state = dict(levels_.get('state') or {}, ready=False, reason=reason, **detail)
    if any(value is not None for value in kept.values()):
        state['withheld'] = kept
    return dict(levels_, **dict.fromkeys(BOUNDARIES), state=state)


def _asset_class(symbol: str) -> str:
    return next((str(kind) for name, _, kind in rules_view()['assets'] if name == symbol), 'unknown')


def asset_levels(store: Store, symbol: str, bars: list[dict], now: datetime, price: float | None = None,
                 option_strikes: list[dict] | None = None, keep_closed: bool = False) -> dict:
    """The levels of one asset, shared by the asset page and the decision cycle so both show the same numbers:
    completed one-minute bars plus stored daily bars keyed by NY session date. Levels are selected around the last
    completed close, independently of the live price tested for a break. A proxy never supplies either.
    The pair is published only while the bars are current (levels_state); otherwise support/resistance are None and
    state.reason says why (withhold). keep_closed (the asset page only): while the venue is closed and the last bar is
    the end of its latest session, the pair anchored to that close is shown for reading, marked state.ready False;
    nothing is tested against it because a closed venue has no live price (break_price)."""
    now = aware(now)
    bars = sorted(completed(bars, MINUTE, now), key=lambda b: aware(b['time']))
    daily = stored_daily(store, symbol, now)
    if not bars and not daily:
        return {}
    kind = _asset_class(symbol)
    # Passing the current quote to _nearest would always put it inside its own range (or remove one boundary).
    result = levels(bars, now, daily=daily or None, option_strikes=option_strikes,
                    day_minutes=lambda day: sessions.day_minutes(kind, day, symbol))
    result['reference'] = {'price': result.get('price'), 'basis': 'last completed minute close' if bars else 'previous daily close',
                           'time': bars[-1]['time'] if bars else (result.get('previous_day') or {}).get('date')}
    result['scope'] += '; support/resistance anchored to ' + result['reference']['basis']
    if price is not None:
        result['price'] = price
    if not bars:  # daily bars only: there is no live series, so nothing is ever tested against these levels
        return result
    state = levels_state(bars, now)
    result['state'] = state
    result['reference']['age_seconds'] = state['reference_age_seconds']
    if state['ready']:
        return result
    if keep_closed and sessions.market_state(kind, now, symbol) != 'LIVE':
        closed = sessions.previous_close(kind, now, symbol)
        ended = aware(bars[-1]['time']) + timedelta(seconds=MINUTE)
        if closed is not None and (closed - ended).total_seconds() <= state['max_gap_seconds']:
            result['state'] = dict(state, basis='last session', reason='Market closed: levels as of the last session close')
            return result
    return withhold(result, state['reason'])


def is_live(data_class: str | None, daily_close: bool | None) -> bool:
    """A live print, not an official daily close (data_class DAILY_CLOSE / DERIVED_DAILY, or the daily_close flag of
    ticks stored before data_class existed)."""
    return data_class in {None, 'LIVE'} and not daily_close


def is_live_quote(quote: dict | None) -> bool:
    """Eligible level metadata, including legacy stub providers; freshness and session are checked separately."""
    return bool(quote and not quote.get('stub')
                and not str(quote.get('provider') or '').endswith(rules_view()['market_data']['exclude_provider_suffix'])
                and is_live(quote.get('data_class'), quote.get('daily_close')))


def break_price(quote: dict | None) -> float | None:
    """The price a level break may be tested with: a live print (never a daily close, a derived close or a stub) that
    is FRESH while its venue is open. Anything else gives None: a close compared with intraday levels is not a break."""
    if not quote or not is_live_quote(quote) or quote.get('freshness_status') != 'FRESH' \
            or quote.get('market_status') != 'LIVE':
        return None
    price = quote.get('price')
    return float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) else None


def quote_freshness(provider: str | None, stamp: datetime, now: datetime, default_seconds: float,
                    future_seconds: float) -> dict:
    """The freshness verdict of one live observation: {age_seconds, threshold_seconds, fresh}. The limit is the
    provider's (config.freshness_seconds, the one lookup). A tick stamped up to `future_seconds` after `now` (the
    exchange's clock, or a tick stored between the cycle taking its time and reading the quote) has age 0 and is
    fresh; one stamped further ahead is a clock problem and never fresh (its negative age is reported)."""
    age = (aware(now) - aware(stamp)).total_seconds()
    threshold = freshness_seconds(provider, default_seconds)
    if -future_seconds <= age < 0:
        age = 0.0
    return {'age_seconds': round(age, 2), 'threshold_seconds': threshold, 'fresh': 0 <= age <= threshold}


def live_tick_filter() -> Any:
    """One SQL eligibility rule for reactions, anomalies and execution outcomes, including legacy live rows."""
    stub = Tick.payload['stub'].as_boolean()
    daily = Tick.payload['daily_close'].as_boolean()
    klass = Tick.payload['data_class'].as_string()
    return and_(~Tick.provider.endswith(rules_view()['market_data']['exclude_provider_suffix'], autoescape=True),
                or_(stub.is_(None), stub.is_(False)), or_(daily.is_(None), daily.is_(False)),
                or_(klass.is_(None), klass == 'LIVE'))


def ticks(store: Store, symbol: str, since: datetime, now: datetime, live_only: bool = False) -> list[dict]:
    """Ticks known at `now` (stubs excluded), each flagged `live` (is_live); quantity only from per-trade fields, never
    24h cumulative volume. `live_only` drops official daily closes: one close per day is not a one-minute bar.
    The payload fields are extracted by the database, so the full JSON payloads are never decoded here."""
    suffix = rules()['market_data']['exclude_provider_suffix']
    quantity, stub = Tick.payload['trade_quantity'].as_float(), Tick.payload['stub'].as_boolean()
    klass, daily = Tick.payload['data_class'].as_string(), Tick.payload['daily_close'].as_boolean()
    with store.session() as session:
        rows = session.execute(select(Tick.timestamp, Tick.price, quantity, stub, klass, daily).where(Tick.symbol == symbol,
            Tick.timestamp >= aware(since), Tick.timestamp <= aware(now), Tick.ingested_at <= aware(now),
            ~Tick.provider.endswith(suffix, autoescape=True)).order_by(Tick.timestamp))
        # UTCDateTime already returns aware UTC timestamps
        found = [{'timestamp': t, 'price': p, 'quantity': q, 'live': is_live(c, d)}
                 for t, p, q, is_stub, c, d in rows if not is_stub]
    return [t for t in found if t['live']] if live_only else found
