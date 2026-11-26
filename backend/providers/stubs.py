"""Development stubs for paid providers (spec §60), used only with PROVIDER_MODE=stub and a missing key.

Every record carries provider '<name>-stub' and stub=true; documents come from the 'dev-stub' source
(anonymous, discovery only) so they can never confirm facts. Stub quotes never reach scoring."""
import json
from datetime import timedelta
from pathlib import Path

from backend.config import ROOT, rules
from backend.database import utcnow
from backend.providers.base import identity
from backend.providers.calendars import calendar_row, fmp_earnings_rows
from backend.schemas import Quote

STUB_STATUS = 'DEVELOPMENT STUB'
STUB_SOURCE = 'dev-stub'


def fixtures() -> dict:
    path = Path(ROOT) / rules()['provider_routing']['stub_fixtures_dir'] / 'stub_data.json'
    return json.loads(path.read_text(encoding='utf-8'))


class StubProvider:
    configured = True

    def __init__(self, name: str):
        self.name = f'{name}-stub'
        self.health = {**identity(self.name), 'status': STUB_STATUS, 'latency_ms': 0, 'last_success': None,
                       'error': None, 'error_type': None, 'stub': True, 'note': fixtures()['_notice']}

    def touch(self) -> None:
        self.health['last_success'] = utcnow().isoformat()


class MassiveStub(StubProvider):
    def __init__(self):
        super().__init__('massive')

    def _quotes(self, table: str) -> list[dict]:
        self.touch()
        return [Quote(symbol=symbol, price=price, timestamp=utcnow(), provider='massive-stub', stub=True).model_dump(mode='json')
                for symbol, price in fixtures()[table].items()]

    async def quotes(self) -> list[dict]:
        return self._quotes('massive_quotes')

    async def indices(self, mapping: dict | None = None) -> list[dict]:
        return self._quotes('massive_indices')

    async def fx_quotes(self) -> list[dict]:
        return self._quotes('massive_fx')


class XStub(StubProvider):
    def __init__(self):
        super().__init__('x')

    async def search(self, query: str, since_id: str | None = None) -> list[dict]:
        self.touch()
        now = utcnow()
        return [{'id': post['id'], 'text': post['text'], 'author_username': post['author_username'], 'stub': True,
                 'created_at': (now - timedelta(minutes=post['minutes_ago'])).isoformat()}
                for post in fixtures()['x_posts'] if not since_id or post['id'] > since_id]


class FMPStub(StubProvider):
    def __init__(self):
        super().__init__('calendar')

    async def calendar(self, start: str, end: str) -> list[dict]:
        self.touch()
        now = utcnow()
        rows = []
        for row in fixtures()['economic_calendar']:
            item = calendar_row(row['event'], row['country'], 'MACRO', now + timedelta(hours=row['hours_from_now']),
                                {'High': 10, 'Medium': 5, 'Low': 1}[row['impact']], self.name, stub=True)
            item.update(consensus=row['estimate'], previous=row['previous'], unit=row['unit'])
            rows.append(item)
        return rows

    async def earnings(self, start: str, end: str) -> list[dict]:
        self.touch()
        today = utcnow().date()
        rows = [{'symbol': r['symbol'], 'date': (today + timedelta(days=r['days_from_now'])).isoformat(),
                 'epsEstimated': r['epsEstimated']} for r in fixtures()['earnings']]
        return [dict(row, stub=True) for row in fmp_earnings_rows(rows, {r['symbol'] for r in rows}, self.name)]


class LicensedNewsStub(StubProvider):
    async def documents(self) -> list[dict]:
        self.touch()
        now = utcnow()
        return [{'source_id': STUB_SOURCE, 'title': item['title'], 'content': item['content'],
                 'url': f'https://example.invalid/{self.name}/{index}', 'published_at': (now - timedelta(minutes=item['minutes_ago'])).isoformat(),
                 'event_type': 'NEWS', 'assets': [], 'direct': False, 'discovery_only': True}
                for index, item in enumerate(fixtures()['licensed_news'])]


class CongressStub(StubProvider):
    def __init__(self):
        super().__init__('congress_api')

    async def calendar(self, start: str, end: str) -> list[dict]:
        self.touch()
        now = utcnow()
        return [calendar_row(f"{b['type']}{b['number']}: {b['title']}", 'US', 'LEGISLATION', now - timedelta(days=b['days_ago']),
                             rules()['provider_routing']['calendars']['legislation_impact'], self.name, None,
                             latest_action=b['action'], stub=True)
                for b in fixtures()['congress_bills']]
