"""Export a REAL event-risk snapshot and interpretation block for the frontend tests.

Deterministic fixture inputs go through the real code paths - the calendar / earnings / FMP row builders, stored ticks
measured by analytics.scheduled_reactions, event_risk.snapshot and interpretation.refresh + interpretation.latest (the
market_interpreter role is unavailable here, so the deterministic summary is what gets stored) - on a temporary SQLite
store. Nothing is fetched and no .env is read. The scenario (Wednesday 2026-10-28, 17:15 UTC):

- FOMC decision at 18:00 UTC (45 min away) with its 18:30 UTC press-conference sub-event, no consensus or pricing;
- US CPI tomorrow 12:30 UTC with NO CONSENSUS;
- AAPL earnings after the US close with a Finnhub estimate (date-only, session window);
- an active verified Hormuz escalation and an unverified X report (potential event);
- a hot US PPI 20 minutes ago (synthetic vendor timestamp) that yields, the dollar and Nasdaq contradict: MARKET
  DIVERGENCE in the post-event block;
- a trade watch with WAIT FOR FED (BTC), CONDITIONAL LONG (BRENT) and LONG (MSFT: the Hormuz event maps no single-name
  equity, so its status does not hinge on the event's relevance sitting near the conditional threshold).

Writes frontend/tests/fixtures/event-risk.real.json; tests/test_event_fixture.py fails while the file is stale.

    .venv/Scripts/python scripts/export_event_fixture.py            # regenerate
    .venv/Scripts/python scripts/export_event_fixture.py --check    # exit 1 when stale
"""
import argparse
import asyncio
import copy
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'frontend' / 'tests' / 'fixtures' / 'event-risk.real.json'
REGENERATE = '.venv/Scripts/python scripts/export_event_fixture.py'
NOW = datetime(2026, 10, 28, 17, 15, tzinfo=UTC)
FED_AT = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
PPI_AT = NOW - timedelta(minutes=20)
KINDS = {'BTC': 'crypto', 'ETH': 'crypto', 'NASDAQ': 'index', 'SP500': 'index', 'DXY': 'index', 'US2Y': 'bond', 'US10Y': 'bond',
         'GOLD': 'commodity', 'BRENT': 'commodity', 'WTI': 'commodity', 'AAPL': 'equity', 'MSFT': 'equity'}


def _decision(category: str, score: float, **extra: Any) -> dict:
    return {'category': category, 'direction': category, 'score': score, 'audit_status': 'PASS',
            'confirmation': 'holds above the prior session high', 'invalidation': 'closes below the prior session low',
            'why': ['published fixture decision'], **extra}


PUBLISHED = {
    'BTC': _decision('LONG SETUP', 44, market_confirmation=63, evidence_confidence=91, invalidation_level=60000.0),
    'NASDAQ': _decision('BULLISH LEAN / WAIT', 22),
    'BRENT': _decision('LONG SETUP', 38, market_confirmation=70, invalidation_level=82.5),
    'MSFT': _decision('LONG SETUP', 35, market_confirmation=72, invalidation_level=400.0),
}


def _regime() -> dict:
    moving = {'Rates': 'down', 'USD': 'down', 'Risk appetite': 'up'}
    return {'regimes': ['RISK_ON'], 'posture': 'CONSTRUCTIVE', 'confidence': 80.0, 'coverage_pct': 90.0, 'basis': 'live',
            'basis_label': 'live intraday', 'method': 'fixture', 'as_of': NOW.isoformat(),
            'features': {'BTC': {'price': 64000.0, 'change_pct': 2.0}, 'NASDAQ': {'price': 20000.0, 'change_pct': 1.0},
                         'SP500': {'price': 6000.0, 'change_pct': 0.8}, 'DXY': {'price': 100.0, 'change_pct': -0.4},
                         'GOLD': {'price': 2500.0, 'change_pct': 0.1}},
            # Per-feature provenance as regime.classify reports it: each market-view fact names its own basis.
            'feature_sources': {s: {'basis': 'live', 'source': provider, 'as_of': NOW.isoformat(), 'change_basis': basis}
                                for s, provider, basis in (('BTC', 'binance', 'rolling_24h'),
                                                           ('NASDAQ', 'alpaca', 'previous_close'),
                                                           ('SP500', 'alpaca', 'previous_close'),
                                                           ('DXY', 'finnhub', 'provider'),
                                                           ('GOLD', 'finnhub', 'previous_close'))},
            'evaluated': [{'regime': 'RISK_ON', 'state': 'ACTIVE', 'confidence': 80.0}],
            'drivers': [{'driver': name, 'direction': moving.get(name, 'flat'), 'arrow': None, 'inputs': [], 'missing': []}
                        for name in ('Rates', 'USD', 'Oil', 'Risk appetite')]}


