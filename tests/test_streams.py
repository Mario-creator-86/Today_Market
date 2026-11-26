import asyncio
import json
import threading
import time

import pytest

from backend import streams


@pytest.mark.asyncio
async def test_memory_bus_orders_trims_and_reads_after_offsets():
    bus = streams.MemoryBus(maxlen=3)
    start = await bus.latest_ids(['quotes', 'alerts'])
    assert start == {'quotes': '0-0', 'alerts': '0-0'}
    for price in [1, 2, 3, 4]:
        await bus.publish('quotes', {'symbol': 'BTC', 'price': price})
    entries = await bus.read({'quotes': '0-0'})
    assert [p['price'] for _, _, p in entries] == [2, 3, 4]  # MAXLEN trimmed the oldest
    latest = await bus.latest_ids(['quotes'])
    assert await bus.read(latest) == []
    with pytest.raises(ValueError):
        await bus.publish('unknown', {})


@pytest.mark.asyncio
async def test_memory_bus_blocking_read_wakes_on_publish_including_from_threads():
    bus = streams.MemoryBus()
    offsets = await bus.latest_ids(['alerts'])
    reader = asyncio.create_task(bus.read(offsets, block_ms=2000))
    await asyncio.sleep(0.05)
    thread = threading.Thread(target=bus.publish_nowait, args=('alerts', {'id': 'a1'}))
    thread.start()
    thread.join()
    [(stream, _, payload)] = await asyncio.wait_for(reader, 1)
    assert stream == 'alerts' and payload == {'id': 'a1'}
    started = time.monotonic()
    assert await bus.read(await bus.latest_ids(['alerts']), block_ms=50) == []
    assert time.monotonic() - started >= 0.04


class FakeRedisClient:
    def __init__(self):
        self.entries: dict[str, list] = {}
        self.calls: list = []
        self.groups: set = set()

    async def xadd(self, key, fields, maxlen, approximate):
        self.calls.append(('xadd', key, maxlen, approximate))
        entry_id = f'{len(self.entries.get(key, [])) + 1}-0'
        self.entries.setdefault(key, []).append((entry_id, fields))
        return entry_id

    async def xread(self, streams, count, block):
        self.calls.append(('xread', dict(streams), count, block))
        return [(key, [e for e in self.entries.get(key, []) if e[0] > offset][:count]) for key, offset in streams.items()
                if any(e[0] > offset for e in self.entries.get(key, []))]

    async def xrevrange(self, key, count):
        return list(reversed(self.entries.get(key, [])))[:count]

    async def xgroup_create(self, key, group, id, mkstream):
        if (key, group) in self.groups:
            raise RuntimeError('BUSYGROUP Consumer Group name already exists')
        self.groups.add((key, group))

    async def xreadgroup(self, group, consumer, streams, count, block):
        return [(key, self.entries.get(key, [])[:count]) for key in streams]

    async def xack(self, key, group, *ids):
        return len(ids)

    async def aclose(self):
        self.calls.append(('close',))


@pytest.mark.asyncio
async def test_redis_bus_uses_prefixed_streams_with_maxlen_and_json_payloads():
    client = FakeRedisClient()
    bus = streams.RedisBus(client=client)
    assert await bus.latest_ids(['decisions']) == {'decisions': '0-0'}
    await bus.publish('decisions', {'symbol': 'BTC', 'score': 12.5, 'levels': {'support': 1}})
    assert client.calls[0] == ('xadd', 'hub:decisions', 10000, True)
    assert json.loads(client.entries['hub:decisions'][0][1]['data'])['score'] == 12.5
    [(stream, entry_id, payload)] = await bus.read({'decisions': '0-0'}, block_ms=100)
    assert (stream, entry_id, payload['levels']) == ('decisions', '1-0', {'support': 1})
    bus.publish_nowait('alerts', {'id': 'x'})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert 'hub:alerts' in client.entries
    await bus.ensure_group('alerts', 'workers')
    await bus.ensure_group('alerts', 'workers')  # BUSYGROUP is not an error
    [(_, group_id, _)] = await bus.read_group('workers', 'w1', ['alerts'])
    assert await bus.ack('alerts', 'workers', group_id) == 1
    await bus.close()
    assert client.calls[-1] == ('close',)


