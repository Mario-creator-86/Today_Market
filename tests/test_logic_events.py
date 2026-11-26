"""Events pipeline, logic audit of 2-3 Oct 2026 (DEC-1, ER-1..ER-5, EVR-1 causal part, EVR-8, EVR-9):

- a trigger (direction and causal reach over many assets) has to be stated by the text: central-bank and macro triggers
  only from the authority itself or a tier-1/2 wire whose HEADLINE states it; a publisher's own name never counts as a
  keyword hit in its own items; a directional regulatory trigger needs a directional word, else a neutral REGULATION
  classification with no expected effects; third-party commentary about one stock stays linked to that stock;
- the same rules classify stored events when they are read (classified_on_read), and an official item takes the type of
  its classification so the documented verification windows apply;
- one start-up repair (Ingestion.repair_stored_events), once per dedupe_version, behind a sqlite3 backup: legacy events
  get a provenance, one publication stored twice is merged into the older id with evidence, reactions and references
  following, merged events are re-split; a second run changes nothing;
- a date-only publication never gets an invented clock time;
- labour-data causal branches (rates channel by default, growth channel in a growth scare); option expiries by tenor;
  the payroll figure as the only headline metric of the NFP row.
Settings never read the real .env; nothing touches the network."""
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from backend import analytics, causal
from backend.config import Settings, rules
from backend.database import RECORDS, Document, Event, Evidence, Store, utcnow
from backend.ingestion import MERGED_EVENT, REPAIR_KEY, Ingestion, classify
from backend.providers import calendars
from backend.schemas import DocumentInput
from backend.service import HubService

SEC_CUSTODY = ('SEC Proposal Would Address How Investment Advisers and Funds Can Custody Crypto Assets Under the Federal '
               'Securities Laws')
SEC_CUSTODY_TEXT = ('The Securities and Exchange Commission today proposed new rules and amendments to provide a tailored '
                    'framework for the custody of crypto assets for registered investment advisers and regulated funds.')
INTEL_NOTE = 'Why Is Intel Stock Falling on Monday?'
INTEL_TEXT = ('Rising Treasury yields, Fed rate hike odds, an Nvidia AI safety launch and profit-taking after a 28.42% '
              'monthly gain are in focus.')
NY = ZoneInfo('America/New_York')


