import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from defusedxml import ElementTree

from backend.config import rules
from backend.providers.base import HTTPProvider, plain_text
from backend.providers.discovery import sec_contact_configured
from backend.providers.models import FMPCalendarRow, FMPEarningsRow, XPost, parse_utc, validate_rows

ATOM = '{http://www.w3.org/2005/Atom}'
RDF_NS = '{http://purl.org/rss/1.0/}'
RDF_ITEM = RDF_NS + 'item'
DC_DATE = '{http://purl.org/dc/elements/1.1/}date'
MAX_FEED_BYTES = 5_000_000
MAX_FEED_ITEMS = 30


def _date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).astimezone(UTC)
    except (ValueError, TypeError):
        try:
            return parse_utc(text.strip())
        except ValueError:
            return None


def parse_feed(content: bytes) -> list[dict]:
    """RSS 2.0, RSS 1.0 (RDF) and Atom entries -> title/link/published/summary."""
    root = ElementTree.fromstring(content)
    def text(node, tag: str) -> str | None:
        return node.findtext(tag) or node.findtext(RDF_NS + tag)

    entries = []
    for item in (root.findall('.//item') or root.findall(f'.//{RDF_ITEM}'))[:MAX_FEED_ITEMS]:
        entries.append({'title': text(item, 'title'), 'link': text(item, 'link'),
                        'published': _date(item.findtext('pubDate') or item.findtext(DC_DATE)),
                        'summary': text(item, 'description') or ''})
    for entry in root.findall(f'.//{ATOM}entry')[:MAX_FEED_ITEMS]:
        link = next((node.get('href') for node in entry.findall(f'{ATOM}link')
                     if node.get('rel', 'alternate') == 'alternate' and node.get('href')), None)
        entries.append({'title': entry.findtext(f'{ATOM}title'), 'link': link,
                        'published': _date(entry.findtext(f'{ATOM}published') or entry.findtext(f'{ATOM}updated')),
                        'summary': entry.findtext(f'{ATOM}summary') or entry.findtext(f'{ATOM}content') or ''})
    return entries


class RSSProvider(HTTPProvider):
    """Official RSS/Atom feed of a registered Tier-1 source. Feed items are OFFICIAL_RELEASE documents whose title and
    summary are plain text (base.plain_text: tags removed, entities decoded)."""

    def __init__(self, source: dict, url: str | None = None, name: str | None = None):
        feed = url or source['rss']
        parts = urlsplit(feed)
        super().__init__(name or source['id'], f'{parts.scheme}://{parts.netloc}')
        self.path = parts.path + ('?' + parts.query if parts.query else '')
        self.source = source

    async def documents(self) -> list[dict]:
        response = await self.request('GET', self.path)
        if len(response.content) > MAX_FEED_BYTES:
            raise ValueError('Feed response exceeds size limit')
        result = []
        for entry in parse_feed(response.content):
            # An RSS description is HTML escaped inside the XML: stored and shown as its plain text, never as markup.
            title = plain_text(entry['title'])
            if not title or not entry['link'] or not entry['published'] or len(title) < 5:
                continue
            result.append({'source_id': self.source['id'], 'title': title[:1000], 'url': entry['link'].strip(),
                           'published_at': entry['published'].isoformat(), 'content': plain_text(entry['summary'])[:40000],
                           'event_type': 'OFFICIAL_RELEASE', 'assets': [], 'direct': True})
        return result


def x_watch_query(config: dict | None = None) -> str:
    """Official accounts, trusted journalists, keywords and cashtags as one recent-search query (<=512 chars)."""
    config = config or rules()
    monitor = config['x_monitoring']
    accounts = [a['account'] for a in config['x_accounts']] + monitor['trusted_journalists']
    terms = [f'from:{a}' for a in accounts] + [f'"{k}"' if ' ' in k else k for k in monitor['keywords']]
    # Cashtag operators require elevated access on some tiers; plain tickers keep the query valid everywhere.
    terms += [c.lstrip('$') for c in monitor['cashtags']]
    query, suffix = '', f" {monitor['exclude']}"
    for term in terms:
        candidate = f'{query} OR {term}' if query else term
        if len(f'({candidate}){suffix}') > 512:
            break
        query = candidate
    return f'({query}){suffix}'


def x_source_for(username: str | None, config: dict | None = None) -> str:
    """Posts from registered official/wire accounts map to their registered source; others are unknown X."""
    config = config or rules()
    accounts = {a['account'].casefold(): a['source_id'] for a in config['x_accounts']}
    return accounts.get((username or '').casefold(), 'x-unknown')


