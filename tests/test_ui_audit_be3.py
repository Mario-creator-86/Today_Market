"""Dashboard audit fixes of the event-risk, regime and interpretation layer:

- market-view facts name the basis of the value they quote (live 24h, 24h via PAXG, last close <day>), never the
  regime-wide label (DASH-01);
- regime driver inputs carry close_status / close_date, and a STALE close that still votes is named in what can break
  and in the summary (DASH-02);
- every event carries the calendar row's own 0-10 impact beside the relevance-based impact_10 (DASH-03, CAL-4);
- the deterministic summary names the closed markets (DASH-05), joins scenarios of the same tone and shows publishers
  instead of provider ids (DASH-13), leaves what can break out of the key risks (DASH-17), writes dates as 'Wed 30 Sep'
  (DASH-18) and says EVENT DATA INCOMPLETE only for a flagged event (M2);
- round 2: an AI key risk that repeats what can break is dropped and the prompt says so (DASH-17), the model's current
  time has an unpadded day (DASH-18) and no calendar impact or impact_10 reaches the model, not even through the active
  events or the risk mode (C10);
- round 3: the model sees providers by label and plain status, a text citing an event id, a status code or a provider id
  is rejected, scores reach the model as whole numbers like the page shows them (REG-dashboard-1), and a date-only row
  without a session is labelled with its whole day and zone while Finnhub session texts carry no vendor codes
  (REG-calendar_fx_perf_alerts-4)."""
import json
import re
from datetime import UTC, datetime, timedelta

from test_event_risk import NOW, RISK_ON, earnings, fomc, make_hub, news, snap
from test_interpretation import REGIME, ai_output
from test_interpretation import fomc as interpreted_fomc
from test_interpretation import snapshot as interpreted_snapshot
from test_regime_last_close import SUNDAY, close, weekend_assets

from backend import event_risk as er
from backend import interpretation
from backend.ai import numeric_allowlist, system_prompt
from backend.audit import untraceable
from backend.config import rules
from backend.notifications import status_text
from backend.providers.base import provider_label
from backend.providers.calendars import calendar_row
from backend.regime import day_label, drivers, evaluate

STALE_VIX = close('VIX', 18.0, provider='cboe', status='STALE', stamp=datetime(2026, 9, 22, 20, tzinfo=UTC),
                  change_pct=-4.44)
EMPTY = {'favored': [], 'pressured': []}


def deterministic(block: dict, regime: dict | None = None) -> tuple[dict, dict]:
    data = interpretation.bundle(block, regime or REGIME, [], NOW)
    return interpretation.deterministic_output(data), data


# --- DASH-01 / C3: the basis of each market-view value -------------------------------------------------------------------

def test_market_view_facts_name_the_basis_of_the_value_used_not_the_regime_label():
    assets = [*weekend_assets(), close('USDJPY', 150.0, provider='ecb', change_pct=-0.3)]
    regime = evaluate(assets, [], SUNDAY, last_close=True)
    assert regime['basis'] == 'last_close' and 'BTC' not in regime['features']  # rolling 24h is not a close
    lists = er.market_view(assets, regime, {})
    facts = {i['symbol']: fact for items in lists.values() for i in items for fact in i['reason_facts']
             if fact.startswith(f"{i['symbol']} ")}  # the measured-change fact
    assert facts['BTC'] == 'BTC +2.00% (live 24h)'  # its own live quote, never 'last close ... + proxies'
    assert facts['GOLD'] == 'GOLD +0.40% (24h via PAXG)'
    assert facts['SP500'] == 'SP500 -0.80% (via SPY)'
    assert facts['USDJPY'] == 'USDJPY -0.30% (last close Fri 25 Sep)'
    assert not any(regime['basis_label'] in fact for fact in facts.values())


def test_change_basis_words_follow_the_source_of_the_value():
    assert er.change_basis({'basis': 'last_close', 'close_date': '2026-09-22', 'close_status': 'STALE'}, False) \
        == 'last close Tue 22 Sep, stale'
    assert er.change_basis({'basis': 'proxy', 'via': 'SPY', 'change_basis': 'previous_close'}, False) == 'via SPY'
    assert er.change_basis({'change_basis': 'rolling_24h'}, True) == 'live 24h'
    assert er.change_basis({'change_basis': 'rolling_24h'}, False) == '24h, not live'  # never 'live' when not FRESH
    assert er.change_basis({'change_basis': 'previous_fix'}, False) == 'vs previous fix'
    assert er.change_basis({'change_basis': 'previous_close'}, True) == 'live intraday'
    assert er.change_basis({}, False) == 'latest quote'
    # A regime without feature sources (an older stored one) keeps its own label for the values it observed.
    assert er._change('NASDAQ', None, RISK_ON) == (1.0, 'pct', 'live intraday')


