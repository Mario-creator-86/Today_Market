import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from backend import refresh, workers
from backend.database import Store, Tick

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)  # Wednesday; NYSE open 13:30-20:00 UTC in July


def row(name, minutes, impact=10, actual=None, row_id=None):
    return {'id': row_id or name, 'name': name, 'impact': impact, 'actual': actual,
            'scheduled_at': (NOW + timedelta(minutes=minutes)).isoformat()}


def test_every_section_10_cadence_is_configured():
    kinds = {c['kind']: c for c in refresh.cadence_table()}
    assert {'quotes', 'derived', 'derivatives', 'breaking_news', 'x_stream', 'x_search', 'hot_topics', 'global_sweep',
            'sec_filings', 'congress', 'central_banks', 'calendar', 'release_watch', 'interpretation', 'ui_quotes'} <= set(kinds)
    assert kinds['derived']['seconds'] == 5 and kinds['derivatives']['seconds'] == 15
    assert kinds['hot_topics']['seconds'] == 900 and kinds['global_sweep']['seconds'] == 300  # research planner: AI budget
    assert kinds['calendar']['seconds'] == 21600 and kinds['interpretation']['seconds'] == 300
    assert 30 <= kinds['x_search']['seconds'] <= 60 and kinds['ui_quotes']['seconds'] == 1
    assert kinds['quotes']['mode'] == 'websocket'


def test_central_bank_pages_tighten_within_fifteen_minutes_of_a_decision():
    fomc = [row('Fed Interest Rate Decision', 10)]
    assert refresh.next_interval('central_banks', NOW, fomc) == 30
    assert refresh.next_interval('central_banks', NOW + timedelta(minutes=24), fomc) == 30  # 14 min after
    assert refresh.next_interval('central_banks', NOW, [row('Fed Interest Rate Decision', 120)]) == 300
    assert refresh.next_interval('central_banks', NOW, [row('Fed Interest Rate Decision', 17)]) == 120  # until window opens
    assert refresh.next_interval('central_banks', NOW, [row('US CPI', 5)]) == 300
    assert refresh.next_interval('central_banks', NOW, [row('ECB Interest Rate Decision', 5, impact=1)]) == 300


def test_release_watch_starts_fifteen_minutes_before_and_polls_at_the_release_second():
    cpi = [row('US CPI', 20)]
    assert refresh.watch_state(NOW, cpi)['mode'] == 'NORMAL'
    assert refresh.watch_state(NOW, cpi)['next_watch_at'] == (NOW + timedelta(minutes=5)).isoformat()
    assert refresh.next_interval('calendar', NOW, cpi) == 300
    before = NOW + timedelta(minutes=10)
    state = refresh.watch_state(before, cpi)
    assert state['mode'] == 'RELEASE_WATCH' and state['events'][0]['phase'] == 'PRE_RELEASE'
    assert refresh.next_interval('release_watch', before, cpi) == 30
    assert refresh.next_interval('release_watch', NOW + timedelta(minutes=19, seconds=50), cpi) == 10
    after = NOW + timedelta(minutes=22)
    assert refresh.watch_state(after, cpi)['events'][0]['phase'] == 'AWAITING_ACTUAL'
    assert refresh.watch_state(after, [row('US CPI', 20, actual=3.1)])['events'][0]['phase'] == 'RELEASED'
    assert refresh.watch_state(NOW + timedelta(minutes=40), cpi)['mode'] == 'NORMAL'
    assert refresh.watch_state(before, [row('Minor', 20, impact=1)])['mode'] == 'NORMAL'
    assert refresh.next_interval('calendar', NOW, []) == 21600


def test_fixed_rate_scheduler_polls_right_after_the_release_time():
    cpi = [row('US CPI', 20)]
    last = NOW + timedelta(minutes=19, seconds=57)  # 3 s before the release
    at_release = NOW + timedelta(minutes=20, seconds=2)
    assert refresh.due('release_watch', at_release, last, cpi)  # crossed the scheduled second
    assert not refresh.due('release_watch', at_release + timedelta(seconds=5), at_release, cpi)
    assert not refresh.due('release_watch', at_release, last, [row('Minor', 20, impact=1)])


