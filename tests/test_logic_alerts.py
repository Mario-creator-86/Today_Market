"""Logic-audit regressions of 2 Oct 2026 for alerts, briefs and performance (findings AFP-01..AFP-06, RI-5):
LEVEL_BREAK cooldown per symbol, side and level kind; levels that no longer describe the market are never tested;
EVENT alerts once per newly confirmed, assessed event with an asset impact; the morning brief waits for the first data
refresh and a cycle on it; brief facts need an assessed importance or an asset impact and appear once; performance hit
rates never mix published signals with counterfactual WAIT / lean outcomes. No network, no real .env."""
from datetime import UTC, datetime, timedelta

import pytest

from backend import briefs
from backend import notifications as n
from backend.database import Decision, Store, Tick
from backend.evaluation import OutcomeTracker, performance, summarize

NOW = datetime(2026, 10, 2, 7, 30, tzinfo=UTC)
REGIME = {'regimes': ['MIXED'], 'posture': 'MIXED', 'confidence': None}


@pytest.fixture
def store(tmp_path):
    result = Store(f'sqlite:///{tmp_path}/logic.db')
    result.initialize()
    return result


def quote(price, fresh=True):
    return {'price': price, 'freshness_status': 'FRESH' if fresh else 'STALE', 'timestamp': NOW.isoformat()}


# --- AFP-02: LEVEL_BREAK cooldown ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_level_break_cooldown_holds_when_the_level_moves_every_cycle(store):
    """The level is re-anchored to every completed minute close: 2692.81, 2691.3, 2690.1 ... The dedupe key holds the
    symbol, side and level kind, never the level value, so the 1800 s cooldown gives one alert, not one per cycle."""
    dispatcher = n.Dispatcher(store, [n.InAppChannel(store)])
    outcomes = []
    for seconds, level in ((0, 2692.81), (60, 2691.3), (79, 2690.1), (127, 2688.3)):
        at = NOW + timedelta(seconds=seconds)
        alert = n.level_break_alert('ETH', quote(level - 5), {'support': level, 'support_basis': 'swing_low'}, {}, at)
        assert alert['dedupe_key'] == 'LEVEL_BREAK:ETH:support:intraday' and alert['metrics']['level'] == level
        assert f'{level:,.6g}' in alert['message']  # the level stays in the text and the metrics
        outcomes.append((await dispatcher.dispatch(alert))['status'])
    assert outcomes == ['NEW', 'DUPLICATE', 'DUPLICATE', 'DUPLICATE']
    assert len(store.list('alerts')) == 1
    # A window boundary inside the cooldown does not reset it; after the cooldown the condition alerts again.
    cooldown = n._config()['cooldown_seconds']['LEVEL_BREAK']
    edge = n.level_break_alert('ETH', quote(2680), {'support': 2685, 'support_basis': 'vwap'}, {}, NOW + timedelta(seconds=1700))
    assert (await dispatcher.dispatch(edge))['status'] == 'DUPLICATE'
    later = n.level_break_alert('ETH', quote(2670), {'support': 2675, 'support_basis': 'swing_low'}, {},
                                NOW + timedelta(seconds=cooldown + 5))
    assert (await dispatcher.dispatch(later))['status'] == 'NEW'


@pytest.mark.asyncio
async def test_level_break_other_side_symbol_or_level_kind_is_a_different_condition(store):
    dispatcher = n.Dispatcher(store, [n.InAppChannel(store)])
    first = n.level_break_alert('ETH', quote(2680), {'support': 2690, 'support_basis': 'session_low'}, {}, NOW)
    other_side = n.level_break_alert('ETH', quote(2740), {'resistance': 2730, 'resistance_basis': 'session_high'}, {}, NOW)
    other_symbol = n.level_break_alert('BTC', quote(83_000), {'support': 83_500, 'support_basis': 'swing_low'}, {}, NOW)
    daily = n.level_break_alert('ETH', quote(2600), {'support': 2650, 'support_basis': 'previous_day_low'}, {},
                                NOW + timedelta(seconds=90))
    assert daily['dedupe_key'] == 'LEVEL_BREAK:ETH:support:daily' and daily['metrics']['level_kind'] == 'daily'
    statuses = [(await dispatcher.dispatch(a))['status'] for a in (first, other_side, other_symbol, daily)]
    assert statuses == ['NEW', 'NEW', 'NEW', 'NEW']
    assert n.level_kind('range_20d_high') == 'daily' and n.level_kind('sma_50') == 'daily'
    assert n.level_kind('option_strike') == 'intraday' and n.level_kind(None) == 'intraday'


