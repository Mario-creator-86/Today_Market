"""Provider factory: builds every adapter from configuration and credentials, and lets callers select
providers by capability instead of by concrete class (spec §5: replaceable providers)."""
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from backend.config import Settings, rules
from backend.providers.alpaca import AlpacaDataProvider
from backend.providers.base import CAPABILITIES
from backend.providers.calendars import (
    CongressCalendar,
    CryptoExpiryCalendar,
    FOMCCalendar,
    TreasuryAuctionCalendar,
)
from backend.providers.cb_calendars import central_bank_calendars
from backend.providers.discovery import (
    EdgarFullTextProvider,
    GdeltEventsProvider,
    OfacRecentActionsProvider,
    OfacSanctionsListProvider,
)
from backend.providers.econ_calendars import release_calendars
from backend.providers.eia_spot import EIASpotPriceProvider
from backend.providers.finnhub import FinnhubProvider
from backend.providers.licensed import LicensedNewsProvider
from backend.providers.market import (
    BinanceProvider,
    CoinbaseProvider,
    DeribitProvider,
    MassiveProvider,
    TreasuryYieldProvider,
)
from backend.providers.news import FMPProvider, RSSProvider, SECProvider, XProvider
from backend.providers.nyfed import NYFedRatesProvider
from backend.providers.official_data import (
    BLSProvider,
    EcbDataProvider,
    EIAWeeklyPetroleumCalendar,
    EIAWeeklyPetroleumProvider,
    EurostatProvider,
    FredProvider,
    TreasuryFiscalProvider,
)
from backend.providers.positioning_sources import (
    BinanceFuturesProvider,
    CFTCCOTProvider,
    CoinbaseIntxProvider,
)
from backend.providers.stubs import CongressStub, FMPStub, LicensedNewsStub, MassiveStub, XStub

GENERIC_FEED_NAMES = {'rss', 'feed', 'feeds', 'index', 'atom', 'news'}


def _feed_label(url: str) -> str:
    """'https://www.bls.gov/feed/empsit.rss' -> 'empsit' (the feed file names the release it carries)."""
    stem = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1].rsplit('.', 1)[0].lower()
    return re.sub(r'[^a-z0-9]+', '_', stem).strip('_')


def feed_providers(sources: Iterable[dict]) -> dict[str, RSSProvider]:
    """One provider per official feed; extra feeds of a source are named '<id>-<feed file>' (or '<id>-<n>')."""
    result: dict[str, RSSProvider] = {}
    for source in sources:
        feeds = ([source['rss']] if source.get('rss') else []) + list(source.get('feeds', []))
        for index, url in enumerate(feeds):
            name = source['id']  # the primary feed carries the source id (source status is looked up by it)
            if index:
                label = _feed_label(url)
                name = f"{source['id']}-{label}" if label and label not in GENERIC_FEED_NAMES else f"{source['id']}-{index}"
                if name in result:
                    name = f"{source['id']}-{index}"
            result[name] = RSSProvider(source, url, name)
    return result


def build_providers(settings: Settings, llm: Any = None) -> dict[str, Any]:
    """All adapters keyed by provider name. With PROVIDER_MODE=stub, paid adapters without a key are replaced
    by labelled development stubs; with keys they are always the real adapters."""
    stub = settings.provider_mode == 'stub'
    secret = {name: getattr(settings, name).get_secret_value() for name in
              ('massive_api_key', 'x_bearer_token', 'fmp_api_key', 'reuters_api_key', 'ap_api_key', 'congress_api_key',
               'finnhub_api_key', 'fred_api_key', 'bls_api_key', 'alpaca_api_key_id', 'alpaca_api_secret_key')}
    providers: list[Any] = [BinanceProvider(), CoinbaseProvider(), DeribitProvider(), TreasuryYieldProvider(),
                            SECProvider(settings.sec_user_agent), TreasuryAuctionCalendar(), FOMCCalendar(), CryptoExpiryCalendar()]
    providers.append(MassiveStub() if stub and not secret['massive_api_key'] else MassiveProvider(secret['massive_api_key']))
    providers.append(XStub() if stub and not secret['x_bearer_token'] else XProvider(secret['x_bearer_token']))
    providers.append(FMPStub() if stub and not secret['fmp_api_key'] else FMPProvider(secret['fmp_api_key']))
    providers.append(CongressStub() if stub and not secret['congress_api_key'] else CongressCalendar(secret['congress_api_key']))
    for name, base, key in [('reuters', settings.reuters_base_url, secret['reuters_api_key']),
                            ('ap', settings.ap_base_url, secret['ap_api_key'])]:
        providers.append(LicensedNewsStub(name) if stub and not (base and key) else LicensedNewsProvider(name, base, key))
    # Vetted free/official sources (see docs/DATA_SOURCES.md); keyed ones stay NOT CONFIGURED without a key.
    providers += [CFTCCOTProvider(), BinanceFuturesProvider(), CoinbaseIntxProvider(), *central_bank_calendars(),
                  FinnhubProvider(secret['finnhub_api_key']), EcbDataProvider(), EurostatProvider(),
                  BLSProvider(secret['bls_api_key']), TreasuryFiscalProvider(), EIAWeeklyPetroleumProvider(),
                  EIAWeeklyPetroleumCalendar(), FredProvider(secret['fred_api_key']), GdeltEventsProvider(),
                  EdgarFullTextProvider(settings.sec_user_agent), OfacRecentActionsProvider(), OfacSanctionsListProvider(),
                  NYFedRatesProvider(), EIASpotPriceProvider(),
                  AlpacaDataProvider(secret['alpaca_api_key_id'], secret['alpaca_api_secret_key'])]
    # Official release schedules for the event-aware dashboard (keyless; BLS needs a contact in SEC_USER_AGENT and falls
    # back on FRED release dates). Finnhub (earnings) and FRED (BLS fallback) serve the calendar capability themselves.
    providers += release_calendars(settings.sec_user_agent)
    if llm is not None:
        providers.append(llm)
    result = {p.name: p for p in providers}
    result.update(feed_providers(rules()['sources']))
    return result


def by_capability(providers: dict[str, Any], capability: str) -> list[Any]:
    """Configured providers implementing a capability protocol (market_data, news, calendar, social, ...)."""
    protocol = CAPABILITIES[capability]
    return [p for p in providers.values() if getattr(p, 'configured', True) and isinstance(p, protocol)]


def by_method(providers: dict[str, Any], method: str) -> list[Any]:
    """Configured providers exposing a callable (positioning, candidates, ...) for capabilities outside base.CAPABILITIES."""
    return [p for p in providers.values() if getattr(p, 'configured', True) and callable(getattr(p, method, None))]


def is_stub(provider: Any) -> bool:
    return bool(getattr(provider, 'health', {}).get('stub'))
