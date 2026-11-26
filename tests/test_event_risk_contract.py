"""Review regressions for the deterministic event engine (docs/EVENT_AWARENESS.md §8, binding field shapes): session
windows of date-only rows, regime sensitivity and EVENT RISK MODE, fact-backed surprise classification, unmapped
vendor events, speaker classes, field shapes and graceful degradation. Synthetic rows through the real row builders;
no network and no real .env (tests/conftest.py blanks every key)."""
import copy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_event_aware_cases import FED_AT, RATE_FACTS, interpretation
from test_event_risk import (
    NOW,
    RISK_ON,
    asset,
    by_id,
    client,  # noqa: F401  (pytest fixture)
    cpi,
    decision,
    fomc,
    make_hub,
    news,
    price_path,
    regime,
    snap,
    statement,
)

from backend import event_risk as er
from backend.database import utcnow
from backend.providers.calendars import calendar_row, fmp_economic_rows
from backend.providers.finnhub import earnings_rows

FLAT = regime(['RISK_ON'], 'CONSTRUCTIVE')
EVENT_ID = 'FED-20261028-FOMC'


def aapl_amc(day: str = '2026-10-28') -> dict:
    return earnings_rows([{'symbol': 'AAPL', 'date': day, 'hour': 'amc', 'quarter': 4, 'year': 2026, 'epsEstimate': 2.02,
                           'revenueEstimate': 1.2e11}])[0]


def boj(day: datetime) -> dict:
    return calendar_row('BoJ monetary policy decision (MPM)', 'JP', 'CENTRAL_BANK', day, 8, 'boj_mpm_calendar',
                        event_class='BOJ', sub_event='DECISION', time_known=False, time_source='none',
                        time_basis='approximate: the BoJ releases its statement when the meeting ends (no fixed time)')


# --- date-only rows anchored to a session (§8) --------------------------------------------------------------------------

def test_after_close_earnings_leave_upcoming_after_their_session_window(tmp_path):
    hub = make_hub(tmp_path)
    rows = [aapl_amc(), cpi(datetime(2026, 10, 29, 12, 30, tzinfo=UTC))]
    assets = [asset(s, NOW) for s in ('AAPL', 'NASDAQ')]
    evening = datetime(2026, 10, 28, 21, 0, tzinfo=UTC)  # 17:00 ET: inside the after-close window
    event = by_id(snap(hub, evening, rows, assets=assets, regime_=RISK_ON)['events'], 'EARN-')
    assert event['phase'] == 'APPROACHING' and event['minutes_until_event'] is None and event['time_bucharest'] is None
    assert event['date_window']['label'] == 'after the US close' and 'after the US close (time TBA)' in event['status_note']
    after = snap(hub, datetime(2026, 10, 29, 1, 0, tzinfo=UTC), rows, assets=assets, regime_=RISK_ON)  # 21:00 ET
    assert by_id(after['events'], 'EARN-')['phase'] == 'POST_EVENT'
    assert after['next_event']['event_id'].startswith('BLS-')  # never 'next event' once the window is over
    morning = snap(hub, datetime(2026, 10, 29, 12, 0, tzinfo=UTC), rows, assets=assets, regime_=RISK_ON)
    assert not [e for e in morning['events'] if e['event_id'].startswith('EARN-')]  # resolved by the next morning
    assert morning['next_event']['event_id'].startswith('BLS-') and morning['next_event']['minutes_until_event'] == 30
    assert morning['asset_event_risk']['AAPL']['score'] == 0.0  # no stale earnings risk the day after


def test_boj_date_only_decision_uses_the_tokyo_statement_day(tmp_path):
    row = boj(datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo('Asia/Tokyo')))
    during = er.build_events([row], [], datetime(2026, 10, 30, 14, 0, tzinfo=UTC))['scheduled'][0]  # 23:00 JST
    assert during['phase'] == 'APPROACHING' and during['date_window']['label'] == 'statement day in Tokyo'
    assert during['date_window']['end_utc'] == '2026-10-30T15:00:00+00:00'
    after = er.build_events([row], [], datetime(2026, 10, 30, 15, 30, tzinfo=UTC))['scheduled'][0]  # 00:30 JST next day
    assert after['phase'] == 'POST_EVENT' and after['kind'] == 'DECISION'
    assert er._next(er.build_events([row], [], datetime(2026, 10, 30, 15, 30, tzinfo=UTC))['scheduled']) is None
    assert er.build_events([row], [], datetime(2026, 10, 30, 17, 1, tzinfo=UTC))['scheduled'] == []  # +120 min: resolved


# --- regime sensitivity and EVENT RISK MODE ----------------------------------------------------------------------------

def test_an_unexposed_moving_driver_never_changes_the_fomc_level_or_risk_mode(tmp_path):
    hub, now = make_hub(tmp_path), FED_AT - timedelta(minutes=30)
    oil = regime(['RISK_ON'], 'CONSTRUCTIVE', {'Oil': 'up'})
    rates = regime(['RISK_ON'], 'CONSTRUCTIVE', {'Rates': 'down', 'Oil': 'up'})
    results = {name: snap(hub, now, [fomc(FED_AT)], regime_=r) for name, r in (('flat', FLAT), ('oil', oil), ('rates', rates))}
    flat, moved, exposed = (results[k]['next_event'] for k in ('flat', 'oil', 'rates'))
    assert moved['relevance_score'] == flat['relevance_score'] and moved['relevance_level'] == flat['relevance_level'] == 'CRITICAL'
    assert exposed['relevance_score'] > flat['relevance_score']  # an exposed moving driver can only raise it
    assert all(results[k]['mode']['active'] for k in results)
    assert er.regime_sensitivity({'BRENT': 1.0}, oil)[0] > er.regime_sensitivity({'BRENT': 1.0}, FLAT)[0]


def test_risk_mode_is_deterministic_for_a_priced_fomc_and_its_live_press_conference(tmp_path):
    hub = make_hub(tmp_path)
    priced = fomc(FED_AT, market_pricing=RATE_FACTS['market_pricing'])
    before = snap(hub, FED_AT - timedelta(minutes=30), [priced], regime_=FLAT)
    assert before['next_event']['relevance_level'] == 'VERY HIGH'  # well priced: below CRITICAL ...
    assert before['mode']['active'] and before['mode']['event_id'] == EVENT_ID  # ... yet class importance keeps the mode
    assert before['mode']['time_utc'] == FED_AT.isoformat()
    live = snap(hub, FED_AT + timedelta(minutes=5), [fomc(FED_AT, **RATE_FACTS)], regime_=FLAT)
    assert live['events'][0]['relevance_level'] == 'VERY HIGH' and live['events'][0]['phase'] == 'IN_PROGRESS'
    assert live['mode']['active'] and live['mode']['phase'] == 'IN_PROGRESS'
    # A vendor release with a high impact but no mapped exposure never switches the mode on.
    vendor = fmp_economic_rows([{'date': (FED_AT - timedelta(minutes=20)).isoformat(), 'event': 'German Unemployment Change',
                                 'country': 'DE', 'impact': 'High', 'estimate': 10}])
    assert not snap(hub, FED_AT - timedelta(minutes=30), vendor, regime_=FLAT)['mode']['active']


