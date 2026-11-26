"""Deterministic score components (spec §22, §25, §27, §28).

Every component is {value, status, basis}. Missing inputs yield status='missing' with value None; they are
listed by name and never shown as a measured zero. No function here calls an LLM or a provider.
"""
import math
from collections.abc import Iterable
from datetime import datetime

from backend import causal, technicals
from backend.config import freshness_seconds
from backend.engines import aware
from backend.notifications import status_text
from backend.providers.base import provider_label

COMPONENTS = ('event', 'macro', 'reaction', 'technical', 'positioning', 'cross_asset', 'regime')


def measured(value: float, basis: str, **detail) -> dict:
    return {'value': round(max(-100.0, min(100.0, value)), 2), 'status': 'measured', 'basis': basis, **detail}


def missing(basis: str, **detail) -> dict:
    return {'value': None, 'status': 'missing', 'basis': basis, **detail}


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _floor(floor: float, share: float) -> float:
    return floor + (1 - floor) * max(0.0, min(1.0, share))


def fmt(value: float) -> str:
    """Price formatting by magnitude, so FX keeps 4 decimals and indices keep 2."""
    return f'{value:,.2f}' if abs(value) >= 1000 else f'{value:.2f}' if abs(value) >= 10 else f'{value:.4f}'


# --- event -------------------------------------------------------------------------------

def _scorable(event: dict, now: datetime) -> bool:
    """Confirmed, causally mapped and already known at `now` (no lookahead in replays/backtests)."""
    return (event.get('verification_status') == 'CONFIRMED' and causal.event_trigger(event) is not None
            and aware(event['first_seen_utc']) <= aware(now))


def _source_quality(event: dict) -> float | None:
    if _number(event.get('source_quality')) is not None:
        return float(event['source_quality'])
    weights = [e['weight'] for e in event.get('evidence', [])
               if e.get('relation', 'supports') == 'supports' and not e.get('discovery_only') and _number(e.get('weight')) is not None]
    return max(weights) if weights else None


def event_factors(event: dict, symbol_paths: list[dict], confirmation: float | None) -> dict[str, dict]:
    """Multipliers applied to the causal net effect. Unmeasured inputs stay neutral (1.0) and are flagged."""
    config = causal.settings()['scoring']['event']
    factors: dict[str, dict] = {'fact_confidence': {'factor': max(0.0, min(1.0, event.get('fact_confidence', 0) / 100)),
                                                    'measured': True}}
    quality = _source_quality(event)
    factors['source_quality'] = {'factor': _floor(config['source_quality_floor'], quality) if quality is not None else 1.0,
                                 'measured': quality is not None, 'input': quality}
    novelty = _number(event.get('novelty'))
    factors['novelty'] = {'factor': _floor(config['novelty_floor'], novelty) if novelty is not None else 1.0,
                          'measured': novelty is not None, 'input': novelty}
    z = _number(event.get('surprise_z_score', event.get('z_score')))
    band = config['surprise']
    surprise = max(band['min'], min(band['max'], 1 + band['per_z'] * (abs(z) - band['z_neutral']))) if z is not None else 1.0
    factors['surprise'] = {'factor': surprise, 'measured': z is not None, 'input': z}
    priced = _number(event.get('priced_in'))
    factors['priced_in'] = {'factor': 1 - config['priced_in_weight'] * max(0.0, min(1.0, priced)) if priced is not None else 1.0,
                            'measured': priced is not None, 'input': priced}
    persistence = max((p['persistence'] for p in symbol_paths), default=None)
    factors['persistence'] = {'factor': _floor(config['persistence_floor'], persistence) if persistence is not None else 1.0,
                              'measured': persistence is not None, 'input': persistence}
    factors['market_confirmation'] = {
        'factor': _floor(config['market_confirmation_floor'], confirmation / 100) if confirmation is not None else 1.0,
        'measured': confirmation is not None, 'input': confirmation}
    return factors