# --- AFP-01 (alert and brief side): levels that no longer describe the market -----------------------------------------

def restart_asset(reference_age: timedelta, *, expires_in: timedelta = timedelta(minutes=4), stale: bool = False) -> dict:
    """BTC +2.5% above the resistance of levels anchored to a minute close `reference_age` old."""
    reference = {'price': 83_840.02, 'basis': 'last completed minute close', 'time': (NOW - reference_age).isoformat()}
    if stale:
        reference['stale'] = True
    levels = {'support': 83_812.4, 'resistance': 83_950.0, 'atr': 41.17, 'reference': reference,
              'support_basis': 'swing_low', 'resistance_basis': 'swing_high'}
    return {'symbol': 'BTC', 'asset_class': 'crypto', 'quote': quote(85_979.2),
            'decision': {'score': 0.0, 'direction': 'WAIT', 'levels': levels, 'expires_at': (NOW + expires_in).isoformat()}}


def test_levels_anchored_to_an_old_bar_or_an_expired_decision_never_alert_or_break(store):
    current = restart_asset(timedelta(minutes=2))
    assert n.current_levels(current, NOW) is current['decision']['levels']
    assert [a['kind'] for a in n.market_alerts(store, [current], NOW)] == ['LEVEL_BREAK']
    assert briefs.level_state(current, NOW) == 'ABOVE_RESISTANCE'
    after_gap = restart_asset(timedelta(hours=20, minutes=25))  # first cycle after a restart: reference of yesterday
    flagged = restart_asset(timedelta(minutes=2), stale=True)   # the technical layer flagged the reference itself
    expired = restart_asset(timedelta(minutes=2), expires_in=-timedelta(hours=20))  # yesterday's last decision
    for asset in (after_gap, flagged, expired):
        assert n.current_levels(asset, NOW) is None
        assert n.market_alerts(store, [asset], NOW) == []
        assert briefs.level_state(asset, NOW) == 'UNKNOWN'
    snapshot = briefs.snapshot(NOW, [after_gap], [], [], REGIME)
    assert snapshot['levels'] == {'BTC': 'UNKNOWN'}
    before = dict(snapshot, levels={'BTC': 'INSIDE_RANGE'})
    assert not [c for c in briefs.changes(before, snapshot, [], []) if c['trigger'] == 'LEVEL_BREAK']
    # Levels anchored to a daily close carry a date, not a clock time: they have no minute age.
    daily = restart_asset(timedelta(minutes=2))
    daily['decision']['levels']['reference'] = {'price': 83_840.0, 'basis': 'previous daily close', 'time': '2026-10-01'}
    assert n.current_levels(daily, NOW) is daily['decision']['levels']
    no_boundary = restart_asset(timedelta(minutes=2))
    no_boundary['decision']['levels'].update(support=None, resistance=None)
    assert briefs.level_state(no_boundary, NOW) == 'UNKNOWN'


# --- AFP-04: EVENT alerts ---------------------------------------------------------------------------------------------

def confirmed(event_id, importance, *, assets=('BTC', 'ETH'), age_hours=1.0, status='CONFIRMED', **extra):
    seen = NOW - timedelta(hours=age_hours)
    return {'id': event_id, 'summary_fact': f'Fact {event_id}', 'event_type': 'REGULATION', 'topic': 'REGULATION',
            'verification_status': status, 'fact_confidence': 94.9, 'importance': importance,
            'importance_basis': 'source tier x extractor importance' if importance is not None else 'not assessed',
            'assets': list(assets), 'affected_assets': [], 'trigger': None, 'first_seen_utc': seen.isoformat(),
            'last_updated_utc': seen.isoformat(), **extra}