# --- DASH-02 / C2: a stale close that still votes -----------------------------------------------------------------------

def test_driver_inputs_carry_close_status_and_date():
    assets = [a for a in weekend_assets() if a['symbol'] != 'SP500'] + [STALE_VIX]
    regime = evaluate(assets, [], SUNDAY, last_close=True)
    inputs = {i['feature']: i for d in regime['drivers'] for i in d['inputs']}
    assert (inputs['VIX']['close_status'], inputs['VIX']['close_date']) == ('STALE', '2026-09-22')
    assert (inputs['US2Y']['close_status'], inputs['US2Y']['close_date']) == ('CURRENT', '2026-09-25')
    assert inputs['VIX']['vote'] == 1  # an older close still votes while its market is closed (by design)
    live = drivers({'DXY': {'change_pct': 0.5}}, {'DXY': {'basis': 'live', 'source': 'finnhub'}})
    dxy = next(d for d in live if d['driver'] == 'USD')['inputs'][0]
    assert (dxy['close_status'], dxy['close_date']) == (None, None)  # not a close


def test_a_stale_close_is_named_in_what_can_break_and_in_the_summary():
    # The stale VIX level must support an active rule before its reversal can invalidate it.
    low_vix = dict(STALE_VIX, quote=dict(STALE_VIX['quote'], price=13.0))
    assets = [a for a in weekend_assets() if a['symbol'] != 'SP500'] + [low_vix]
    regime = evaluate(assets, [], SUNDAY, last_close=True)
    breaks = er.what_can_break(None, EMPTY, regime, [])
    assert {'text': 'VIX reversal higher (stale close Tue 22 Sep)', 'kind': 'driver', 'source': 'Risk appetite'} in breaks
    assert not any('US2Y' in item['text'] for item in breaks)  # unrelated to these active rules
    block = er._regime_block(regime)  # what the interpreter reads from the event-risk snapshot
    assert {'feature': 'VIX', 'close_status': 'STALE', 'close_date': '2026-09-22'} in \
        next(d for d in block['drivers'] if d['driver'] == 'Risk appetite')['inputs']
    output, data = deterministic(interpreted_snapshot(what_can_break=breaks), block)
    assert 'Risk appetite ↑ (VIX −4.44%; VIX close of Tue 22 Sep is stale)' in output['market_summary']
    assert untraceable(output, data) == set() and interpretation.violations(output, data) == []


# --- DASH-03 / CAL-4 / C10: calendar impact beside relevance --------------------------------------------------------------

def test_events_and_risk_mode_carry_the_calendar_impact_beside_the_relevance(tmp_path):
    pce = calendar_row('Personal income & outlays (PCE)', 'US', 'MACRO', NOW + timedelta(hours=20), 9,
                       'bea_release_calendar', time_known=True, time_basis='BEA release schedule',
                       attribution='Source: U.S. Bureau of Economic Analysis')
    hormuz = news('hormuz-1', 'Tanker attacked in the Strait of Hormuz; shipping halted', 'CONFIRMED',
                  NOW - timedelta(hours=2), trigger='GEOPOLITICAL_ENERGY', event_type='GEOPOLITICAL_ENERGY')
    result = snap(make_hub(tmp_path), NOW, [fomc(NOW + timedelta(minutes=30)), pce], [hormuz], regime_=RISK_ON)
    fed = result['next_event']
    assert fed['impact'] == 10 and fed['impact_10'] == round(fed['relevance_score'] / 10, 1)
    listed = er.build_events([pce], [], NOW, regime=RISK_ON)['scheduled'][0]
    assert listed['impact'] == 9  # the number the calendar and 'Next important events' show
    mode = result['mode']
    assert mode['active'] and (mode['impact'], mode['relevance_score'], mode['relevance_level']) == \
        (fed['impact'], fed['relevance_score'], fed['relevance_level'])
    assert result['active_events'][0]['impact'] is None  # an unscheduled event has no calendar row
    assert er.risk_mode([], [])['impact'] is None


# --- DASH-13: sources by name ---------------------------------------------------------------------------------------------