def test_market_hours_congress_and_interpretation_cadences():
    assert refresh.next_interval('sec_filings', NOW, us_open=True) == 60
    assert refresh.next_interval('sec_filings', NOW, us_open=False) == 900
    assert refresh.us_market_open(datetime(2026, 7, 1, 15, 0, tzinfo=UTC))
    assert not refresh.us_market_open(datetime(2026, 7, 4, 15, 0, tzinfo=UTC))  # Saturday
    bill = {'event_type': 'REGULATION', 'importance': 0.8, 'last_updated_utc': (NOW - timedelta(hours=2)).isoformat()}
    assert refresh.congress_active([bill], NOW)
    assert not refresh.congress_active([dict(bill, importance=0.2)], NOW)
    assert not refresh.congress_active([dict(bill, last_updated_utc=(NOW - timedelta(days=3)).isoformat())], NOW)
    assert refresh.next_interval('congress', NOW, congress_is_active=True) == 300
    assert refresh.next_interval('congress', NOW) == 1800
    assert refresh.next_interval('interpretation', NOW, markets_active=False) == 900
    assert refresh.due('hot_topics', NOW, None)
    assert not refresh.due('hot_topics', NOW, NOW - timedelta(seconds=30))
    assert refresh.due('hot_topics', NOW, (NOW - timedelta(seconds=900)).isoformat())


def event(event_id, status='UNCONFIRMED', kind='NEWS', importance=None, sources=()):
    return {'id': event_id, 'verification_status': status, 'event_type': kind, 'importance': importance,
            'sources': list(sources)}


def test_immediate_recalculation_triggers_cover_every_section_10_case():
    before = refresh.trigger_state([event('old')], [row('US CPI', -5)], {'BTC': {'change': 0.5, 'unit': 'pct'}},
                                   {'BTC': 'INSIDE_RANGE'}, [])
    assert refresh.detect_triggers(None, before) == []
    assert refresh.detect_triggers(before, before) == []
    events = [event('old', status='CONFIRMED'), event('war', kind='GEOPOLITICAL_ENERGY'),
              event('fed', kind='OFFICIAL_RELEASE', sources=['Federal Reserve']), event('filler', importance=0.1)]
    after = refresh.trigger_state(events, [row('US CPI', -5, actual=3.4)],
                                  {'BTC': {'change': 2.0, 'unit': 'pct'}, 'US10Y': {'change': 12.0, 'unit': 'bp'}},
                                  {'BTC': 'BELOW_SUPPORT'}, ['DIVERGENCE:BTC:x'])
    found = {(t['trigger'], t['id']) for t in refresh.detect_triggers(before, after)}
    assert found == {('FACT_STATUS_CHANGE', 'old'), ('BREAKING_EVENT', 'war'), ('CENTRAL_BANK_STATEMENT', 'fed'),
                     ('MACRO_RELEASE', 'US CPI'), ('LARGE_MOVE', 'BTC'), ('LARGE_MOVE', 'US10Y'),
                     ('TECHNICAL_BREAKOUT', 'BTC'), ('CROSS_ASSET_DIVERGENCE', 'DIVERGENCE:BTC:x')}
    # Large moves are edge-triggered: an ongoing move does not re-trigger every cycle.
    assert not any(t['trigger'] == 'LARGE_MOVE' for t in refresh.detect_triggers(after, after))


def test_backfilled_stale_events_do_not_trigger_a_recalculation():
    before = refresh.trigger_state([event('live')], [])
    backfill = event('old-release', status='STALE', kind='OFFICIAL_RELEASE', importance=1.0, sources=['Federal Reserve'])
    after = refresh.trigger_state([event('live', status='STALE'), backfill], [])
    assert refresh.detect_triggers(before, after) == []  # neither a backfill nor a fact ageing out is news
    revived = refresh.trigger_state([event('live', status='STALE'), dict(backfill, verification_status='CONFIRMED')], [])
    assert {(t['trigger'], t['id']) for t in refresh.detect_triggers(after, revived)} == {('FACT_STATUS_CHANGE', 'old-release')}


@pytest.mark.asyncio
async def test_recalc_requests_debounce_and_respect_the_minimum_interval():
    clock = [100.0]
    requests = refresh.RecalcRequests(min_interval=0.05, clock=lambda: clock[0])
    assert await requests.next(0.01) == []  # regular cadence
    requests.request([{'trigger': 'MACRO_RELEASE', 'id': 'cpi'}, {'trigger': 'UNKNOWN'}])
    clock[0] += 1
    reasons = await asyncio.wait_for(requests.next(10), 1)
    assert reasons == [{'trigger': 'MACRO_RELEASE', 'id': 'cpi'}]
    requests.request([{'trigger': 'LARGE_MOVE', 'id': 'BTC'}])
    started = asyncio.get_running_loop().time()
    assert [r['trigger'] for r in await requests.next(10)] == ['LARGE_MOVE']
    assert asyncio.get_running_loop().time() - started >= 0.04  # waited out the minimum interval


