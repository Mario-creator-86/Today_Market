"""Discovery and sanctions adapters: GDELT raw exports, SEC EDGAR full-text search, OFAC Recent Actions and the
OFAC Sanctions List Service. All HTTP is mocked (no network)."""
import hashlib
import io
import json
import zipfile
from datetime import date

import httpx
import pytest

from backend.config import rules
from backend.providers.base import ProviderUnavailable
from backend.providers.discovery import (
    GDELT_COLUMNS,
    GDELT_WIDTH,
    DiscoveryProvider,
    EdgarFullTextProvider,
    GdeltEventsProvider,
    OfacRecentActionsProvider,
    OfacSanctionsListProvider,
    PacedGuard,
    parse_lastupdate,
    sec_contact_configured,
)
from backend.schemas import DocumentInput

OPEN_LIMITS = {'min_interval_seconds': 0, 'max_calls': 1000, 'per_seconds': 60}


def respond(monkeypatch, handler):
    """handler(method, url, kwargs) -> (status, body[, headers[, final_url]])."""
    calls = []

    async def request(self, method, url, **kwargs):
        calls.append({'method': method, 'url': url, 'kwargs': kwargs, 'headers': dict(self.headers)})
        status, body, *rest = handler(method, url, kwargs)
        headers = rest[0] if rest else {}
        final_url = rest[1] if len(rest) > 1 else url
        content = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
        return httpx.Response(status, content=content, headers=headers, request=httpx.Request(method, final_url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    return calls


def open_guard(provider):
    provider.guard = PacedGuard(provider.name, OPEN_LIMITS)
    return provider


def test_fragment_merges_without_redefining_keys():
    config = rules()
    assert config['discovery_version'] == '1.2.0'
    assert {'gdelt', 'edgar_fts', 'ofac_actions', 'ofac_sls', 'program_assets', 'program_topic_labels'} <= set(config['discovery'])
    assert config['discovery']['edgar_fts']['rate_limit']['min_interval_seconds'] >= 0.1  # SEC: max 10 req/s


@pytest.mark.asyncio
async def test_paced_guard_spaces_calls_and_refuses_beyond_budget():
    now = [100.0]
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
    guard = PacedGuard('x', {'min_interval_seconds': 0.25, 'max_calls': 2, 'per_seconds': 60}, lambda: now[0], sleep)
    await guard.acquire()
    await guard.acquire()
    assert slept == [0.25]
    with pytest.raises(ProviderUnavailable) as refused:
        await guard.acquire()
    assert refused.value.status_code == 429
    now[0] += 61
    await guard.acquire()


# --- GDELT ---------------------------------------------------------------------------------------------------

def gdelt_line(**values) -> str:
    fields = [''] * GDELT_WIDTH
    defaults = {'global_event_id': '1', 'sql_date': '20260926', 'is_root_event': '1', 'event_code': '190',
                'event_base_code': '190', 'root_code': '19', 'quad_class': '4', 'goldstein': '-10', 'num_mentions': '8',
                'num_sources': '2', 'num_articles': '8', 'avg_tone': '-6.5', 'date_added': '20260926203000',
                'source_url': 'https://news.example/a'}
    for name, value in dict(defaults, **values).items():
        fields[GDELT_COLUMNS[name]] = value
    return '\t'.join(fields)


def gdelt_zip(batch: str, lines: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f'{batch}.export.CSV', '\n'.join(lines) + '\n')
    return buffer.getvalue()


def lastupdate(batch: str, archive: bytes, host: str = 'data.gdeltproject.org') -> str:
    md5 = hashlib.md5(archive, usedforsecurity=False).hexdigest()
    return (f'{len(archive)} {md5} http://{host}/gdeltv2/{batch}.export.CSV.zip\n'
            f'100 {"0" * 32} http://{host}/gdeltv2/{batch}.mentions.CSV.zip\n')


GDELT_LINES = [
    gdelt_line(global_event_id='11', action_country='IR', actor1_name='IRAN', actor1_country='IRN',
               action_geo_name='Strait of Hormuz, Iran'),                                   # candidate
    gdelt_line(global_event_id='12', action_country='IR', root_code='18', event_code='183', num_mentions='4'),  # same URL
    gdelt_line(global_event_id='13', action_country='US', num_mentions='20', source_url='https://news.example/us'),
    gdelt_line(global_event_id='14', actor1_country='RUS', root_code='04', event_code='040', quad_class='1',
               goldstein='1', num_mentions='10', source_url='https://news.example/ru'),     # intensity only
    gdelt_line(global_event_id='15', action_country='IR', num_mentions='1', source_url='https://news.example/low'),
    gdelt_line(global_event_id='16', action_country='IR', sql_date='20250926', source_url='https://news.example/old'),
    gdelt_line(global_event_id='17', action_country='IR', goldstein='abc'),                # rejected
    gdelt_line(global_event_id='18', action_country='IR', sql_date='20261399'),            # rejected: not a date
    'too\tshort',                                                                            # rejected
]


@pytest.mark.asyncio
async def test_gdelt_batch_yields_discovery_only_candidates_and_intensity(monkeypatch):
    batch = '20260926203000'
    archive = gdelt_zip(batch, GDELT_LINES)

    def handler(method, url, kwargs):
        if url.endswith('lastupdate.txt'):
            return 200, lastupdate(batch, archive)
        assert url == f'https://data.gdeltproject.org/gdeltv2/{batch}.export.CSV.zip'
        return 200, archive, {'Last-Modified': 'Sat, 26 Sep 2026 20:19:49 GMT'}
    calls = respond(monkeypatch, handler)
    provider = open_guard(GdeltEventsProvider())
    assert isinstance(provider, DiscoveryProvider)
    result = await provider.batch()
    assert result['status'] == 'OK' and result['checksum'].startswith('md5') and result['live'] is False
    assert result['as_of'] == '2026-09-26T20:19:49+00:00'  # file publication time, not the forward batch label
    [candidate] = result['candidates']
    assert candidate['global_event_id'] == '11' and candidate['source_url'] == 'https://news.example/a'
    assert candidate['discovery_only'] is True and candidate['verification_status'] == 'UNCONFIRMED'
    assert candidate['countries'] == ['IRAN'] and 'ENERGY' in candidate['topics'] and candidate['root_label'] == 'FIGHT'
    assert candidate['delay'].startswith('15-minute batch') and 'GDELT' in candidate['attribution']
    assert candidate['as_of'] == result['as_of'] and candidate['date_added_utc'] == '2026-09-26T20:30:00+00:00'
    iran, russia = result['intensity']['IRAN'], result['intensity']['RUSSIA']
    assert iran['events'] == 3 and iran['conflict_events'] == 3 and iran['goldstein_mean'] == -10.0  # old event excluded
    assert russia == dict(russia, events=1, conflict_events=0, coercive_events=0, mentions=10)
    assert 'UNITED STATES' not in result['intensity'] and provider.health['rejected_rows'] == 3
    again = await provider.batch()
    assert again['status'] == 'UNCHANGED' and again['candidates'] == [] and len(calls) == 3  # zip not re-fetched


@pytest.mark.asyncio
async def test_gdelt_listed_batch_404_falls_back_without_claiming_a_checksum(monkeypatch):
    listed, previous = '20260926211500', '20260926210000'
    archive = gdelt_zip(previous, GDELT_LINES[:1])

    def handler(method, url, kwargs):
        if url.endswith('lastupdate.txt'):
            return 200, lastupdate(listed, b'not published')
        if listed in url:
            return 404, b''
        return 200, archive
    respond(monkeypatch, handler)
    provider = open_guard(GdeltEventsProvider())
    result = await provider.batch()
    assert result['status'] == 'OK_FALLBACK' and result['batch_id'] == previous and result['batches_behind'] == 1
    assert result['checksum'].startswith('zip CRC only') and result['listed_batch_id'] == listed
    assert result['as_of'] == '2026-09-26T21:00:00+00:00' and 'used 20260926210000' in provider.health['note']

    respond(monkeypatch, lambda m, u, k: (200, lastupdate(listed, b'x')) if u.endswith('.txt') else (404, b''))
    nothing = await open_guard(GdeltEventsProvider()).batch()
    assert nothing['status'] == 'BATCH_NOT_PUBLISHED' and nothing['candidates'] == []


@pytest.mark.asyncio
async def test_gdelt_rejects_checksum_mismatch_and_foreign_hosts(monkeypatch):
    batch = '20260926203000'
    archive = gdelt_zip(batch, GDELT_LINES[:1])
    respond(monkeypatch, lambda m, u, k: (200, lastupdate(batch, b'other bytes')) if u.endswith('.txt') else (200, archive))
    with pytest.raises(ValueError, match='checksum'):
        await open_guard(GdeltEventsProvider()).batch()
    with pytest.raises(ValueError):
        parse_lastupdate(lastupdate(batch, archive, host='evil.example'))


# --- SEC EDGAR full-text search ------------------------------------------------------------------------------

def edgar_hit(adsh='0001045810-26-000073', doc='nvda-20260826.htm', file_type='8-K', items=('2.02', '9.01'),
              cik='0001045810', name='NVIDIA CORP  (NVDA)  (CIK 0001045810)'):
    return {'_id': f'{adsh}:{doc}', '_source': {'ciks': [cik], 'display_names': [name], 'form': '8-K', 'adsh': adsh,
                                               'file_date': '2026-09-25', 'items': list(items), 'file_type': file_type}}


def test_sec_contact_rule_rejects_placeholders():
    assert not sec_contact_configured('MarketIntelligence contact@example.com')
    assert not sec_contact_configured('MarketIntelligenceHub')
    assert sec_contact_configured('Research Desk ops@research-desk.test')


@pytest.mark.asyncio
async def test_edgar_is_not_configured_without_contact(monkeypatch):
    monkeypatch.delenv('SEC_USER_AGENT', raising=False)
    calls = respond(monkeypatch, lambda m, u, k: (200, {}))
    provider = EdgarFullTextProvider()
    assert provider.configured is False and provider.health['status'] == 'NOT CONFIGURED'
    with pytest.raises(ProviderUnavailable, match='NOT CONFIGURED'):
        await provider.records(date(2026, 9, 26))
    assert calls == []
    monkeypatch.setenv('SEC_USER_AGENT', 'Research Desk ops@research-desk.test')
    assert EdgarFullTextProvider().configured is True


@pytest.mark.asyncio
async def test_edgar_queries_filter_items_dedupe_filings_and_build_documents(monkeypatch):
    def handler(method, url, kwargs):
        params = kwargs['params']
        if params.get('q') == '"Results of Operations and Financial Condition"':
            return 200, {'hits': {'total': {'value': 4}, 'hits': [
                edgar_hit(doc='nvda-ex99_1.htm', file_type='EX-99.1'), edgar_hit(),
                edgar_hit(adsh='0001045810-26-000080', items=('8.01',)),              # wrong item: filtered
                {'_id': 'bad', '_source': {'adsh': 'nope', 'ciks': [], 'form': '8-K', 'file_date': 'x'}}]}}
        return 200, {'hits': {'total': {'value': 0}, 'hits': []}}
    calls = respond(monkeypatch, handler)
    provider = open_guard(EdgarFullTextProvider('Research Desk ops@research-desk.test'))
    records = await provider.records(date(2026, 9, 26))
    assert len(calls) == len(rules()['discovery']['edgar_fts']['queries'])
    first = calls[0]
    assert first['url'] == 'https://efts.sec.gov/LATEST/search-index'
    assert first['headers']['user-agent'] == 'Research Desk ops@research-desk.test'
    assert first['kwargs']['params']['forms'] == '8-K' and '0001045810' in first['kwargs']['params']['ciks']
    assert first['kwargs']['params']['startdt'] == '2026-09-23' and first['kwargs']['params']['enddt'] == '2026-09-26'
    assert 'ciks' not in calls[2]['kwargs']['params']  # market-wide bankruptcy query
    [record] = records
    assert record['document'] == 'nvda-20260826.htm' and record['assets'] == ['NVDA'] and record['tickers'] == ['NVDA']
    assert record['url'] == 'https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/nvda-20260826.htm'
    assert record['as_of'] == '2026-09-25T04:00:00+00:00' and record['live'] is False  # file_date, 00:00 US Eastern and 'EDGAR' in record['attribution']
    assert provider.health['rejected_rows'] == 1 and provider.health['query_totals']['tracked-item-2.02'] == 4
    document = DocumentInput.model_validate(provider.document(record))
    assert document.source_id == 'sec-filings' and document.event_type == 'EARNINGS' and not document.discovery_only
    assert 'keyword match' in document.content and 'day-granular' in document.content


# --- OFAC Recent Actions -------------------------------------------------------------------------------------

def action_block(slug, title, day, category='Sanctions List Updates'):
    return (f'<div class="margin-bottom-4 search-result views-row"><div><div class="font-sans-lg"><a href="/recent-actions/'
            f'{slug}" hreflang="en">{title}</a></div></div><div><div class="margin-top-1">{day} -   \n'
            f'<a href="/recent-actions/sanctions-list-updates">{category}</a></div></div></div>')


OFAC_PAGE = ('<html><div class="view-content">'
             + action_block('20260917', 'Iran-related Designations; Cuba Designations', 'September 17, 2026')
             + action_block('20261210', 'Counter Terrorism Designations &amp; Removals', 'December 10, 2026', 'Other')
             + '<div class="views-row"><a href="/recent-actions/20260901">broken entry without a date</a></div>'
             + '</div></html>')


@pytest.mark.asyncio
async def test_ofac_recent_actions_are_dated_official_documents(monkeypatch):
    calls = respond(monkeypatch, lambda m, u, k: (200, OFAC_PAGE))
    provider = open_guard(OfacRecentActionsProvider())
    actions = await provider.actions()
    assert calls[0]['url'] == 'https://ofac.treasury.gov/recent-actions' and provider.timeout >= 30
    iran, terror = actions
    assert iran['url'] == 'https://ofac.treasury.gov/recent-actions/20260917' and iran['category'] == 'Sanctions List Updates'
    assert iran['assets'] == ['BRENT', 'GOLD', 'WTI'] and iran['topics'] == ['SANCTIONS_IRAN']
    assert iran['published_at'] == '2026-09-17T04:00:00+00:00'  # date only: start of day in Washington (EDT)
    assert terror['published_at'] == '2026-12-10T05:00:00+00:00' and terror['title'] == 'Counter Terrorism Designations & Removals'
    assert terror['assets'] == [] and provider.health['rejected_rows'] == 1
    document = DocumentInput.model_validate(provider.document(iran))
    assert document.source_id == 'ofac' and document.event_type == 'SANCTIONS' and 'date only' in document.content
    assert 'public domain' in document.content


# --- OFAC Sanctions List Service -----------------------------------------------------------------------------

DELTA = b'''<?xml version="1.0" encoding="utf-8"?>
<sanctionsData xmlns="https://www.treasury.gov/ofac/DeltaFile/1.0">
  <publicationInfo><datePublished>2026-09-17T00:00:00-04:00</datePublished><publicationType>Standard Action</publicationType></publicationInfo>
  <entities>
    <entity id="58614" action="add">
      <generalInfo><identityId>1</identityId><entityType refId="601">Entity</entityType></generalInfo>
      <sanctionsLists><sanctionsList id="1" refId="1550">SDN List</sanctionsList></sanctionsLists>
      <sanctionsPrograms><sanctionsProgram id="2" refId="3">IRAN-EO13902</sanctionsProgram></sanctionsPrograms>
      <names>
        <name id="9"><isPrimary>false</isPrimary><translations><translation id="1"><isPrimary>true</isPrimary>
          <formattedFullName>ALIAS NAME</formattedFullName></translation></translations></name>
        <name id="10"><isPrimary>true</isPrimary><translations><translation id="2"><isPrimary>true</isPrimary>
          <formattedFullName>EXAMPLE SHIPPING LLC</formattedFullName></translation></translations></name>
      </names>
      <features><feature id="5"><type featureTypeId="344">Digital Currency Address - XBT</type><value>bc1qexample</value></feature></features>
      <identityDocuments><identityDocument id="3"><name nameId="1">NOT THE ENTITY NAME</name></identityDocument></identityDocuments>
    </entity>
    <entity id="58651"><addresses><address id="1"/></addresses></entity>
  </entities>
</sanctionsData>'''
HISTORY = [{'publicationID': 974, 'datePublished': '2026-09-16T10:03:25.555527'},
           {'publicationID': 976, 'datePublished': '2026-09-17T13:32:59.55116'},
           {'publicationID': 977, 'datePublished': '2026-09-18T10:01:54.865286'},
           {'publicationID': 'x', 'datePublished': 'nope'}]
S3 = 'https://wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com/DeltaArchive/d.xml?X-Amz-Signature=abc'


@pytest.mark.asyncio
async def test_sls_deltas_follow_official_redirect_and_track_new_publications(monkeypatch):
    history = list(HISTORY)

    def handler(method, url, kwargs):
        if '/changes/history/' in url:
            return 200, history
        assert kwargs.get('follow_redirects') is True
        return 200, DELTA, {'Content-Type': 'text/xml'}, S3
    calls = respond(monkeypatch, handler)
    provider = open_guard(OfacSanctionsListProvider())
    first = await provider.updates(2026)
    assert [u['publication_id'] for u in first] == [976, 977] and provider.health['rejected_rows'] == 1
    update = first[0]
    assert update['published_at'] == '2026-09-17T17:32:59.551160+00:00'  # naive US Eastern -> UTC
    assert update['counts'] == {'add': 1, 'update': 1} and update['programs'] == ['IRAN-EO13902']
    assert update['digital_currency_addresses'] == {'XBT': 1} and update['assets'] == ['BRENT', 'BTC', 'GOLD', 'WTI']
    added = update['entities'][0]
    assert added['name'] == 'EXAMPLE SHIPPING LLC' and added['lists'] == ['SDN List'] and update['live'] is False
    document = DocumentInput.model_validate(provider.document(update))
    assert document.source_id == 'ofac-sls' and str(document.url) == 'https://sanctionslistservice.ofac.treas.gov/changes/976'
    assert '1 addition, 1 amendment' in document.title and 'Added: EXAMPLE SHIPPING LLC' in document.content
    assert 'at 13:32 EDT on 17 Sep 2026: 1 addition, 1 amendment.' in document.content  # the time in words, never ISO

    history.append({'publicationID': 984, 'datePublished': '2026-09-23T10:07:28.650545'})
    fetched = len(calls)
    second = await provider.updates(2026)
    assert [u['publication_id'] for u in second] == [984] and len(calls) == fetched + 2  # history + one delta
    assert await provider.updates(2026) == []


@pytest.mark.asyncio
async def test_sls_rejects_deltas_from_unexpected_hosts(monkeypatch):
    respond(monkeypatch, lambda m, u, k: (200, DELTA, {}, 'https://evil.example/d.xml'))
    provider = open_guard(OfacSanctionsListProvider())
    with pytest.raises(ValueError, match='unexpected host'):
        await provider.delta({'publication_id': 976, 'published_at': '2026-09-17T17:32:59+00:00'})


@pytest.mark.asyncio
async def test_gdelt_fallback_does_not_reprocess_or_report_an_outage_for_a_processed_batch(monkeypatch):
    listed, previous = '20260926211500', '20260926210000'
    archive = gdelt_zip(previous, GDELT_LINES[:1])

    def handler(method, url, kwargs):
        if url.endswith('lastupdate.txt'):
            return 200, lastupdate(listed, b'not published')
        return (404, b'') if listed in url else (200, archive)
    calls = respond(monkeypatch, handler)
    provider = open_guard(GdeltEventsProvider())
    assert (await provider.batch())['status'] == 'OK_FALLBACK'
    fetched = len(calls)
    again = await provider.batch()
    assert again['status'] == 'UNCHANGED' and again['batch_id'] == previous and again['candidates'] == []
    assert len(calls) == fetched + 2  # lastupdate + the still-missing listed batch; the processed zip is not re-fetched
    assert 'newest processed batch is still' in provider.health['note']


@pytest.mark.asyncio
async def test_sls_keeps_fetched_deltas_when_a_later_delta_fails(monkeypatch):
    def handler(method, url, kwargs):
        if '/changes/history/' in url:
            return 200, HISTORY[:3]
        if url.endswith('/977'):
            return 200, b'<not xml', {}, S3
        return 200, DELTA, {}, S3
    respond(monkeypatch, handler)
    provider = open_guard(OfacSanctionsListProvider())
    first = await provider.updates(2026)
    assert [u['publication_id'] for u in first] == [976] and provider.last_publication_id == 976
    assert 'publication 977' in provider.health['error']
    respond(monkeypatch, lambda m, u, k: (200, HISTORY[:3]) if '/history/' in u else (200, DELTA, {}, S3))
    assert [u['publication_id'] for u in await provider.updates(2026)] == [977]  # retried, nothing skipped