def test_create_bus_picks_redis_only_when_configured():
    assert isinstance(streams.create_bus(''), streams.MemoryBus)
    assert isinstance(streams.create_bus('redis://localhost:6379/0'), streams.RedisBus)


@pytest.mark.asyncio
async def test_fanout_shares_snapshots_coalesces_quotes_and_forwards_other_streams():
    bus = streams.MemoryBus()
    builds = []

    def snapshot():
        builds.append(threading.current_thread().name)
        return {'assets': len(builds)}
    fanout = streams.Fanout(bus, snapshot=snapshot)
    fanout.quote_seconds, fanout.snapshot_seconds = 0.05, 10
    first, second = fanout.subscribe(), fanout.subscribe()
    fanout.start()
    try:
        assert (await asyncio.wait_for(first.get(), 1))['type'] == 'snapshot'
        assert (await asyncio.wait_for(second.get(), 1))['type'] == 'snapshot'
        assert len(builds) == 1 and builds[0] != threading.current_thread().name  # built once, off the loop
        await asyncio.sleep(0.05)
        for price in [1, 2, 3]:
            await bus.publish('quotes', {'symbol': 'BTC', 'price': price})
        await bus.publish('quotes', {'symbol': 'ETH', 'price': 9})
        await bus.publish('alerts', {'id': 'a1'})
        seen = [await asyncio.wait_for(first.get(), 1) for _ in range(2)]
        by_type = {m['type']: m['data'] for m in seen}
        assert by_type['alerts'] == {'id': 'a1'}
        assert sorted((q['symbol'], q['price']) for q in by_type['quotes']) == [('BTC', 3), ('ETH', 9)]
    finally:
        await fanout.stop()


def test_new_clients_get_the_cached_snapshot_only_while_it_is_current(monkeypatch):
    fanout = streams.Fanout(streams.MemoryBus())
    fanout.latest_snapshot, fanout.snapshot_at = {'assets': 1}, 100.0
    monkeypatch.setattr(streams.time, 'monotonic', lambda: 100.0 + fanout.snapshot_seconds)
    assert fanout.subscribe().get_nowait()['type'] == 'snapshot'
    monkeypatch.setattr(streams.time, 'monotonic', lambda: 101.0 + fanout.snapshot_seconds)
    assert fanout.subscribe().empty()  # an old snapshot would present stale quotes as fresh


def test_slow_clients_drop_their_backlog_and_wait_for_the_next_full_snapshot():
    """A full queue is emptied (its frames no longer describe one state) and the client is marked for the next full
    build: a price-only frame means nothing without the snapshot it updates."""
    fanout = streams.Fanout(streams.MemoryBus(), queue_size=2)
    queue = fanout.subscribe()
    for index in range(3):
        fanout.broadcast({'type': 'alerts', 'data': index})
    assert [queue.get_nowait()['data'] for _ in range(queue.qsize())] == [2]
    assert queue in fanout.resync
    fanout.unsubscribe(queue)
    assert queue not in fanout.resync
    fanout.broadcast({'type': 'alerts', 'data': 3})
    assert queue.empty()


def test_snapshot_fingerprint_ignores_time_only_fields():
    base = {'as_of': 'a', 'assets': [{'symbol': 'BTC', 'quote': {'price': 1, 'age_seconds': 1.5}}],
            'event_risk': {'next_event': {'minutes_until': 30, 'name': 'CPI'}}, 'watch': {'events': [{'seconds_to_release': 9}]}}
    later = {'as_of': 'b', 'assets': [{'symbol': 'BTC', 'quote': {'price': 1, 'age_seconds': 7.0}}],
             'event_risk': {'next_event': {'minutes_until': 29, 'name': 'CPI'}}, 'watch': {'events': [{'seconds_to_release': 4}]}}
    assert streams.fingerprint(base) == streams.fingerprint(later)
    moved = {**later, 'assets': [{'symbol': 'BTC', 'quote': {'price': 2, 'age_seconds': 7.0}}]}
    assert streams.fingerprint(moved) != streams.fingerprint(base)