def test_per_asset_event_risk_has_an_exposure_floor(tmp_path):
    floor = er._risk()['asset_risk']['exposure_floor']
    assert 0 < floor < 1 and er.exposure_factor(0.0) == 0.0 and er.exposure_factor(1.0) == 1.0
    assert er.exposure_factor(0.75) == pytest.approx(floor + (1 - floor) * 0.75)
    now = FED_AT - timedelta(minutes=30)
    result = snap(make_hub(tmp_path), now, [fomc(FED_AT)], assets=[asset('BTC', now, decision('LONG SETUP', 44))], regime_=FLAT)
    fed = result['next_event']
    assert result['asset_event_risk']['BTC']['score'] == round(fed['relevance_score'] * er.exposure_factor(0.75), 1)
    assert result['asset_event_risk']['BTC']['level'] == 'VERY HIGH'  # even with no driver moving


# --- unmapped vendor events and speaker classes ------------------------------------------------------------------------

def test_unmapped_vendor_events_are_capped_and_a_vendor_press_conference_joins_its_decision(tmp_path):
    now = datetime(2026, 10, 29, 8, 30, tzinfo=UTC)
    vendor = fmp_economic_rows([
        {'date': '2026-10-29T08:55:00Z', 'event': 'German Unemployment Change', 'country': 'DE', 'impact': 'High', 'estimate': 10},
        {'date': '2026-10-29T13:45:00Z', 'event': 'ECB Press Conference', 'country': 'EU', 'impact': 'High'}])
    decision_at = datetime(2026, 10, 29, 13, 15, tzinfo=UTC)
    ecb = calendar_row('ECB monetary policy decision', 'EU', 'CENTRAL_BANK', decision_at, 9, 'ecb_calendar', event_class='ECB',
                       sub_event='DECISION', time_known=True, time_basis='configured 14:15 CET')
    press = calendar_row('ECB press conference', 'EU', 'CENTRAL_BANK', decision_at + timedelta(minutes=30), 8, 'ecb_calendar',
                         event_class='ECB', sub_event='PRESS_CONFERENCE', parent_event_id=ecb['id'], time_known=True,
                         time_basis='configured 14:45 CET')
    result = snap(make_hub(tmp_path), now, [*vendor, ecb, press], regime_=RISK_ON)
    german = by_id(result['events'], 'EVT-')
    assert german['affected_assets'] == [] and german['relevance_score'] <= 55 and german['relevance_level'] == 'MODERATE'
    assert german['dominance_score'] <= 55 and 'capped' in german['dominance_basis']['exposure']
    assert result['next_event']['event_class'] == 'ECB'  # the mapped decision, not the vendor release
    ecb_events = [e for e in result['events'] if e['event_class'] == 'ECB']
    assert len(ecb_events) == 1 and [s['row_id'] for s in ecb_events[0]['sub_events']] == [press['id']]
    assert 'calendar' in ecb_events[0]['source_ids']  # the vendor row is a duplicate of the bank's own sub-event


def test_vice_chair_rows_use_vice_chair_classes_and_same_day_speeches_stay_separate():
    day = NOW + timedelta(hours=2)
    rows = [calendar_row('Testimony -- Vice Chair for Supervision Bowman', 'US', 'CENTRAL_BANK', day, 5, 'fed_events_calendar',
                         event_class='FED_TESTIMONY', time_known=True, time_basis='Federal Reserve calendar'),
            calendar_row('Semiannual Monetary Policy Report to the Congress -- Chair Powell', 'US', 'CENTRAL_BANK',
                         day + timedelta(minutes=30), 9, 'fed_events_calendar', event_class='FED_TESTIMONY', time_known=True),
            calendar_row('Speech -- Vice Chair Jefferson', 'US', 'CENTRAL_BANK', day - timedelta(hours=1), 4,
                         'fed_events_calendar', event_class='FED_SPEECH', time_known=True),
            calendar_row('Speech -- Vice Chair for Supervision Bowman', 'US', 'CENTRAL_BANK', day + timedelta(hours=3), 4,
                         'fed_events_calendar', event_class='FED_SPEECH', time_known=True)]
    events = er.build_events(rows, [], NOW, regime=RISK_ON)['scheduled']
    classes = {e['name']: (e['event_class'], e['speaker_role'], e['kind'], e['is_decision']) for e in events}
    assert classes['Testimony -- Vice Chair for Supervision Bowman'] == ('FED_TESTIMONY_VICE_CHAIR', 'VICE_CHAIR', 'TESTIMONY', False)
    assert classes['Semiannual Monetary Policy Report to the Congress -- Chair Powell'] == ('FED_TESTIMONY', 'CHAIR', 'TESTIMONY', False)
    speeches = [e for e in events if e['event_class'] == 'FED_SPEECH_VICE_CHAIR']
    assert len(speeches) == 2 and len({e['event_id'] for e in speeches}) == 2  # two speakers, two events
    vice = next(e for e in events if e['event_class'] == 'FED_TESTIMONY_VICE_CHAIR')
    chair = next(e for e in events if e['event_class'] == 'FED_TESTIMONY')
    assert vice['relevance_score'] < chair['relevance_score'] and vice['relevance_level'] not in {'VERY HIGH', 'CRITICAL'}
    assert vice['dominance_score'] <= 75


# --- field shapes (§8) ---------------------------------------------------------------------------------------------------