def _assets(now: datetime) -> list[dict]:
    from backend.sessions import session_info
    result = []
    for symbol, kind in KINDS.items():
        info = session_info(kind, now, symbol)
        quote = {'price': 100.0, 'timestamp': now.isoformat(), 'session': info, 'market_state': info['state'],
                 'freshness_status': 'FRESH'}
        quote.update(_regime()['features'].get(symbol, {}))
        result.append({'symbol': symbol, 'name': symbol, 'asset_class': kind, 'quote': quote,
                       'decision': copy.deepcopy(PUBLISHED.get(symbol)), 'session': info})
    return result


def _news() -> list[dict]:
    from backend import causal
    from backend.config import rules
    kinds = {symbol: kind for symbol, _, kind in rules()['assets']}

    def item(news_id: str, summary: str, status: str, seen: datetime, trigger: str, event_type: str, sources: list[str]) -> dict:
        # The macro assets the causal graph reaches, never a single-name equity: MSFT keeps a plain LONG whatever the
        # event's relevance (a low exposure put its event risk within 2 points of the conditional threshold).
        affected = [a for a in causal.affected_assets(trigger) if kinds.get(a) != 'equity']
        return {'id': news_id, 'event_id': news_id, 'summary_fact': summary, 'event_type': event_type,
                'verification_status': status, 'fact_confidence': 95.0 if status == 'CONFIRMED' else 20.0,
                'first_seen_utc': seen.isoformat(), 'last_updated_utc': seen.isoformat(), 'event_time': seen.isoformat(),
                'trigger': trigger, 'assets': [], 'affected_assets': affected, 'importance': 0.9,
                'sources': sources, 'evidence': []}
    return [item('a3f1c2d4-hormuz', 'Tanker attacked in the Strait of Hormuz; shipping halted', 'CONFIRMED',
                 NOW - timedelta(hours=6), 'GEOPOLITICAL_ENERGY', 'GEOPOLITICAL_ENERGY', ['Reuters', 'AP']),
            item('b7e9d0c1-x-post', 'Missile strike reported on a tanker near Hormuz', 'UNCONFIRMED',
                 NOW - timedelta(minutes=10), 'GEOPOLITICAL_ENERGY', 'SOCIAL_DISCOVERY', ['X (unknown account)'])]


def _calendar() -> list[dict]:
    from backend.providers.calendars import _config, calendar_row, fmp_economic_rows
    from backend.providers.finnhub import earnings_rows
    fed = _config()
    common = {'bank': 'fed', 'event_class': 'FOMC', 'time_known': True, 'time_source': 'config', 'sep': False,
              'attribution': fed.get('fomc_attribution'), 'meeting_days': ['2026-10-27', '2026-10-28']}
    decision = calendar_row('FOMC rate decision', 'US', 'CENTRAL_BANK', FED_AT, fed['fomc_impact'], 'fomc_calendar',
                            fed['fomc_url'], sub_event='DECISION', time_basis=fed['fomc_time_basis'], **common)
    press = calendar_row('FOMC press conference', 'US', 'CENTRAL_BANK', FED_AT + timedelta(minutes=30),
                         fed['fomc_press_conference_impact'], 'fomc_calendar', fed['fomc_url'], sub_event='PRESS_CONFERENCE',
                         parent_event_id=decision['id'], time_basis=fed['fomc_press_conference_time_basis'], **common)
    cpi = calendar_row('Consumer Price Index', 'US', 'MACRO', datetime(2026, 10, 29, 12, 30, tzinfo=UTC), 10,
                       'bls_release_calendar', 'https://www.bls.gov/schedule/news_release/cpi.htm', event_class='US_CPI',
                       time_known=True, time_source='source', time_basis='BLS release schedule (ICS)',
                       reference_period='September 2026', attribution='Source: U.S. Bureau of Labor Statistics')
    aapl = earnings_rows([{'symbol': 'AAPL', 'date': '2026-10-28', 'hour': 'amc', 'quarter': 4, 'year': 2026,
                           'epsEstimate': 2.02, 'revenueEstimate': 1.2e11}])
    ppi = fmp_economic_rows([{'date': PPI_AT.isoformat(), 'event': 'PPI MoM', 'country': 'US', 'impact': 'High',
                              'estimate': 0.2, 'previous': 0.1, 'actual': 0.5, 'unit': '%'}])
    return [decision, press, cpi, *aapl, *ppi]