def event_component(symbol: str, events: list[dict], now: datetime, context: dict | None = None,
                    confirmations: dict[str, float | None] | None = None) -> dict:
    """Confirmed events with a causal path to the symbol: direction/impact/freshness from the graph, then factors."""
    entries, total = [], 0.0
    for event in events:
        if not _scorable(event, now):
            continue
        items = [p for p in causal.event_paths(event, now, context) if p['active']]
        reaching = [p for p in items if p['contributions'].get(symbol)]
        if not reaching:
            continue
        confirmation = (confirmations or {}).get(event['id'], _number(event.get('market_confirmation')))
        factors = event_factors(event, reaching, confirmation)
        product = math.prod(f['factor'] for f in factors.values())
        contribution = causal.net_effect(reaching, symbol) * product
        total += contribution
        entries.append({'event_id': event['id'], 'summary': event.get('summary_fact', ''), 'trigger': causal.event_trigger(event),
                        'fact_confidence': event.get('fact_confidence', 0), 'contribution': round(contribution, 4),
                        'factors': factors, 'unmeasured_factors': [k for k, f in factors.items() if not f['measured']],
                        'paths': [{'rule_id': p['rule_id'], 'contribution': round(p['contributions'][symbol] * product, 4),
                                   'drivers': p['drivers'][symbol], 'lineage': p.get('lineage', {}).get(symbol, [])}
                                  for p in reaching]})
    if not entries:
        return missing(f'No verified catalyst with an active causal path to {symbol}', events=[])
    return measured(total, f'{len(entries)} verified catalyst(s) through the causal graph', events=entries)


# --- macro -------------------------------------------------------------------------------

def macro_component(symbol: str, releases: list[dict], now: datetime, context: dict | None = None) -> dict:
    """Released surprises (z-scores) mapped through causal triggers; no z-score means not measured."""
    config = causal.settings()['scoring']['macro']
    entries, total = [], 0.0
    for release in releases:
        mapped = causal.macro_trigger(release)
        stamp = release.get('released_at') or release.get('scheduled_utc') or release.get('scheduled_at')
        if not mapped or not stamp or _number(release.get('actual')) is None:
            continue
        hours = (aware(now) - aware(stamp)).total_seconds() / 3600
        if not 0 <= hours <= config['max_age_hours']:
            continue
        items = [p for p in causal.paths(mapped['trigger'], hours, context) if p['contributions'].get(symbol)]
        if not items:
            continue
        scale = min(1.0, abs(mapped['z_score']) / config['z_full_scale'])
        contribution = causal.net_effect(items, symbol) * scale
        total += contribution
        entries.append({'name': release.get('name'), 'z_score': mapped['z_score'], 'trigger': mapped['trigger'],
                        'provider': release.get('provider'), 'released_at': aware(stamp).isoformat(),
                        'contribution': round(contribution, 4),
                        'paths': [{'rule_id': p['rule_id'], 'contribution': round(p['contributions'][symbol] * scale, 4),
                                   'drivers': p['drivers'][symbol], 'lineage': p.get('lineage', {}).get(symbol, [])} for p in items]})
    if not entries:
        return missing(f'No released macro surprise with a z-score maps to {symbol}', releases=[])
    return measured(total, '; '.join(f"{e['name']} z={e['z_score']:+.1f} -> {e['trigger']}" for e in entries), releases=entries)


# --- reaction / cross-asset ---------------------------------------------------------------

def _horizons() -> list[str]:
    return list(causal.settings()['scoring']['market_confirmation']['horizon_weights'])


def _reaction_event(symbol: str, reactions: list[dict], event_id: str | None = None) -> list[dict]:
    """Eligible horizons of one catalyst, shared by score and confirmation percentage."""
    config = causal.settings()['scoring']['reaction']
    order = _horizons()
    own = [r for r in reactions if r.get('symbol') == symbol and r.get('expected_direction') in {-1, 1}
           and r.get('status') in config['status_scores'] and r.get('horizon') in order]
    if event_id is not None:
        own = [r for r in own if r.get('event_id') == event_id]
    elif len({r.get('event_id') for r in own}) > 1:
        dated = [r for r in own if r.get('baseline_timestamp')]
        newest = max(dated, key=lambda r: aware(r['baseline_timestamp'])) if dated else None
        own = [r for r in own if newest and r.get('event_id') == newest.get('event_id')]
    return own