def test_consensus_kind_decision_and_incomplete_flag_shapes(tmp_path):
    ppi = fmp_economic_rows([{'date': (NOW + timedelta(hours=3)).isoformat(), 'event': 'PPI MoM', 'country': 'US',
                              'impact': 'High', 'estimate': 0.2, 'previous': 0.1, 'unit': '%'}])[0]
    minutes = calendar_row('FOMC Minutes', 'US', 'CENTRAL_BANK', NOW + timedelta(hours=4), 7, 'fed_events_calendar',
                           event_class='FOMC_MINUTES', time_known=True)
    lone = calendar_row('BoE press conference', 'GB', 'CENTRAL_BANK', NOW + timedelta(hours=5), 6, 'boe_mpc_calendar',
                        event_class='BOE', sub_event='PRESS_CONFERENCE', time_known=True)
    now = NOW - timedelta(hours=2)
    rows = [fomc(FED_AT), ppi, minutes, lone, aapl_amc()]
    result = snap(make_hub(tmp_path), now, rows, regime_=RISK_ON)
    events = {e['event_class'] + (':' + e['kind'] if e['kind'] == 'PRESS_CONFERENCE' else ''): e for e in result['events']}
    fed, producer = events['FOMC'], events['US_PPI']
    assert (fed['kind'], fed['is_decision'], fed['speaker_role']) == ('DECISION', True, None)
    assert (events['FOMC_MINUTES']['kind'], events['FOMC_MINUTES']['is_decision']) == ('MINUTES', False)
    assert (events['BOE:PRESS_CONFERENCE']['is_decision'], events['US_PPI']['kind']) == (False, 'RELEASE')
    assert producer['consensus'] == {'label': 'Consensus', 'values': [{'name': 'PPI MoM', 'value': 0.2, 'unit': '%'}],
                                     'source': 'calendar'}
    assert 'Previous: 0.1 %' in producer['known_information']  # previous is never an expectation
    earnings = events['EARNINGS']
    assert earnings['consensus']['label'] == 'Finnhub estimate'
    assert [v['name'] for v in earnings['consensus']['values']] == ['EPS', 'Revenue']
    assert any('not Street consensus' in fact for fact in earnings['known_information'])
    # EVENT DATA INCOMPLETE is flagged at the configured levels only (the date-only earnings reasons stay quiet).
    assert earnings['data_incomplete'] == ['EXACT TIME UNKNOWN'] and earnings['data_incomplete_flag'] is False
    assert fed['data_incomplete_flag'] is (fed['relevance_level'] in {'CRITICAL', 'VERY HIGH'})
    assert {i['event_id'] for i in result['data_incomplete']} == {e['event_id'] for e in result['events'] if e['data_incomplete_flag']}
    assert fed['surprise'] == er.no_surprise() and set(result['mode']) >= {'time_utc', 'phase'}


# --- surprise classification (§8) --------------------------------------------------------------------------------------

def _fed_post_event(hub, now: datetime) -> None:
    """Yields and the dollar fall, Nasdaq rallies after the decision (dovish-looking reactions)."""
    price_path(hub.store, 'US2Y', FED_AT, 3.90, {1: 3.89, 5: 3.88, 15: 3.86, 30: 3.84, 60: 3.82})
    price_path(hub.store, 'DXY', FED_AT, 100.0, {1: 99.95, 5: 99.9, 15: 99.7, 30: 99.6, 60: 99.5})
    price_path(hub.store, 'NASDAQ', FED_AT, 20000.0, {1: 20010.0, 5: 20040.0, 15: 20100.0, 30: 20150.0, 60: 20200.0})


def test_an_uncited_or_unbacked_ai_surprise_is_undetermined_and_never_grades(tmp_path):
    hub = make_hub(tmp_path)
    now = FED_AT + timedelta(minutes=100)
    _fed_post_event(hub, now)
    assets = [asset('NASDAQ', now, decision('LONG SETUP', 40, market_confirmation=70))]
    hawkish = interpretation(EVENT_ID, 'HAWKISH_SURPRISE', now - timedelta(minutes=2))  # inferred from nothing
    for news_items in ([], [statement('fomc-st', 'Federal Reserve issues FOMC statement', FED_AT + timedelta(minutes=1))]):
        result = snap(hub, now, [fomc(FED_AT, **RATE_FACTS)], news_items, assets=copy.deepcopy(assets), regime_=RISK_ON,
                      interpretation=hawkish)
        fed, post = by_id(result['events'], 'FED-'), result['post_event'][0]
        assert fed['surprise'] == er.no_surprise()
        assert post['resolved_scenario'] is None and 'UNDETERMINED' in post['resolution_basis']
        assert post['confirmation'] is None and post['divergence'] is False and post['expected'] == {}
        assert 'MARKET DIVERGENCE' not in (result['asset_event_risk']['NASDAQ']['status_reason'] or '')
    assert fed['surprise_basis']['allowed_assessments'] == ['HAWKISH_SURPRISE', 'DOVISH_SURPRISE', 'NEUTRAL_PRICED', 'UNDETERMINED']
    # A citation of a document that is not an official document of this event does not back the call either.
    wrong = interpretation(EVENT_ID, 'HAWKISH_SURPRISE', now - timedelta(minutes=2), ['ev-other'])
    other = statement('other', 'Federal Reserve issues FOMC statement', FED_AT + timedelta(minutes=1), source_id='ecb')
    assert by_id(snap(hub, now, [fomc(FED_AT, **RATE_FACTS)], [other], regime_=RISK_ON, interpretation=wrong)['events'],
                 'FED-')['surprise']['validated'] is False


def test_deterministic_and_pre_guidance_labels_are_never_a_validated_classification(tmp_path):
    hub = make_hub(tmp_path)
    now = FED_AT + timedelta(minutes=100)
    documents = [statement('fomc-st', 'Federal Reserve issues FOMC statement', FED_AT + timedelta(minutes=1))]
    rows = [fomc(FED_AT, **RATE_FACTS)]
    fallback = interpretation(EVENT_ID, 'NEUTRAL_PRICED', FED_AT + timedelta(minutes=10), ['ev-fomc-st'], source='deterministic')
    early = interpretation(EVENT_ID, 'NEUTRAL_PRICED', FED_AT + timedelta(minutes=10), ['ev-fomc-st'])  # guidance pending
    for block in (fallback, early):
        post = snap(hub, now, rows, documents, regime_=RISK_ON, interpretation=block)['post_event'][0]
        assert post['surprise'] == er.no_surprise() and post['resolved_scenario'] is None
        assert 'AI classification NEUTRAL_PRICED' not in ' '.join(post['summary_facts'])