@pytest.mark.asyncio
async def test_event_alerts_fire_once_per_newly_confirmed_assessed_event_with_an_asset_impact(store):
    events = [confirmed('energy', 0.68), confirmed('fed', 0.9, assets=('US2Y',)),
              confirmed('unassessed', None),                      # importance never assessed: no alert, no AI call
              confirmed('minor', 0.3),                            # assessed below the threshold
              confirmed('no-assets', 0.9, assets=()),             # no identified asset impact
              confirmed('rumour', 0.9, status='UNCONFIRMED'),     # not verified
              confirmed('backlog', 0.9, age_hours=30)]            # older than event.max_age_hours: a restart is silent
    alerts = n.event_alerts(events, NOW)
    assert {a['event_id']: a['priority'] for a in alerts} == {'energy': 'MEDIUM', 'fed': 'HIGH'}
    assert {a['dedupe_key'] for a in alerts} == {'EVENT:-:energy', 'EVENT:-:fed'}  # per event id, not per status
    assert alerts[0]['metrics']['assets'] == ['BTC', 'ETH'] and alerts[0]['metrics']['importance'] == 0.68
    dispatcher = n.Dispatcher(store, [n.InAppChannel(store)])
    assert [r['status'] for r in await dispatcher.dispatch_many(alerts)] == ['NEW', 'NEW']  # two events, one day
    # The next cycles, and the day after (a new evidence document updates the event): never a second alert.
    for later in (timedelta(minutes=1), timedelta(hours=25), timedelta(days=3)):
        updated = [dict(e, last_updated_utc=(NOW + later).isoformat()) for e in events[:2]]
        again = n.event_alerts(updated, NOW + later)
        assert [r['status'] for r in await dispatcher.dispatch_many(again)] == ['DUPLICATE', 'DUPLICATE']
    assert sorted(a['id'] for a in store.list('alerts')) == ['EVENT:-:energy', 'EVENT:-:fed']


def test_event_alert_accepts_a_causal_trigger_as_the_asset_impact():
    from backend import causal
    trigger = sorted(causal.triggers())[0]
    event = confirmed('causal', 0.85, assets=(), trigger=trigger)
    alert = n.event_alert(event, NOW)
    assert alert is not None and alert['metrics']['assets'] == causal.affected_assets(trigger)
    assert n.event_alert(confirmed('plain', 0.85, assets=()), NOW) is None


# --- AFP-05: the morning brief waits for the first refresh --------------------------------------------------------------

def test_morning_brief_waits_for_the_first_refresh_and_a_cycle_on_it():
    cadence = briefs.refresh.cadence('interpretation')['seconds']
    started = datetime(2026, 10, 2, 7, 27, 18, tzinfo=UTC)
    state = {'started_at': started.isoformat(), 'refreshed_at': None}
    # First cycle at start: quotes and closes of the previous run, polls still running.
    assert briefs.morning_ready(started, state, started) == (False, 'waiting for the first market data refresh after start')
    refreshed = started + timedelta(seconds=62)
    state['refreshed_at'] = refreshed.isoformat()
    # Refreshed, but the only completed cycle started before the refresh (its decisions used yesterday's data).
    assert briefs.morning_ready(refreshed + timedelta(seconds=5), state, started) == (
        False, 'waiting for an analysis cycle on the refreshed data')
    assert briefs.morning_ready(refreshed + timedelta(seconds=5), state, None)[0] is False
    second = started + timedelta(seconds=cadence + 20)
    assert briefs.morning_ready(second, state, second) == (True, None)  # a cycle started after the refresh
    # A provider that never answers does not cancel the day's brief: after one analysis cadence the wait ends, and the
    # brief still needs a cycle started after that bound.
    down = {'started_at': started.isoformat(), 'refreshed_at': None}
    late = started + timedelta(seconds=cadence + 30)
    assert briefs.morning_ready(late, down, started)[0] is False and briefs.morning_ready(late, down, late) == (True, None)
    # No background refresh in this process (tests, one-off jobs): nothing to wait for.
    assert briefs.morning_ready(started, None, None) == (True, None)


# --- AFP-06: brief facts ----------------------------------------------------------------------------------------------

