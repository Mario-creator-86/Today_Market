"""Point-in-time replay. Each simulated decision sees only records ingested by the simulated time.

Ticks need ingested_at <= T, evidence needs the document and its event link ingested by T (a later, backdated
article cannot count), calendar actuals are masked until actual_ingested_at <= T. AI roles are not replayed: the
replay rebuilds the deterministic score and gates only. Nothing is written to the store.
"""
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import calibration, sessions
from backend import evaluation as ev
from backend.config import freshness_seconds, get_settings
from backend.database import RECORDS, Document, Event, Evidence, Store, Tick, utcnow
from backend.engines import aware, causal_impact, confidence, reaction, technical_levels
from backend.sessions import ruleset

LIMITATIONS = [
    'AI extractor/chief/auditor roles are not replayed; published direction assumes deterministic gates only',
    'Event type and asset tags are the latest stored values; AI enrichment history is not versioned',
    'Source registry and weights are the current configuration',
]


def config() -> dict:
    return ruleset()['autolearn']['backtest']


def evidence_as_of(session: Session, sources: dict[str, dict], event_id: str, at: datetime) -> list[dict]:
    rows = session.execute(select(Evidence, Document).join(Document, Evidence.document_id == Document.id).where(
        Evidence.event_id == event_id, Document.created_at <= at, Evidence.created_at <= at)).all()
    weights = ruleset()['source_weights']
    result = []
    for link, document in rows:
        source = sources.get(document.source_id, {})
        result.append({**document.payload, 'id': link.id, 'document_id': document.id, 'source_id': document.source_id,
                       'published_at': aware(document.published_at).isoformat(),
                       'ingested_at': aware(document.created_at).isoformat(), 'relation': link.relation,
                       'group': source.get('independence_group', document.source_id),
                       'weight': weights.get(source.get('kind'), weights['anonymous']),
                       'official': source.get('official', False)})
    return result


def events_as_of(session: Session, sources: dict[str, dict], at: datetime) -> list[dict]:
    rows = session.scalars(select(Event).where(Event.created_at <= at)
                           .order_by(Event.created_at.desc()).limit(config()['max_events']))
    result = []
    for event in rows:
        evidence = evidence_as_of(session, sources, event.id, at)
        verified = confidence(evidence, at)
        result.append({'id': event.id, 'event_type': event.event_type, 'assets': event.payload.get('assets', []),
                       'verification_status': verified['status'], 'fact_confidence': verified['confidence'],
                       'first_seen_utc': aware(event.created_at).isoformat(), 'evidence_ids': [e['id'] for e in evidence],
                       'source_ids': sorted({e['source_id'] for e in evidence})})
    return result


def calendar_as_of(store: Store, at: datetime, items: list[dict] | None = None) -> list[dict]:
    """Releases known by `at`; an actual is visible only once its first-seen ingestion time has passed."""
    result = []
    for item in store.list('macro_calendar', config()['max_calendar_rows']) if items is None else items:
        known = item.get('ingested_at')
        if not known or aware(known) > at:
            continue
        seen = item.get('actual_ingested_at')
        visible = item.get('actual') is not None and isinstance(seen, str) and aware(seen) <= at
        result.append(dict(item, actual=item.get('actual') if visible else None,
                           actual_status='KNOWN' if visible else 'NOT YET KNOWN' if seen else
                           ('UNKNOWN INGESTION TIME' if item.get('actual') is not None else 'NOT RELEASED')))
    return result


def tick_as_of(session: Session, symbol: str, at: datetime, stale: float) -> Tick | None:
    """The latest live tick known at `at`, while it is inside the freshness limit of its provider
    (config.freshness_seconds, the lookup the live gate uses; `stale` is the default for unlisted providers)."""
    tick = session.scalar(select(Tick).where(*ev._usable(symbol, at), Tick.timestamp <= at)
                          .order_by(Tick.timestamp.desc(), Tick.id.desc()).limit(1))
    if tick is None or (at - aware(tick.timestamp)).total_seconds() > freshness_seconds(tick.provider, stale):
        return None
    return tick