def test_snapshot_fingerprint_ignores_build_stamps_and_routine_polls_but_not_status():
    polled = {'event_risk': {'generated_at': 'a'},
              'providers': [{'name': 'fred', 'status': 'ONLINE', 'latency_ms': 80.0, 'last_success': 'a'}]}
    later = {'event_risk': {'generated_at': 'b'},
             'providers': [{'name': 'fred', 'status': 'ONLINE', 'latency_ms': 95.5, 'last_success': 'b'}]}
    assert streams.fingerprint(polled) == streams.fingerprint(later)
    failing = {**later, 'providers': [{'name': 'fred', 'status': 'OFFLINE', 'latency_ms': None, 'last_success': 'b'}]}
    assert streams.fingerprint(failing) != streams.fingerprint(later)


@pytest.mark.asyncio
async def test_fanout_pushes_snapshots_on_the_cadence_and_only_when_they_changed():
    """Regression (UI audit DASH-07 / SHELL-25): the full dashboard (~580 KB) was pushed every 1-4 s to every tab
    whether or not anything changed."""
    builds = []
    content = {'price': 1}

    def snapshot():
        builds.append(1)
        return {'as_of': len(builds), 'assets': [dict(content)]}
    fanout = streams.Fanout(streams.MemoryBus(), snapshot=snapshot)
    fanout.snapshot_seconds, fanout.full_seconds = 0.2, 0
    first = fanout.subscribe()
    fanout.start()
    try:
        assert (await asyncio.wait_for(first.get(), 1))['data']['assets'] == [{'price': 1}]
        await asyncio.sleep(0.5)  # two more cadence builds with the same content (only as_of moved)
        assert len(builds) >= 2 and first.empty()
        late = fanout.subscribe()  # joins between builds: it still gets the current snapshot at once
        assert (await asyncio.wait_for(late.get(), 1))['type'] == 'snapshot' and first.empty()
        content['price'] = 2
        changed = await asyncio.wait_for(first.get(), 1)
        assert changed['data']['assets'] == [{'price': 2}]
        assert (await asyncio.wait_for(late.get(), 1))['data']['assets'] == [{'price': 2}]
        start = len(builds)
        await asyncio.sleep(0.3)
        assert len(builds) - start <= 2  # at most one build per cadence, never a tight loop
    finally:
        await fanout.stop()


def test_ui_snapshot_cadence_is_five_seconds():
    assert streams.Fanout(streams.MemoryBus()).snapshot_seconds == 5


def test_snapshot_fingerprint_compares_decaying_confidence_at_display_precision():
    """Fact confidence decays on every read; a change below the displayed 0.1 must not re-send the snapshot."""
    base = {'events': [{'id': 'a', 'fact_confidence': 91.26, 'verification_status': 'CONFIRMED'}]}
    decayed = {'events': [{'id': 'a', 'fact_confidence': 91.21, 'verification_status': 'CONFIRMED'}]}
    shown_change = {'events': [{'id': 'a', 'fact_confidence': 91.19, 'verification_status': 'CONFIRMED'}]}
    assert streams.fingerprint(base) == streams.fingerprint(decayed)
    assert streams.fingerprint(base) != streams.fingerprint(shown_change)