def test_the_latest_fact_backed_ai_classification_is_kept_when_later_runs_drop_it(tmp_path):
    hub = make_hub(tmp_path)
    now = FED_AT + timedelta(minutes=110)
    _fed_post_event(hub, now)
    documents = [statement('fomc-st', 'Federal Reserve issues FOMC statement', FED_AT + timedelta(minutes=1))]
    stored = {'r1': interpretation(EVENT_ID, 'DOVISH_SURPRISE', FED_AT + timedelta(minutes=95), ['ev-fomc-st']),
              'r2': interpretation(EVENT_ID, 'UNDETERMINED', FED_AT + timedelta(minutes=100), source='deterministic'),
              'r3': dict(interpretation('OTHER-EVENT', 'UNDETERMINED', FED_AT + timedelta(minutes=105)))}
    hub.store.put_many('interpretations', stored)
    result = snap(hub, now, [fomc(FED_AT, **RATE_FACTS)], documents, regime_=RISK_ON, interpretation=None)
    fed, post = by_id(result['events'], 'FED-'), result['post_event'][0]
    assert fed['surprise'] == {'assessment': 'DOVISH_SURPRISE', 'source': 'ai', 'evidence_ids': ['ev-fomc-st'], 'validated': True}
    assert post['resolved_scenario'] == 'C' and post['confirmation'] == 100.0
    # A stored per-event record of the interpreter that is not validated is never used, whatever next_events says.
    hub.store.put('interpretations', 'r4', dict(interpretation(EVENT_ID, 'HAWKISH_SURPRISE', FED_AT + timedelta(minutes=108),
                                                               ['ev-fomc-st']),
                                                assessments={EVENT_ID: {'assessment': 'HAWKISH_SURPRISE', 'source': 'ai',
                                                                      'evidence_ids': ['ev-fomc-st'], 'validated': False}}))
    again = snap(hub, now, [fomc(FED_AT, **RATE_FACTS)], documents, regime_=RISK_ON, interpretation=None)
    assert by_id(again['events'], 'FED-')['surprise']['assessment'] == 'DOVISH_SURPRISE'


def test_fed_chair_speech_needs_its_speech_text_before_a_surprise_counts(tmp_path):
    hub = make_hub(tmp_path)
    at = NOW - timedelta(minutes=20)
    speech = calendar_row('Speech -- Chair Powell', 'US', 'CENTRAL_BANK', at, 7, 'fed_events_calendar',
                          event_class='FED_SPEECH_CHAIR', time_known=True)
    hawkish = interpretation('FED-20261028-FED_SPEECH_CHAIR', 'HAWKISH_SURPRISE', NOW - timedelta(minutes=1), ['ev-sp'])
    bare = by_id(snap(hub, NOW, [speech], regime_=RISK_ON, interpretation=hawkish)['events'], 'FED-')
    assert bare['phase'] == 'IN_PROGRESS' and bare['surprise'] == er.no_surprise()
    assert bare['surprise_basis']['allowed_assessments'] == ['UNDETERMINED', 'NEUTRAL_PRICED']  # the speech is still going
    later = NOW + timedelta(minutes=40)  # the speech (45 min) is over; the Fed published its text
    text = statement('sp', 'Speech by Chair Powell on the economic outlook', at + timedelta(minutes=5))
    after_block = interpretation('FED-20261028-FED_SPEECH_CHAIR', 'HAWKISH_SURPRISE', later - timedelta(minutes=1), ['ev-sp'])
    without = by_id(snap(hub, later, [speech], regime_=RISK_ON, interpretation=after_block)['events'], 'FED-')
    assert without['surprise_basis']['allowed_assessments'] == ['UNDETERMINED'] and without['surprise']['validated'] is False
    backed = by_id(snap(hub, later, [speech], [text], regime_=RISK_ON, interpretation=after_block)['events'], 'FED-')
    assert backed['official_documents'][0]['kind'] == 'SPEECH'
    assert backed['surprise'] == {'assessment': 'HAWKISH_SURPRISE', 'source': 'ai', 'evidence_ids': ['ev-sp'], 'validated': True}


# --- graceful degradation (§8) -----------------------------------------------------------------------------------------

def test_an_event_risk_failure_never_breaks_the_dashboard_or_asset_endpoints(client, monkeypatch):  # noqa: F811
    hub = client.app.state.hub
    hub.store.put_many('macro_calendar', {r['id']: r for r in [fomc(utcnow() + timedelta(minutes=40))]})
    healthy = client.get('/api/dashboard').json()
    assert healthy['event_risk'] is not None and 'event_risk_error' not in healthy

    def broken(*_args, **_kwargs):
        raise RuntimeError('malformed rules fragment')
    monkeypatch.setattr(er, 'snapshot', broken)
    monkeypatch.setattr(er, 'overlay', broken)
    body = client.get('/api/dashboard')
    assert body.status_code == 200
    data = body.json()
    assert data['event_risk'] is None and data['event_risk_error'] == 'event risk unavailable (RuntimeError)'
    assert data['assets'] and all(a['event_risk'] is None for a in data['assets']) and data['regime']
    listed = client.get('/api/assets')
    assert listed.status_code == 200 and listed.json()[0]['event_risk_error'] == 'event risk unavailable (RuntimeError)'
    assert client.get(f"/api/assets/{listed.json()[0]['symbol']}").status_code == 200
    failed = client.get('/api/event-risk')
    assert failed.status_code == 503 and 'malformed' not in failed.text


def test_dashboard_interpretation_uses_current_snapshot_even_without_a_readable_stored_run(client, monkeypatch):  # noqa: F811
    from backend import interpretation as module
    hub = client.app.state.hub
    assert client.get('/api/dashboard').json()['interpretation']['source'] == 'deterministic'
    output = module.deterministic_output({'generated_at': utcnow().isoformat(), 'events': [], 'regime': {}})
    hub.store.put('interpretations', 'row-1', {'source': 'deterministic', 'model': None, 'generated_at': utcnow().isoformat(),
                                               'basis_hash': 'h', 'output': output, 'label': module.DETERMINISTIC_LABEL})
    served = client.get('/api/dashboard').json()['interpretation']
    assert served['source'] == 'deterministic' and served['basis_hash'] != 'h' and not served['stale']
    assert hub.store.get('interpretations', 'row-1')['basis_hash'] == 'h'  # no read-time writes

    def broken(*_args, **_kwargs):
        raise ValueError('bad stored row')
    monkeypatch.setattr(module, 'latest', broken)
    response = client.get('/api/dashboard')
    assert response.status_code == 200 and response.json()['interpretation']['source'] == 'deterministic'


# --- event-risk audit of 2 Oct 2026 (EVR-1, EVR-3 .. EVR-10, RI-1) ------------------------------------------------------

def nfp(at: datetime, **extra) -> dict:
    return calendar_row('US employment situation (NFP, unemployment, AHE)', 'US', 'MACRO', at, 10, 'fred',
                        event_class='US_NFP', time_known=True, time_basis='FRED release calendar date + 08:30 ET', **extra)


def official_news(news_id: str, summary: str, published: datetime, source_id: str, *, event_type: str = 'OFFICIAL_RELEASE',
                  seen: datetime | None = None, assets: tuple[str, ...] = ()) -> dict:
    """A CONFIRMED news event carrying one official document of `source_id`, with no causal trigger."""
    item = statement(news_id, summary, published, source_id=source_id)
    first_seen = (seen or published).isoformat()
    return dict(item, event_type=event_type, assets=list(assets), affected_assets=[], trigger=None,
                first_seen_utc=first_seen, last_updated_utc=first_seen, importance=None)