class FakeLock:
    held: set = set()

    def __init__(self, name):
        self.name = name

    def acquire(self, blocking=False):
        if self.name in FakeLock.held:
            return False
        FakeLock.held.add(self.name)
        return True

    def release(self):
        FakeLock.held.discard(self.name)


class FakeRedis:
    def __init__(self):
        self.values = {}

    def lock(self, name, timeout, blocking_timeout):
        return FakeLock(name)

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def close(self):
        pass


def test_beat_schedule_follows_refresh_config_and_locks_prevent_overlap():
    assert set(workers.beat_schedule(enabled=False)) == {'evaluation'}  # beat beside the local runtime
    schedule = workers.beat_schedule(enabled=True)
    assert schedule['poll-central_banks']['schedule'] == 30 and schedule['poll-hot_topics']['schedule'] == 900
    assert schedule['poll-release_watch']['schedule'] == 5 and schedule['interpretation']['task'] == 'hub.analyze'
    assert schedule['evaluation']['schedule'] == 60 and schedule['poll-model_discovery']['schedule'] == 3600
    assert workers.celery_owned() == set()  # local default: the asyncio runtime schedules everything
    client = FakeRedis()
    assert workers.run_locked('hot_topics', lambda: 'ran', client)['status'] == 'ok'
    assert client.values['hub:lastrun:hot_topics']
    FakeLock.held.add('hub:hot_topics:lock')
    try:
        assert workers.run_locked('hot_topics', lambda: 'ran', client)['status'] == 'already_running'
    finally:
        FakeLock.held.clear()


def test_adaptive_tasks_skip_until_due(monkeypatch):
    client = FakeRedis()
    client.set('hub:lastrun:congress', datetime.now(UTC).isoformat())
    monkeypatch.setattr(workers, '_with_hub', lambda operation: False)  # congress not active -> 30 min cadence
    assert workers.run_locked('congress', lambda: 'ran', client, check_due=True)['status'] == 'not_due'


def test_missing_runtime_poller_reports_unavailable(monkeypatch):
    class Runtime:
        async def social(self):
            return 'polled'
    monkeypatch.setattr(workers, '_with_hub', lambda operation: operation(None, Runtime()))
    assert workers.poll_once('x_search') == 'polled'
    assert workers.poll_once('sec_filings')['status'] == 'UNAVAILABLE'


def test_retention_aggregates_bars_before_pruning_raw_ticks(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/retention.db')
    store.initialize()
    now = datetime(2026, 7, 1, 12, 0, 30, tzinfo=UTC)
    with store.session() as session:
        for second, price in enumerate([100.0, 101.0, 99.0, 100.5]):
            stamp = datetime(2026, 7, 1, 11, 59, tzinfo=UTC) + timedelta(seconds=second * 10)
            session.add(Tick(symbol='BTC', price=price, timestamp=stamp, ingested_at=stamp, provider='binance',
                             payload={'trade_quantity': 1.0}))
        old = now - timedelta(days=30)
        session.add(Tick(symbol='BTC', price=50.0, timestamp=old, ingested_at=old, provider='binance', payload={}))
        session.add(Tick(symbol='BTC', price=1.0, timestamp=now - timedelta(seconds=5), ingested_at=now,
                         provider='massive-stub', payload={'stub': True}))
        session.commit()
    result = workers.run_retention(store, now, ['BTC'])
    assert result['pruned']['ticks'] == 1 and result['bars_written']['BTC'] > 0
    [minute] = store.bars('BTC', 60, now - timedelta(hours=1))
    assert (minute['open'], minute['high'], minute['low'], minute['close'], minute['volume'], minute['ticks']) == \
        (100.0, 101.0, 99.0, 100.5, 4.0, 4)
    assert len(store.bars('BTC', 5, now - timedelta(hours=1))) == 4
    assert store.bars('BTC', 3600, now - timedelta(days=1))[0]['close'] == 100.5  # stub tick never aggregated


def test_observe_state_builds_moves_and_levels_from_stored_data(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/observe.db')
    store.initialize()
    with store.session() as session:
        for at, price in [(NOW - timedelta(seconds=930), 100.0), (NOW - timedelta(seconds=5), 102.0)]:
            session.add(Tick(symbol='BTC', price=price, timestamp=at, ingested_at=at, provider='binance', payload={}))
        session.commit()
    assets = [{'symbol': 'BTC', 'quote': {'price': 102.0, 'freshness_status': 'FRESH'},
               'decision': {'levels': {'support': 90, 'resistance': 101}}}]
    state = refresh.observe_state(store, assets, [], [], NOW)
    assert state['moves']['BTC'] == {'change': 2.0, 'unit': 'pct'} and state['levels'] == {'BTC': 'ABOVE_RESISTANCE'}