class XProvider(HTTPProvider):
    def __init__(self, key: str):
        super().__init__('x', 'https://api.x.com/2', key, bool(key))
        self.since_id: str | None = None

    @property
    def auth(self) -> dict:
        return {'Authorization': f'Bearer {self.key}'}

    def _posts(self, body: dict) -> list[dict]:
        users = {u.get('id'): u.get('username') for u in (body.get('includes') or {}).get('users', []) if isinstance(u, dict)}
        rows = [dict(row, author_username=users.get(row.get('author_id'))) for row in body.get('data') or [] if isinstance(row, dict)]
        return validate_rows(XPost, rows, self.health)

    async def search(self, query: str, since_id: str | None = None) -> list[dict]:
        if not 1 <= len(query) <= 512:
            raise ValueError('Invalid X query length')
        params = {'query': query, 'tweet.fields': 'created_at,author_id', 'expansions': 'author_id',
                  'user.fields': 'username', 'max_results': rules()['x_monitoring']['max_results']}
        if since_id or self.since_id:
            params['since_id'] = since_id or self.since_id
        body = (await self.request('GET', '/tweets/search/recent', headers=self.auth, params=params)).json()
        posts = self._posts(body if isinstance(body, dict) else {})
        newest = (body.get('meta') or {}).get('newest_id') if isinstance(body, dict) else None
        if newest:
            self.since_id = newest
        return posts

    async def set_stream_rules(self, query: str) -> dict:
        """Filtered stream (real-time, when the account tier allows it): replace rules with the watch query."""
        current = (await self.request('GET', '/tweets/search/stream/rules', headers=self.auth)).json()
        ids = [r['id'] for r in current.get('data') or [] if isinstance(r, dict) and r.get('id')]
        if ids:
            await self.request('POST', '/tweets/search/stream/rules', headers=self.auth, json={'delete': {'ids': ids}})
        return (await self.request('POST', '/tweets/search/stream/rules', headers=self.auth,
                                   json={'add': [{'value': query[:512], 'tag': 'market-watch'}]})).json()

    def parse_stream_line(self, line: str) -> list[dict]:
        """One newline-delimited JSON object from GET /tweets/search/stream."""
        line = line.strip()
        if not line:
            return []
        body = json.loads(line)
        if isinstance(body, dict) and isinstance(body.get('data'), dict):
            body = dict(body, data=[body['data']])
        return self._posts(body if isinstance(body, dict) else {})


class FMPProvider(HTTPProvider):
    def __init__(self, key: str):
        super().__init__('calendar', 'https://financialmodelingprep.com/stable', key, bool(key))

    async def calendar(self, start: str, end: str) -> list[dict]:
        response = await self.request('GET', '/economic-calendar', params={'from': start, 'to': end, 'apikey': self.key})
        return validate_rows(FMPCalendarRow, response.json(), self.health)

    async def earnings(self, start: str, end: str) -> list[dict]:
        response = await self.request('GET', '/earnings-calendar', params={'from': start, 'to': end, 'apikey': self.key})
        return validate_rows(FMPEarningsRow, response.json(), self.health)


class SECProvider(HTTPProvider):
    """EDGAR submissions for a CIK watchlist; filings become Tier-1 SEC filing documents."""

    def __init__(self, user_agent: str):
        super().__init__('sec_edgar', 'https://data.sec.gov', configured=sec_contact_configured(user_agent),
                         headers={'User-Agent': user_agent})
        self.user_agent = user_agent

    async def filings(self, cik: str) -> dict:
        if not cik.isdecimal() or len(cik) > 10:
            raise ValueError('CIK must contain up to ten digits')
        response = await self.request('GET', f'/submissions/CIK{cik.zfill(10)}.json', headers={'User-Agent': self.user_agent})
        body = response.json()
        if not isinstance(body, dict) or not isinstance((body.get('filings') or {}).get('recent'), dict):
            raise ValueError('Invalid EDGAR submissions payload')
        return body

    @staticmethod
    def filing_documents(body: dict, ticker: str, cik: str, config: dict | None = None) -> list[dict]:
        config = (config or rules())['provider_routing']['sec']
        recent = body['filings']['recent']
        columns = ['form', 'accessionNumber', 'acceptanceDateTime', 'primaryDocument', 'primaryDocDescription']
        if any(not isinstance(recent.get(c), list) for c in columns[:4]):
            raise ValueError('Invalid EDGAR recent filings table')
        documents = []
        for row in zip(*(recent.get(c) or [''] * len(recent['form']) for c in columns), strict=False):
            form, accession, accepted, primary, description = row
            if form not in config['forms'] or not accession or not accepted:
                continue
            try:
                published = parse_utc(accepted)
            except ValueError:
                continue
            folder = f"{config['archive_url']}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
            documents.append({'source_id': 'sec-filings', 'title': f'{ticker} files {form}: {description or primary}'[:1000],
                              'url': f'{folder}/{primary}' if primary else f'{folder}/', 'published_at': published.isoformat(),
                              'content': f'{body.get("name", ticker)} ({ticker}) filed form {form} with the SEC.',
                              'event_type': 'EARNINGS' if form in {'10-Q', '10-K'} else 'REGULATORY_FILING',
                              'assets': [ticker], 'direct': True})
            if len(documents) >= config['max_filings_per_company']:
                break
        return documents

    async def documents(self) -> list[dict]:
        watch = rules()['provider_routing']['sec']['cik_watchlist']
        result = []
        for ticker, cik in watch.items():
            result.extend(self.filing_documents(await self.filings(cik), ticker, cik))
        return result