def test_snapshot_fingerprint_ignores_per_poll_rate_limit_counters():
    """Regression (UI audit DASH-07): Alpaca's x-ratelimit-reset and Binance's used weight moved on every poll."""
    base = {'providers': [{'name': 'alpaca', 'status': 'ONLINE', 'x_ratelimit_reset': '1759050000',
                           'x_ratelimit_remaining': '199', 'news_lag_seconds': 12.5},
                          {'name': 'binance_futures', 'status': 'ONLINE', 'used_weight_1m': 40}]}
    polled = {'providers': [{'name': 'alpaca', 'status': 'ONLINE', 'x_ratelimit_reset': '1759050015',
                             'x_ratelimit_remaining': '187', 'news_lag_seconds': 40.1},
                            {'name': 'binance_futures', 'status': 'ONLINE', 'used_weight_1m': 95}]}
    assert streams.fingerprint(base) == streams.fingerprint(polled)
    assert streams.fingerprints(base)[0] == streams.fingerprints(polled)[0]


def test_live_parts_are_fingerprinted_per_symbol_apart_from_the_rest():
    def snapshot(btc: float, paxg: float, level: str = 'LOW') -> dict:
        return {'as_of': 'a', 'event_risk': {'level': level},
                'assets': [{'symbol': 'BTC', 'decision': {'direction': 'WAIT'}, 'quote': {'price': btc, 'age_seconds': 1},
                            'proxy': None},
                           {'symbol': 'GOLD', 'decision': None, 'quote': None, 'proxy': {'price': paxg}}]}
    rest, live = streams.fingerprints(snapshot(100, 2000))
    moved_rest, moved = streams.fingerprints(snapshot(101, 2000))
    assert moved_rest == rest  # a price move alone never changes the rest
    assert moved['quotes']['BTC'] != live['quotes']['BTC'] and moved['quotes']['GOLD'] == live['quotes']['GOLD']
    assert moved['proxies'] == live['proxies']
    assert streams.fingerprints(snapshot(100, 2000, 'HIGH'))[0] != rest
    stripped, parts = streams.split_live(snapshot(100, 2000))
    assert all('quote' not in a and 'proxy' not in a for a in stripped['assets'])
    assert parts == {'quotes': {'BTC': {'price': 100, 'age_seconds': 1}, 'GOLD': None},
                     'proxies': {'BTC': None, 'GOLD': {'price': 2000}}}


@pytest.mark.asyncio
async def test_price_moves_go_out_as_asset_quotes_and_full_snapshots_are_rate_limited(monkeypatch):
    """Regression (UI audit DASH-07 / SHELL-25, contract C15): crypto quotes change on every build, so the ~660 KB
    dashboard was pushed every 5 s to every tab. Only the changed quotes/proxies go out between full snapshots."""
    clock = {'now': 1000.0}
    monkeypatch.setattr(streams.time, 'monotonic', lambda: clock['now'])
    state = {'btc': 100.0, 'eth': 10.0, 'paxg': 2000.0, 'level': 'LOW', 'build': 0}

    def snapshot():
        state['build'] += 1
        return {'as_of': f"t{state['build']}", 'event_risk': {'level': state['level']},
                'assets': [{'symbol': 'BTC', 'quote': {'price': state['btc']}, 'proxy': None},
                           {'symbol': 'ETH', 'quote': {'price': state['eth']}, 'proxy': None},
                           {'symbol': 'GOLD', 'quote': None, 'proxy': {'price': state['paxg']}}]}
    fanout = streams.Fanout(streams.MemoryBus(), snapshot=snapshot)
    assert fanout.full_seconds == 30
    client = fanout.subscribe()
    fanout.pending.clear()
    await fanout._snapshot_round(True)
    assert client.get_nowait()['type'] == 'snapshot'  # the first build is always sent in full
    clock['now'] += 5
    state['btc'] = 101.0
    await fanout._snapshot_round(True)
    frame = client.get_nowait()
    assert frame == {'type': 'asset_quotes', 'data': {'as_of': 't2', 'quotes': {'BTC': {'price': 101.0}}, 'proxies': {}}}
    assert client.empty()
    clock['now'] += 5
    await fanout._snapshot_round(True)
    assert client.empty()  # nothing changed: no frame at all
    clock['now'] += 5
    state.update(level='HIGH', paxg=2001.0)  # something besides prices changed, 15 s after the last full snapshot
    await fanout._snapshot_round(True)
    held = client.get_nowait()
    assert held['type'] == 'asset_quotes' and held['data']['proxies'] == {'GOLD': {'price': 2001.0}}
    assert held['data']['quotes'] == {} and client.empty()
    late = fanout.subscribe()  # a client connecting now still gets the latest full build at once
    assert late.get_nowait()['data']['event_risk'] == {'level': 'HIGH'}
    clock['now'] += 15  # 30 s after the last full snapshot: the change seen earlier goes out in full
    await fanout._snapshot_round(True)
    full = client.get_nowait()
    assert full['type'] == 'snapshot' and full['data']['event_risk'] == {'level': 'HIGH'}
    assert late.get_nowait()['type'] == 'snapshot' and client.empty()
    clock['now'] += 5
    state['eth'] = 11.0
    await fanout._snapshot_round(True)
    assert client.get_nowait()['data']['quotes'] == {'ETH': {'price': 11.0}}  # compared with the full snapshot just sent