def reaction_component(symbol: str, reactions: list[dict], basket: dict | None = None) -> dict:
    """Latest horizon of the newest event, blended only with that event's basket. No confirmation is not opposition."""
    config = causal.settings()['scoring']['reaction']
    order = _horizons()
    basket = basket or {}
    own = _reaction_event(symbol, reactions, basket.get('event_id'))
    latest = max(own, key=lambda r: order.index(r['horizon'])) if own else None
    expected = latest['expected_direction'] if latest else 0
    parts: list[tuple[float, float]] = []
    basis: list[str] = []
    if latest:
        parts.append((config['own_weight'], expected * config['status_scores'][latest['status']]))
        change = _number(latest.get('change'))
        basis.append(f"{symbol} {latest['status']} at {latest['horizon']}"
                     + (f" ({change:+.2f} {latest.get('unit', '')})" if change is not None else ''))
    pct = _number(basket.get('confirmation_pct'))
    if pct is not None and expected:
        # The complement includes flat assets, not just assets moving against the catalyst.
        parts.append((1 - config['own_weight'], expected * max(0.0, min(100.0, pct))))
        basis.append(f"basket {pct:.0f}% confirming at {basket.get('horizon', 'latest window')}" + (' with divergence' if basket.get('divergence') else ''))
    if not parts:
        return missing(f'No measured post-event reaction for {symbol}')
    weight = sum(w for w, _ in parts)
    return measured(sum(w * v for w, v in parts) / weight, '; '.join(basis))


def observed_moves(features: dict[str, dict[str, float]]) -> dict[str, dict]:
    """Regime features (price, change_pct) to per-asset change in the asset's unit (bp for yields)."""
    from backend.analytics import unit_for
    per_unit = causal.settings()['market_units']['bp_per_unit']
    result = {}
    for symbol, values in features.items():
        change, price = _number(values.get('change_pct')), _number(values.get('price'))
        if change is None or ':' in symbol:
            continue
        unit = unit_for(symbol)
        if unit == 'bp':
            if price is None:
                continue
            # Level change implied by the percent change of the current level: price - price / (1 + c/100).
            change = price * change / (100 + change) * per_unit
        result[symbol] = {'change': change, 'unit': unit}
    return result


def cross_asset_component(symbol: str, path_items: list[dict], moves: dict[str, dict]) -> dict:
    """Share of causally linked assets moving as the paths expect, signed by the symbol's own expectation."""
    from backend.analytics import tolerance
    config = causal.settings()['scoring']['cross_asset']
    own = _sign(causal.net_effect(path_items, symbol))
    if not own:
        return missing(f'No causal expectation for {symbol}')
    linked = {a for p in path_items if p['active'] for a in p['contributions']} - {symbol}
    aligned: list[str] = []
    opposed: list[str] = []
    flat: list[str] = []
    for asset in sorted(linked):
        expected = _sign(causal.net_effect(path_items, asset))
        move = moves.get(asset)
        if not expected or not move:
            continue
        change = move['change'] * expected
        noise = tolerance(asset)['noise']
        (aligned if change > noise else opposed if change < -noise else flat).append(asset)
    observed = len(aligned) + len(opposed) + len(flat)
    if observed < config['min_observed']:
        return missing(f'Fewer than {config["min_observed"]} linked assets observed', aligned=aligned, opposed=opposed)
    value = own * (len(aligned) - len(opposed)) / observed * 100
    return measured(value, f'{len(aligned)}/{observed} linked assets move as the causal paths expect',
                    aligned=aligned, opposed=opposed, flat=flat)


# --- technical / positioning / regime -------------------------------------------------------

def technical_component(symbol: str, price: float | None, levels: dict | None) -> dict:
    """Level breaks only: above resistance is positive, below support negative, extension scaled by ATR.
    `price` is the fresh live print of an open venue (technicals.break_price): a daily close, a stale or a closed-market
    price is None and the component is missing. Levels withheld after a data gap (technicals.levels_state) are missing
    too, with the reason the levels carry."""
    config = causal.settings()['scoring']['technical']
    support, resistance = _number((levels or {}).get('support')), _number((levels or {}).get('resistance'))
    price = _number(price)
    if price is None:
        return missing(f'No fresh live price of an open market to test against the levels of {symbol}')
    if support is None or resistance is None:
        state = (levels or {}).get('state')
        reason = state.get('reason') if isinstance(state, dict) and not state.get('ready') else None
        return missing(f'{reason} ({symbol})' if reason else f'No measured support/resistance for {symbol}')
    atr = _number((levels or {}).get('atr'))
    if price > resistance:
        extension = (price - resistance) / atr * config['per_atr'] if atr else 0.0
        return measured(config['break_score'] + extension, f'Price {fmt(price)} above resistance {fmt(resistance)}')
    if price < support:
        extension = (support - price) / atr * config['per_atr'] if atr else 0.0
        return measured(-config['break_score'] - extension, f'Price {fmt(price)} below support {fmt(support)}')
    return measured(0.0, f'Price {fmt(price)} inside {fmt(support)}-{fmt(resistance)}; no level break')


