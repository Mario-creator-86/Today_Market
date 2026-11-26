"""Federal Reserve Bank of New York Markets API (public, no key): EFFR with the FOMC target range.
Used for the Fed policy rate when FRED is not configured; labelled with its effective date, never as live."""
from pydantic import Field

from backend.providers.base import HTTPProvider
from backend.providers.models import ProviderRow, validate_rows

ATTRIBUTION = 'Source: Federal Reserve Bank of New York'


class EffrRow(ProviderRow):
    effectiveDate: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    type: str
    percentRate: float = Field(ge=-5, le=30)
    targetRateFrom: float = Field(ge=-5, le=30)
    targetRateTo: float = Field(ge=-5, le=30)


class NYFedRatesProvider(HTTPProvider):
    def __init__(self):
        super().__init__('nyfed_rates', 'https://markets.newyorkfed.org')

    async def policy_rate(self) -> dict | None:
        """{bank, policy_rate (target midpoint), policy_rate_range, effr, as_of, source, verified, basis, delay}."""
        body = (await self.request('GET', '/api/rates/unsecured/effr/last/1.json')).json()
        rows = validate_rows(EffrRow, (body or {}).get('refRates', []), self.health)
        if not rows:
            return None
        row = rows[0]
        low, high = row['targetRateFrom'], row['targetRateTo']
        if low > high:
            raise ValueError('Inverted FOMC target range')
        # Published around 09:00 ET for the previous business day: T+1, labelled as such.
        return {'bank': 'fed', 'currency': 'USD', 'policy_rate': round((low + high) / 2, 4), 'rate': round((low + high) / 2, 4),
                'policy_rate_range': [low, high], 'effr': row['percentRate'], 'as_of': f"{row['effectiveDate']}T00:00:00+00:00",
                'source': ATTRIBUTION, 'verified': True, 'delay': 'T+1 (EFFR effective date)',
                'basis': 'FOMC target range midpoint as published with EFFR by the New York Fed'}