def test_known_information_names_the_publisher_never_the_provider_ids():
    pce = calendar_row('Personal income & outlays (PCE)', 'US', 'MACRO', NOW + timedelta(hours=20), 9,
                       'bea_release_calendar', time_known=True, time_basis='BEA release schedule',
                       attribution='Source: U.S. Bureau of Economic Analysis')
    events = er.build_events([pce, fomc(NOW + timedelta(hours=2))], [], NOW)['scheduled']
    known = {e['name']: e['known_information'][0] for e in events}
    assert known['Personal income & outlays (PCE)'] == 'Source: U.S. Bureau of Economic Analysis'
    assert known['FOMC rate decision'] == 'Source: fomc calendar'  # no attribution on the row: its provider, in words
    assert not any('_' in fact or '(Source:' in fact for fact in known.values())


def test_scenarios_are_named_with_their_directions_in_one_sentence():
    # 2 Oct 2026: the per-event bull / bear / neutral cases repeated the scenario grid the page already shows; the
    # deterministic summary now gives one sentence per event (interpretation._explain).
    jolts = interpreted_fomc(
        event_id='BLS-20261029-JOLTS', name='US JOLTS', event_class='US_JOLTS', sub_events=[], surprise_basis={},
        scenarios=[{'id': 'A', 'label': 'Stronger than consensus', 'expected': {'SP500': -1, 'NASDAQ': -1, 'DXY': 1, 'US2Y': 1}},
                   {'id': 'B', 'label': 'Weaker than consensus', 'expected': {'SP500': -1, 'NASDAQ': -1, 'DXY': -1, 'US2Y': -1}}])
    output, data = deterministic(interpreted_snapshot(jolts))
    explained = next(e for e in output['next_events'] if e['event_id'] == 'BLS-20261029-JOLTS')
    assert explained['why_it_matters'] == ('Possible outcomes: Stronger than consensus (SP500 ↓, NASDAQ ↓, DXY ↑, US2Y ↑) or '
                                           'Weaker than consensus (SP500 ↓, NASDAQ ↓, DXY ↓, US2Y ↓).')
    assert set(explained) == {'event_id', 'why_it_matters', 'surprise_assessment', 'evidence_ids'}
    assert untraceable(output, data) == set()
    bare, _ = deterministic(interpreted_snapshot(interpreted_fomc(scenarios=[])))
    assert bare['next_events'][0]['why_it_matters'].startswith('No expected direction is defined for this event')


# --- DASH-05: closed markets by name --------------------------------------------------------------------------------------

def test_the_summary_names_the_closed_markets_and_says_all_only_when_all_are():
    partly = {'crypto': True, 'us_equities': False, 'us_rates': True, 'fx': True, 'commodities': True}
    sentence = ('US equities closed: event implications there cannot be confirmed until that market reopens; US rates, '
                'FX, commodities and crypto markets are open.')
    assert interpretation._weekend(partly) == sentence
    two = dict(partly, commodities=False)
    assert interpretation._weekend(two).startswith('US equities and commodities closed: event implications there cannot '
                                                   'be confirmed until they reopen; US rates, FX and crypto markets are')
    shut = {'crypto': True, 'us_equities': False, 'us_rates': False, 'fx': False, 'commodities': False}
    assert interpretation._weekend(shut) == ('Traditional markets are closed (US equities, US rates, FX, commodities), so '
                                             'event implications for them cannot be confirmed until they reopen; crypto '
                                             'market is open.')
    assert interpretation._weekend(dict(partly, us_equities=True)) is None
    watch = [{'asset': 'NASDAQ', 'trade_status': 'WAIT', 'awaiting_reopen': True}]
    output, data = deterministic(interpreted_snapshot(markets_open=partly, trade_watch=watch))
    # Said once (2 Oct 2026): in the summary; the market view carries the regime and the event only.
    assert sentence in output['market_summary'] and sentence not in output['ai_view']
    assert 'Traditional markets are closed' not in output['ai_view'] + output['market_summary']
    assert interpretation.violations(output, data) == []


# --- DASH-17: key risks do not repeat what can break ----------------------------------------------------------------------

def test_deterministic_key_risks_leave_what_can_break_to_its_own_panel():
    block = interpreted_snapshot()
    output, _ = deterministic(block)
    assert not set(output['key_risks']) & set(block['what_can_break'])
    assert output['key_risks'] == ['EVENT DATA INCOMPLETE: FOMC rate decision (consensus, market pricing)']