def positioning_component(summary: dict | None) -> dict:
    """Crowding/extreme rules owned by backend.positioning; calls vs puts never set direction."""
    from backend.positioning import positioning_component as crowding
    result = dict(crowding(summary))
    if result['status'] == 'measured' and _number(result['value']) is None:
        return missing('Positioning value not numeric')
    return result


def regime_exposure(symbol: str, regime_name: str) -> int:
    nodes = causal.graph().nodes
    total = sum(direction * nodes[variable].assets.get(symbol, 0)
                for variable, direction in causal.settings()['scoring']['regime']['variables'].get(regime_name, []))
    return _sign(total)


def regime_component(symbol: str, regime: dict | None, expected: int = 0) -> dict:
    """Active regimes' exposure for the asset, weighted by rule coverage; basis states alignment with the causal expectation."""
    if not regime or regime.get('posture') in {None, 'INSUFFICIENT_DATA'}:
        return missing('Regime not classified from fresh cross-asset data')
    coverage = {r['regime']: r['confidence'] for r in regime.get('evaluated', []) if r.get('state') == 'ACTIVE'}
    active = [r for r in regime.get('regimes', []) if r != 'MIXED']
    if not active:
        return measured(0.0, 'No directional regime active (MIXED)')
    scores = [regime_exposure(symbol, r) * coverage.get(r, 100.0) for r in active]
    value = sum(scores) / len(scores)
    relation = '' if not expected or not value else ' - aligned with causal expectation' if _sign(value) == expected else ' - opposes causal expectation'
    return measured(value, f"{', '.join(active)} exposure for {symbol}{relation}")


# --- aggregation ------------------------------------------------------------------------------

def missing_components(components: dict[str, dict]) -> list[str]:
    weights = causal.settings()['weights']
    return [k for k in weights if components.get(k, {}).get('status') != 'measured']


def score_asset(symbol: str, now: datetime, *, events: Iterable[dict] = (), releases: Iterable[dict] = (),
                reactions: Iterable[dict] = (), basket: dict | None = None, price: float | None = None,
                levels: dict | None = None, positioning: dict | None = None, regime: dict | None = None,
                context: dict | None = None, confirmations: dict[str, float | None] | None = None) -> dict:
    """All seven components plus the causal paths and gates they imply."""
    events, releases = list(events), list(releases)
    context = context if context is not None else causal.build_context(regime, events, now)
    features = (regime or {}).get('features', {})
    path_items = [p for e in events if _scorable(e, now) for p in causal.event_paths(e, now, context)]
    path_items += causal.market_paths(features, context)
    event = event_component(symbol, events, now, context, confirmations)
    components = {'event': event, 'macro': macro_component(symbol, releases, now, context),
                  'reaction': reaction_component(symbol, list(reactions), basket),
                  'technical': technical_component(symbol, price, levels), 'positioning': positioning_component(positioning),
                  'cross_asset': cross_asset_component(symbol, path_items, observed_moves(features)),
                  'regime': regime_component(symbol, regime, _sign(causal.net_effect(path_items, symbol)))}
    gate = causal.conflict_gate(path_items, symbol)
    return {'components': components, 'components_missing': missing_components(components), 'paths': path_items,
            'conflict': causal.conflict(path_items, symbol), 'gates': [gate] if gate else [], 'context': context}


# --- gates (§24) ----------------------------------------------------------------------------------

def _reaches(event: dict, symbol: str, now: datetime, context: dict | None = None) -> bool:
    """The event has an ACTIVE causal path that contributes to this asset now (the same test as the event component:
    a decayed or suppressed path, or one that only names the asset with weight 0, does not reach it)."""
    if not causal.event_trigger(event):
        return False
    return any(p['active'] and p['contributions'].get(symbol) for p in causal.event_paths(event, now, context))


def important_unverified(event: dict, config: dict) -> bool:
    """Whether an unverified event is important enough to block (config scoring.gates, note_unverified): an assessed
    importance of at least min_importance; without an assessment only an authoritative source (tier weight of at
    least unassessed_min_tier_weight). A missing rating alone never blocks."""
    importance = _number(event.get('importance'))
    if importance is not None:
        return importance >= config['min_importance']
    tier = _number(event.get('tier_weight'))
    return tier is not None and tier >= config['unassessed_min_tier_weight']


