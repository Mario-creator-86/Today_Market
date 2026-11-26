"""Read models for market coverage (docs/MARKET_COVERAGE.md): close_status, change vs the previous stored close, asset
sessions, coverage statuses, proxy selection, daily bars on the asset detail and retention floors (the last-close regime
basis is in test_regime_last_close.py, daily-bar technicals in test_technicals.py). No network: providers are built
without keys and never called; rows are written straight to a temporary database."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_technicals import daily_store as daily_store

from backend import decision
from backend import service as service_module
from backend.config import Settings, rules
from backend.database import Bar, Decision, Store, Tick, daily_close_providers, retention_cutoffs
from backend.providers import proxies
from backend.schemas import Quote
from backend.service import HubService, displayed_providers, notices
from backend.sessions import close_status

SUNDAY = datetime(2026, 9, 27, 12, tzinfo=UTC)
FRIDAY_CLOSE = datetime(2026, 9, 25, 20, tzinfo=UTC)  # 16:00 New York
THURSDAY_CLOSE = datetime(2026, 9, 24, 20, tzinfo=UTC)


def at(*parts):
    return datetime(*parts, tzinfo=UTC)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for name in ('ALPACA_API_KEY_ID', 'ALPACA_API_SECRET_KEY'):
        monkeypatch.setenv(name, '')
    store = Store(f'sqlite:///{tmp_path}/coverage.db')
    store.initialize()
    result = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    result.initialize()
    return result


def add_tick(hub, symbol, stamp, price, provider, payload=None):
    with hub.store.session() as session:
        session.add(Tick(symbol=symbol, price=price, timestamp=stamp, ingested_at=stamp, provider=provider,
                         payload=payload or {}))
        session.commit()


def daily_payload(symbol, stamp, price, provider, **extra):
    return Quote(symbol=symbol, price=price, timestamp=stamp, provider=provider, daily_close=True,
                 data_class=extra.pop('data_class', 'DAILY_CLOSE'), **extra).model_dump(mode='json')


def put_proxy(hub, source, symbol, stamp, price, change_pct=0.5, change_basis='previous_close'):
    key, payload = proxies.payload(source, {'symbol': symbol, 'price': price, 'timestamp': stamp, 'change_pct': change_pct,
                                            'change_basis': change_basis})
    hub.store.put('proxy_quotes', key, payload)


def by_symbol(assets):
    return {a['symbol']: a for a in assets}


def core(coverage):
    """The coverage status fields (level/proxy splits and per-source health are tested on their own)."""
    return {k: coverage[k] for k in ('status', 'providers', 'missing_keys', 'disabled')}


# --- close_status: venue calendar + publication lag ------------------------------------------------------------------

@pytest.mark.parametrize('provider,symbol,observed,now,status', [
    ('treasury', 'US10Y', FRIDAY_CLOSE, SUNDAY, 'CURRENT'),                          # weekend: Friday is the latest
    ('treasury', 'US10Y', THURSDAY_CLOSE, SUNDAY, 'STALE'),
    ('treasury', 'US10Y', THURSDAY_CLOSE, at(2026, 9, 25, 21), 'CURRENT'),           # Fri 17:00 NY, not yet published
    ('treasury', 'US10Y', THURSDAY_CLOSE, at(2026, 9, 26, 1), 'STALE'),              # Fri 21:00 NY, Friday's is due
    ('treasury', 'US10Y', at(2026, 10, 9, 20), at(2026, 10, 13, 12), 'CURRENT'),     # Columbus Day: no Treasury close
    ('treasury', 'US10Y', at(2026, 11, 25, 21), at(2026, 11, 27, 15), 'CURRENT'),    # Thanksgiving
    ('ecb', 'EURUSD', at(2026, 9, 24, 12, 15), at(2026, 9, 25, 14, 30), 'CURRENT'),  # Fri 16:30 CET, inside the grace
    ('ecb', 'EURUSD', at(2026, 9, 24, 12, 15), at(2026, 9, 25, 15, 30), 'STALE'),    # Fri 17:30 CET, today's fix is due
    ('ecb', 'EURUSD', at(2026, 9, 25, 12, 15), SUNDAY, 'CURRENT'),
    ('ecb', 'EURUSD', at(2026, 4, 2, 12, 15), at(2026, 4, 7, 10), 'CURRENT'),        # Good Friday + Easter Monday
    ('ecb-derived', 'DXY', at(2026, 9, 23, 12, 15), SUNDAY, 'STALE'),
    ('fred', 'SP500', THURSDAY_CLOSE, at(2026, 9, 28, 15), 'CURRENT'),               # Mon 11:00 NY: T+1 not yet due
    ('fred', 'SP500', at(2026, 9, 23, 20), at(2026, 9, 28, 15), 'STALE'),
    ('fred', 'SP500', THURSDAY_CLOSE, at(2026, 9, 28, 17), 'STALE'),                 # Mon 13:00 NY: Friday's is due
    ('eia', 'WTI', at(2026, 9, 22, 20), SUNDAY, 'CURRENT'),                          # weekly: Wednesday release through Tuesday
    ('eia', 'WTI', at(2026, 9, 15, 20), SUNDAY, 'STALE'),
    ('eia', 'WTI', at(2026, 9, 22, 20), at(2026, 9, 30, 20), 'CURRENT'),             # release day, inside the grace
    ('eia', 'WTI', at(2026, 9, 22, 20), at(2026, 10, 1, 23), 'STALE'),
    ('fred', 'WTI', at(2026, 9, 22, 20), SUNDAY, 'CURRENT'),                         # FRED mirrors the weekly EIA table
    ('binance', 'BTC', SUNDAY, SUNDAY, None),                                        # no schedule: never CURRENT
])
def test_close_status_matrix(provider, symbol, observed, now, status):
    assert close_status(provider, symbol, observed, now)['status'] == status


# --- service.quote ---------------------------------------------------------------------------------------------------

def test_daily_close_change_vs_previous_stored_close_skips_live_ticks(hub):
    add_tick(hub, 'US10Y', THURSDAY_CLOSE, 4.10, 'treasury', {'daily_close': True})  # legacy row: no data_class
    add_tick(hub, 'US10Y', THURSDAY_CLOSE + timedelta(hours=1), 4.30, 'massive')  # a live tick is never a close
    add_tick(hub, 'US10Y', FRIDAY_CLOSE, 4.16, 'treasury', daily_payload('US10Y', FRIDAY_CLOSE, 4.16, 'treasury'))
    quote = hub.quote('US10Y', 'bond', SUNDAY)
    assert quote['change_bp'] == pytest.approx(6.0) and quote['change_pct'] == pytest.approx(1.4634)
    assert quote['change_basis'] == 'previous_close' and quote['previous_close'] == 4.10
    assert quote['previous_close_at'] == THURSDAY_CLOSE.isoformat()
    assert quote['data_class'] == 'DAILY_CLOSE' and quote['close_status'] == 'CURRENT' and quote['close_date'] == '2026-09-25'
    assert quote['freshness_status'] == 'STALE' and quote['market_status'] == 'FRIDAY CLOSE'  # never FRESH


def test_provider_change_is_kept_and_first_close_has_no_change(hub):
    add_tick(hub, 'US2Y', FRIDAY_CLOSE, 3.5, 'treasury',
             daily_payload('US2Y', FRIDAY_CLOSE, 3.5, 'treasury', change_bp=-6.0, change_basis='previous_close'))
    quote = hub.quote('US2Y', 'bond', SUNDAY)
    assert quote['change_bp'] == -6.0 and 'previous_close' not in {k for k, v in quote.items() if v is not None}
    add_tick(hub, 'US5Y', FRIDAY_CLOSE, 3.7, 'treasury', daily_payload('US5Y', FRIDAY_CLOSE, 3.7, 'treasury'))
    first = hub.quote('US5Y', 'bond', SUNDAY)
    assert first['change_bp'] is None and first['change_pct'] is None  # nothing earlier: never invented


def test_old_fix_never_reports_a_live_market(hub):
    friday = at(2026, 9, 25, 16)  # FX is open
    add_tick(hub, 'EURUSD', at(2026, 9, 23, 12, 15), 1.17, 'ecb',
             daily_payload('EURUSD', at(2026, 9, 23, 12, 15), 1.17, 'ecb', change_pct=0.1, change_basis='previous_fix'))
    old = hub.quote('EURUSD', 'fx', friday)
    assert old['close_status'] == 'STALE' and old['market_status'] == 'LAST CLOSE' and old['market_state'] == 'LIVE'
    add_tick(hub, 'EURUSD', at(2026, 9, 25, 12, 15), 1.18, 'ecb',
             daily_payload('EURUSD', at(2026, 9, 25, 12, 15), 1.18, 'ecb', change_pct=0.2, change_basis='previous_fix'))
    current = hub.quote('EURUSD', 'fx', friday)
    assert current['close_status'] == 'CURRENT' and current['market_status'] == 'LIVE'
    assert current['freshness_status'] == 'STALE' and current['data_class'] == 'DAILY_CLOSE'


def stub_tick(hub, symbol, stamp, price):
    add_tick(hub, symbol, stamp, price, 'massive-stub',
             Quote(symbol=symbol, price=price, timestamp=stamp, provider='massive-stub', stub=True).model_dump(mode='json'))


def test_stub_ticks_never_hide_a_current_real_close(hub):
    # PROVIDER_MODE=stub: the Massive stub stamps SP500 'now' every cycle; FRED's Thursday close is the latest expected.
    add_tick(hub, 'SP500', THURSDAY_CLOSE, 6604.7, 'fred', daily_payload('SP500', THURSDAY_CLOSE, 6604.7, 'fred',
                                                                         change_pct=-0.5, change_basis='previous_close'))
    stub_tick(hub, 'SP500', SUNDAY - timedelta(seconds=5), 5000.0)
    quote = hub.quote('SP500', now=SUNDAY)
    assert quote['provider'] == 'fred' and quote['price'] == 6604.7 and quote['stub'] is False
    assert quote['close_status'] == 'CURRENT' and quote['data_class'] == 'DAILY_CLOSE'
    assert by_symbol(hub.assets(SUNDAY))['SP500']['coverage']['status'] == 'DAILY_ONLY'


def test_stub_is_kept_when_the_real_tick_is_outside_its_window(hub):
    add_tick(hub, 'NASDAQ', at(2026, 9, 22, 20), 21000.0, 'fred', daily_payload('NASDAQ', at(2026, 9, 22, 20), 21000.0, 'fred'))
    stub_tick(hub, 'NASDAQ', SUNDAY - timedelta(seconds=5), 18000.0)
    quote = hub.quote('NASDAQ', now=SUNDAY)  # Tuesday's close is STALE on Sunday: never presented as the current one
    assert quote['provider'] == 'massive-stub' and quote['stub'] is True
    # A source without a publication schedule counts while its tick is FRESH.
    friday = at(2026, 9, 25, 15)
    add_tick(hub, 'GOLD', friday - timedelta(seconds=10), 3790.0, 'massive', {'data_class': 'LIVE'})
    stub_tick(hub, 'GOLD', friday - timedelta(seconds=1), 2000.0)
    assert hub.quote('GOLD', now=friday)['provider'] == 'massive'
    assert hub.quote('GOLD', now=friday + timedelta(hours=1))['provider'] == 'massive-stub'


FRED_NOTICE = 'This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.'


def test_fred_notice_whenever_a_fred_value_is_displayed(hub):
    board = hub.dashboard()
    assert board['notices'] == []  # nothing from FRED is shown
    now = datetime.now(UTC)
    add_tick(hub, 'VIX', now - timedelta(days=1), 17.4, 'fred', daily_payload('VIX', now - timedelta(days=1), 17.4, 'fred'))
    board = hub.dashboard()
    assert board['notices'] == [{'id': 'fred_api', 'text': FRED_NOTICE, 'providers': ['fred']}]
    assert hub.asset('VIX')['notices'] == board['notices'] and hub.asset('BTC')['notices'] == []


def test_notices_cover_proxies_and_regime_inputs():
    config = rules()
    assert notices(['binance', None, 'treasury'], config) == []
    assets = [{'quote': None, 'proxy': {'provider': 'fred'}}]
    assert notices(displayed_providers(assets), config)[0]['id'] == 'fred_api'
    regime = {'feature_sources': {'CREDIT:BAA10Y': {'basis': 'last_close', 'source': 'Source: FRED', 'provider': 'fred'},
                                  'US2Y': {'basis': 'last_close', 'source': 'treasury'}}}
    assert displayed_providers([], regime) == ['fred', 'treasury']


# --- assets: session, coverage, proxy --------------------------------------------------------------------------------

def test_asset_without_quote_carries_its_session(hub):
    sp500 = by_symbol(hub.assets(SUNDAY))['SP500']
    assert sp500['quote'] is None
    assert sp500['session']['state'] == 'FRIDAY CLOSE' and sp500['session']['label'] == 'Market closed · Friday close'


def test_coverage_statuses(hub):
    add_tick(hub, 'US10Y', FRIDAY_CLOSE, 4.16, 'treasury', daily_payload('US10Y', FRIDAY_CLOSE, 4.16, 'treasury'))
    add_tick(hub, 'BTC', SUNDAY - timedelta(seconds=2), 65000, 'binance', {'data_class': 'LIVE'})
    put_proxy(hub, 'binance', 'PAXG', SUNDAY - timedelta(seconds=30), 3790.5, change_basis='rolling_24h')
    assets = by_symbol(hub.assets(SUNDAY))
    assert core(assets['SP500']['coverage']) == {'status': 'NOT_CONFIGURED', 'providers': [], 'disabled': [],
                                           'missing_keys': ['FRED_API_KEY', 'MASSIVE_API_KEY', 'FINNHUB_API_KEY',
                                                            'ALPACA_API_KEY_ID', 'ALPACA_API_SECRET_KEY']}
    assert core(assets['US10Y']['coverage']) == {'status': 'DAILY_ONLY', 'providers': ['treasury'], 'missing_keys': [],
                                           'disabled': []}
    assert assets['US2Y']['coverage']['status'] == 'NO_DATA_YET'
    assert assets['BTC']['coverage']['status'] == 'OK'
    gold = assets['GOLD']['coverage']
    assert gold['status'] == 'PROXY_ONLY' and gold['providers'] == ['binance', 'coinbase']
    assert gold['missing_keys'] == ['MASSIVE_API_KEY', 'ALPACA_API_KEY_ID', 'ALPACA_API_SECRET_KEY']
    wti = assets['WTI']['coverage']  # served by the EIA spot provider, never the weekly petroleum report or the EIA feed
    assert wti['status'] == 'NO_DATA_YET' and wti['providers'] == ['eia'] and 'FRED_API_KEY' in wti['missing_keys']
    hub.set_enabled('treasury_yields', False)  # switched off: not a missing key, and not "no source configured"
    switched = by_symbol(hub.assets(SUNDAY))
    assert core(switched['US5Y']['coverage']) == {'status': 'DISABLED', 'providers': [], 'missing_keys': [], 'disabled': ['treasury']}
    assert core(switched['US10Y']['coverage']) == {'status': 'DAILY_ONLY', 'providers': [], 'missing_keys': [],
                                             'disabled': ['treasury']}  # its stored close is still shown


def test_disabled_status_needs_every_level_source_switched_off(hub):
    hub.set_enabled('ecb_data', False)
    assets = by_symbol(hub.assets(SUNDAY))
    eurusd = assets['EURUSD']['coverage']  # Massive could still serve it: a key is missing, not every source is off
    assert core(eurusd) == {'status': 'NOT_CONFIGURED', 'providers': [], 'missing_keys': ['MASSIVE_API_KEY'], 'disabled': ['ecb']}
    assert assets['DXY']['coverage']['disabled'] == ['ecb-derived'] and assets['DXY']['coverage']['status'] == 'NOT_CONFIGURED'
    hub.set_enabled('massive', False)
    assets = by_symbol(hub.assets(SUNDAY))
    assert core(assets['EURUSD']['coverage']) == {'status': 'DISABLED', 'providers': [], 'missing_keys': [],
                                            'disabled': ['ecb', 'massive']}
    assert assets['GOLD']['coverage']['status'] == 'NO_DATA_YET'  # its PAXG proxy sources are still ready
    hub.set_enabled('ecb_data', True)
    assert by_symbol(hub.assets(SUNDAY))['EURUSD']['coverage']['status'] == 'NO_DATA_YET'


def test_missing_keys_name_every_unset_key_of_a_multi_key_source(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/alpaca.db')
    store.initialize()
    hub = HubService(store, Settings(_env_file=None, enable_ingestion=False, finnhub_api_key='',
                                     alpaca_api_key_id='test-key-id', alpaca_api_secret_key=''))
    coverage = by_symbol(hub.assets(SUNDAY))['AAPL']['coverage']  # only the key ID is saved: Alpaca is not configured
    assert coverage['status'] == 'NOT_CONFIGURED' and coverage['providers'] == []
    assert coverage['missing_keys'] == ['FINNHUB_API_KEY', 'ALPACA_API_SECRET_KEY']


def test_configured_key_removes_it_from_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setenv('ALPACA_API_KEY_ID', '')
    store = Store(f'sqlite:///{tmp_path}/keys.db')
    store.initialize()
    hub = HubService(store, Settings(_env_file=None, enable_ingestion=False, fred_api_key='test-fred-key'))
    coverage = by_symbol(hub.assets(SUNDAY))['SP500']['coverage']
    assert coverage['status'] == 'NO_DATA_YET' and coverage['providers'] == ['fred']
    assert 'FRED_API_KEY' not in coverage['missing_keys']
    hub.providers = dict(hub.providers, massive=SimpleNamespace(configured=True, health={'name': 'massive', 'stub': True}))
    assert 'massive' not in by_symbol(hub.assets(SUNDAY))['SP500']['coverage']['providers']  # a stub never counts


@pytest.mark.parametrize('finnhub_age,alpaca_age,provider,freshness', [
    (30, 60, 'finnhub', 'FRESH'),     # both fresh: coverage priority
    (500, 60, 'alpaca', 'FRESH'),     # finnhub past its 120 s: the fresh fallback wins
    (7000, 7100, 'finnhub', 'STALE'),  # none fresh: the latest one, marked STALE
])
def test_proxy_selection_priority_and_freshness(hub, finnhub_age, alpaca_age, provider, freshness):
    put_proxy(hub, 'finnhub', 'SPY', SUNDAY - timedelta(seconds=finnhub_age), 580.1)
    put_proxy(hub, 'alpaca', 'SPY', SUNDAY - timedelta(seconds=alpaca_age), 580.3)
    proxy = by_symbol(hub.assets(SUNDAY))['SP500']['proxy']
    assert proxy['provider'] == provider and proxy['freshness_status'] == freshness and proxy['symbol'] == 'SPY'
    assert proxy['proxy_for'] == 'SP500' and proxy['relation'] == 'direction_only' and proxy['label'].startswith('SPY ETF')
    assert proxy['market_status'] == 'FRIDAY CLOSE'  # the ETF's own session


def test_proxy_is_never_a_tick_nor_a_gate(hub):
    put_proxy(hub, 'binance', 'PAXG', SUNDAY - timedelta(seconds=10), 3790.5, change_basis='rolling_24h')
    put_proxy(hub, 'finnhub', 'SPY', SUNDAY - timedelta(seconds=10), 580.1)
    created = SUNDAY - timedelta(seconds=30)
    payload = {'id': 'setup', 'symbol': 'SP500', 'direction': 'LONG', 'category': 'LONG SETUP', 'score': 80,
               'created_at': created.isoformat(), 'audit_status': 'PASS', 'reasons': [], 'input_snapshot': {}}
    with hub.store.session() as session:
        session.add(Decision(id='setup', symbol='SP500', direction='LONG', score=80, created_at=created, payload=payload))
        session.commit()
    assets = by_symbol(hub.assets(SUNDAY))
    assert assets['GOLD']['proxy']['market_status'] == 'LIVE' and assets['GOLD']['proxy']['freshness_status'] == 'FRESH'
    assert assets['GOLD']['quote'] is None and assets['SP500']['quote'] is None
    assert assets['SP500']['decision']['direction'] == 'WAIT'  # a fresh proxy never keeps a setup actionable
    with hub.store.session() as session:
        assert session.query(Tick).filter(Tick.symbol.in_(['GOLD', 'SP500'])).count() == 0


def test_dashboard_reads_proxies_once_and_carries_the_new_fields(hub, monkeypatch):
    put_proxy(hub, 'finnhub', 'SPY', SUNDAY - timedelta(seconds=10), 580.1)
    reads = []
    original = hub.store.list
    monkeypatch.setattr(hub.store, 'list', lambda table, limit=500: (reads.append(table), original(table, limit))[1])
    board = hub.dashboard()
    assert reads.count('proxy_quotes') == 1
    assert all({'session', 'coverage', 'proxy'} <= asset.keys() for asset in board['assets'])
    assert {'basis', 'basis_label', 'missing_required'} <= board['regime'].keys()


def copy_daily_bars(store, hub):
    with store.session() as source, hub.store.session() as target:
        for bar in source.query(Bar).all():
            target.merge(Bar(**{c: getattr(bar, c) for c in ('symbol', 'interval_seconds', 'start', 'open', 'high', 'low',
                                                               'close', 'volume', 'ticks', 'provider')}))
        target.commit()


def test_asset_detail_uses_stored_daily_bars(hub, daily_store, monkeypatch):
    copy_daily_bars(daily_store[0], hub)
    monkeypatch.setattr(service_module, 'utcnow', lambda: at(2026, 9, 28, 14))
    detail = hub.asset('NVDA')
    assert detail['bars'] == [] and detail['technical_levels']['previous_day']['date'] == '2026-09-25'


def test_decision_cycle_levels_match_the_asset_page(hub, daily_store, monkeypatch):
    copy_daily_bars(daily_store[0], hub)
    monday = at(2026, 9, 28, 14)
    monkeypatch.setattr(service_module, 'utcnow', lambda: monday)
    levels = decision.CycleContext(hub, monday).levels['NVDA']  # no one-minute bars: daily bars alone define them
    assert levels == hub.asset('NVDA')['technical_levels']
    assert levels['previous_day']['date'] == '2026-09-25' and levels['sma_20'] is not None
    assert levels['daily_basis'] == 'daily bars: Alpaca IEX'


# --- retention -------------------------------------------------------------------------------------------------------

def test_retention_keeps_daily_bars_and_daily_closes(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/retention.db')
    store.initialize()
    now = SUNDAY
    ticks, bars = retention_cutoffs(now, 168, {'60': 30, '86400': 30})
    assert bars[86400] == now - timedelta(days=400) and bars[60] == now - timedelta(days=30)
    with store.session() as session:
        for key, provider, age in [('live', 'binance', 10), ('close', 'treasury', 10), ('ancient', 'treasury', 500)]:
            session.add(Tick(id=key, symbol='US10Y', price=4.0, provider=provider, timestamp=now - timedelta(days=age),
                             ingested_at=now - timedelta(days=age), payload={}))
        for age in (300, 450):
            session.add(Bar(symbol='NVDA', interval_seconds=86400, start=now - timedelta(days=age), open=1, high=1, low=1,
                            close=1, volume=0, ticks=0, provider='alpaca'))
        session.commit()
    removed = store.prune(ticks, {86400: now - timedelta(days=30)})  # even a too-short horizon keeps 400 days
    assert removed == {'ticks': 2, 'bars_86400s': 1}
    with store.session() as session:
        assert [t.id for t in session.query(Tick).all()] == ['close']
        assert session.query(Bar).count() == 1


def test_daily_close_providers_come_from_the_publication_schedules():
    config = rules()
    providers = daily_close_providers(config)
    assert providers == ('ecb', 'ecb-derived', 'eia', 'fred', 'treasury')
    daily_levels = {s['provider'] for spec in config['coverage']['assets'].values() for s in spec['level']
                    if s['data_class'] != 'LIVE'}
    assert set(providers) == daily_levels  # every daily level source has a schedule, and no live feed is retained
    assert not {'finnhub', 'alpaca', 'binance', 'massive'} & set(providers)


def test_live_source_last_trades_keep_the_normal_tick_retention(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/live.db')
    store.initialize()
    now = SUNDAY
    with store.session() as session:
        for key, provider in [('finnhub', 'finnhub'), ('alpaca', 'alpaca'), ('fred', 'fred'), ('ecb', 'ecb')]:
            session.add(Tick(id=key, symbol='AAPL', price=1.0, provider=provider, timestamp=now - timedelta(days=10),
                             ingested_at=now - timedelta(days=10), payload={}))
        session.commit()
    ticks, _ = retention_cutoffs(now, 168, {})
    assert store.prune(ticks, {}) == {'ticks': 2}
    with store.session() as session:
        assert sorted(t.id for t in session.query(Tick).all()) == ['ecb', 'fred']


# --- display latency: the regime's correlation legs are read once per symbol and briefly reused on display ------------

def test_regime_reads_each_correlation_leg_once_and_reuses_them_only_for_display(hub, monkeypatch):
    calls = []
    monkeypatch.setattr(hub, 'bars', lambda symbol, days=2: calls.append(symbol) or [])
    symbols = sorted({a for pair in rules()['regime']['correlation_pairs'] for a in pair})
    hub.regime(last_close=True)
    assert sorted(calls) == symbols  # BTC sits in two pairs but is read once
    hub.regime(last_close=True)
    assert sorted(calls) == symbols  # the display regime reuses the read within correlation_bars_cache_seconds
    hub.regime()
    assert sorted(calls) == sorted(symbols * 2)  # decision scoring always reads fresh bars


def test_correlation_bar_reuse_expires(hub, monkeypatch):
    calls = []
    monkeypatch.setattr(hub, 'bars', lambda symbol, days=2: calls.append(symbol) or [])
    now = datetime.now(UTC)
    hub.correlation_bars(now, reuse=True)
    first = len(calls)
    hub.correlation_bars(now + timedelta(seconds=rules()['regime']['correlation_bars_cache_seconds']), reuse=True)
    assert len(calls) == 2 * first