# --- DASH-18: dates as the page writes them -------------------------------------------------------------------------------

def test_dates_read_like_the_page_and_stay_traceable(tmp_path):
    cpi = interpreted_fomc(event_id='BLS-20261030-CPI', name='US CPI', event_class='US_CPI', sub_events=[],
                           surprise_basis={}, scenarios=[], time_utc='2026-10-30T12:30:00+00:00', time_bucharest='14:30',
                           date_bucharest='2026-10-30')
    output, data = deterministic(interpreted_snapshot(cpi))
    # The market view names the event once; its day and time are on the event card (2 Oct 2026: each fact once).
    assert output['ai_view'].endswith('The dominant event to watch is US CPI.') and '14:30' not in output['ai_view']
    assert '2026-10-30' not in json.dumps(output, ensure_ascii=False)
    assert data['event_risk']['next_event']['day_bucharest'] == 'Fri 30 Oct'
    assert 'impact' not in data['event_risk']['next_event']  # calendar impact is for the page, not the model
    assert untraceable(output, data) == set() and interpretation.violations(output, data) == []
    assert interpretation._when({'date_bucharest': '2026-10-30', 'time_known': False}, None) == 'on Fri 30 Oct (time TBA)'
    apple = snap(make_hub(tmp_path), NOW, [earnings('AAPL', datetime(2026, 10, 29, 20, 0, tzinfo=UTC))], regime_=RISK_ON)
    note = next(e for e in apple['events'] if e['event_class'] == 'EARNINGS')['status_note']
    assert note.startswith('AAPL EARNINGS on Thu 29 Oct') and note.endswith('(time TBA)')
    assert (day_label('2026-09-25'), day_label('2026-09-25T20:00:00+00:00'), day_label(None), day_label('soon')) == \
        ('Fri 25 Sep', 'Fri 25 Sep', None, None)


# --- M2: EVENT DATA INCOMPLETE only when flagged --------------------------------------------------------------------------

def test_event_data_incomplete_is_said_only_for_a_flagged_event():
    pce = interpreted_fomc(event_id='BEA-20261030-US_PCE', name='US personal income & outlays (PCE)',
                           event_class='US_PCE', relevance_level='HIGH', relevance_score=69.6, sub_events=[],
                           surprise_basis={}, scenarios=[], data_incomplete=['NO CONSENSUS'], data_incomplete_flag=False)
    output, _ = deterministic(interpreted_snapshot(pce, data_incomplete=[]))
    # A missing consensus and pricing is shown by the next-event card; the summary states expectations only when a
    # value exists (2 Oct 2026: each fact once).
    assert 'NO CONSENSUS' not in output['market_summary'] and 'Market expectation' not in output['market_summary']
    assert 'EVENT DATA INCOMPLETE' not in output['market_summary'] and output['key_risks'] == []
    flagged = dict(pce, relevance_level='VERY HIGH', data_incomplete_flag=True)
    listed = [{'event_id': pce['event_id'], 'name': pce['name'], 'reasons': ['NO CONSENSUS']}]
    output, _ = deterministic(interpreted_snapshot(flagged, data_incomplete=listed))
    assert 'EVENT DATA INCOMPLETE' not in output['market_summary']  # said once, as the key risk below
    assert output['key_risks'] == ['EVENT DATA INCOMPLETE: US personal income & outlays (PCE) (NO CONSENSUS)']
    priced = dict(pce, consensus={'core_pce_mom': '0.2%'}, data_incomplete=[])
    output, _ = deterministic(interpreted_snapshot(priced, data_incomplete=[]))
    assert ('Market expectation for US personal income & outlays (PCE) — consensus: core pce mom 0.2%; market pricing: '
            'UNKNOWN.') in output['market_summary']


# --- round 2 ---------------------------------------------------------------------------------------------------------------

def test_an_ai_key_risk_that_repeats_what_can_break_is_dropped_and_the_prompt_says_so():
    breaks = [{'text': 'US2Y reversal higher', 'kind': 'driver', 'source': 'Rates'},
              {'text': 'VIX reversal higher (stale close Tue 22 Sep)', 'kind': 'driver', 'source': 'Risk appetite'},
              'Fed hawkish surprise']
    data = interpretation.bundle(interpreted_snapshot(what_can_break=breaks), REGIME, [], NOW)
    other = 'Consensus and market pricing for the FOMC decision are missing.'
    risks = ['us2y  reversal HIGHER', 'VIX reversal higher (stale close Tue 22 Sep)', ' Fed hawkish surprise ', other]
    stored, _ = interpretation.finalize(ai_output(key_risks=risks), data, 'ai', NOW.isoformat())
    assert stored['key_risks'] == [other]  # the page shows the rest in What can break, right above
    text, version, _ = system_prompt('market_interpreter', {})
    assert 'key_risks never repeat an item of what_can_break' in text and version == '3.1.0'