def test_evr1_a_scenario_gives_no_direction_to_an_asset_whose_causal_paths_conflict():
    """Stronger and weaker labour data both said equities and crypto DOWN: the rates path and the growth path nearly
    cancelled. An asset the causal engine itself calls unresolved gets no direction; and since causal 2.3.0 the labour
    branches are mirror images through the rates channel (stronger: yields and USD up, equities and crypto down; weaker:
    the opposite), the growth path leading only in a GROWTH_SCARE regime."""
    templates = er._exp()['scenario_templates']
    for cls, spec in er._exp()['event_classes'].items():
        if not spec.get('scenario_template'):
            continue
        event = {'event_class': cls, 'symbol': None, 'affected_assets': er.exposure(cls)}
        found = [s for s in er.scenarios(event) if s['trigger']]
        for index, first in enumerate(found):
            assert not set(first['expected']) & set(first['unresolved'])
            for second in found[index + 1:]:
                same = [s for s, v in first['expected'].items() if v and second['expected'].get(s) == v]
                assert not same, f"{cls} ({templates[spec['scenario_template']]['scenarios'][0]['label']}): {same}"
    stronger, weaker = er.scenarios({'event_class': 'US_NFP', 'symbol': None, 'affected_assets': er.exposure('US_NFP')})
    assert stronger['expected'] == {'DXY': 1, 'US2Y': 1, 'EURUSD': -1, 'GBPUSD': -1, 'USDJPY': 1,
                                    'SP500': -1, 'NASDAQ': -1, 'RUSSELL': -1, 'BTC': -1, 'ETH': -1}
    assert weaker['expected'] == {s: -v for s, v in stronger['expected'].items()}  # mirror images
    assert stronger['unresolved'] == weaker['unresolved'] == []
    # In a growth scare the growth channel leads: weak data is bad news for equities, strong data a relief.
    scare = {'regimes': ['GROWTH_SCARE'], 'posture': 'DEFENSIVE'}
    stronger_scare, weaker_scare = er.scenarios({'event_class': 'US_NFP', 'symbol': None, 'affected_assets': er.exposure('US_NFP')}, scare)
    assert stronger_scare['expected']['SP500'] == 1 and weaker_scare['expected']['SP500'] == -1
    assert stronger_scare['expected']['US2Y'] == 1 and weaker_scare['expected']['US2Y'] == -1  # the rates leg stays
    escalation = er.scenarios({'event_class': 'GEO_ENERGY', 'symbol': None, 'affected_assets': er.exposure('GEO_ENERGY')})[0]
    assert 'US10Y' in escalation['unresolved'] and 'US10Y' not in escalation['expected'] and escalation['expected']['BRENT'] == 1


def test_evr1_a_rally_after_a_weak_jobs_report_is_not_a_market_divergence(tmp_path):
    hub, t0 = make_hub(tmp_path), datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
    for symbol, after in (('BTC', 101.9), ('ETH', 101.7)):
        price_path(hub.store, symbol, t0, 100.0, {1: after, 5: after, 15: after})
    for actual in (29, 180):  # weak: the crypto rally is the expected rates-channel reaction; strong: it diverges
        row = nfp(t0, consensus=75, actual=actual, unit='k')
        result = snap(hub, t0 + timedelta(minutes=20), [row], assets=[asset(s, t0) for s in ('BTC', 'ETH')], regime_=FLAT)
        post = result['post_event'][0]
        weak = actual < 75
        assert post['resolved_scenario'] == ('B' if weak else 'A') and post['divergence'] is (not weak)
        assert post['expected']['BTC'] == post['expected']['ETH'] == (1 if weak else -1)


def test_evr3_a_verified_unscheduled_energy_shock_switches_event_risk_mode_on(tmp_path):
    hub, now = make_hub(tmp_path), datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
    hormuz = news('hormuz-2', 'Missile strike reported on tanker in the Strait of Hormuz', 'CONFIRMED',
                  now - timedelta(minutes=20), trigger='GEOPOLITICAL_ENERGY', event_type='GEOPOLITICAL_ENERGY')
    assets = [asset(s, now) for s in ('BTC', 'BRENT', 'NASDAQ')]
    result = snap(hub, now, [], [hormuz], assets=assets, regime_=FLAT)
    active = result['active_events'][0]
    assert active['status'] == 'UNRESOLVED' and result['mode']['active'] and result['mode']['event_id'] == active['event_id']
    assert 'Oil' in result['mode']['affected_groups']
    # The mode does not hinge on the composite reaching CRITICAL: class importance 90 with exposed assets is enough
    # while the event is fresh (risk_mode.active_minutes), as for scheduled events.
    below = dict(active, relevance_level='VERY HIGH', relevance_score=84.0)
    assert er.risk_mode([], [below], now)['active'] is True
    limit = er._risk()['risk_mode']['active_minutes']
    assert er.risk_mode([], [below], now + timedelta(minutes=limit - 21))['active'] is True
    assert er.risk_mode([], [below], now + timedelta(minutes=limit))['active'] is False  # bounded in time
    assert er.risk_mode([], [dict(below, status='DE-ESCALATING')], now)['active'] is False
    assert er.risk_mode([], [dict(below, affected_assets=[])], now)['active'] is False
    assert er.risk_mode([], [dict(below, relevance_level='CRITICAL')], now + timedelta(hours=20))['active'] is True
    # A class below importance_min (sanctions: 70) and an unverified report never switch it on.
    sanctions = official_news('ofac-iran', 'OFAC: Iran-related designations', now - timedelta(minutes=20), 'ofac',
                              event_type='SANCTIONS', assets=('BRENT', 'WTI', 'GOLD'))
    rumour = dict(hormuz, id='x-9', verification_status='UNCONFIRMED')
    quiet = snap(hub, now, [], [sanctions, rumour], assets=assets, regime_=FLAT)
    assert quiet['active_events'] and quiet['potential_events'] and not quiet['mode']['active']
    assert er.risk_mode([], [])['active'] is False