def fact(event_id, *, status='CONFIRMED', hours=1.0, confidence=95.0, kind='OFFICIAL_RELEASE', **extra):
    seen = (NOW - timedelta(hours=hours)).isoformat()
    return {'id': event_id, 'summary_fact': f'Fact {event_id}', 'event_type': kind, 'verification_status': status,
            'fact_confidence': confidence, 'sources': ['Bank of Japan'], 'first_seen_utc': seen, 'event_time': seen,
            'importance': None, **extra}


def sections(brief):
    return {s['title']: s['items'] for s in brief['sections']}


def test_confirmed_facts_need_an_assessed_importance_or_an_asset_impact():
    events = [fact('tankan', confidence=99.23),                                    # never assessed, no asset: filler
              fact('speech', confidence=99.1, extraction=None),
              fact('dao', extraction={'potential_importance': 35}),                # assessed below the minimum
              fact('sec', confidence=94.9, assets=['BTC', 'ETH', 'COIN'], hours=5),  # unassessed, asset impact
              fact('assessed', extraction={'potential_importance': 55}, hours=2),    # assessed, no asset
              fact('newer', extraction={'potential_importance': 55}, hours=0.5),
              fact('contract', importance=0.55, hours=3)]
    listed = sections(briefs.compose('MORNING', NOW, [], events, [], REGIME))['Confirmed facts']
    names = [item.split(' (')[0] for item in listed]
    # Asset impact first, then importance, then recency; an unassessed importance is never the maximum.
    assert names == ['Fact sec', 'Fact newer', 'Fact assessed', 'Fact contract']
    assert briefs.reportable(events[0]) is False and briefs.reportable(events[2]) is False
    assert briefs.reportable(events[3]) is True and briefs.fact_rank(events[3])[:2] == (True, 0.0)


def test_no_fact_is_repeated_across_brief_sections():
    major = fact('fed', importance=0.9, kind='CENTRAL_BANK', sources=['Federal Reserve'])       # catalyst and topic
    other = fact('ecb', extraction={'potential_importance': 55}, kind='CENTRAL_BANK', sources=['ECB'])
    developing = fact('boj', status='PARTIALLY_CONFIRMED', kind='CENTRAL_BANK', assets=['USDJPY'])
    twin = dict(other, id='ecb-2')  # the same fact stored twice
    changed = [{'trigger': 'MAJOR_EVENT', 'text': 'Newly confirmed: Fact fed'}]
    for kind, extra in (('MORNING', {}), ('INTRADAY', {'changed': changed})):
        brief = briefs.compose(kind, NOW, [], [major, other, developing, twin], [], REGIME, **extra)
        items = [item for s in brief['sections'] for item in s['items']]
        for name in ('Fact fed', 'Fact ecb', 'Fact boj'):
            assert sum(name in item for item in items) == 1, (kind, name, items)
    morning = sections(briefs.compose('MORNING', NOW, [], [major, other, developing, twin], [], REGIME))
    assert [i.split(' (')[0] for i in morning['Top catalysts']] == ['Fact fed']
    assert [i.split(' (')[0] for i in morning['Confirmed facts']] == ['Fact ecb']
    assert morning['Central banks'] == ['Fact boj (partially confirmed, 95% confidence)']
    assert list(morning)[:2] == ['Top catalysts', 'Confirmed facts']
    intraday = sections(briefs.compose('INTRADAY', NOW, [], [major, other], [], REGIME, changed))
    assert intraday['What changed since last update'] == ['Newly confirmed: Fact fed'] and 'Top catalysts' not in intraday


# --- RI-5: a daily close is labelled by its own date and status -------------------------------------------------------

def test_daily_close_labels_follow_the_quotes_own_close_date_and_status():
    base = {'price': 14.21, 'change_pct': -4.44, 'freshness_status': 'STALE', 'data_class': 'DAILY_CLOSE',
            'market_status': 'FRIDAY CLOSE', 'timestamp': '2026-09-22T20:00:00+00:00', 'close_date': '2026-09-22'}
    assert briefs.quote_label(dict(base, close_status='STALE')) == 'STALE close 22 Sep'      # a Tuesday close, overdue
    assert briefs.quote_label(dict(base, close_status='CURRENT', close_date='2026-09-25')) == 'DAILY CLOSE 25 Sep'
    derived = dict(base, data_class='DERIVED_DAILY', close_status='CURRENT', close_date='2026-09-25')
    assert briefs.quote_label(derived) == 'DERIVED 25 Sep'
    # No publication calendar for the source (close_status unknown): still a daily close of its own date, never
    # 'STALE' by assumption and never the venue's session.
    unknown = dict(base, close_status=None, close_date='2026-09-25')
    assert briefs.quote_label(unknown) == 'DAILY CLOSE 25 Sep'
    asset = {'symbol': 'VIX', 'asset_class': 'index', 'decision': None, 'quote': dict(base, close_status='STALE')}
    brief = briefs.compose('MORNING', datetime(2026, 9, 28, 4, 30, tzinfo=UTC), [asset], [], [], REGIME)
    assert sections(brief)['Equities'] == ['VIX 14.21 (-4.44%) · STALE close 22 Sep']
    assert brief['format'] == briefs.BRIEF_FORMAT


