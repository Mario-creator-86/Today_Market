import json
from datetime import UTC, date, datetime

import httpx
import pytest

from backend.config import Settings
from backend.providers import news
from backend.providers.calendars import FOMCCalendar, TreasuryAuctionCalendar, fmp_economic_rows, parse_fomc
from backend.providers.licensed import LicensedNewsProvider
from backend.providers.market import DeribitProvider, MassiveProvider, TreasuryYieldProvider
from backend.providers.news import (
    FMPProvider,
    RSSProvider,
    SECProvider,
    XProvider,
    parse_feed,
    x_source_for,
    x_watch_query,
)
from backend.providers.registry import build_providers, by_capability, is_stub
from backend.providers.stubs import FMPStub, LicensedNewsStub, MassiveStub, XStub

ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>CPI for all items increases 0.4% in August</title>
<link rel="alternate" href="https://www.bls.gov/news.release/archives/cpi_09112026.htm"/>
<published>2026-09-11T08:30:00-04:00</published><summary>Consumer prices</summary></entry></feed>'''
RDF = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>Policy statement released</title><link>https://www.boj.or.jp/en/a.htm</link>
<dc:date>2026-09-25T17:00:00+09:00</dc:date></item></rdf:RDF>'''
YIELDS = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"><entry><content><m:properties>
<d:NEW_DATE>2026-09-24T00:00:00</d:NEW_DATE><d:BC_2YEAR>4.70</d:BC_2YEAR><d:BC_10YEAR>5.10</d:BC_10YEAR></m:properties></content></entry>
<entry><content><m:properties><d:NEW_DATE>2026-09-25T00:00:00</d:NEW_DATE><d:BC_2YEAR>4.81</d:BC_2YEAR>
<d:BC_10YEAR>5.17</d:BC_10YEAR><d:BC_30YEAR></d:BC_30YEAR></m:properties></content></entry></feed>'''


def respond(monkeypatch, handler):
    async def request(self, method, url, **kwargs):
        status, body = handler(method, url, kwargs)
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(status, content=content, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)


def test_feed_parser_handles_atom_and_rdf_in_utc():
    atom = parse_feed(ATOM)[0]
    assert atom['link'].endswith('cpi_09112026.htm') and atom['published'] == datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
    rdf = parse_feed(RDF)[0]
    assert rdf['title'] == 'Policy statement released' and rdf['published'] == datetime(2026, 9, 25, 8, tzinfo=UTC)


@pytest.mark.asyncio
async def test_rss_provider_sends_identifying_headers(monkeypatch):
    seen = {}

    async def request(self, method, url, **kwargs):
        seen.update(self.headers)
        return httpx.Response(200, content=ATOM, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    docs = await RSSProvider({'id': 'bls', 'rss': 'https://www.bls.gov/feed/cpi.rss'}).documents()
    assert docs[0]['source_id'] == 'bls' and docs[0]['published_at'].endswith('+00:00')
    assert 'MarketIntelligenceHub' in seen['user-agent']


def test_treasury_yields_are_latest_daily_close_never_live():
    quotes = TreasuryYieldProvider.parse(YIELDS)
    assert {q['symbol']: q['price'] for q in quotes} == {'US2Y': 4.81, 'US10Y': 5.17}
    assert all(q['daily_close'] and q['provider'] == 'treasury' for q in quotes)
    assert quotes[0]['timestamp'] == '2026-09-25T20:00:00Z'  # 16:00 New York (EDT)


def test_sec_filings_become_tier1_documents():
    body = {'name': 'NVIDIA CORP', 'filings': {'recent': {
        'form': ['8-K', '4', '10-Q'], 'accessionNumber': ['0001045810-26-000100', '0001045810-26-000101', '0001045810-26-000102'],
        'acceptanceDateTime': ['2026-09-25T16:05:12.000Z', '2026-09-25T16:06:00.000Z', '2026-08-27T20:10:00.000Z'],
        'primaryDocument': ['nvda-8k.htm', 'x.xml', 'nvda-10q.htm'], 'primaryDocDescription': ['8-K', '', '10-Q']}}}
    docs = SECProvider.filing_documents(body, 'NVDA', '1045810')
    assert [d['event_type'] for d in docs] == ['REGULATORY_FILING', 'EARNINGS']
    assert docs[0]['url'] == 'https://www.sec.gov/Archives/edgar/data/1045810/000104581026000100/nvda-8k.htm'
    assert docs[0]['source_id'] == 'sec-filings' and docs[0]['assets'] == ['NVDA']


@pytest.mark.asyncio
async def test_x_search_validates_maps_official_accounts_and_tracks_since_id(monkeypatch):
    calls = []

    def handler(method, url, kwargs):
        calls.append(kwargs['params'])
        return 200, {'data': [{'id': '11', 'text': 'Policy statement', 'created_at': '2026-09-26T10:00:00Z', 'author_id': '1'},
                              {'id': 'bad', 'text': 'x', 'created_at': 'nope'}],
                     'includes': {'users': [{'id': '1', 'username': 'federalreserve'}]}, 'meta': {'newest_id': '11'}}
    respond(monkeypatch, handler)
    provider = XProvider('token')
    posts = await provider.search(x_watch_query())
    assert len(posts) == 1 and provider.health['rejected_rows'] == 1
    assert x_source_for(posts[0]['author_username']) == 'x-federalreserve' and x_source_for('someone') == 'x-unknown'
    await provider.search('fed')
    assert calls[1]['since_id'] == '11' and len(x_watch_query()) <= 512


def test_massive_stream_messages_parse_traded_volume():
    quotes = MassiveProvider('k').parse_stream_message(json.dumps([
        {'ev': 'status', 'message': 'authenticated'}, {'ev': 'A', 'sym': 'NVDA', 'c': 101.5, 'v': 1200, 'e': 1790449200000}]))
    assert quotes == [dict(quotes[0], symbol='NVDA', price=101.5, volume=1200, provider='massive')]


@pytest.mark.asyncio
async def test_invalid_provider_rows_are_dropped_and_counted(monkeypatch):
    respond(monkeypatch, lambda m, u, k: (200, {'result': [{'instrument_name': 'BTC-27DEC26-100000-C', 'open_interest': 5},
                                                           {'instrument_name': 'BTC-27DEC26-100000-C', 'open_interest': -1},
                                                           {'instrument_name': 12}]}))
    provider = DeribitProvider()
    assert len(await provider.metrics('BTC')) == 1 and provider.health['rejected_rows'] == 2
    respond(monkeypatch, lambda m, u, k: (200, [{'date': '2026-10-01 12:30:00', 'event': 'CPI', 'country': 'US', 'impact': 'High',
                                                 'estimate': 3.0, 'actual': 3.2}, {'date': 'not a date', 'event': 'x'}]))
    fmp = FMPProvider('key')
    rows = fmp_economic_rows(await fmp.calendar('2026-10-01', '2026-10-02'))
    assert len(rows) == 1 and rows[0]['raw_surprise'] == pytest.approx(0.2) and rows[0]['scheduled_at'].endswith('+00:00')


@pytest.mark.asyncio
async def test_licensed_gateway_uses_cursor_and_rejects_invalid_documents(monkeypatch):
    params = []

    def handler(method, url, kwargs):
        params.append(kwargs.get('params'))
        return 200, {'documents': [{'title': 'Wire headline text', 'content': '', 'url': 'https://www.reuters.com/a',
                                    'published_at': '2026-09-26T10:00:00Z'}, {'title': 'bad'}], 'next_cursor': 'c1'}
    respond(monkeypatch, handler)
    gateway = LicensedNewsProvider('reuters', 'https://gateway.example', 'k')
    assert len(await gateway.documents()) == 1 and gateway.health['rejected_rows'] == 1
    await gateway.documents()
    assert params == [{}, {'since': 'c1'}]


FOMC_HTML = '''<h4>2026 FOMC Meetings</h4><div class="fomc-meeting__month col-xs-5"><strong>January</strong></div>
<div class="fomc-meeting__date col-xs-4">27-28</div><div class="fomc-meeting__month"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date">30-1*</div><h4>2025 FOMC Meetings</h4>'''