def quote_as_of(session: Session, symbol: str, at: datetime, stale: float) -> dict | None:
    tick = tick_as_of(session, symbol, at, stale)
    if tick is None:
        return None
    return {'price': tick.price, 'bid': tick.payload.get('bid'), 'ask': tick.payload.get('ask'), 'tick_id': tick.id,
            'timestamp': aware(tick.timestamp).isoformat(), 'ingested_at': aware(tick.ingested_at).isoformat(),
            'provider': tick.provider}


def bars_as_of(session: Session, symbol: str, at: datetime) -> list[dict]:
    cfg = config()
    ticks = list(session.scalars(select(Tick).where(
        *ev._usable(symbol, at), Tick.timestamp <= at, Tick.timestamp >= at - timedelta(hours=cfg['bar_lookback_hours']))
        .order_by(Tick.timestamp.desc()).limit(cfg['max_bar_ticks'])))[::-1]
    buckets: dict[str, dict] = {}
    for tick in ticks:
        minute = aware(tick.timestamp).replace(second=0, microsecond=0).isoformat()
        bar = buckets.setdefault(minute, {'time': minute, 'open': tick.price, 'high': tick.price, 'low': tick.price,
                                          'close': tick.price, 'volume': 0})
        bar.update(high=max(bar['high'], tick.price), low=min(bar['low'], tick.price), close=tick.price)
        bar['volume'] += tick.payload.get('trade_quantity', 0)
    return list(buckets.values())


def reaction_as_of(session: Session, symbol: str, events: list[dict], quote: dict | None, expected: int,
                   stale: float) -> dict | None:
    if not events or not quote or not expected:
        return None
    cfg = config()
    event_time = aware(events[-1]['first_seen_utc'])
    if aware(quote['timestamp']) < event_time + timedelta(seconds=cfg['reaction_delay_seconds']):
        return None
    baseline = tick_as_of(session, symbol, event_time, stale)
    if baseline is None:
        return None
    return dict(reaction(baseline.price, quote['price'], expected, cfg['reaction_tolerance_pct']),
                baseline=baseline.price, baseline_timestamp=aware(baseline.timestamp).isoformat())


def rebuild(session: Session, sources: dict[str, dict], symbol: str, at: datetime,
            weights: dict[str, float], stale: float, events: list[dict] | None = None) -> dict:
    """Deterministic decision for `symbol` at `at` from point-in-time inputs (mirrors the live gates)."""
    cfg, kind = config(), ev.asset_classes()[symbol]
    events = events_as_of(session, sources, at) if events is None else events
    relevant = [e for e in events if symbol in e['assets']]
    confirmed = [e for e in relevant if e['verification_status'] == 'CONFIRMED']
    components: dict[str, float] = {'event': max(-100, min(100, sum(causal_impact(e, symbol, at) for e in confirmed)))}
    quote, bars = quote_as_of(session, symbol, at, stale), bars_as_of(session, symbol, at)
    levels = technical_levels(bars[:-1])
    state = sessions.market_state(kind, at, symbol)
    blocks = []
    if quote is None:
        blocks.append('Critical data missing or stale')
    if state != 'LIVE':
        blocks.append('Market closed or session unverified')
    if not confirmed:
        blocks.append('No verified asset-specific catalyst')
    if any(e['verification_status'] != 'CONFIRMED' for e in relevant):
        blocks.append('Relevant evidence unverified or contradictory')
    if kind == 'fx' and not session.scalar(select(RECORDS['central_bank_state'].id)
                                           .where(RECORDS['central_bank_state'].created_at <= at).limit(1)):
        blocks.append('Relative central-bank and yield comparison unavailable')
    sign = 1 if components['event'] > 0 else -1 if components['event'] < 0 else 0
    if quote and levels and sign and (quote['price'] > levels['resistance'] if sign > 0 else quote['price'] < levels['support']):
        components['technical'] = sign * cfg['technical_magnitude']
    else:
        blocks.append('Required price confirmation absent')
    measured = reaction_as_of(session, symbol, confirmed, quote, sign, stale)
    if measured and measured['status'] == 'CONFIRMED':
        components['reaction'] = sign * cfg['reaction_magnitude']
    else:
        blocks.append('Post-event market reaction unconfirmed')
    score = calibration.score_with(components, weights)
    label = calibration.category(score, bool(blocks), components)
    return {'symbol': symbol, 'at': at.isoformat(), 'score': score, 'category': label, 'blocks': blocks,
            'components': components, 'levels': levels, 'market_state': state, 'quote': quote,
            'events': [e['id'] for e in confirmed], 'relevant_events': len(relevant),
            'ai_replayed': False}