# --- AFP-03: published signals and counterfactual outcomes are never mixed ----------------------------------------------

T = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def outcome(decision_id, return_pct, *, actionable, side=1, horizon='4h'):
    return {'decision_id': decision_id, 'symbol': 'BTC', 'horizon': horizon, 'side': side, 'actionable': actionable,
            'counterfactual': not actionable, 'return_pct': return_pct, 'raw_return_pct': return_pct * side,
            'decision_at': T.isoformat(), 'score': 40.0 if actionable else 5.0, 'groups': {'asset': ['BTC']}}


def test_summarize_reports_signal_and_counterfactual_hit_rates_apart():
    only_counterfactual = summarize([outcome(f'w{i}', 1.0 if i < 82 else -1.0, actionable=False) for i in range(148)])
    assert only_counterfactual['observations'] == 148 and only_counterfactual['actionable'] == 0
    assert only_counterfactual['hit_rate'] is None and only_counterfactual['hit_rate_basis'] is None  # no signal measured
    assert only_counterfactual['counterfactual_hit_rate'] == pytest.approx(82 / 148)
    assert only_counterfactual['counterfactual_only'] is True and only_counterfactual['false_positive_rate'] is None
    mixed = summarize([outcome('s1', 2.0, actionable=True), outcome('s2', -1.0, actionable=True),
                       *[outcome(f'w{i}', 1.0, actionable=False) for i in range(8)]])
    assert mixed['hit_rate'] == 0.5 and mixed['hit_rate_basis'] == 'published'  # 8 winning leans do not lift it to 90%
    assert mixed['counterfactual_hit_rate'] == 1.0 and mixed['counterfactual_only'] is False
    assert (mixed['actionable'], mixed['counterfactual'], mixed['observations']) == (2, 8, 10)
    assert summarize([])['hit_rate'] is None and summarize([])['counterfactual_only'] is False


def test_performance_api_says_which_outcomes_are_signals(store):
    with store.session() as session:
        session.add(Tick(symbol='BTC', price=100, timestamp=T, ingested_at=T, provider='test', payload={}))
        session.add(Tick(symbol='BTC', price=103, timestamp=T + timedelta(seconds=14400), ingested_at=T + timedelta(seconds=14400),
                         provider='test', payload={}))
        for key, score, direction, second in (('lean', 20.0, 'BULLISH LEAN / WAIT', 0), ('wait', -3.0, 'WAIT', 1)):
            session.add(Decision(id=key, symbol='BTC', direction=direction, score=score, created_at=T + timedelta(seconds=second),
                                 payload={'id': key, 'symbol': 'BTC', 'direction': direction, 'category': direction,
                                          'score': score, 'audit_status': 'BLOCK', 'levels': {},
                                          'input_snapshot': {'quote': {'price': 100}}}))
        session.commit()
    OutcomeTracker(store).evaluate(T + timedelta(seconds=14401))
    report = performance(store)
    assert report['published_signals'] == 0 and report['counterfactual_decisions'] == 2
    four_hours = report['metrics']['4h']
    assert four_hours['counterfactual'] == 2 and four_hours['actionable'] == 0 and four_hours['counterfactual_only'] is True
    assert four_hours['hit_rate'] is None and four_hours['counterfactual_hit_rate'] == 0.5
    assert 'published signals only' in report['hit_rate_basis']
    assert performance(store, population='actionable')['metrics']['4h']['observations'] == 0
