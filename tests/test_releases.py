"""EVR-4 (2 Oct 2026 audit): the outcome of a scheduled release from the stored official series (backend/releases.py),
attached to its calendar row, recorded, alerted and observed by the runtime, with the release watch polling the series
providers right after a mapped release; AFP-04 (EVENT alerts from the derived loop) and AFP-05 (the morning brief waits
for the first refresh and a cycle on it) runtime wiring. No network, no real .env."""
from datetime import UTC, datetime, timedelta

import pytest

from backend import briefs, notifications, releases
from backend.config import Settings
from backend.database import Store, utcnow
from backend.providers.calendars import calendar_row, carry_lent
from backend.runtime import REFRESH_KINDS, Runtime
from backend.service import HubService

T0 = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)  # the US employment report, 08:30 ET / 15:30 Bucharest


def nfp(at: datetime = T0, **extra) -> dict:
    return calendar_row('US employment situation (NFP, unemployment, AHE)', 'US', 'MACRO', at, 10, 'fred',
                        event_class='US_NFP', time_known=True, time_basis='FRED release calendar date + 08:30 ET', **extra)


def bls(series: str, values: dict[str, float], unit: str = 'thousands') -> list[dict]:
    """Stored BLS rows ('bls:<series>:<period>' payloads), newest period first, as series_rows returns them."""
    return [{'id': f'bls:{series}:{period}', 'provider': 'bls', 'series': series, 'period': period, 'value': value, 'unit': unit,
             'ingested_at': (T0 + timedelta(minutes=2)).isoformat()}
            for period, value in sorted(values.items(), reverse=True)]


PAYROLLS = bls('CES0000000001', {'2026-07': 158882.0, '2026-08': 159015.0, '2026-09': 159044.0})
UNRATE = bls('LNS14000000', {'2026-07': 4.1, '2026-08': 4.1, '2026-09': 4.2}, 'pct')
SERIES = {'bls:CES0000000001': PAYROLLS, 'bls:LNS14000000': UNRATE}
NFP_TEXT = 'payrolls +29k (prev +133k), unemployment 4.2% (prev 4.1%)'


def loader(table: dict[str, list[dict]]):
    return lambda key: list(table.get(key, []))


# --- the outcome -------------------------------------------------------------------------------------------------------

def test_reference_period_from_the_row_or_the_class_rule():
    assert releases.expected_period(T0, {'reference_period': 'September 2026'}) == '2026-09'
    assert releases.expected_period(T0, {'reference_period': 'week ending 2026-09-25'}) == '2026-09-25'
    assert releases.expected_period(T0, {}, 'previous_month') == '2026-09'  # 2 Oct 08:30 ET: the September report
    assert releases.expected_period(datetime(2026, 11, 6, 13, 30, tzinfo=UTC), None, 'previous_month') == '2026-10'
    assert releases.expected_period(datetime(2026, 10, 1, 2, 0, tzinfo=UTC), None, 'previous_month') == '2026-08'  # 30 Sep 22:00 ET
    wednesday = datetime(2026, 9, 30, 14, 30, tzinfo=UTC)  # the EIA report covers the week ended Friday 25 Sep
    assert releases.expected_period(wednesday, None, 'previous_week') == '2026-09-25'
    assert releases.expected_period(wednesday + timedelta(days=1), None, 'previous_week') == '2026-09-25'  # holiday delay


