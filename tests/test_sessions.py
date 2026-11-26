from datetime import UTC, datetime

import pytest

from backend.sessions import market_state, next_session_close, session_close, session_info


def at(*parts):
    return datetime(*parts, tzinfo=UTC)


@pytest.mark.parametrize('kind,moment,state', [
    ('equity', at(2026, 9, 25, 22, 30), 'AFTER HOURS'),     # Friday 18:30 New York
    ('equity', at(2026, 9, 26, 12), 'FRIDAY CLOSE'),        # Saturday
    ('equity', at(2026, 9, 28, 3), 'FRIDAY CLOSE'),         # Monday before pre-market
    ('equity', at(2026, 9, 28, 12), 'PREMARKET'),           # Monday 08:00 New York
    ('equity', at(2026, 9, 28, 15), 'LIVE'),
    ('equity', at(2026, 9, 24, 3), 'LAST CLOSE'),           # Thursday overnight
    ('equity', at(2026, 11, 26, 16), 'LAST CLOSE'),         # Thanksgiving holiday
    ('equity', at(2026, 11, 27, 18, 30), 'AFTER HOURS'),    # after the 13:00 early close
    ('index', at(2026, 9, 25, 22, 30), 'FRIDAY CLOSE'),     # cash index has no extended session
    ('fx', at(2026, 9, 26, 12), 'FRIDAY CLOSE'),
    ('fx', at(2026, 9, 27, 22), 'LIVE'),                    # Sunday 18:00 New York
    ('fx', at(2026, 12, 25, 12), 'LAST CLOSE'),             # configured holiday
    ('fx', at(2027, 1, 1, 12), 'LAST CLOSE'),
    ('crypto', at(2026, 9, 26, 12), 'LIVE'),
    ('commodity', at(2026, 9, 28, 21, 30), 'LAST CLOSE'),   # CME maintenance break 16:00-17:00 Chicago
    ('commodity', at(2026, 9, 28, 23, 30), 'LIVE'),
    ('bond', at(2026, 9, 26, 12), 'FRIDAY CLOSE'),
    ('unknown', at(2026, 9, 28, 15), 'UNKNOWN'),
])
def test_market_states(kind, moment, state):
    assert market_state(kind, moment) == state


def test_symbol_venues_override_asset_class():
    sunday_night = at(2026, 9, 28, 3)
    assert market_state('index', sunday_night) == 'FRIDAY CLOSE'
    assert market_state('index', sunday_night, 'DXY') == 'LIVE'
    assert session_info('index', sunday_night, 'DXY')['venue'] == 'ICE_US'
    assert market_state('index', at(2026, 9, 28, 20, 5), 'VIX') == 'LIVE'  # VIX regular session ends 16:15 New York


def test_session_closes():
    friday_evening = at(2026, 9, 25, 22, 30)
    assert session_close('equity', friday_evening) == at(2026, 9, 28, 20)
    assert next_session_close('equity', friday_evening) == at(2026, 9, 29, 20)
    assert session_close('equity', at(2026, 9, 28, 15)) == at(2026, 9, 28, 20)
    assert session_close('equity', at(2026, 11, 27, 15)) == at(2026, 11, 27, 18)
    assert session_close('fx', at(2026, 9, 28, 12)) == at(2026, 9, 28, 21)
    assert next_session_close('fx', at(2026, 9, 28, 12)) == at(2026, 9, 29, 21)
    assert session_close('fx', at(2026, 12, 25, 12)) == at(2026, 12, 28, 22)
    assert next_session_close('fx', at(2026, 9, 25, 20)) == at(2026, 9, 28, 21)
    assert session_close('crypto', at(2026, 9, 26, 12)) == at(2026, 9, 27)
    assert session_close('crypto', at(2026, 9, 27)) == at(2026, 9, 28)
    assert next_session_close('crypto', at(2026, 9, 26, 12)) == at(2026, 9, 28)


def test_unknown_or_out_of_calendar_range_is_never_guessed():
    assert session_close('unknown', at(2026, 9, 28)) is None
    assert next_session_close('unknown', at(2026, 9, 28)) is None
    far = at(2045, 1, 3, 15)
    assert market_state('equity', far) == 'UNKNOWN'
    assert session_close('equity', far) is None
    info = session_info('unknown', at(2026, 9, 28))
    assert info['state'] == 'UNKNOWN' and info['venue'] is None