def decision_gates(symbol: str, now: datetime, *, quote: dict | None, events: list[dict], event: dict,
                   provider_health: dict[str, str], macro_measured: bool = False, context: dict | None = None) -> list[str]:
    """Block reasons scoped to the facts and feeds that actually drive this asset's score (no AI can bypass them).
    `context` is the cycle's causal context (regimes, confirmed triggers), so reach is judged as the score judges it."""
    config = causal.settings()['scoring']['gates']
    blocks = []
    if not quote or quote.get('freshness_status') != 'FRESH':
        blocks.append('Critical data missing or stale')
    elif quote.get('market_status') not in config['live_states']:
        blocks.append('Market closed or price unavailable')
    if quote and not technicals.is_live_quote(quote):
        blocks.append('Non-live quote cannot confirm a decision')
    contributing = {e['event_id'] for e in event.get('events', [])} if event.get('status') == 'measured' else set()
    scored = [e for e in events if e['id'] in contributing]
    feeds = {quote['provider']} if quote and quote.get('provider') else set()
    feeds |= {x['source_id'] for e in scored for x in e.get('evidence', []) if x.get('source_id')}
    feeds |= {'calendar'} if macro_measured else set()
    offline = sorted(f for f in feeds if provider_health.get(f) in config['blocking_provider_statuses'])
    disputed = [e for e in scored if e.get('ai_disputed_evidence')]
    for item in disputed:  # an AI fact-check can raise a dispute (never confirm); a disputed catalyst cannot drive a setup
        blocks.append(f"Fact-checker disputes evidence: {item.get('summary_fact', item['id'])}")
    if offline:  # named by label and plain status: 'Finnhub (US equities), rate limited', never a provider id
        blocks.append('Major provider unavailable: '
                      + '; '.join(f'{provider_label(f)}, {status_text(provider_health[f])}' for f in offline))
    max_age = config['unverified_max_age_hours'] * 3600
    for item in events:
        age = (aware(now) - aware(item['first_seen_utc'])).total_seconds()
        if not 0 <= age <= max_age or not _reaches(item, symbol, now, context):
            continue
        if item.get('verification_status') in {'CONTRADICTED', 'FALSE'}:
            blocks.append(f"High-quality sources contradict: {item.get('summary_fact', item['id'])}")
        elif item.get('verification_status') in {'UNCONFIRMED', 'PARTIALLY_CONFIRMED'} and important_unverified(item, config):
            blocks.append(f"Important fact unverified: {item.get('summary_fact', item['id'])}")
    kinds = {s['id']: s.get('kind') for s in causal.settings().get('sources', [])}
    if scored and all(all((x.get('kind') or kinds.get(x.get('source_id', ''))) in config['rumor_kinds'] for x in e.get('evidence', [])
                          if x.get('relation', 'supports') == 'supports') for e in scored):
        blocks.append('Score based mainly on social rumors')
    return list(dict.fromkeys(blocks))


# --- graded percentages (§25) --------------------------------------------------------------------

def evidence_confidence(event: dict) -> float | None:
    """Fact confidence weighted by each verified catalyst's absolute contribution; None when no catalyst scored."""
    entries = event.get('events', []) if event.get('status') == 'measured' else []
    weight = sum(abs(e['contribution']) for e in entries)
    if not weight:
        return None
    return round(sum(abs(e['contribution']) * e['fact_confidence'] for e in entries) / weight, 1)