def test_nfp_outcome_is_the_payroll_change_and_the_unemployment_level_with_their_previous_readings():
    found = releases.outcome('US_NFP', T0, loader(SERIES), nfp())
    assert found['text'] == NFP_TEXT and found['period'] == '2026-09' and found['headline'] == 'payrolls'
    assert (found['actual'], found['previous'], found['unit'], found['values_source']) == (29.0, 133.0, 'k', 'bls')
    assert found['actual_ingested_at'] == (T0 + timedelta(minutes=2)).isoformat()
    assert [m['text'] for m in found['metrics']] == ['payrolls +29k (prev +133k)', 'unemployment 4.2% (prev 4.1%)']
    assert found['series'] == ['bls:CES0000000001', 'bls:LNS14000000']
    # The newest stored period is still August (the poll ran before the release): no outcome, nothing guessed.
    stale = {key: rows[1:] for key, rows in SERIES.items()}
    assert releases.outcome('US_NFP', T0, loader(stale), nfp()) is None
    # FRED's copy (fallback) carries the previous value on the row when the earlier period is not stored.
    fred = {'fred:PAYEMS': [{'id': 'fred:PAYEMS:2026-09-01', 'provider': 'fred', 'period': '2026-09-01', 'value': 159044.0,
                             'previous_value': 159015.0}],
            'fred:UNRATE': [{'id': 'fred:UNRATE:2026-09-01', 'provider': 'fred', 'period': '2026-09-01', 'value': 4.2, 'previous_value': 4.1}]}
    assert releases.outcome('US_NFP', T0, loader(fred), nfp())['text'] == 'payrolls +29k, unemployment 4.2% (prev 4.1%)'
    assert releases.outcome('US_NFP', T0, loader({}), nfp()) is None
    assert releases.outcome('US_GDP', T0, loader(SERIES), {}) is None  # no series mapped for the class


def test_percent_changes_and_weekly_changes_follow_the_series_spec():
    cpi = {'bls:CUSR0000SA0': bls('CUSR0000SA0', {f'{y}-{m:02d}': 320.0 + 12 * (y - 2025) + 0.4 * m for y in (2025, 2026) for m in range(1, 13)
                                                 if (y, m) <= (2026, 8)}, 'index'),
           'bls:CUSR0000SA0L1E': bls('CUSR0000SA0L1E', {'2026-07': 330.0, '2026-08': 331.0}, 'index')}
    found = releases.outcome('US_CPI', datetime(2026, 9, 11, 12, 30, tzinfo=UTC), loader(cpi), {})
    assert found['text'] == 'CPI +0.1% m/m (prev +0.1%), CPI +3.7% y/y (prev +3.7%), core CPI +0.3% m/m'
    assert found['headline'] == 'CPI' and found['unit'] == '%' and found['actual'] == pytest.approx(0.1195, abs=1e-3)
    eia = {'eia:WPSR_CRUDE_COMMERCIAL': [
        {'id': 'eia:WPSR_CRUDE_COMMERCIAL:2026-09-25', 'provider': 'eia', 'period': '2026-09-25', 'value': 427.32, 'previous': 426.398},
        {'id': 'eia:WPSR_CRUDE_COMMERCIAL:2026-09-18', 'provider': 'eia', 'period': '2026-09-18', 'value': 426.398, 'previous': 423.4}]}
    weekly = releases.outcome('EIA_WPSR', datetime(2026, 9, 30, 14, 30, tzinfo=UTC), loader(eia), {'reference_period': 'week ending 2026-09-25'})
    assert weekly['text'] == 'crude stocks +0.9 mb (prev +3.0 mb)' and weekly['values_source'] == 'eia'


