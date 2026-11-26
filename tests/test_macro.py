from datetime import UTC, datetime, timedelta

import pytest

from backend.database import Store
from backend.macro import indicator_key, record_release, release_surprise, surprise_history

T0 = datetime(2024, 1, 11, 13, 30, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    result = Store(f'sqlite:///{tmp_path}/macro.db')
    result.initialize()
    return result


def test_indicator_identity_is_stable_across_months():
    assert indicator_key('US', 'CPI YoY (Aug)') == indicator_key('us', 'CPI YoY (Sep)') == 'US:cpi:yoy'
    assert indicator_key('US', 'CPI MoM') != indicator_key('US', 'CPI YoY')


def test_first_known_actual_time_is_never_overwritten(store):
    row = {'name': 'CPI YoY', 'country': 'US', 'scheduled_at': T0.isoformat(), 'consensus': 3.0, 'actual': 3.2}
    first = record_release(store, row, now=T0 + timedelta(minutes=1))
    again = record_release(store, dict(row, actual=3.3), now=T0 + timedelta(days=2))
    assert first['actual_ingested_at'] == again['actual_ingested_at'] == (T0 + timedelta(minutes=1)).isoformat()
    assert again['raw_surprise'] == pytest.approx(0.3)


def test_z_score_uses_only_history_known_at_the_time(store):
    surprises = [0.1, -0.1, 0.2, -0.2, 0.0, 0.1, -0.1, 0.2, -0.2, 0.0, 0.1, -0.1, 0.2, -0.2, 0.0, 0.1, -0.1, 0.2, -0.2, 0.0]
    for index, value in enumerate(surprises):
        when = T0 + timedelta(days=30 * index)
        record_release(store, {'name': 'CPI YoY', 'country': 'US', 'scheduled_at': when.isoformat(), 'consensus': 3.0,
                               'actual': 3.0 + value}, now=when + timedelta(minutes=1))
    target = T0 + timedelta(days=30 * len(surprises))
    row = {'name': 'CPI YoY', 'country': 'US', 'scheduled_at': target.isoformat(), 'consensus': 3.0, 'actual': 3.3}
    result = release_surprise(store, row, now=target + timedelta(minutes=1))
    assert result['observations'] == 20 and result['z_score'] and result['z_score'] > 2
    # Replaying just before the last history item was ingested must not see it (no lookahead).
    last = T0 + timedelta(days=30 * 19)
    assert len(surprise_history(store, 'US:cpi:yoy', target, last)) == 19
    assert release_surprise(store, dict(row, actual=None)) is None