def test_evr4_the_released_nfp_stays_on_the_snapshot_with_what_was_published(tmp_path):
    hub = make_hub(tmp_path)
    t0 = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
    bls = official_news('bls-nfp', 'Both payroll employment (+29,000) and unemployment rate (4.2%) change little in September',
                        datetime(2026, 10, 2, 11, 51, 8, tzinfo=UTC), 'bls', seen=datetime(2026, 10, 2, 19, 35, tzinfo=UTC))
    unrelated = official_news('bls-other', 'County employment and wages in Ohio', datetime(2026, 10, 2, 14, 0, tzinfo=UTC), 'bls')
    hub.store.put_many('official_series', {
        'fred:PAYEMS:2026-09-01': {'provider': 'fred', 'series': 'PAYEMS', 'value': 159044.0, 'previous_value': 159015.0,
                                   'period': '2026-09-01', 'realtime_start': '2026-10-02'},
        'fred:PAYEMS:2026-08-01': {'provider': 'fred', 'series': 'PAYEMS', 'value': 159015.0, 'previous_value': 158913.0,
                                   'period': '2026-08-01', 'realtime_start': '2026-09-04'},
        'fred:UNRATE:2026-09-01': {'provider': 'fred', 'series': 'UNRATE', 'value': 4.2, 'previous_value': 4.1,
                                   'period': '2026-09-01', 'realtime_start': '2026-10-02'}})
    evening = datetime(2026, 10, 2, 19, 37, tzinfo=UTC)  # 22:37 Bucharest: the event resolved at 14:15Z
    assets = [asset(s, evening) for s in ('BTC', 'NASDAQ', 'US2Y')]
    result = snap(hub, evening, [nfp(t0)], [bls, unrelated], assets=assets, regime_=FLAT)
    assert not [e for e in result['events'] if e['event_class'] == 'US_NFP']  # RESOLVED: gone from the upcoming list
    [released] = result['released_today']
    assert released['event_class'] == 'US_NFP' and released['event_id'].startswith('BLS-20261002')
    # The outcome read from the official series comes first (every headline number with its previous reading; FRED's
    # copy carries the previous value), the publisher's document stays linked.
    text = 'payrolls +29k (prev +102k), unemployment 4.2% (prev 4.1%)'
    assert released['summary'] == released['values_text'] == text and released['summary_basis'] == 'official series'
    assert released['outcome']['text'] == text and released['outcome']['source'] == 'fred'
    assert [v['name'] for v in released['values']] == ['payrolls', 'unemployment']
    assert released['news_event_id'] == 'bls-nfp' and released['document']['event_id'] == 'bls-nfp'
    assert released['time_utc'] == t0.isoformat() == released['released_utc'] and released['phase'] == 'RESOLVED'
    assert {'US2Y', 'DXY', 'NASDAQ', 'BTC'} <= {a['symbol'] for a in released['affected_assets']}
    assert released['actual'] is None and released['reaction'] is None  # no vendor actual, no live tick: nothing invented
    # Without the series the publisher's own summary is what was published.
    (tmp_path / 'empty').mkdir()
    empty_hub = make_hub(tmp_path / 'empty')
    [by_document] = snap(empty_hub, evening, [nfp(t0)], [bls, unrelated], assets=assets, regime_=FLAT)['released_today']
    assert by_document['summary'].startswith('Both payroll employment (+29,000)') and by_document['summary_basis'] == 'official document'
    assert by_document['values_text'] is None and by_document['outcome'] is None
    # Nothing published is held: the app does not claim a release. Before the release and the day after: not listed.
    assert snap(empty_hub, evening, [nfp(t0)], [unrelated], assets=assets, regime_=FLAT)['released_today'] == []
    assert snap(hub, t0 - timedelta(minutes=45), [nfp(t0)], [bls], assets=assets, regime_=FLAT)['released_today'] == []
    next_day = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
    assert snap(hub, next_day, [nfp(t0)], [bls], assets=assets, regime_=FLAT)['released_today'] == []
    # A calendar actual (vendor row) is stated as such.
    [with_actual] = snap(empty_hub, evening, [nfp(t0, consensus=75, actual=29, unit='k')], [], assets=assets,
                         regime_=FLAT)['released_today']
    assert with_actual['summary'] == 'Actual 29 k vs consensus 75' and with_actual['summary_basis'] == 'calendar actual'


def test_evr4_an_attached_outcome_is_the_event_s_published_fact_and_carries_the_measured_reaction(tmp_path):
    """The runtime attaches the outcome to the calendar row (backend/releases.attach). The event then states it in
    `outcome` and 'Published: ...', the post-event block grades nothing without a consensus, and released_today appends
    the measured reaction once a window is measured; the entry stays until the Bucharest day ends."""
    from backend import releases
    hub, t0 = make_hub(tmp_path), datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
    outcome = {'event_class': 'US_NFP', 'period': '2026-09', 'headline': 'payrolls', 'actual': 29.0, 'previous': 133.0, 'unit': 'k',
               'metrics': [{'name': 'payrolls', 'series': 'bls:CES0000000001', 'show': 'change', 'period': '2026-09', 'value': 29.0,
                            'previous': 133.0, 'unit': 'k', 'text': 'payrolls +29k (prev +133k)', 'provider': 'bls'},
                           {'name': 'unemployment', 'series': 'bls:LNS14000000', 'show': 'level', 'period': '2026-09', 'value': 4.2,
                            'previous': 4.1, 'unit': '%', 'text': 'unemployment 4.2% (prev 4.1%)', 'provider': 'bls'}],
               'text': 'payrolls +29k (prev +133k), unemployment 4.2% (prev 4.1%)', 'values_source': 'bls',
               'actual_ingested_at': (t0 + timedelta(minutes=2)).isoformat(), 'series': ['bls:CES0000000001', 'bls:LNS14000000']}
    row = releases.attached_row(nfp(t0), outcome)
    assert row['actual'] == 29.0 and row['previous'] == 133.0 and row['unit'] == 'k' and row['values_source'] == 'bls'
    assert set(row['lent_fields']) >= set(releases.LENT) and row['headline_metric'] == 'payrolls'
    for symbol, after in (('US2Y', 100.03), ('DXY', 100.2)):
        price_path(hub.store, symbol, t0, 100.0, {1: after, 5: after, 15: after})
    assets = [asset(s, t0) for s in ('US2Y', 'DXY', 'BTC')]
    live = snap(hub, t0 + timedelta(minutes=20), [row], [], assets=assets, regime_=FLAT)
    event = by_id(live['events'], 'BLS-20261002')
    assert event['phase'] == 'POST_EVENT' and event['actual'] == 29.0 and event['consensus'] == 'NO CONSENSUS'
    assert event['outcome']['text'] == outcome['text'] and event['outcome']['source'] == 'bls'
    assert f"Published: {outcome['text']} (source: bls)" in event['known_information']
    assert not [f for f in event['known_information'] if f.startswith('Actual:')]  # the fact is stated once
    [post] = live['post_event']
    assert post['resolution_basis'] == 'NO CONSENSUS: surprise direction unknown' and post['expected'] == {}
    [item] = live['released_today']
    assert item['summary_basis'] == 'official series' and item['reaction']['horizon'] == '15m'
    assert item['summary'] == f"{outcome['text']} · reaction T+15m: DXY +0.20%, US2Y +3.00 bp"
    # Resolved and still listed in the evening, with the reaction measured from the stored ticks.
    evening = snap(hub, datetime(2026, 10, 2, 19, 37, tzinfo=UTC), [row], [], assets=assets, regime_=FLAT)
    assert not [e for e in evening['events'] if e['event_class'] == 'US_NFP'] and evening['post_event'] == []
    [item] = evening['released_today']
    assert item['phase'] == 'RESOLVED' and item['summary'].endswith('reaction T+15m: DXY +0.20%, US2Y +3.00 bp')