def market_confirmation(symbol: str, reactions: list[dict], baskets: list[dict]) -> float | None:
    """Horizon-weighted confirmation for the score's selected catalyst, never a mix of opposing events."""
    config = causal.settings()['scoring']['market_confirmation']
    basket_ids = {b['event_id'] for b in baskets if b.get('event_id') is not None}
    selected = next(iter(basket_ids)) if len(basket_ids) == 1 else None
    reactions = _reaction_event(symbol, reactions, selected)
    selected = selected if selected is not None else reactions[0].get('event_id') if reactions else None
    if selected is not None:
        baskets = [b for b in baskets if b.get('event_id') == selected]
    elif len(basket_ids) > 1:
        baskets = []  # no unambiguous event to associate with these percentages
    weights, credit = config['horizon_weights'], config['credit']
    total, weight = 0.0, 0.0
    for horizon, w in weights.items():
        own = [credit.get(r['status'], 0.0) * 100 for r in reactions
               if r.get('symbol') == symbol and r.get('horizon') == horizon and r.get('expected_direction') in {-1, 1}]
        pct = [p for b in baskets if b.get('horizon') == horizon and (p := _number(b.get('confirmation_pct'))) is not None]
        parts = [(config['own_weight'], sum(own) / len(own))] if own else []
        parts += [(1 - config['own_weight'], sum(pct) / len(pct))] if pct else []
        if parts:
            local = sum(a * b for a, b in parts) / sum(a for a, _ in parts)
            total, weight = total + w * local, weight + w
    return round(total / weight, 1) if weight else None


def data_quality_parts(quote: dict | None, provider_status: str | None, components: dict[str, dict],
                       history_observations: int, now: datetime, stale_seconds: float) -> dict[str, float]:
    config = causal.settings()['scoring']['data_quality']
    freshness = 0.0
    if quote and quote.get('freshness_status') == 'FRESH' and quote.get('timestamp'):
        age = max(0.0, (aware(now) - aware(quote['timestamp'])).total_seconds())
        # The limit this quote was judged with (its provider's), not one limit for every feed.
        limit = _number(quote.get('freshness_threshold_seconds')) or freshness_seconds(quote.get('provider'), stale_seconds)
        freshness = max(0.0, 100 * (1 - age / limit))
    weights = causal.settings()['weights']
    coverage = 100 * sum(w for k, w in weights.items() if components.get(k, {}).get('status') == 'measured') / sum(weights.values())
    history = 100 * min(1.0, history_observations / causal.settings()['thresholds']['min_history'])
    return {'freshness': round(freshness, 1), 'provider': float(config['provider_scores'].get(provider_status or '', 0)),
            'coverage': round(coverage, 1), 'history': round(history, 1)}


def data_quality(quote: dict | None, provider_status: str | None, components: dict[str, dict],
                 history_observations: int, now: datetime, stale_seconds: float) -> float:
    weights = causal.settings()['scoring']['data_quality']['weights']
    parts = data_quality_parts(quote, provider_status, components, history_observations, now, stale_seconds)
    return round(sum(parts[k] * w for k, w in weights.items()) / sum(weights.values()), 1)


# --- text (§27) ----------------------------------------------------------------------------------

def _labels(lineage: list[str]) -> list[str]:
    nodes = causal.graph().nodes
    return [nodes[v].label for v in lineage if v in nodes]


def cross_conditions(symbol: str, side: int, path_items: list[dict], cross_levels: dict[str, dict] | None = None) -> tuple[list[str], list[str]]:
    """Confirming and invalidating cross-asset conditions from the causal paths, citing levels when measured."""
    config = causal.settings()['scoring']['cross_asset']
    own = _sign(causal.net_effect(path_items, symbol))
    if not own:
        return [], []
    orientation = side * own
    ranked = sorted(((abs(net), asset, _sign(net)) for asset in config['reference_assets'] if asset != symbol
                     and (net := causal.net_effect(path_items, asset))), reverse=True)[:config['text_max_assets']]
    confirm, invalidate = [], []
    for _, asset, expected in ranked:
        direction = expected * orientation
        level = (cross_levels or {}).get(asset, {})
        up, down = _number(level.get('resistance')), _number(level.get('support'))
        confirm.append(f'{asset} above {fmt(up)}' if direction > 0 and up is not None else f'{asset} below {fmt(down)}'
                       if direction < 0 and down is not None else f"{asset} {'up' if direction > 0 else 'down'}")
        invalidate.append(f'{asset} loses {fmt(down)}' if direction > 0 and down is not None else f'{asset} reclaims {fmt(up)}'
                          if direction < 0 and up is not None else f"{asset} {'down' if direction > 0 else 'up'}")
    return confirm, invalidate