def test_a_failed_build_never_leaves_a_waiting_client_with_price_frames_only():
    """Review finding: a failed build used to drop the waiting clients, so their next frame could be 'asset_quotes'."""
    state = {'fail': True, 'price': 1.0}

    def build():
        if state['fail']:
            raise RuntimeError('snapshot unavailable')
        return {'as_of': 'x', 'assets': [{'symbol': 'BTC', 'quote': {'price': state['price']}}], 'events': []}

    async def scenario():
        fanout = streams.Fanout(streams.MemoryBus(), snapshot=build)
        first = fanout.subscribe()  # nothing built yet: waits for a build
        await fanout._snapshot_round(True)  # the build fails
        assert first.empty() and first in fanout.resync
        state['fail'] = False
        await fanout._snapshot_round(True)  # a full snapshot first
        assert first.get_nowait()['type'] == 'snapshot'
        state['price'] = 2.0
        fanout.built_at, fanout.full_sent_at = None, None
        await fanout._snapshot_round(True)  # only the price moved: a price-only frame, now meaningful
        assert first.get_nowait()['type'] == 'asset_quotes'

    asyncio.run(scenario())


def test_price_derived_text_never_re_sends_the_full_snapshot_on_its_own():
    base = {'assets': [{'symbol': 'BTC', 'quote': {'price': 1}}],
            'event_risk': {'favored': [{'symbol': 'BTC', 'reason_facts': ['BTC -2.70% (live 24h)']}],
                           'events': [{'id': 'e', 'dominance_score': 67.8, 'relevance_score': 70.2}]},
            'regime': {'features': {'GOLD': {'change_pct': -2.9}},
                       'drivers': [{'name': 'Risk appetite', 'input': 'BTC -2.70%', 'inputs': {'BTC': {'change': -2.7}}}]}}
    moved = {'assets': [{'symbol': 'BTC', 'quote': {'price': 2}}],
             'event_risk': {'favored': [{'symbol': 'BTC', 'reason_facts': ['BTC -2.65% (live 24h)']}],
                            'events': [{'id': 'e', 'dominance_score': 67.9, 'relevance_score': 70.4}]},
             'regime': {'features': {'GOLD': {'change_pct': -2.8}},
                        'drivers': [{'name': 'Risk appetite', 'input': 'BTC -2.65%', 'inputs': {'BTC': {'change': -2.65}}}]}}
    (rest, live), (rest2, live2) = streams.fingerprints(base), streams.fingerprints(moved)
    assert rest == rest2 and live != live2
    renamed = dict(moved, regime=dict(moved['regime'], drivers=[{'name': 'Rates', 'input': 'x', 'inputs': {}}]))
    assert streams.fingerprints(renamed)[0] != rest  # a real change (another driver) still counts