def test_evr5_a_date_only_boj_decision_follows_its_published_statement():
    row = boj(datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo('Asia/Tokyo')))
    published = datetime(2026, 10, 30, 3, 40, tzinfo=UTC)
    doc = statement('boj-1', 'Statement on Monetary Policy', published, source_id='boj',
                    ingested=published + timedelta(minutes=1))
    before = er.build_events([row], [doc], datetime(2026, 10, 30, 3, 30, tzinfo=UTC))['scheduled'][0]
    assert before['phase'] == 'APPROACHING' and before['time_known'] is False and before['official_documents'] == []
    live = er.build_events([row], [doc], datetime(2026, 10, 30, 3, 45, tzinfo=UTC))['scheduled'][0]
    assert live['time_known'] is True and live['time_utc'] == published.isoformat() and live['phase'] == 'IN_PROGRESS'
    assert live['time_bucharest'] == '05:40' and live['date_window'] is None and live['minutes_until_event'] == -5
    assert 'official statement published at 05:40 Bucharest' in live['time_basis']
    assert [d['evidence_id'] for d in live['official_documents']] == ['ev-boj-1']
    basis = live['surprise_basis']
    assert basis['official_document_ids'] == ['ev-boj-1'] and 'official documents received: statement' in basis['basis']
    assert 'HAWKISH_SURPRISE' in basis['allowed_assessments'] and 'EXACT TIME UNKNOWN' not in live['data_incomplete']
    post = er.build_events([row], [doc], datetime(2026, 10, 30, 6, 0, tzinfo=UTC))['scheduled'][0]
    assert post['phase'] == 'POST_EVENT'  # 11 hours earlier than the end of the Tokyo day
    assert er.build_events([row], [doc], datetime(2026, 10, 30, 12, 0, tzinfo=UTC))['scheduled'] == []  # resolved
    # A document of another bank, or one outside the Tokyo statement day, changes nothing.
    other = statement('ecb-1', 'Monetary policy statement', published, source_id='ecb')
    early = statement('boj-0', 'Statement on Monetary Policy', datetime(2026, 10, 29, 3, 0, tzinfo=UTC), source_id='boj')
    still = er.build_events([row], [other, early], datetime(2026, 10, 30, 3, 45, tzinfo=UTC))['scheduled'][0]
    assert still['phase'] == 'APPROACHING' and still['time_known'] is False


def test_evr6_a_notice_without_assets_or_trigger_is_news_not_an_active_risk(tmp_path):
    hub, now = make_hub(tmp_path), datetime(2026, 10, 2, 19, 37, tzinfo=UTC)
    seen = datetime(2026, 10, 2, 19, 35, tzinfo=UTC)
    midnight_et = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)  # a date-only publication stamp ('2 Oct')
    terror = official_news('ofac-sdgt', 'OFAC: Counter Terrorism Designations', midnight_et, 'ofac', event_type='SANCTIONS',
                           seen=seen)
    listing = official_news('sls-1045', 'OFAC sanctions list update (publication 1045): 5 additions [SDGT]',
                            datetime(2026, 10, 2, 15, 55, tzinfo=UTC), 'ofac-sls', event_type='SANCTIONS', seen=seen)
    webinar = official_news('ofsi-1', 'Financial sanctions webinars and events', seen, 'ofsi')
    assets = [asset(s, now) for s in ('GOLD', 'WTI', 'BRENT', 'BTC', 'DXY')]
    result = snap(hub, now, [], [terror, listing, webinar], assets=assets, regime_=FLAT)
    assert result['active_events'] == [] and result['potential_events'] == []
    assert all(result['asset_event_risk'][s]['score'] == 0.0 for s in ('GOLD', 'WTI', 'BRENT', 'BTC', 'DXY'))
    assert er.classify_news(webinar) is None  # the bare word 'sanctions' no longer classifies an event
    # Iran-related designations carry their own oil / gold mapping: one active event for the publisher's day, exposed
    # only on the assets the event itself names, first seen when the app saw it.
    iran = official_news('ofac-iran', 'OFAC: Iran-related Designations', midnight_et, 'ofac', event_type='SANCTIONS',
                         seen=seen, assets=('BRENT', 'GOLD', 'WTI'))
    iran_list = official_news('sls-1041', 'OFAC sanctions list update (publication 1041): 31 additions [IRAN]',
                              datetime(2026, 10, 2, 16, 36, tzinfo=UTC), 'ofac-sls', event_type='SANCTIONS', seen=seen,
                              assets=('BRENT', 'GOLD', 'WTI'))
    [active] = snap(hub, now, [], [iran, iran_list, terror], assets=assets, regime_=FLAT)['active_events']
    assert {a['symbol'] for a in active['affected_assets']} == {'BRENT', 'GOLD', 'WTI'}  # not BTC / DXY of the class map
    assert active['first_seen_utc'] == seen.isoformat()
    assert any('1 other notice merged' in fact for fact in active['known_information'])
    [alone] = snap(hub, now, [], [iran], assets=assets, regime_=FLAT)['active_events']
    assert alone['published_utc'] == midnight_et.isoformat() and alone['published_date_only'] is True
    # The 24 h window of a date-only stamp starts at first seen: still active 23 h after the app saw it.
    late = seen + timedelta(hours=23)
    assert snap(hub, late, [], [iran], assets=[asset('BRENT', late)], regime_=FLAT)['active_events']
    assert er.date_only_stamp(datetime(2026, 10, 2, 15, 55, 41, tzinfo=UTC)) is False


def test_evr7_the_calendar_and_the_dashboard_agree_on_relevance(tmp_path):
    hub, now = make_hub(tmp_path), datetime(2026, 10, 2, 19, 37, tzinfo=UTC)
    eia = calendar_row('EIA Weekly Petroleum Status Report', 'US', 'MACRO', datetime(2026, 10, 7, 14, 30, tzinfo=UTC), 5,
                       'eia_wpsr_calendar', event_class='EIA_WPSR', time_known=True)
    moving = regime(['RISK_ON'], 'CONSTRUCTIVE', {'Oil': 'down'})
    shown = snap(hub, now, [eia], assets=[asset('WTI', now)], regime_=moving)['events'][0]
    listed = er.calendar_context([eia], now, regime=moving, news=[], stored_history=er.history(hub.store))[0]
    assert listed['relevance_score'] == shown['relevance_score'] and listed['relevance_level'] == shown['relevance_level']
    static = er.calendar_context([eia], now)[0]
    assert static['relevance_score'] < listed['relevance_score']  # what the calendar page showed before