def test_the_current_time_given_to_the_model_has_an_unpadded_day():
    block = interpreted_snapshot(generated_at='2026-10-05T04:22:00+00:00')
    assert interpretation.bundle(block, REGIME, [], NOW)['as_of_bucharest'] == 'Mon 5 Oct 2026 07:22 Europe/Bucharest'


def test_no_impact_number_reaches_the_model_through_events_active_events_or_the_risk_mode(tmp_path):
    hormuz = news('hormuz-1', 'Tanker attacked in the Strait of Hormuz; shipping halted', 'CONFIRMED',
                  NOW - timedelta(hours=2), trigger='GEOPOLITICAL_ENERGY', event_type='GEOPOLITICAL_ENERGY')
    block = snap(make_hub(tmp_path), NOW, [fomc(NOW + timedelta(minutes=30))], [hormuz], regime_=RISK_ON)
    assert block['mode']['impact'] == 10 and block['active_events'][0]['impact_10'] is not None  # on the page
    data = interpretation.bundle(block, REGIME, [], NOW)
    seen = data['event_risk']
    shown = [seen['mode'], seen['next_event'], seen['dominant_event'], *seen['events'], *seen['active_events']]
    assert not any({'impact', 'impact_10'} & set(item) for item in shown)
    # importance is explained by relevance, a whole number as the page shows it
    assert seen['mode']['relevance_score'] == round(block['mode']['relevance_score'])
    active = seen['active_events'][0]
    assert (active['status'], active['timing']) == ('UNRESOLVED', 'ANYTIME') and 'row_id' not in active
    assert all('weight' not in a for a in active['affected_assets'])
    output = interpretation.deterministic_output(data)
    assert untraceable(output, data) == set() and interpretation.violations(output, data) == []


# --- round 3 ---------------------------------------------------------------------------------------------------------------

def test_the_model_sees_providers_by_label_and_plain_status_never_ids_or_codes():
    health = [{'name': 'finnhub', 'label': 'Finnhub (US equities)', 'status': 'RATE_LIMITED', 'error': 'HTTPStatusError'},
              {'name': 'massive', 'status': 'AUTH_ERROR'}, {'name': 'binance', 'label': 'Binance', 'status': 'ONLINE'}]
    data = interpretation.bundle(interpreted_snapshot(), REGIME, health, NOW)
    assert data['provider_health'] == [{'name': 'Finnhub (US equities)', 'status': status_text('RATE_LIMITED')},
                                       {'name': provider_label('massive'), 'status': status_text('AUTH_ERROR')}]
    assert not re.search(r'finnhub|massive|RATE_LIMITED|AUTH_ERROR|HTTPStatusError', json.dumps(data['provider_health']))


def test_a_text_citing_an_event_id_a_status_code_or_a_provider_id_is_rejected():
    data = interpretation.bundle(interpreted_snapshot(), REGIME, [], NOW)
    leaks = ['The US employment situation report (BLS-20261002-US_NFP) is flagged as incomplete.',
             'Some data providers are degraded (finnhub RATE_LIMITED, massive AUTH_ERROR).',
             'FED-20261028-FOMC can change the view.',  # an event id of the input
             'The Hormuz report (GEO-20261028-a3f1c2d4) is unresolved.',  # an event id shape with a hex suffix
             'A HAWKISH_SURPRISE would lift US2Y.', 'The x-federalreserve feed is quiet.']
    for text in leaks:
        problems = interpretation.violations(ai_output(key_risks=[text]), data)
        assert any('ids or codes' in p for p in problems), text
    problems = interpretation._plain_language_problems(ai_output(key_risks=[leaks[1]]), data)
    assert "['AUTH_ERROR', 'RATE_LIMITED', 'finnhub']" in ' '.join(problems)  # 'massive' is also an ordinary word
    plain = ('Finnhub (US equities) is rate limited, so some quotes may lag; a massive surprise from the Fed at 20:00 '
             'Bucharest would matter more than the regime.')
    assert interpretation._plain_language_problems(ai_output(key_risks=[plain]), data) == []
    text, _, _ = system_prompt('market_interpreter', {})
    assert 'Never write an event_id, an evidence_id, a provider id or a code in capitals' in text