def test_outcome_attaches_once_to_the_calendar_row_and_records_the_release(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/releases.db')
    store.initialize()
    row = nfp()
    store.put('macro_calendar', row['id'], row)
    now = T0 + timedelta(minutes=3)
    assert releases.attach(store, [row], now) == []  # no stored series yet: nothing attached, nothing guessed
    store.put_many('official_series', {r['id']: {k: v for k, v in r.items() if k not in ('id', 'ingested_at')}
                                       for rows in SERIES.values() for r in rows})
    attached = releases.attach(store, [row], now)
    assert [r['id'] for r in attached] == [row['id']]
    stored = store.get('macro_calendar', row['id'])
    assert (stored['actual'], stored['previous'], stored['unit'], stored['values_source']) == (29.0, 133.0, 'k', 'bls')
    assert stored['release_outcome']['text'] == NFP_TEXT and stored['consensus'] is None
    assert stored['actual_ingested_at'] and set(stored['lent_fields']) == set(releases.LENT)
    assert [m['name'] for m in stored['metrics']] == ['payrolls', 'unemployment'] and stored['headline_metric'] == 'payrolls'
    # Recorded once, with the time the series row was first stored (no lookahead), the outcome and no surprise.
    [record] = store.list('macro_releases')
    assert record['outcome']['text'] == NFP_TEXT and record['values_source'] == 'bls' and record['event_class'] == 'US_NFP'
    assert record['actual'] == 29.0 and record['consensus'] is None and 'raw_surprise' not in record
    first = max(releases.series_rows(store, key)[0]['ingested_at'] for key in SERIES)  # the newest period's first store
    assert record['actual_ingested_at'] == stored['actual_ingested_at'] == first
    # Idempotent, and the calendar re-fetch (a row without values of its own) keeps what was attached.
    assert releases.attach(store, [stored], now + timedelta(hours=1)) == []
    refetched = carry_lent(stored, nfp())
    assert refetched['actual'] == 29.0 and refetched['release_outcome']['text'] == NFP_TEXT and refetched['values_source'] == 'bls'
    assert releases.attach(store, [refetched], now + timedelta(hours=1)) == []
    assert len(store.list('macro_releases')) == 1


def test_outcome_never_replaces_a_vendor_actual_and_respects_its_window(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/releases.db')
    store.initialize()
    vendor = nfp(consensus=75, actual=31, unit='k')
    assert releases.attach(store, [vendor], T0 + timedelta(minutes=3), loader(SERIES)) == []
    assert releases.attach(store, [nfp()], T0 - timedelta(minutes=30), loader(SERIES)) == []  # not released yet
    late = T0 + timedelta(hours=releases._config()['attach_window_hours'] + 1)
    assert releases.attach(store, [nfp()], late, loader(SERIES)) == []  # history: nothing to show today
    date_only = calendar_row('US employment situation', 'US', 'MACRO', T0, 10, 'fred', event_class='US_NFP', time_known=False,
                             time_source='none', time_basis='date only')
    assert not releases.mapped(date_only) and releases.attach(store, [date_only], T0 + timedelta(hours=1), loader(SERIES)) == []
    assert releases.providers_for(['US_NFP', 'EIA_WPSR']) == [('bls_data', 'series'), ('fred', 'latest'), ('eia_wpsr', 'inventories')]
    assert releases.providers_for(['US_GDP']) == []


def test_release_alert_states_the_outcome_and_that_no_surprise_is_computable():
    row = releases.attached_row(nfp(), releases.outcome('US_NFP', T0, loader(SERIES), nfp()))
    alert = notifications.macro_release_alert(row, T0 + timedelta(minutes=3))
    assert alert['priority'] == 'HIGH' and alert['dedupe_key'] == f"MACRO_RELEASE:-:{row['id']}"
    assert alert['message'] == ('US employment situation (NFP, unemployment, AHE) (US): payrolls +29k (prev +133k), '
                                'unemployment 4.2% (prev 4.1%); no consensus: surprise not computable')
    assert alert['metrics']['outcome_text'] == NFP_TEXT and alert['metrics']['consensus'] is None
    vendor = notifications.macro_release_alert(nfp(consensus=75, actual=31, unit='k'), T0)
    assert vendor['message'].endswith(': actual 31, consensus 75')
    # The intraday brief's 'What changed' line states the same numbers.
    before = {'confirmed_events': [], 'released': [], 'regimes': [], 'scores': {}, 'directions': {}, 'levels': {}}
    after = dict(before, released=[row['id']])
    [change] = briefs.changes(before, after, [], [row])
    assert change == {'trigger': 'MACRO_RELEASE', 'text': f"{row['name']} released: {NFP_TEXT}"}


# --- the runtime --------------------------------------------------------------------------------------------------------

class Fake:
    configured = True

    def __init__(self, name, **methods):
        self.name, self.health = name, {'name': name, 'status': 'ONLINE'}
        self.calls = 0
        for key, value in methods.items():
            setattr(self, key, value)


def returns(owner: Fake, value):
    async def call(*args, **kwargs):
        owner.calls += 1
        return value
    return call


@pytest.fixture
def runtime(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/loops.db')
    store.initialize()
    hub = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    hub.initialize()
    return Runtime(hub)


def bls_records(scheduled: datetime | None = None) -> list[dict]:
    """The stored BLS rows as the provider returns them, re-dated so the newest period is the reference period of a
    release scheduled at `scheduled` (now): the runtime tests stay valid in any month."""
    newest = releases.expected_period(scheduled or utcnow(), None, 'previous_month')
    year, month = int(newest[:4]), int(newest[5:7])
    periods = [f'{(year if month - back > 0 else year - 1):04d}-{((month - back - 1) % 12) + 1:02d}' for back in (0, 1, 2)]
    result = []
    for rows in SERIES.values():
        for row, period in zip(rows, periods, strict=True):
            result.append({k: v for k, v in dict(row, period=period).items() if k not in ('id', 'ingested_at')})
    return result


@pytest.mark.asyncio
async def test_release_watch_polls_the_series_providers_right_after_a_mapped_release(runtime, monkeypatch):
    row = nfp(utcnow() - timedelta(seconds=90))  # released 90 s ago, inside the watch trail, no actual yet
    runtime.hub.store.put('macro_calendar', row['id'], row)
    provider = Fake('bls_data')
    provider.series = returns(provider, bls_records())
    fred = Fake('fred')
    fred.latest = returns(fred, [])
    runtime.hub.providers = {'bls_data': provider, 'fred': fred}
    observed = []
    monkeypatch.setattr(runtime, 'observe', lambda: observed.append(1))
    state = runtime.hub.calendar(window=True)
    from backend import refresh
    watch = refresh.watch_state(utcnow(), state)
    assert watch['active'] and watch['events'][0]['phase'] == 'AWAITING_ACTUAL'
    attached = await runtime.poll_release_outcomes(watch['events'])
    assert [r['id'] for r in attached] == [row['id']] and provider.calls == 1 and fred.calls == 1
    assert runtime.last_run['bls_data'] > 0  # the regular BLS poll is pushed back: one daily budget
    stored = runtime.hub.store.get('macro_calendar', row['id'])
    assert stored['release_outcome']['text'] == NFP_TEXT and stored['actual'] == 29.0
    [alert] = runtime.hub.store.list('alerts')
    assert alert['kind'] == 'MACRO_RELEASE' and NFP_TEXT in alert['message'] and observed == [1]
    # The row carries its outcome now: no further targeted poll; an unmapped or still pending row is never polled.
    assert await runtime.poll_release_outcomes(refresh.watch_state(utcnow(), runtime.hub.calendar(window=True))['events']) == []
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_release_watch_spaces_its_targeted_polls(runtime):
    row = nfp(utcnow() - timedelta(seconds=60))
    runtime.hub.store.put('macro_calendar', row['id'], row)
    provider = Fake('bls_data')
    provider.series = returns(provider, [])  # BLS has not published yet
    runtime.hub.providers = {'bls_data': provider}
    watched = [{'id': row['id'], 'phase': 'AWAITING_ACTUAL'}]
    assert await runtime.poll_release_outcomes(watched) == [] and provider.calls == 1
    assert await runtime.poll_release_outcomes(watched) == [] and provider.calls == 1  # within poll_seconds: no call
    config = runtime.hub.store and releases.sessions.ruleset()['official_data']['release_poll']
    runtime.release_polls[row['id']] = [-1e9] * config['max_polls']
    assert await runtime.poll_release_outcomes(watched) == [] and provider.calls == 1  # budget for the row spent


@pytest.mark.asyncio
async def test_official_poll_attaches_the_outcome_when_the_series_arrive(runtime, monkeypatch):
    row = nfp(utcnow() - timedelta(hours=7))  # the app was down over the release and restarted in the evening
    runtime.hub.store.put('macro_calendar', row['id'], row)
    provider = Fake('bls_data')
    provider.series = returns(provider, bls_records())
    runtime.hub.providers = {'bls_data': provider}
    monkeypatch.setattr(runtime, 'save_health', returns(Fake('health'), None))
    await runtime.poll_official()
    stored = runtime.hub.store.get('macro_calendar', row['id'])
    assert stored['release_outcome']['text'] == NFP_TEXT and stored['values_source'] == 'bls'
    assert [a['kind'] for a in runtime.hub.store.list('alerts')] == ['MACRO_RELEASE']
    assert [r['outcome']['text'] for r in releases.latest_outcomes(runtime.hub.store)] == [NFP_TEXT]


@pytest.mark.asyncio
async def test_derived_loop_sends_each_event_alert_once(runtime, monkeypatch):
    now = utcnow()
    seen = (now - timedelta(minutes=5)).isoformat()
    event = {'id': 'evt-energy', 'summary_fact': 'Tanker attacked in the Strait of Hormuz', 'event_type': 'GEOPOLITICAL_ENERGY',
             'topic': 'GEOPOLITICAL_ENERGY', 'verification_status': 'CONFIRMED', 'fact_confidence': 95.0, 'importance': 0.9,
             'assets': ['BRENT', 'WTI'], 'affected_assets': [], 'trigger': None, 'first_seen_utc': seen, 'last_updated_utc': seen}
    unassessed = dict(event, id='evt-unassessed', importance=None)
    monkeypatch.setattr(runtime.hub, 'events', lambda limit=100: [event, unassessed])
    monkeypatch.setattr(runtime, 'plain_assets', list)
    monkeypatch.setattr(runtime, 'observe', lambda: None)
    for _ in range(3):
        await runtime.derived()
    alerts = [a for a in runtime.hub.store.list('alerts') if a['kind'] == 'EVENT']
    assert [a['event_id'] for a in alerts] == ['evt-energy'] and alerts[0]['priority'] == 'HIGH'
    assert runtime.event_alerted == {'evt-energy'}  # the unassessed event may still alert once it is assessed


def test_morning_brief_gate_follows_the_first_refresh_and_the_analysis_cycle(runtime, monkeypatch):
    runtime.scheduled_kinds = set(REFRESH_KINDS) | {'derived'}
    runtime.refresh_state['started_at'] = (utcnow() - timedelta(seconds=10)).isoformat()
    morning = datetime(2026, 10, 2, 5, 0, tzinfo=UTC)  # 08:00 Bucharest: due
    assert briefs.morning_due(morning) == '2026-10-02'
    briefs.set_gate(runtime.morning_gate)
    try:
        assert briefs.morning_due(morning) is None  # no refresh yet
        assert briefs.morning_state(morning) == (False, 'waiting for the first market data refresh after start')
        for kind in ('derived', 'quotes', 'treasury_yields', 'equity_quotes', 'alpaca_quotes'):
            runtime.mark_first(kind)
        assert runtime.refresh_state['refreshed_at'] is None
        runtime.mark_first('eia_spot')  # a failing poll counts as completed: it is marked after the attempt
        refreshed = runtime.refresh_state['refreshed_at']
        assert refreshed is not None and runtime.mark_first('eia_spot') is None and runtime.refresh_state['refreshed_at'] == refreshed
        later = utcnow() + timedelta(seconds=5)
        assert briefs.morning_state(later) == (False, 'waiting for an analysis cycle on the refreshed data')
        runtime.cycle_started_at = later  # analyze() records the cycle start before recalculating
        assert briefs.morning_state(later) == (True, None) and briefs.morning_due(later) == briefs.local_now(later).date().isoformat()
    finally:
        briefs.set_gate(None)
    assert briefs.gate() is None and briefs.morning_due(morning) == '2026-10-02'