def test_evr7_calendar_endpoint_uses_the_dashboard_inputs(client, monkeypatch):  # noqa: F811
    hub = client.app.state.hub
    now = utcnow()
    eia = calendar_row('EIA Weekly Petroleum Status Report', 'US', 'MACRO', now + timedelta(days=2), 5, 'eia_wpsr_calendar',
                       event_class='EIA_WPSR', time_known=True)
    hub.store.put('macro_calendar', eia['id'], eia)
    moving = regime(['RISK_ON'], 'CONSTRUCTIVE', {'Oil': 'down'})
    original = hub.event_risk_inputs
    monkeypatch.setattr(hub, 'event_risk_inputs', lambda at: dict(original(at), regime=moving))
    row = next(r for r in client.get('/api/calendar').json() if r['id'] == eia['id'])
    event = next(e for e in client.get('/api/events/upcoming').json() if e['row_id'] == eia['id'])
    assert row['relevance_score'] == pytest.approx(event['relevance_score'], abs=0.2) and row['relevance_level'] == event['relevance_level']
    assert row['relevance_score'] > er.calendar_context([eia], now)[0]['relevance_score']


def test_evr8_a_daily_option_expiry_is_never_the_key_event():
    def expiry(at: datetime) -> dict:
        return calendar_row('BTC options expiry', 'CRYPTO', 'OPTION_EXPIRY', at, 4, 'crypto_expiries', currency='BTC',
                            event_class='CRYPTO_EXPIRY', time_known=True)
    saturday = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
    assert [er.expiry_tenor(d) for d in (saturday, datetime(2026, 10, 9, 8, tzinfo=UTC), datetime(2026, 10, 30, 8, tzinfo=UTC),
                                         datetime(2026, 12, 25, 8, tzinfo=UTC))] == ['daily', 'weekly', 'monthly', 'quarterly']
    daily = er.build_events([expiry(saturday)], [], saturday - timedelta(hours=1), regime=FLAT)['scheduled']
    assert daily[0]['importance_score'] == 20 and daily[0]['relevance_score'] < er._risk()['dashboard_min_relevance']
    assert er._next(daily) is None  # was 42.8 MODERATE and the 'Next key event' every morning
    monthly_at = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
    monthly = er.build_events([expiry(monthly_at)], [], monthly_at - timedelta(hours=1), regime=FLAT)['scheduled']
    assert monthly[0]['importance_score'] == 50 and monthly[0]['relevance_score'] >= er._risk()['dashboard_min_relevance']
    assert er.tenor_importance('US_CPI', saturday) is None


def test_evr9_an_unemployment_rate_above_consensus_is_the_weaker_branch():
    t0 = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
    lent = nfp(t0, consensus=4.1, actual=4.2, metric='Unemployment Rate', unit='%', values_source='calendar')
    event = er.build_events([lent], [], t0 + timedelta(minutes=20))['scheduled'][0]
    scenario, basis = er.resolved_scenario(event)
    assert scenario == 'B' and 'actual 4.2 above consensus 4.1' in basis and 'higher value is the weaker outcome' in basis
    payrolls = er.build_events([nfp(t0, consensus=75, actual=180, metric='Nonfarm Payrolls')], [], t0 + timedelta(minutes=20))
    assert er.resolved_scenario(payrolls['scheduled'][0]) == ('A', 'actual 180 above consensus 75')
    lower = er.build_events([nfp(t0, consensus=4.2, actual=4.1, metric='Unemployment Rate')], [], t0 + timedelta(minutes=20))
    assert er.resolved_scenario(lower['scheduled'][0])[0] == 'A'
    broken = copy.deepcopy(er.settings())
    broken['event_exposure']['scenario_templates']['labor_release']['surprise_polarity'][0]['sign'] = 0
    with pytest.raises(ValueError, match='surprise_polarity'):
        er.validate(broken)


def test_evr10_the_not_confirming_reason_names_the_missing_move():
    assets = [asset('BTC', NOW, decision('BULLISH LEAN / WAIT', 30), change=-0.53)]
    view = er.market_view(assets, RISK_ON, {})
    item = next(i for i in view['non_confirming'] if i['symbol'] == 'BTC')
    assert item['reason_facts'][-1] == 'current price change does not confirm (no move beyond noise in the implied direction)'
    assert any(f.startswith('Decision BULLISH LEAN / WAIT') for f in item['reason_facts'])  # context, listed, not denied
    assert not any('no aligned decision side' in f for f in item['reason_facts'])


def test_ri1_a_reversal_the_current_observations_already_show_is_not_a_future_risk():
    features = {'SP500': {'price': 7743.41, 'change_pct': 0.51}}
    risk_on = dict(regime(['RISK_ON'], 'CONSTRUCTIVE', {'Risk appetite': 'up'}, features), basis='last_close')
    risk_on['drivers'] = [{'driver': 'Risk appetite', 'direction': 'up', 'arrow': None, 'missing': [],
                           'inputs': [{'feature': 'SP500', 'close_status': 'CURRENT', 'close_date': '2026-09-25'}]}]
    pending = er.what_can_break(None, {'favored': [], 'pressured': []}, risk_on, [])
    assert [(b['text'], b['kind']) for b in pending] == [('SP500 reversal lower', 'driver')]
    diverging = dict(risk_on, divergences=[{'feature': 'SP500', 'historical_value': 0.51, 'current_value': -0.5043,
                                            'text': 'SP500 -0.50% (vs previous close via SPY) opposes the historical input '
                                                    '+0.51%; the periods and instruments may differ.'}])
    [observed] = er.what_can_break(None, {'favored': [], 'pressured': []}, diverging, [])
    assert observed['kind'] == 'observed'
    assert observed['text'] == ('SP500 reversal lower already under way: SP500 -0.50% (vs previous close via SPY) '
                                'opposes the historical input +0.51%')
    # A current move in the regime's own direction is no reversal: the item stays a future condition.
    same_side = dict(risk_on, divergences=[{'feature': 'SP500', 'historical_value': -0.3, 'current_value': 0.4, 'text': 'x'}])
    assert er.what_can_break(None, {'favored': [], 'pressured': []}, same_side, [])[0]['kind'] == 'driver'