def _ticks(store: Any) -> None:
    """Hot PPI met by falling yields, a weaker dollar and a Nasdaq rally (baseline 30 s before, then T+1m/5m/15m)."""
    from backend.database import Tick
    paths = {'US2Y': (3.90, [3.89, 3.87, 3.84]), 'US10Y': (4.20, [4.19, 4.17, 4.15]), 'DXY': (100.0, [99.9, 99.7, 99.6]),
             'NASDAQ': (20000.0, [20050.0, 20120.0, 20200.0])}
    with store.session() as session:
        for symbol, (before, after) in paths.items():
            stamps = [PPI_AT - timedelta(seconds=30)] + [PPI_AT + timedelta(minutes=m, seconds=-10) for m in (1, 5, 15)]
            for stamp, price in zip(stamps, [before, *after], strict=True):
                session.add(Tick(symbol=symbol, price=price, timestamp=stamp, ingested_at=stamp, provider='fixture', payload={}))
        session.commit()


class FixtureHub:
    """The HubService surface event_risk.snapshot and interpretation.refresh read, over fixture inputs."""

    def __init__(self, store: Any):
        self.store = store
        self.settings = SimpleNamespace(quote_stale_seconds=15)
        self.ai = SimpleNamespace(available=lambda role: False, ready=lambda: False)
        self._assets, self._news, self._calendar, self._regime = _assets(NOW), _news(), _calendar(), _regime()

    def assets(self, now: datetime | None = None, *, event_overlay: bool = True) -> list[dict]:
        return copy.deepcopy(self._assets)

    def events(self, limit: int = 100) -> list[dict]:
        return copy.deepcopy(self._news[:limit])

    def calendar(self, window: bool = False, upcoming_hours: int | None = None) -> list[dict]:
        return copy.deepcopy(self._calendar)

    def regime(self, assets: list[dict] | None = None, events: list[dict] | None = None, last_close: bool = False) -> dict:
        return copy.deepcopy(self._regime)

    def health(self) -> list[dict]:
        return []


def build() -> dict:
    """{event_risk, interpretation, assets} from the real code paths on a temporary store."""
    from backend import event_risk, interpretation
    from backend.database import Store
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        store = Store(f"sqlite:///{Path(folder, 'fixture.db').as_posix()}")
        try:
            store.initialize()
            _ticks(store)
            hub = FixtureHub(store)
            asyncio.run(interpretation.refresh(hub, [], NOW))
            block = interpretation.latest(store, NOW)
            snapshot = event_risk.snapshot(hub, NOW)
            assets = hub.assets(NOW, event_overlay=False)
            event_risk.apply_assets(assets, snapshot['asset_event_risk'])
        finally:
            store.engine.dispose()
    return {'generated_by': 'scripts/export_event_fixture.py', 'regenerate': REGENERATE, 'as_of': NOW.isoformat(),
            'event_risk': snapshot, 'interpretation': block,
            'assets': [{k: a.get(k) for k in ('symbol', 'name', 'asset_class', 'decision', 'event_risk')} for a in assets]}


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--check', action='store_true', help='exit 1 when the fixture file is stale')
    args = parser.parse_args(argv)
    text = render()
    current = OUTPUT.read_text(encoding='utf-8') if OUTPUT.exists() else None
    if args.check:
        print('fixture up to date' if current == text else f'fixture stale: regenerate with {REGENERATE}')
        return 0 if current == text else 1
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding='utf-8', newline='\n')
    print(f'wrote {OUTPUT.relative_to(ROOT).as_posix()}')
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