def replay(store: Store, start: datetime, end: datetime, step_seconds: int, symbols: list[str] | None = None,
           weights: dict[str, float] | None = None, horizons: list[str] | None = None,
           evaluate: bool = True, now: datetime | None = None) -> dict:
    """Rebuild decisions every `step_seconds` in [start, end] and measure their outcomes without lookahead."""
    start, end, now = aware(start), aware(end), aware(now or utcnow())
    if step_seconds <= 0 or end < start or end > now:
        raise ValueError('Invalid replay window (step must be positive and the window must end by now)')
    steps = int((end - start).total_seconds() // step_seconds) + 1
    if steps > config()['max_steps']:
        raise ValueError(f"Replay exceeds {config()['max_steps']} steps")
    known = ev.asset_classes()
    symbols = [s for s in (symbols or list(known)) if s in known]
    weights = dict(weights or ruleset()['weights'])
    labels = [h for h in (horizons or ev.horizons()) if h in ev.horizons()]
    stale = get_settings().quote_stale_seconds
    sources = {s['id']: s for s in store.list('sources', config()['max_sources'])}
    releases = store.list('macro_calendar', config()['max_calendar_rows'])  # read once, masked per step
    decisions: list[dict] = []
    outcomes: dict[str, list[dict]] = {label: [] for label in labels}
    with store.session() as session:
        for index in range(steps):
            at = start + timedelta(seconds=index * step_seconds)
            events = events_as_of(session, sources, at)
            known_calendar = calendar_as_of(store, at, releases)
            calendar = {'known': len(known_calendar),
                        'actuals_known': sum(item['actual'] is not None for item in known_calendar)}
            for symbol in symbols:
                decision = rebuild(session, sources, symbol, at, weights, stale, events)
                decision['calendar'] = calendar
                decision['outcomes'] = {}
                if evaluate:
                    side, actionable = ev.signal_side(decision['category'], {}, decision['score'])
                    for label in labels:
                        closes = ev.horizon_end(label, at, symbol)
                        if closes is None or closes > now:
                            continue
                        # The entry tick and the limit of the window's last tick follow the provider of the quote that
                        # gated the decision (one freshness lookup for the gate and its outcome).
                        entry = tick_as_of(session, symbol, at, stale)
                        limit = freshness_seconds(entry.provider, stale) if entry is not None else stale
                        path = ev.measure(session, symbol, side, at, closes, limit, entry=entry) if entry is not None else None
                        if path:
                            row = dict(path, decision_at=at.isoformat(), side=side, actionable=actionable,
                                       counterfactual=not actionable, horizon=label)
                            decision['outcomes'][label] = row
                            outcomes[label].append(row)
                decisions.append(decision)
    return {'start': start.isoformat(), 'end': end.isoformat(), 'step_seconds': step_seconds, 'symbols': symbols,
            'weights': weights, 'decisions': decisions, 'limitations': LIMITATIONS,
            'summary': {label: ev.summarize(rows) for label, rows in outcomes.items()}}


def compare_weights(store: Store, start: datetime, end: datetime, step_seconds: int, challenger_id: str,
                    horizon: str | None = None, symbols: list[str] | None = None) -> dict:
    """Replay the champion and one configured challenger over the same point-in-time inputs."""
    spec = next((c for c in calibration.challengers() if c['id'] == challenger_id), None)
    if spec is None:
        raise ValueError('Unknown challenger')
    horizon = horizon or ruleset()['autolearn']['report_horizon']
    champion: Any = calibration.champion_weights(store)
    runs = {name: replay(store, start, end, step_seconds, symbols, weights, [horizon])
            for name, weights in (('champion', champion['weights']), ('challenger', spec['weights']))}
    return {'horizon': horizon, 'challenger_id': challenger_id, 'limitations': LIMITATIONS,
            **{name: run['summary'][horizon] for name, run in runs.items()}}