def confirmation_text(symbol: str, score: float, levels: dict | None, path_items: list[dict],
                      cross_levels: dict[str, dict] | None = None) -> dict:
    """Concrete confirmation/invalidation: the asset's own level plus cross-asset conditions from the causal paths."""
    side = _sign(score)
    support, resistance = _number((levels or {}).get('support')), _number((levels or {}).get('resistance'))
    if not side:
        return {'confirmation': f'No directional bias for {symbol}; wait for a score beyond the lean threshold',
                'invalidation': 'Not applicable while neutral', 'confirmation_level': None, 'invalidation_level': None}
    confirm, invalidate = cross_conditions(symbol, side, path_items, cross_levels)
    trigger_level, stop_level = (resistance, support) if side > 0 else (support, resistance)
    if trigger_level is None or stop_level is None:
        own_confirm = f'Await measured support/resistance for {symbol}'
        own_invalidate = f'Await measured support/resistance for {symbol}'
    elif side > 0:
        own_confirm, own_invalidate = f'{symbol} holds above {fmt(trigger_level)} (resistance)', f'{symbol} breaks below {fmt(stop_level)} (support)'
    else:
        own_confirm, own_invalidate = f'{symbol} holds below {fmt(trigger_level)} (support)', f'{symbol} reclaims {fmt(stop_level)} (resistance)'
    invalidation = f"{own_invalidate} or {' + '.join(invalidate)}" if invalidate else own_invalidate
    return {'confirmation': ' + '.join([own_confirm, *confirm]), 'invalidation': invalidation,
            'confirmation_level': trigger_level, 'invalidation_level': stop_level}


def why_bullets(symbol: str, components: dict[str, dict]) -> list[str]:
    """Explain directional evidence, neutral measurements and unavailable inputs distinctly."""
    bullets = []
    event = components.get('event', {})
    if event.get('status') == 'measured':
        entries = sorted(event['events'], key=lambda e: -abs(e['contribution']))[:causal.settings()['scoring']['text']['why_max_events']]
        for entry in entries:
            best = max(entry['paths'], key=lambda p: abs(p['contribution']))
            chain = ' -> '.join(_labels(best['lineage']))
            move = 'higher' if entry['contribution'] > 0 else 'lower'
            # The confidence belongs to the fact; the direction is the causal rule's reading of it, not a confirmed effect.
            bullets.append(f"{entry['summary']} (fact confirmed, {entry['fact_confidence']:.0f}%). "
                           f"Rule reading: {chain} -> {symbol} {move}")
    for name, label in (('macro', 'Macro'), ('reaction', 'Reaction'), ('technical', 'Technical'),
                        ('cross_asset', 'Cross-asset'), ('positioning', 'Positioning'), ('regime', 'Regime')):
        item = components.get(name, {})
        if item.get('status') == 'measured' and item.get('value'):
            bullets.append(f"{label}: {item['basis']} ({item['value']:+.0f})")
    for name, item in components.items():
        if item.get('basis') and (item.get('status') != 'measured' or item.get('value') == 0):
            state = 'neutral' if item.get('status') == 'measured' else 'unavailable'
            bullets.append(f"{name.replace('_', ' ').capitalize()} ({state}): {item['basis']}")
    return bullets


def driver_contributions(components: dict[str, dict], weights: dict[str, float] | None = None) -> dict:
    """Signed weighted contributions by driver (Rates, USD, Geopolitics/Energy, Technical, Positioning, Other) and Net."""
    graph = causal.graph()
    weights = weights or causal.settings()['weights']
    rows = dict.fromkeys([*graph.drivers, graph.other_driver], 0.0)
    for name in ('event', 'macro'):
        item = components.get(name, {})
        if item.get('status') != 'measured':
            continue
        paths = [p for entry in item.get('events', item.get('releases', [])) for p in entry['paths']]
        raw = sum(p['contribution'] for p in paths)
        scale = item['value'] * weights[name] / raw if raw else 0.0
        for path in paths:
            share = path['contribution'] * scale / len(path['drivers'])
            for driver in path['drivers']:
                key = driver if driver in rows else graph.other_driver
                rows[key] += share
    fixed = {'technical': 'Technical', 'positioning': 'Positioning'}
    for name in ('reaction', 'technical', 'positioning', 'cross_asset', 'regime'):
        item = components.get(name, {})
        if item.get('status') == 'measured':
            key = fixed.get(name, graph.other_driver)
            rows[key if key in rows else graph.other_driver] += item['value'] * weights[name]
    result = [{'driver': k, 'value': round(v, 2)} for k, v in rows.items()]
    return {'rows': result, 'net': round(max(-100.0, min(100.0, sum(rows.values()))), 2),
            'unmeasured': [k for k in weights if components.get(k, {}).get('status') != 'measured']}
