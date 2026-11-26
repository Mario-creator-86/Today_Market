"""Contract adapter for a licensed news gateway; never scrapes paid publications."""
from pydantic import ValidationError

from backend.providers.base import HTTPProvider
from backend.schemas import DocumentInput

MAX_DOCUMENTS = 100


class LicensedNewsProvider(HTTPProvider):
    """GET {base}/news?since=<cursor> -> {"documents": [...], "next_cursor": "..."} (customer-operated gateway
    mapping licensed Reuters/AP fields to the public document contract). Long-poll gateways may hold the request."""

    def __init__(self, name: str, base_url: str, key: str):
        super().__init__(name, base_url, key, bool(base_url and key), timeout=60)
        self.cursor: str | None = None

    async def documents(self) -> list[dict]:
        params = {'since': self.cursor} if self.cursor else {}
        response = await self.request('GET', '/news', headers={'Authorization': f'Bearer {self.key}'}, params=params)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('documents'), list):
            raise ValueError('Licensed gateway must return documents array')
        result, rejected = [], 0
        for row in payload['documents'][:MAX_DOCUMENTS]:
            try:
                document = DocumentInput.model_validate(dict(row, source_id=self.name, discovery_only=False))
            except (ValidationError, TypeError):
                rejected += 1
                continue
            result.append(document.model_dump(mode='json'))
        self.health['rejected_rows'] = self.health.get('rejected_rows', 0) + rejected
        if isinstance(payload.get('next_cursor'), str):
            self.cursor = payload['next_cursor']
        return result