def test_fomc_parser_takes_decision_day_including_cross_month_meetings():
    assert parse_fomc(FOMC_HTML, 2026) == [date(2026, 1, 28), date(2026, 5, 1)]


@pytest.mark.asyncio
async def test_calendar_sources_use_published_times_and_dst(monkeypatch):
    respond(monkeypatch, lambda m, u, k: (200, [
        {'cusip': '912797VJ3', 'securityType': 'Bill', 'securityTerm': '13-Week', 'auctionDate': '2026-09-28T00:00:00',
         'closingTimeCompetitive': '11:30 AM'},
        {'cusip': '91282CQQ7', 'securityType': 'Note', 'securityTerm': '2-Year', 'auctionDate': '2026-09-29T00:00:00',
         'closingTimeCompetitive': ''}]))
    rows = await TreasuryAuctionCalendar().calendar('2026-09-26', '2026-10-31')
    assert rows[0]['scheduled_at'] == '2026-09-28T15:30:00+00:00' and rows[0]['time_known']
    assert rows[1]['time_known'] is False and rows[1]['impact'] == 6
    respond(monkeypatch, lambda m, u, k: (200, FOMC_HTML.replace('January', 'December').replace('27-28', '8-9').encode()))
    fomc = await FOMCCalendar().calendar('2026-12-01', '2026-12-31')
    assert fomc[0]['scheduled_at'] == '2026-12-09T19:00:00+00:00' and fomc[0]['impact'] == 10  # 14:00 EST


