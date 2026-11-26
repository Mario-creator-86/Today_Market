from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.database import Bar, Store, Tick
from backend.technicals import (
    aggregate_ticks,
    atr,
    levels,
    previous_week,
    resample,
    session_levels,
    stored_daily,
    swings,
    ticks,
    volume_profile,
    vwap,
)

NOW = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)  # Wednesday


def minute_bars(start, closes, volume=1.0):
    bars = []
    for i, close in enumerate(closes):
        stamp = (start + timedelta(minutes=i)).isoformat()
        bars.append({'time': stamp, 'timestamp': stamp, 'open': close, 'high': close + 0.5, 'low': close - 0.5,
                     'close': close, 'volume': volume})
    return bars


def daily_bars(days, end):
    out = []
    for i in range(days):
        stamp = (end - timedelta(days=days - i)).isoformat()
        out.append({'time': stamp, 'timestamp': stamp, 'open': 100 + i, 'high': 105 + i, 'low': 95 + i, 'close': 100 + i,
                    'volume': None})
    return out


def test_tick_aggregation_never_invents_volume():
    quotes = [{'timestamp': NOW + timedelta(seconds=s), 'price': p} for s, p in [(1, 100), (20, 102), (59, 101), (61, 99)]]
    bars = aggregate_ticks(quotes)
    assert [(b['open'], b['high'], b['low'], b['close'], b['volume']) for b in bars] == [(100, 102, 100, 101, None),
                                                                                         (99, 99, 99, 99, None)]
    trades = [dict(q, quantity=0.5) for q in quotes]
    assert [b['volume'] for b in aggregate_ticks(trades)] == [1.5, 0.5]
    hourly = resample(minute_bars(NOW, [100 + i for i in range(120)]), 3600)
    assert len(hourly) == 2 and hourly[0]['close'] == 159 and hourly[0]['volume'] == 60 and hourly[1]['open'] == 160


def test_vwap_atr_and_swings():
    bars = minute_bars(NOW, [100, 101, 102, 103, 102, 101, 100, 99, 98, 99, 100])
    assert vwap(bars) == pytest.approx(sum(b['close'] for b in bars) / len(bars))
    assert vwap([dict(b, volume=None) for b in bars]) is None
    assert atr(bars, 5) == pytest.approx(1.5) and atr(bars[:3], 5) is None
    points = swings(bars, 2, 5)
    assert [s['price'] for s in points['highs']] == [103.5] and [s['price'] for s in points['lows']] == [97.5]


def test_volume_profile_point_of_control_and_value_area():
    bars = minute_bars(NOW, [100] * 8 + [110] * 2 + [90])
    profile = volume_profile(bars, 10, 70)
    assert profile['value_area_low'] <= 100 <= profile['value_area_high'] and abs(profile['poc'] - 100) <= 2
    assert profile['value_area_high'] < 110 and profile['total_volume'] == 11
    assert volume_profile([dict(b, volume=None) for b in bars], 10, 70) is None


def test_previous_week_uses_calendar_week():
    daily = daily_bars(14, datetime(2026, 3, 18, tzinfo=UTC))
    week = previous_week(daily, NOW)
    assert week['bars'] == 7 and week['low'] == 95 + 5 and week['high'] == 105 + 11


def test_levels_combine_sources_and_pick_nearest_support_resistance():
    intraday = minute_bars(NOW - timedelta(hours=2), [100 + (i % 7) for i in range(120)])
    daily = daily_bars(25, datetime(2026, 3, 18, tzinfo=UTC))
    strikes = [{'strike': 110000 / 1000, 'open_interest': 500, 'expiry': '2026-03-27T08:00:00+00:00'}]
    result = levels(intraday, NOW, price=104.2, daily=daily, option_strikes=strikes)
    assert result['previous_day'] == {'high': 129, 'low': 119, 'bars': 1}
    assert result['range_20d'] == {'high': 129, 'low': 100, 'bars': 20} and 'range_50d' not in result
    assert result['session']['anchor'] == 'UTC day' and result['vwap'] is not None and result['atr'] is not None
    assert result['support'] < 104.2 < result['resistance'] and result['resistance_basis']
    assert result['options'] == strikes and result['volume_available']


def test_levels_without_volume_or_history_leave_levels_absent():
    intraday = [dict(b, volume=None) for b in minute_bars(NOW - timedelta(minutes=30), [100 + (i % 5) for i in range(30)])]
    result = levels(intraday, NOW)
    assert result['vwap'] is None and result['volume_profile'] is None and not result['volume_available']
    assert result['previous_day'] is None and 'range_20d' not in result
    assert levels([], NOW) == {}