def test_scores_reach_the_model_as_the_page_shows_them():
    event = interpreted_fomc(relevance_score=70.2, dominance_score=92.2, expected_volatility_score=75.0)
    regime = dict(REGIME, confidence=33.3, coverage_pct=89.6,
                  evaluated=[{'regime': 'RISK_ON', 'state': 'ACTIVE', 'confidence': 33.3, 'missing_inputs': []}])
    data = interpretation.bundle(interpreted_snapshot(event), regime, [], NOW)
    seen = data['event_risk']['next_event']
    assert (seen['relevance_score'], seen['dominance_score'], seen['expected_volatility_score']) == (70, 92, 75)
    assert (data['regime']['confidence'], data['regime']['coverage_pct']) == (33, 90)
    assert data['regime']['evaluated'][0]['confidence'] == 33 and seen['fact_confidence'] == 99  # shown as 99%
    # Half away from zero, like the page; only the configured score keys change.
    assert interpretation.as_shown({'decision_score': -35.5, 'score': 64.5, 'price': 64.55}, {'decision_score', 'score'}) \
        == {'decision_score': -36, 'score': 65, 'price': 64.55}
    allowed = set(numeric_allowlist('market_interpreter'))
    page = ai_output(key_risks=['Regime confidence is only 33% and the FOMC relevance is 70/100.'])
    assert interpretation.violations(page, data) == [] and untraceable(page, data, allowed) == set()
    raw = ai_output(key_risks=['Regime confidence is only 33.3 and the FOMC relevance is 70.2.'])
    assert untraceable(raw, data, allowed) == {'33.3', '70.2'}  # rejected and retried, never shown
    output = interpretation.deterministic_output(data)
    assert output['dominant_event']['reason'].startswith('Dominance score 92/100 with CRITICAL relevance')
    assert 'backend' not in json.dumps(output).casefold() and untraceable(output, data, allowed) == set()


def test_the_deterministic_summary_never_names_an_event_by_its_id():
    post = {'event_id': 'BLS-20261028-US_PPI', 'event_class': 'US_PPI', 'phase': 'POST_EVENT', 'divergence': True}
    output, data = deterministic(interpreted_snapshot(post_event=[post]))
    assert 'MARKET DIVERGENCE after US PPI:' in output['market_summary']
    assert interpretation._plain_language_problems(output, data) == []


def test_a_date_only_row_without_a_session_names_its_whole_day_and_zone(tmp_path):
    t0 = datetime(2026, 10, 30, 4, 0, tzinfo=UTC)  # 00:00 in New York
    window = er.date_window({'country': 'US'}, 'OTHER', t0)
    assert window['label'] == 'whole day, New York time' and window['zone'] == 'America/New_York'
    assert window['start'] == t0 and window['end'] == t0 + timedelta(days=1)
    assert er.date_window({'country': 'EU'}, 'OTHER', t0)['label'] == 'whole day, Central European time'
    assert er.whole_day_label('America/Chicago') == 'whole day, Chicago time'  # a zone without a name: its city
    msft = calendar_row('MSFT earnings', 'US', 'EARNINGS', t0, 6, 'finnhub_earnings', symbol='MSFT', time_known=False,
                        time_basis='date only (Finnhub gives no session)')
    event = next(e for e in snap(make_hub(tmp_path), NOW, [msft], regime_=RISK_ON)['events']
                 if e['event_class'] == 'EARNINGS')
    assert event['date_window']['label'] == 'whole day, New York time'
    assert event['status_note'] == 'MSFT EARNINGS on Fri 30 Oct (time TBA)'  # the whole day adds nothing here
    assert 'publisher local day' not in json.dumps(event)


def test_finnhub_session_texts_carry_no_vendor_codes_and_still_resolve_their_session():
    sessions = rules()['finnhub']['earnings']['sessions']
    for hour in ('bmo', 'amc', 'dmh'):
        basis = sessions[hour]['time_basis']
        assert not re.search(r'\b(?:bmo|amc|dmh|hour)\b', basis, re.IGNORECASE), basis
        assert er._session_key({'time_basis': basis}) == hour  # the window still follows the session
    assert er.date_window({'time_basis': sessions['amc']['time_basis']}, 'EARNINGS', NOW)['label'] == 'after the US close'