@pytest.fixture
def hub(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/events.db')
    store.initialize()
    result = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    result.initialize()
    return result


def ingest(hub, source_id, title, content, url, published=None, **extra):
    return hub.ingest_document(DocumentInput(source_id=source_id, title=title, content=content, url=url,
                                             published_at=published or utcnow() - timedelta(minutes=5), **extra),
                               trusted_transport=True)


def stored_event(hub, event_id, doc_id, source, title, content, url, published, created=None, event_type='OFFICIAL_RELEASE',
                 payload=None, provenance='provider_retrieval', **doc_payload):
    """An event and its creating document as earlier versions stored them."""
    created = created or published
    with hub.store.session() as session:
        if session.get(Event, event_id) is None:
            base = {'event_time': published.isoformat(), 'assets': [], 'affected_assets': []}
            if provenance:
                base['provenance'] = provenance
            session.add(Event(id=event_id, summary_fact=title, event_type=event_type, created_at=created, updated_at=created,
                              payload=dict(base, **(payload or {}))))
        session.add(Document(id=doc_id, source_id=source, fingerprint=f'{doc_id:x>64}', title=title, content=content,
                             url=url, published_at=published, created_at=created,
                             payload={'provenance': provenance or 'provider_retrieval', 'direct': True, **doc_payload}))
        session.add(Evidence(id=f'e-{doc_id}', event_id=event_id, document_id=doc_id))
        session.commit()


# --- DEC-1 / ER-1: a trigger has to be stated by the text --------------------------------------------------------------

def test_a_neutral_regulatory_item_gets_no_direction_and_a_directional_one_needs_a_direction_word():
    custody = causal.derive_trigger(SEC_CUSTODY, SEC_CUSTODY_TEXT, 'sec')
    assert custody['trigger'] is None and custody['event_type'] == 'REGULATION' and custody['classification'] == 'rule'
    assert custody['affected_assets'] == [] and set(custody['assets']) == {'BTC', 'ETH', 'COIN', 'MSTR', 'BMNR'}
    assert custody['matched'][0] == 'source:sec'  # the actor is the publisher itself, never the word 'SEC' in its text
    proposes = causal.derive_trigger('SEC Proposes New Regulation Crypto Assets', '', 'sec')
    assert proposes['trigger'] is None and proposes['event_type'] == 'REGULATION'
    sues = causal.derive_trigger('SEC Charges Crypto Exchange With Operating an Unregistered Platform', '', 'sec')
    assert sues['trigger'] == 'CRYPTO_REGULATION' and 'BTC' in sues['affected_assets']
    relief = causal.derive_trigger('SEC Approves Spot Ether Exchange-Traded Products', 'The Commission approved ...', 'sec')
    assert relief['trigger'] == 'CRYPTO_REGULATORY_RELIEF'
    faqs = causal.derive_trigger('CFTC Staff Releases Updates to FAQs Concerning Registrants and Digital Asset Activities', '', 'cftc')
    assert faqs['trigger'] is None and faqs['event_type'] == 'REGULATION'
    # A wire may state a directional trigger in its headline; a stock note (tier 4) never gives the Fed trigger.
    assert causal.derive_trigger('SEC sues Coinbase over staking programme', '', 'reuters')['trigger'] == 'CRYPTO_REGULATION'


def test_central_bank_and_macro_triggers_come_only_from_the_authority_or_a_wire_headline():
    intel = causal.derive_trigger(INTEL_NOTE, INTEL_TEXT, 'benzinga')
    assert intel == dict(intel, trigger=None, event_type=None, topic=None, affected_assets=[], classification='none')
    assert causal.derive_trigger('Fed hikes rates by 25 bp, signals more tightening', '', 'benzinga')['trigger'] is None
    wire = causal.derive_trigger('Fed raises rates by 25 bp, signals further tightening', 'Markets fell.', 'reuters')
    assert (wire['trigger'], wire['event_type'], wire['topic']) == ('FED_HAWKISH', 'CENTRAL_BANK', 'FED_POLICY')
    body_only = causal.derive_trigger('Stocks slip at the open', 'Traders cite Fed rate hike odds.', 'reuters')
    assert body_only['trigger'] is None  # a tier-2 wire must state it in the HEADLINE
    own = causal.derive_trigger('Federal Reserve issues FOMC statement',
                                'The Committee decided to raise the target range for the federal funds rate.', 'fed')
    assert own['trigger'] == 'FED_HAWKISH' and own['matched'][0] == 'source:fed'  # the authority: the whole text counts
    schedule = causal.derive_trigger('Federal Reserve Board announces board meeting schedule', 'The Fed meets.', 'fed')
    assert schedule['trigger'] is None and schedule['event_type'] == 'CENTRAL_BANK' and schedule['classification'] == 'source'
    hot = causal.derive_trigger('Consumer prices rose more than expected in September', 'CPI up 0.5%.', 'bls')
    assert hot['trigger'] == 'INFLATION_HOT'
    assert causal.derive_trigger('Consumer prices rose more than expected in September', '', 'benzinga')['trigger'] is None


def test_ingestion_stores_the_principle_and_third_party_commentary_stays_with_its_stock(hub):
    custody = ingest(hub, 'sec', SEC_CUSTODY, SEC_CUSTODY_TEXT, 'https://www.sec.gov/newsroom/press-releases/2026-120')
    assert custody['trigger'] is None and custody['event_type'] == 'REGULATION' and custody['affected_assets'] == []
    assert {'BTC', 'ETH', 'COIN'} <= set(custody['assets']) and custody['why_it_matters'] is None
    assert custody['topic'] == 'CRYPTO_REGULATION' and custody['topic_basis'] == 'rule'
    intel = ingest(hub, 'benzinga', INTEL_NOTE, INTEL_TEXT, 'https://www.benzinga.com/quote/INTC', assets=['NVDA'])
    assert intel['trigger'] is None and intel['affected_assets'] == [] and intel['assets'] == ['NVDA']
    assert intel['event_type'] == 'NEWS' and intel['topic_basis'] != 'rule'
    sues = ingest(hub, 'sec', 'SEC Charges Crypto Lender With Fraud', 'The Commission charged ...',
                  'https://www.sec.gov/newsroom/press-releases/2026-121')
    assert sues['trigger'] == 'CRYPTO_REGULATION' and sues['trigger_basis'] == 'rule' and 'BTC' in sues['affected_assets']


# --- read-time classification of stored events and ER-2 typed verification windows -------------------------------------

def test_a_stored_trigger_the_rules_no_longer_justify_is_cleared_when_the_event_is_read(hub):
    published = utcnow() - timedelta(hours=2)
    stored_event(hub, 'custody', 'custody-doc', 'sec', SEC_CUSTODY, SEC_CUSTODY_TEXT,
                 'https://www.sec.gov/newsroom/press-releases/2026-120', published,
                 payload={'trigger': 'CRYPTO_REGULATION', 'trigger_basis': 'rule', 'topic': 'CRYPTO_REGULATION',
                          'topic_basis': 'rule', 'assets': ['BTC', 'ETH', 'COIN', 'MSTR', 'BMNR'],
                          'affected_assets': ['BTC', 'ETH', 'COIN', 'MSTR', 'BMNR']})
    event = hub.event('custody')
    assert event['trigger'] is None and event['stored_trigger'] == 'CRYPTO_REGULATION' and event['classified_on_read'] is True
    assert event['affected_assets'] == [] and set(event['assets']) == {'BTC', 'ETH', 'COIN', 'MSTR', 'BMNR'}
    assert event['why_it_matters'] is None and event['event_type'] == 'REGULATION'
    stored_event(hub, 'intel', 'intel-doc', 'benzinga', INTEL_NOTE, INTEL_TEXT, 'https://www.benzinga.com/quote/INTC',
                 published, event_type='NEWS',
                 payload={'trigger': 'FED_HAWKISH', 'trigger_basis': 'rule', 'topic': 'FED_POLICY', 'topic_basis': 'rule',
                          'assets': ['NVDA', 'BTC', 'ETH'], 'affected_assets': ['BTC', 'ETH'], 'entities': ['intel', 'nvidia']})
    intel = hub.event('intel')
    assert intel['trigger'] is None and intel['affected_assets'] == [] and intel['assets'] == ['NVDA']
    assert intel['topic'] == 'INTEL_NVIDIA' and intel['topic_basis'] == 'entities'  # the rule that named the topic is gone
    # A legacy event (stored before classification existed) is classified; an AI-accepted trigger is kept.
    stored_event(hub, 'legacy', 'legacy-doc', 'reuters', 'Fed raises rates by 25 bp, signals further tightening', 'Markets fell.',
                 'https://www.reuters.com/markets/fed-1', published, event_type='NEWS', payload={'assets': []})
    legacy = hub.event('legacy')
    assert legacy['trigger'] == 'FED_HAWKISH' and legacy['classified_on_read'] is True and 'stored_trigger' not in legacy
    stored_event(hub, 'accepted', 'accepted-doc', 'benzinga', INTEL_NOTE, INTEL_TEXT, 'https://www.benzinga.com/quote/INTC2',
                 published, event_type='NEWS', payload={'trigger': 'FED_HAWKISH', 'trigger_basis': 'extraction', 'assets': ['NVDA'],
                                                        'affected_assets': ['BTC']})
    assert hub.event('accepted')['trigger'] == 'FED_HAWKISH' and 'classified_on_read' not in hub.event('accepted')
    with hub.store.session() as session:  # read-only: the stored rows never change
        assert session.get(Event, 'custody').payload['trigger'] == 'CRYPTO_REGULATION'


def test_official_items_take_the_type_of_their_classification_so_the_documented_windows_apply(hub):
    fed = ingest(hub, 'fed', 'Federal Reserve Board announces approval of application by Peoples Bancorp Inc.', 'Approved.',
                 'https://www.federalreserve.gov/newsevents/pressreleases/orders20260925a.htm')
    assert fed['event_type'] == 'CENTRAL_BANK' and fed['topic'] == 'FED_POLICY' and fed['trigger'] is None
    with hub.store.session() as session:
        assert session.get(Event, fed['id']).event_type == 'CENTRAL_BANK'  # stored typed, not OFFICIAL_RELEASE
    bls = ingest(hub, 'bls', 'Both payroll employment (+29,000) and unemployment rate (4.2%) change little in September', 'Text.',
                 'https://www.bls.gov/news.release/empsit.nr0.htm')
    assert bls['event_type'] == 'MACRO' and bls['topic'] == 'US_MACRO'
    # A stored OFFICIAL_RELEASE item of the Fed is read as CENTRAL_BANK: 30-hour-old evidence stays inside the 72 h window.
    old = utcnow() - timedelta(hours=30)
    stored_event(hub, 'fed-old', 'fed-old-doc', 'fed', 'Federal Reserve Board issues enforcement action with a former employee',
                 'Text.', 'https://www.federalreserve.gov/newsevents/pressreleases/enf20260901a.htm', old)
    stored_event(hub, 'news-old', 'news-old-doc', 'reuters', 'Bank shares rise after the enforcement action', 'Text.',
                 'https://www.reuters.com/markets/banks-1', old, event_type='NEWS')
    assert hub.event('fed-old')['event_type'] == 'CENTRAL_BANK' and hub.event('fed-old')['verification_status'] == 'CONFIRMED'
    assert hub.event('news-old')['event_type'] == 'NEWS' and hub.event('news-old')['verification_status'] == 'STALE'
    # A type a provider set on purpose is kept.
    assert causal.classified_type('SANCTIONS', {'event_type': 'REGULATION'}) == 'SANCTIONS'
    assert causal.classified_type('OFFICIAL_RELEASE', {'event_type': None}) == 'OFFICIAL_RELEASE'
    assert classify('x', '', 'bls', 1, 'OFFICIAL_RELEASE')[1] == 'MACRO'


# --- ER-3 / ER-4: the start-up repair ----------------------------------------------------------------------------------

def duplicated_and_merged(hub):
    """A Fed order stored twice (the older event without a provenance), a reaction and a research job on the duplicate,
    and a JOLTS event that earlier rules merged with last month's release."""
    published = utcnow() - timedelta(days=2)
    url = 'https://www.federalreserve.gov/newsevents/pressreleases/orders20260925a.htm'
    title = 'Federal Reserve Board announces approval of application by Peoples Bancorp Inc.'
    stored_event(hub, 'old-fed', 'old-fed-doc', 'fed', title, 'Approved.', url, published, provenance=None)
    stored_event(hub, 'new-fed', 'new-fed-doc', 'fed', title, 'Approved.', url, published,
                 created=published + timedelta(minutes=15), payload={'trigger': None, 'trigger_basis': 'none'})
    relationships = RECORDS['event_relationships']
    with hub.store.session() as session:
        session.add(relationships(id='reaction:new-fed:BTC:15m', payload={'event_id': 'new-fed', 'symbol': 'BTC', 'move': 0.4}))
        session.add(relationships(id='reaction:old-fed:ETH:15m', payload={'event_id': 'old-fed', 'symbol': 'ETH', 'move': 0.1}))
        session.add(relationships(id='reaction:new-fed:ETH:15m', payload={'event_id': 'new-fed', 'symbol': 'ETH', 'move': 0.2}))
        session.add(RECORDS['research_jobs'](id='job-1', payload={'event_id': 'new-fed', 'status': 'done'}))
        session.commit()
    july = utcnow() - timedelta(days=3)
    june = july - timedelta(days=28)
    run = utcnow() - timedelta(days=1)
    joined = {'dedupe': {'match': True, 'basis': 'similarity', 'lexical': 0.88}}
    stored_event(hub, 'jolts', 'july', 'bls', 'Job openings, hires, and total separations change little in July', 'JOLTS July',
                 'https://www.bls.gov/jolts_09012026.htm', july, run)
    stored_event(hub, 'jolts', 'june', 'bls', 'Job openings and total separations change little in June; hires unchanged',
                 'JOLTS June', 'https://www.bls.gov/jolts_08042026.htm', june, run + timedelta(seconds=1), **joined)
    with hub.store.session() as session:
        row = session.get(Event, 'jolts')
        row.payload = dict(row.payload, extraction={'potential_importance': 70}, ai_enriched=True)
        session.commit()


def counts(hub) -> dict:
    relationships = RECORDS['event_relationships']
    with hub.store.session() as session:
        return {'events': session.scalar(select(func.count(Event.id))),
                'evidence': session.scalar(select(func.count(Evidence.id))),
                'reactions': len(session.scalars(select(relationships.id).where(
                    relationships.id.startswith('reaction:', autoescape=True))).all()),
                'archived': len(session.scalars(select(relationships.id).where(
                    relationships.id.startswith('merged:', autoescape=True))).all())}


def test_the_startup_repair_merges_duplicates_into_the_older_id_and_splits_merged_events_behind_a_backup(hub):
    duplicated_and_merged(hub)
    before = counts(hub)
    assert before == {'events': 3, 'evidence': 4, 'reactions': 3, 'archived': 0}
    version = rules()['dedupe']['dedupe_version']
    path = hub.store.engine.url.database
    assert not os.path.exists(f'{path}.pre-repair-{version}')
    result = hub.repair_stored_events(force=True)  # initialize() already recorded this version on the empty database
    assert result['status'] == 'repaired' and result['backup'] == f'{path}.pre-repair-{version}'
    assert (result['provenance_filled'], result['events_merged'], result['events_split'], result['events_created']) == (1, 1, 1, 1)
    assert os.path.isfile(result['backup'])
    backup = Store(f'sqlite:///{result["backup"]}')  # the copy holds the state before the repair
    with backup.session() as session:
        assert session.scalar(select(func.count(Event.id))) == 3 and session.get(Event, 'new-fed') is not None
    after = counts(hub)
    assert after['events'] == 3 and after['evidence'] == 4  # one merged away, one split off; no evidence lost
    assert after['reactions'] + after['archived'] == before['reactions']  # no reaction lost
    kept = hub.event('old-fed')
    assert kept['provenance'] == 'provider_retrieval' and kept['merged_event_ids'] == ['new-fed']
    assert [e['document_id'] for e in kept['evidence']] == ['old-fed-doc'] and kept['evidence'][0]['duplicate_document_ids'] == ['new-fed-doc']
    assert hub.event('new-fed')['id'] == 'old-fed'  # the merged id still opens the kept event
    relationships = RECORDS['event_relationships']
    with hub.store.session() as session:
        assert session.get(Event, 'new-fed') is None
        moved = session.get(relationships, 'reaction:old-fed:BTC:15m')
        assert moved.payload['event_id'] == 'old-fed' and moved.payload['merged_from_event'] == 'new-fed'
        assert session.get(relationships, 'reaction:old-fed:ETH:15m').payload['move'] == 0.1  # the kept event's own row stays
        assert session.get(relationships, 'merged:reaction:new-fed:ETH:15m').payload['merged_into'] == 'old-fed'
        assert session.get(RECORDS['research_jobs'], 'job-1').payload['event_id'] == 'old-fed'
        assert session.get(relationships, MERGED_EVENT + 'new-fed').payload['event_id'] == 'old-fed'
    jolts = hub.event('jolts')
    assert [e['document_id'] for e in jolts['evidence']] == ['july'] and 'extraction' not in jolts and jolts['ai_reset']['fields']
    [split] = [e for e in hub.events() if e.get('split_from') == 'jolts']
    assert [e['document_id'] for e in split['evidence']] == ['june']
    audit = [a for a in hub.store.list('audit_logs') if a.get('action') == 'events_repair']
    assert len(audit) == 1 and audit[0]['merged'][0]['kept'] == 'old-fed' and audit[0]['provenance_filled_event_ids'] == ['old-fed']
    assert hub.store.get('configuration_versions', REPAIR_KEY)['dedupe_version'] == version
    # Idempotent: a second run is skipped, a forced one finds nothing, and nothing changes either time.
    assert hub.repair_stored_events()['skipped'] is True
    forced = hub.repair_stored_events(force=True)
    assert forced['status'] == 'nothing_to_repair' and forced['events_merged'] == 0 and forced['events_split'] == 0
    assert counts(hub) == after


def test_the_repair_never_changes_anything_without_a_backup_and_never_raises(hub, monkeypatch, caplog):
    duplicated_and_merged(hub)
    before = counts(hub)
    monkeypatch.setattr(Ingestion, '_backup_database', lambda self, version: None)  # not SQLite, or the copy failed
    with caplog.at_level('WARNING'):
        result = hub.repair_stored_events(force=True)
    assert result['status'] == 'skipped' and counts(hub) == before
    assert any(r.message == 'events_repair_skipped' for r in caplog.records)
    assert hub.store.get('configuration_versions', REPAIR_KEY)['status'] == 'nothing_to_repair'  # from initialize()
    monkeypatch.undo()

    def broken(self, session):
        raise RuntimeError('boom')
    monkeypatch.setattr(Ingestion, '_duplicate_groups', broken)
    assert hub.repair_stored_events(force=True) == {'status': 'failed', 'error_type': 'RuntimeError'}
    assert counts(hub) == before


# --- ER-5: a date-only publication never gets an invented clock time ---------------------------------------------------

def test_a_date_only_publication_is_served_with_its_date_and_no_time(hub):
    midnight = datetime(2026, 9, 28, 0, 0, tzinfo=NY).astimezone(UTC)  # the adapter's stamp for 'September 28, 2026'
    event = ingest(hub, 'ofac', 'OFAC: Issuance of Amended Venezuela General Licenses', 'OFAC published amended licences.',
                   'https://ofac.treasury.gov/recent-actions/20260928', midnight)
    assert event['time_known'] is False and event['event_time_known'] is False and event['event_date'] == '2026-09-28'
    assert event['event_time'] == midnight.isoformat()  # kept to order and age the publication only
    [evidence] = event['evidence']
    assert evidence['time_known'] is False and evidence['published_date'] == '2026-09-28'
    with hub.store.session() as session:
        stored = session.get(Event, event['id']).payload
        assert stored['time_known'] is False and stored['event_date'] == '2026-09-28'
        assert session.get(Document, evidence['document_id']).payload['published_date'] == '2026-09-28'
    [served] = [e for e in hub.events() if e['id'] == event['id']]
    assert served['event_time_known'] is False
    timed = ingest(hub, 'ofac', 'OFAC sanctions list update (publication 1045): 5 additions [SDGT]', 'Added: ...',
                   'https://ofac.treasury.gov/recent-actions/20261002-sls', datetime(2026, 10, 2, 15, 55, tzinfo=UTC))
    assert timed['time_known'] is True and timed['event_time_known'] is True and 'event_date' not in timed
    assert timed['evidence'][0]['time_known'] is True


# --- EVR-1 (causal part): the labour branches ------------------------------------------------------------------------

def directions(trigger: str, *regimes: str) -> dict[str, int]:
    paths = causal.paths(trigger, 0.0, {'regimes': list(regimes), 'posture': None, 'confirmed_triggers': []})
    return analytics.expected_effects({'causal_paths': paths})


def test_labour_data_moves_risk_assets_through_the_rates_channel_unless_a_growth_scare_is_active():
    strong, weak = directions('JOBS_STRONG'), directions('JOBS_WEAK')
    for symbol in ('SP500', 'NASDAQ', 'RUSSELL', 'BTC', 'ETH', 'NVDA', 'COIN'):
        assert strong[symbol] == -1 and weak[symbol] == 1, symbol
    assert strong['US2Y'] == 1 and strong['DXY'] == 1 and strong['EURUSD'] == -1 and strong['USDJPY'] == 1
    assert weak == {s: -v for s, v in strong.items()}  # mirror images, no asset unresolved
    assert analytics.unresolved_effects({'causal_paths': causal.paths('JOBS_WEAK')}) == []
    for trigger in ('JOBS_STRONG', 'JOBS_WEAK'):
        for symbol in ('SP500', 'BTC'):
            assert not causal.conflict(causal.paths(trigger), symbol)['conflicting']
    # Growth scare: bad news is bad news (and strong data a relief) for risk assets; the rates leg keeps its sign.
    scare_strong, scare_weak = directions('JOBS_STRONG', 'GROWTH_SCARE'), directions('JOBS_WEAK', 'GROWTH_SCARE')
    assert scare_strong['SP500'] == scare_strong['BTC'] == 1 and scare_weak['SP500'] == scare_weak['BTC'] == -1
    assert scare_strong['US2Y'] == 1 and scare_weak['US2Y'] == -1 and scare_weak['DXY'] == -1
    assert [p['exceptions_fired'] for p in causal.paths('JOBS_WEAK', 0.0, {'regimes': ['GROWTH_SCARE']})] == \
        [['growth-scare-bad-news-is-bad-news'], []]
    # Any other regime leaves the default branches.
    assert directions('JOBS_WEAK', 'INFLATION_SHOCK')['SP500'] == 1 and directions('JOBS_STRONG', 'RISK_OFF')['SP500'] == -1


# --- EVR-8 / EVR-9: option expiries by tenor; the payroll figure as the NFP headline metric -----------------------------

def test_option_expiries_carry_their_tenor_and_its_importance():
    saturday, friday = datetime(2026, 10, 3, 8, tzinfo=UTC), datetime(2026, 10, 9, 8, tzinfo=UTC)
    monthly, quarterly = datetime(2026, 10, 30, 8, tzinfo=UTC), datetime(2026, 12, 25, 8, tzinfo=UTC)
    assert [calendars.expiry_tenor(d) for d in (saturday, friday, monthly, quarterly)] == ['daily', 'weekly', 'monthly', 'quarterly']
    configured = rules()['provider_routing']['calendars']['option_expiry_importance']
    rows = [calendars.expiry_row('BTC', at, 58) for at in (saturday, friday, monthly, quarterly)]
    assert [r['tenor'] for r in rows] == ['daily', 'weekly', 'monthly', 'quarterly']
    assert [r['importance_score'] for r in rows] == [configured[t] for t in ('daily', 'weekly', 'monthly', 'quarterly')]
    assert rows[0]['importance_score'] < 40 <= rows[2]['importance_score']  # a daily expiry never reaches the key-event bar
    assert rows[0]['event_class'] == 'CRYPTO_EXPIRY' and rows[0]['instruments'] == 58 and rows[0]['time_known'] is True
    assert rows[0]['id'] != rows[1]['id'] and rows[0]['name'] == 'BTC options expiry'


@pytest.mark.asyncio
async def test_the_deribit_calendar_writes_the_tenor_of_every_expiry(monkeypatch):
    import json

    import httpx
    daily, monthly = datetime(2026, 10, 3, 8, tzinfo=UTC), datetime(2026, 10, 30, 8, tzinfo=UTC)
    instruments = [{'instrument_name': f'BTC-{i}', 'expiration_timestamp': int(at.timestamp() * 1000), 'strike': 80000.0,
                    'option_type': 'call'} for i, at in enumerate([daily, daily, monthly])]

    async def request(self, method, url, **kwargs):
        return httpx.Response(200, content=json.dumps({'result': instruments}).encode(), request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = calendars.CryptoExpiryCalendar()
    rows = await provider.calendar('2026-10-01', '2026-10-31')
    currencies = rules()['provider_routing']['deribit']['currencies']
    assert [(r['tenor'], r['importance_score'], r['instruments']) for r in rows] == [('daily', 20, 2), ('monthly', 50, 1)] * len(currencies)
    assert [r['currency'] for r in rows] == [c for c in currencies for _ in range(2)]
    assert all(r['provider'] == 'crypto_expiries' and r['category'] == 'OPTION_EXPIRY' for r in rows)


def test_the_unemployment_rate_is_never_the_headline_metric_of_the_nfp_row():
    unemployment = {'name': 'Unemployment Rate', 'provider': 'calendar', 'consensus': 4.1, 'actual': 4.2}
    payrolls = {'name': 'Non Farm Payrolls', 'provider': 'calendar', 'consensus': 75, 'actual': 29}
    assert calendars.headline_metric('US_NFP', [unemployment, payrolls]) is payrolls
    assert calendars.headline_metric('US_NFP', [unemployment]) is unemployment  # a lone metric is the row's only values
    assert calendars.headline_metric('US_NFP', [unemployment, {'name': 'Average Hourly Earnings', 'provider': 'calendar'}]) is None
    assert 'unemployment' not in ' '.join(rules()['provider_routing']['calendars']['dedupe']['headline_metrics']['US_NFP'])