@pytest.mark.asyncio
async def test_stubs_are_labelled_and_never_look_like_trusted_evidence():
    quotes = await MassiveStub().quotes()
    assert all(q['provider'] == 'massive-stub' and q['stub'] for q in quotes)
    docs = await LicensedNewsStub('reuters').documents()
    assert all(d['source_id'] == 'dev-stub' and d['discovery_only'] and d['url'].startswith('https://example.invalid/') for d in docs)
    posts = await XStub().search('q')
    assert all(p['author_username'] == 'dev_stub_account' and p['stub'] for p in posts)
    assert all(r['stub'] for r in await FMPStub().calendar('2026-01-01', '2027-01-01'))


def test_registry_uses_stubs_only_without_keys_and_selects_by_capability():
    stubbed = build_providers(Settings(provider_mode='stub'))
    assert is_stub(stubbed['massive-stub']) and 'massive' not in stubbed
    live = build_providers(Settings(provider_mode='stub', massive_api_key='k'))
    assert 'massive' in live and not is_stub(live['massive'])
    default = build_providers(Settings())
    assert not any(is_stub(p) for p in default.values())
    names = {p.name for p in by_capability(default, 'news')}
    # SEC requires a real contact User-Agent; with the placeholder it stays NOT CONFIGURED.
    assert {'fed', 'bls', 'bls-empsit', 'bls-ppi', 'bls-jolts'} <= names and 'x' not in names and 'sec_edgar' not in names
    contact = build_providers(Settings(sec_user_agent='Research Desk ops@research-desk.test'))
    assert 'sec_edgar' in {p.name for p in by_capability(contact, 'news')}
    assert news.RSSProvider in {type(p) for p in by_capability(default, 'news')}


def test_primary_feeds_keep_the_source_id_and_extra_feeds_are_named_by_file():
    from backend.providers.registry import feed_providers
    names = set(feed_providers([
        {'id': 'bea', 'domain': 'bea.gov', 'rss': 'https://apps.bea.gov/rss/rss.xml'},
        {'id': 'bls', 'domain': 'bls.gov', 'rss': 'https://www.bls.gov/feed/cpi.rss',
         'feeds': ['https://www.bls.gov/feed/empsit.rss', 'https://www.bls.gov/other/feed.xml']}]))
    assert names == {'bea', 'bls', 'bls-empsit', 'bls-2'}