def test_ticks_reader_uses_trade_quantity_only(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/ticks.db')
    store.initialize()
    with store.session() as session:
        session.add(Tick(id='a', symbol='BTC', price=100, provider='t', timestamp=NOW, ingested_at=NOW,
                         payload={'volume': 12345.0, 'trade_quantity': 0.2}))
        session.add(Tick(id='b', symbol='BTC', price=101, provider='t', timestamp=NOW + timedelta(seconds=5),
                         ingested_at=NOW + timedelta(seconds=5), payload={'volume': 12346.0}))
        session.commit()
    rows = ticks(store, 'BTC', NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
    assert [r['quantity'] for r in rows] == [0.2, None]
    assert aggregate_ticks(rows)[0]['volume'] == 0.2


def test_aggregate_ticks_orders_and_buckets_on_utc_minutes():
    rows = [{'timestamp': NOW + timedelta(seconds=70), 'price': 103.0, 'quantity': None},
            {'timestamp': (NOW + timedelta(seconds=5)).astimezone(ZoneInfo('America/New_York')), 'price': 99.0,
             'quantity': 1.0},
            {'timestamp': NOW + timedelta(seconds=1), 'price': 100.0, 'quantity': None},
            {'timestamp': NOW + timedelta(seconds=30), 'price': 101.0, 'quantity': 2.0},
            {'timestamp': NOW + timedelta(seconds=40), 'price': float('nan'), 'quantity': 5.0}]
    first, second = aggregate_ticks(rows)
    assert first == {'time': NOW.isoformat(), 'timestamp': NOW.isoformat(), 'open': 100.0, 'high': 101.0,
                     'low': 99.0, 'close': 101.0, 'volume': 3.0}
    assert second['time'] == (NOW + timedelta(minutes=1)).isoformat() and second['volume'] is None


def test_aggregated_history_drops_partial_first_day_and_week():
    partial = minute_bars(NOW - timedelta(hours=22), [100 + (i % 9) for i in range(22 * 60)])  # starts yesterday 14:00
    result = levels(partial, NOW)
    assert result['previous_day'] is None and result['previous_week'] is None
    full = minute_bars(datetime(2026, 3, 17, tzinfo=UTC), [100 + (i % 9) for i in range(36 * 60)])  # from 00:00
    assert levels(full, NOW)['previous_day'] == {'high': 108.5, 'low': 99.5, 'bars': 1}
    assert levels(full, NOW)['previous_week'] is None  # history starts inside the current week


def test_ticks_reader_excludes_stub_providers(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/stub.db')
    store.initialize()
    with store.session() as session:
        session.add(Tick(id='s', symbol='BTC', price=1, provider='massive-stub', timestamp=NOW, ingested_at=NOW,
                         payload={'stub': True}))
        session.add(Tick(id='f', symbol='BTC', price=2, provider='feed', timestamp=NOW, ingested_at=NOW,
                         payload={'stub': True}))
        session.commit()
    assert ticks(store, 'BTC', NOW - timedelta(minutes=1), NOW) == []


# --- stored daily bars (NY session dates) -----------------------------------------------------------------------------

NY = ZoneInfo('America/New_York')
SUNDAY = datetime(2026, 9, 27, 12, tzinfo=UTC)
HOLIDAYS = {date(2026, 7, 3), date(2026, 9, 7)}


def sessions_between(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5 and day not in HOLIDAYS:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def daily_store(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/daily.db')
    store.initialize()
    days = sessions_between(date(2026, 6, 15), date(2026, 9, 25))
    with store.session() as session:
        for i, day in enumerate(days):
            start = datetime.combine(day, time(0), NY).astimezone(UTC)
            session.add(Bar(symbol='NVDA', interval_seconds=86400, start=start, open=100 + i, high=102 + i, low=98 + i,
                            close=100 + i, volume=1000, ticks=1, provider='alpaca'))
        forming = datetime.combine(date(2026, 9, 28), time(0), NY).astimezone(UTC)  # Monday's bar, still forming
        session.add(Bar(symbol='NVDA', interval_seconds=86400, start=forming, open=1, high=999, low=1, close=999,
                        volume=10, ticks=1, provider='alpaca'))
        session.commit()
    return store, days


def test_daily_bars_monday_uses_friday_and_last_week(daily_store):
    store, days = daily_store
    monday = datetime(2026, 9, 28, 14, tzinfo=UTC)  # 10:00 New York
    daily = stored_daily(store, 'NVDA', monday)
    result = levels([], monday, price=170.0, daily=daily)
    last = len(days) - 1
    assert result['previous_day'] == {'high': 102 + last, 'low': 98 + last, 'bars': 1, 'close': 100 + last, 'date': '2026-09-25'}
    week = [i for i, d in enumerate(days) if date(2026, 9, 21) <= d <= date(2026, 9, 25)]
    assert result['previous_week'] == {'high': 102 + week[-1], 'low': 98 + week[0], 'bars': 5,
                                       'start': '2026-09-21', 'end': '2026-09-25'}
    closes = [100 + i for i in range(len(days))]
    assert result['sma_20'] == pytest.approx(sum(closes[-20:]) / 20)
    assert result['sma_50'] == pytest.approx(sum(closes[-50:]) / 50)
    assert result['daily_basis'] == 'daily bars: Alpaca IEX' and 'Alpaca IEX' in result['scope']
    assert result['range_20d']['high'] == 102 + last and result['range_50d']['high'] != 999  # forming day excluded
    assert result['support'] is not None or result['resistance'] is not None


def test_daily_bars_weekend_belongs_to_the_coming_week(daily_store):
    store, _ = daily_store
    saturday = datetime(2026, 9, 26, 15, tzinfo=UTC)
    result = session_levels(stored_daily(store, 'NVDA', saturday), saturday)
    assert result['reference_session'] == '2026-09-28' and result['previous_day']['date'] == '2026-09-25'
    assert (result['previous_week']['start'], result['previous_week']['end']) == ('2026-09-21', '2026-09-25')


def test_utc_aligned_daily_rows_keep_their_session_date(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/utc.db')
    store.initialize()
    with store.session() as session:
        session.add(Bar(symbol='AAPL', interval_seconds=86400, start=datetime(2026, 9, 25, tzinfo=UTC), open=1, high=2,
                        low=1, close=2, volume=0, ticks=0, provider=''))
        session.commit()
    rows = stored_daily(store, 'AAPL', SUNDAY)
    assert rows[0]['session_date'] == '2026-09-25'
    assert session_levels(rows, SUNDAY)['daily_basis'] == 'stored daily bars'
