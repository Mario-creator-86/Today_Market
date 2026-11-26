"""One analysis cycle: deterministic components -> gates -> category -> (AI explanation + audit only for
actionable setups) -> reproducible decision record (spec §22-§25, §35, §52).

AI never computes numbers or changes a category: the chief may explain, the auditor may only add blocks."""
import hashlib
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from backend import analytics, audit, calibration, causal, engines, positioning, scoring, technicals
from backend.config import freshness_seconds, rules
from backend.database import Decision, uid, utcnow
from backend.engines import aware
from backend.schemas import iso

if TYPE_CHECKING:
    from backend.service import HubService


def _config() -> dict:
    return rules()['decision']


class CycleContext:
    """Everything shared by all assets in one cycle, computed once."""

    def __init__(self, hub: 'HubService', now: datetime):
        self.hub, self.now = hub, aware(now)
        hub.record_configuration(rules())  # every decision of this cycle references a recorded configuration
        self.assets = hub.assets(self.now, event_overlay=False)  # decisions never read the event overlay
        self.by_symbol = {a['symbol']: a for a in self.assets}
        self.events = hub.events()
        self.bars = {a['symbol']: hub.bars(a['symbol'], live_only=True) for a in self.assets}
        self.regime = hub.regime(self.assets, self.events, bars=self.bars)  # the correlation legs reuse these bars
        self.context = causal.build_context(self.regime, self.events, self.now)
        calendar = hub.calendar()
        self.releases = [r for r in calendar if r.get('actual') is not None]
        self.health = {p['name']: p['status'] for p in hub.health()}
        self.weights = calibration.champion_weights(hub.store)
        self.positioning = {c: self._positioning(c) for c in rules()['provider_routing']['deribit']['currencies']}
        self.levels = {s: self._levels(s) for s in self.by_symbol}
        self.reactions, self.baskets = self._reactions()
        self.central_banks = {str(row['bank']): row for row in hub.store.list('central_bank_state') if row.get('bank')}

    def _levels(self, symbol: str) -> dict:
        """The same levels as the asset page (technicals.asset_levels): stored daily bars keyed by NY session date
        give previous day/week and SMA20/SMA50, so a decision is tested against the levels the user sees."""
        quote = self.by_symbol[symbol].get('quote')
        bars = self.bars[symbol]  # asset_levels filters completed bars against the cycle's clock
        strikes = positioning.option_strike_levels(self.positioning_summary(symbol)) if symbol in {'BTC', 'ETH'} else None
        # Only a fresh live print of an open venue is a price of this cycle; a daily close never enters the levels.
        return technicals.asset_levels(self.hub.store, symbol, bars, self.now, price=technicals.break_price(quote),
                                       option_strikes=strikes)

    def positioning_summary(self, symbol: str) -> dict | None:
        return self.positioning.get(symbol)

    def _positioning(self, currency: str) -> dict | None:
        options, futures, as_of = positioning.stored_rows(self.hub.store, currency)
        return positioning.summarize(currency, options, futures, self.now, as_of) if options or futures else None

    def _reactions(self) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        horizon = self.now - timedelta(hours=_config()['reaction_event_age_hours'])
        by_symbol: dict[str, list[dict]] = {}
        baskets: dict[str, list[dict]] = {}
        for event in self.events:
            if event['verification_status'] != 'CONFIRMED' or not horizon <= aware(event['first_seen_utc']) <= self.now:
                continue
            found = analytics.event_reactions(self.hub.store, event['id'])
            baskets[event['id']] = found['baskets']
            for row in found['reactions']:
                by_symbol.setdefault(row['symbol'], []).append(row)
        return by_symbol, baskets

    def latest_basket(self, symbol: str) -> tuple[dict | None, dict[str, float | None]]:
        """Latest-window basket of the most recent confirmed event that is expected to move this symbol."""
        confirmations = {event_id: rows[-1].get('confirmation_pct') for event_id, rows in self.baskets.items() if rows}
        for event in self.events:  # newest first
            rows = self.baskets.get(event['id']) or []
            if rows and symbol in basket_symbols(rows[-1]):
                return rows[-1], confirmations
        return None, confirmations


def basket_symbols(basket: dict) -> set[str]:
    """Symbols a stored basket expected to move. 'expected' is their count; baskets stored before 'expected_symbols'
    existed are read from their per-status lists (every expected symbol is in exactly one of them)."""
    listed = basket.get('expected_symbols')
    if isinstance(listed, list):
        return {str(s) for s in listed}
    return {str(s) for key in ('confirming', 'partial', 'diverging', 'missing', 'no_reaction')
            for s in basket.get(key) or [] if isinstance(basket.get(key), list)}


def bank_label(currency: str) -> str:
    """Display name of a currency's central bank ('ECB', 'Fed': official_data.central_bank_meetings.banks short names,
    keyed by the decision.fx_banks id), else the currency code; never the internal bank id."""
    config = rules()
    bank = config['decision']['fx_banks'].get(currency)
    spec = config['official_data']['central_bank_meetings']['banks'].get(bank or '') or {}
    return str(spec.get('short') or currency)


def fx_policy_block(symbol: str, asset_class: str, banks: dict[str, dict]) -> str | None:
    """FX needs both central banks of the pair (never single-bank causality, spec §4E)."""
    if asset_class != 'fx' or len(symbol) != 6:
        return None
    mapping = _config()['fx_banks']
    base, other = banks.get(mapping.get(symbol[:3], '')), banks.get(mapping.get(symbol[3:], ''))
    result = analytics.relative_policy(base, other)
    if 'policy_differential' not in result:
        return f'Relative central-bank comparison unavailable ({bank_label(symbol[:3])} vs {bank_label(symbol[3:])})'
    return None


def _cross_levels(ctx: CycleContext) -> dict[str, dict]:
    return {s: ctx.levels.get(s, {}) for s in _config()['reference_assets'] if ctx.levels.get(s)}


def stale_seconds(hub: Any, quote: dict | None) -> float:
    """The freshness limit of this quote's provider (config.freshness_seconds, the lookup HubService.quote used)."""
    return freshness_seconds((quote or {}).get('provider'), hub.settings.quote_stale_seconds)


def lean_recheck(symbol: str, latest: dict, levels: dict, components: dict[str, dict], blocks: list[str],
                 weights: dict[str, float], candidate: dict) -> tuple[dict, dict, bool] | None:
    """The publication check of a lean (no AI prose is bound to the analysed price): the technical component is
    recomputed at the re-read price and the lean is published while its price confirmation still holds.
    Returns (technical component, decision, adopt): with the same technical value, or a break still on the lean's side
    (stronger or weaker) that keeps the candidate's category, the re-read values are adopted, so the stored score,
    components and quote belong to one price. When the re-read price would lift the lean into a setup band (which needs
    the AI path of the next cycle), the lean is published as analysed (adopt False). None = the confirmation was lost
    or reversed, or the score fell out of the lean band: the caller blocks."""
    old, fresh = components['technical'], scoring.technical_component(symbol, technicals.break_price(latest), levels)
    if fresh.get('value') == old.get('value') and fresh['status'] == old['status']:
        return old, candidate, True
    side = engines.category_side(candidate['category'])
    was, current = (old.get('value') or 0.0) * side, (fresh.get('value') or 0.0) * side
    if (was > 0 and current <= 0) or current < 0 <= was:  # the break was lost, or a break against the lean appeared
        return None
    rechecked = engines.decide(dict(components, technical=fresh), blocks, weights=weights)
    if rechecked['category'] == candidate['category']:
        return fresh, rechecked, True
    if engines.category_side(rechecked['category']) == side and abs(rechecked['score']) >= abs(candidate['score']):
        return old, candidate, False
    return None


def _explanation_numbers(explanation: dict) -> set[str]:
    """The numbers an explanation cites, read as audit.pre_audit reads them (its invented_numbers check)."""
    return audit.numbers(' '.join([*explanation.get('reasons', []), explanation.get('confirmation', ''),
                                   explanation.get('invalidation', '')]))


def reuse_key(symbol: str, candidate: dict, evidence_ids: list[str]) -> tuple:
    return symbol, candidate['category'], candidate['direction'], tuple(sorted(set(evidence_ids))), rules()['content_hash']


def explanation_input_hash(inputs: dict) -> str:
    """Bind prose and its auditor verdict to field-specific values and context, excluding only quote clock metadata."""
    comparable = dict(inputs)
    if isinstance(inputs.get('quote'), dict):
        comparable['quote'] = {k: v for k, v in inputs['quote'].items() if k not in {'timestamp', 'age_seconds'}}
    return hashlib.sha256(json.dumps(comparable, sort_keys=True, default=str).encode()).hexdigest()


def reusable(hub: Any, key: tuple, inputs: dict, now: datetime) -> dict | None:
    """The chief analyst's explanation (and the auditor's verdict, when it ran) of the same setup, stored less than
    decision.ai_reuse_seconds ago, while every number it cites is still in the current inputs; None otherwise. The
    analysis runs every few minutes and asked both reserve roles again on every cycle while a setup stayed actionable
    (config/rules.d/decision.json, docs/AI_AGENTS.md). Kept in memory (hub.decision_ai_reuse): a restart asks again."""
    cache = getattr(hub, 'decision_ai_reuse', None)
    seconds = float(_config().get('ai_reuse_seconds') or 0)
    entry = cache.get(key[0]) if isinstance(cache, dict) and seconds > 0 else None
    if not entry or entry['key'] != key or not 0 <= (now - entry['at']).total_seconds() < seconds:
        return None
    if entry.get('input_hash') != explanation_input_hash(inputs):
        return None
    if not _explanation_numbers(entry['explanation']) <= audit.numbers(str(inputs)):
        return None
    return entry


def remember(hub: Any, key: tuple, now: datetime, explanation: dict | None, ai_audit: dict | None,
             runs: dict[str, str | None], reused: dict | None, *, inputs: dict) -> None:
    """Keeps an explanation for reusable(); an auditor verdict is added to it (a reused entry keeps its first time, so
    it expires ai_reuse_seconds after the chief analyst's request). An unavailable answer is never kept."""
    cache = getattr(hub, 'decision_ai_reuse', None)
    if not isinstance(cache, dict) or not explanation or not float(_config().get('ai_reuse_seconds') or 0) > 0:
        return
    entry = reused or {'key': key, 'at': now, 'explanation': explanation, 'audit': None, 'runs': {},
                       'input_hash': explanation_input_hash(inputs)}
    entry['runs'] = {**entry['runs'], **{role: run for role, run in runs.items() if run}}
    if ai_audit is not None:
        entry['audit'] = ai_audit
    cache[key[0]] = entry


async def analyse(ctx: CycleContext, asset: dict) -> dict:
    hub, now, symbol, kind = ctx.hub, ctx.now, asset['symbol'], asset['asset_class']
    quote, levels = asset['quote'], ctx.levels.get(symbol, {})
    basket, confirmations = ctx.latest_basket(symbol)
    # A level break is tested with a fresh live print of an open venue only (technicals.break_price): a daily close, a
    # stale or a closed-market price leaves the technical component missing instead of scoring a full-strength break.
    scored = scoring.score_asset(symbol, now, events=ctx.events, releases=ctx.releases, reactions=ctx.reactions.get(symbol, []),
                                 basket=basket, price=technicals.break_price(quote), levels=levels,
                                 positioning=ctx.positioning_summary(symbol), regime=ctx.regime, context=ctx.context,
                                 confirmations=confirmations)
    components = scored['components']
    blocks = scoring.decision_gates(symbol, now, quote=quote, events=ctx.events, event=components['event'],
                                    provider_health=ctx.health, macro_measured=components['macro']['status'] == 'measured',
                                    context=ctx.context)
    blocks += scored['gates']
    if quote and quote.get('stub'):
        blocks.append('Development stub data cannot drive a decision')
    fx_block = fx_policy_block(symbol, kind, ctx.central_banks)
    if fx_block:
        blocks.append(fx_block)
    weights = ctx.weights['weights']
    candidate = engines.decide(components, blocks, weights=weights)
    text = scoring.confirmation_text(symbol, candidate['score'], levels, scored['paths'], _cross_levels(ctx))
    contributing = {e['event_id'] for e in components['event'].get('events', [])}
    evidence_ids = [x['id'] for e in ctx.events if e['id'] in contributing for x in e.get('evidence', [])]
    snapshot_events = [{k: e.get(k) for k in ('id', 'summary_fact', 'event_type', 'verification_status', 'fact_confidence',
                                                'first_seen_utc', 'last_updated_utc', 'trigger', 'evidence')}
                       for e in ctx.events if e['id'] in contributing]
    inputs = {'asset': symbol, 'quote': quote, 'events': snapshot_events, 'components': {k: v.get('value') for k, v in components.items()},
              'component_status': {k: v['status'] for k, v in components.items()}, 'levels': levels,
              'evidence_ids': evidence_ids, 'basket': basket,
              'macro_releases': [{k: r.get(k) for k in ('name', 'provider', 'released_at', 'z_score')}
                                 for r in components['macro'].get('releases', [])], 'regime': {k: ctx.regime.get(k) for k in ('regimes', 'posture', 'confidence')},
              'decision': {k: candidate[k] for k in ('category', 'direction', 'score')}, 'confirmation': text}
    ai_runs: dict[str, str | None] = {}
    explanation = None
    final_blocks = list(blocks)
    audit_result: dict[str, Any] = {'status': 'NOT_REQUIRED', 'checks': [], 'reasons': []}
    horizon = rules().get('autolearn', {}).get('report_horizon', _config()['report_horizon_fallback'])
    regime = {k: ctx.regime.get(k) for k in ('regimes', 'posture', 'confidence')}
    # Attached before the audit so probabilities_valid checks what will actually be published.
    probability = calibration.historical_probability(hub.store, symbol, candidate['score'], regime, horizon, now)
    reused: dict | None = None
    needs_ai_audit = candidate['category'] in _config()['ai_for_categories'] and not blocks
    ai_audit = None
    ai_input_hash = explanation_input_hash(inputs) if needs_ai_audit else None
    if needs_ai_audit:
        key = reuse_key(symbol, candidate, evidence_ids)
        reused = reusable(hub, key, inputs, now)
        if reused:
            explanation, ai_runs['chief_analyst'] = reused['explanation'], reused['runs'].get('chief_analyst')
        else:
            explanation, ai_runs['chief_analyst'] = await hub.ai.run_with_id('chief_analyst', inputs)
        if not explanation:
            final_blocks.append('Chief analyst explanation unavailable')
        latest = hub.quote(symbol)  # prices can move while the model answers
        if not latest or latest.get('freshness_status') != 'FRESH' or latest.get('market_status') != 'LIVE':
            final_blocks.append('Critical quote unavailable after explanation')
        elif scoring.technical_component(symbol, technicals.break_price(latest), levels).get('value') != components['technical'].get('value'):
            final_blocks.append('Price confirmation changed during analysis')
        elif explanation_input_hash(dict(inputs, quote=latest)) != ai_input_hash:
            final_blocks.append('Analysis inputs changed during AI generation')
        else:
            quote = inputs['quote'] = latest
        draft = dict(candidate, reasons=final_blocks, explanation=explanation, created_at=now.isoformat(), historical_probability=probability)
        deterministic = audit.pre_audit(draft, inputs, utcnow(), stale_seconds(hub, inputs['quote']), hub.settings.max_future_seconds)
        if not final_blocks and deterministic['status'] == 'PASS':
            if reused and reused.get('audit') is not None:
                ai_audit, ai_runs['auditor'] = reused['audit'], reused['runs'].get('auditor')
            else:
                ai_audit, ai_runs['auditor'] = await hub.ai.run_with_id('auditor', dict(inputs, explanation=explanation))
    # Every directional candidate shares the cycle snapshot; earlier assets may have awaited remote AI calls.
    if engines.category_side(candidate['category']):
        stage = 'after audit' if needs_ai_audit else 'before publication'
        latest = hub.quote(symbol)
        if not latest or latest.get('freshness_status') != 'FRESH' or latest.get('market_status') != 'LIVE':
            final_blocks.append(f'Critical quote unavailable {stage}')
        elif not technicals.is_live_quote(latest):
            final_blocks.append('Non-live quote cannot confirm a decision')
        elif needs_ai_audit:  # prose and audit are bound to the analysed price: any change of the technical value blocks
            if scoring.technical_component(symbol, technicals.break_price(latest), levels).get('value') != components['technical'].get('value'):
                final_blocks.append('Price confirmation changed during audit')
            elif explanation_input_hash(dict(inputs, quote=latest)) != ai_input_hash:
                final_blocks.append('Analysis inputs changed during AI generation')
            else:
                quote = inputs['quote'] = latest
        else:
            held = lean_recheck(symbol, latest, levels, components, blocks, weights, candidate)
            if held is None:
                final_blocks.append('Price confirmation changed before publication')
            elif held[2]:  # still valid at the re-read price: publish it with the values of that price
                analysed = candidate['score']
                components['technical'], candidate = held[0], held[1]
                inputs['components']['technical'] = components['technical'].get('value')
                inputs['component_status']['technical'] = components['technical']['status']
                inputs['decision'] = {k: candidate[k] for k in ('category', 'direction', 'score')}
                text = scoring.confirmation_text(symbol, candidate['score'], levels, scored['paths'], _cross_levels(ctx))
                inputs['confirmation'] = text
                quote = inputs['quote'] = latest
                if candidate['score'] != analysed:
                    probability = calibration.historical_probability(hub.store, symbol, candidate['score'], regime, horizon, now)
        checked_at = utcnow()
        final_blocks.extend(hub.publication_blocks(dict(candidate, created_at=now.isoformat(), input_snapshot=inputs,
                                                       invalidation_level=text.get('invalidation_level')), latest, checked_at))
    if needs_ai_audit:
        deterministic = audit.pre_audit(draft, inputs, checked_at, stale_seconds(hub, inputs['quote']), hub.settings.max_future_seconds)
        audit_result = audit.combine(deterministic, ai_audit)
        if audit_result['status'] != 'PASS':
            final_blocks.extend(audit_result['reasons'] or ['AI auditor unavailable or blocked publication'])
        if any(hub.event(str(e['id']))['verification_status'] != 'CONFIRMED' for e in snapshot_events):
            final_blocks.append('Evidence changed during analysis')
        if final_blocks:
            audit_result.update(status='BLOCK', reasons=list(dict.fromkeys([*audit_result['reasons'], *final_blocks])))
        else:
            remember(hub, key, now, explanation, ai_audit, ai_runs, reused, inputs=inputs)
    result = engines.decide(components, list(dict.fromkeys(final_blocks)), weights=weights) if final_blocks != blocks else candidate
    decided_at = utcnow()
    if result['score'] != candidate['score']:
        probability = calibration.historical_probability(hub.store, symbol, result['score'], regime, horizon, now)
    models = {role: (hub.store.get('ai_runs', run_id) or {}).get('model') for role, run_id in ai_runs.items() if run_id}
    config = rules()
    payload = dict(result, id=uid(), symbol=symbol, created_at=now.isoformat(), decided_at=decided_at.isoformat(),
                   price=quote['price'] if quote else None,
                   why=scoring.why_bullets(symbol, components) + ([r for r in explanation['reasons']] if explanation else []),
                   confirmation=(explanation or {}).get('confirmation') or text['confirmation'],
                   invalidation=(explanation or {}).get('invalidation') or text['invalidation'],
                   confirmation_level=text.get('confirmation_level'), invalidation_level=text.get('invalidation_level'),
                   levels=levels, explanation=explanation, drivers=scoring.driver_contributions(components, weights),
                   causal_paths=[p for p in scored['paths'] if symbol in p.get('effects', {})],
                   evidence_confidence=scoring.evidence_confidence(components['event']),
                   market_confirmation=scoring.market_confirmation(symbol, ctx.reactions.get(symbol, []), [b for b in [basket] if b]),
                   data_quality=scoring.data_quality(quote, ctx.health.get(quote['provider']) if quote else None, components,
                                                     len(ctx.bars.get(symbol, [])), now, stale_seconds(hub, quote)),
                   historical_probability=probability,
                   calibration_forecast=calibration.calibration_forecast(hub.store, symbol, result['score'], horizon, decided_at),
                   regime=regime, audit=audit_result, ai_reused=reused is not None, audit_status=audit_result['status'] if engines.is_setup(result['category']) else
                   ('BLOCK' if final_blocks else 'NOT_REQUIRED'), ai_run_ids=ai_runs,
                   versions={'rules': config['version'], 'content_hash': config['content_hash'], 'causal': config.get('causal_version'),
                             'sources': config.get('sources_version'), 'weights': ctx.weights.get('source'),
                             'weights_candidate': ctx.weights.get('candidate_id'), 'models': models},
                   basis_counts={'events': len(snapshot_events), 'macro_releases': len(components['macro'].get('releases', [])),
                                 'reactions': len(ctx.reactions.get(symbol, [])), 'bars': len(ctx.bars.get(symbol, []))},
                   input_snapshot=inputs)
    payload['input_hash'] = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()
    with hub.store.session() as session:
        session.add(Decision(id=payload['id'], symbol=symbol, direction=payload['direction'], score=payload['score'],
                             created_at=now, payload=payload))
        session.commit()
    return payload


def record_id(payload: dict) -> str:
    """Human-readable reproducible id for §52 audit views, e.g. D-20260926-1a2b3c."""
    stamp = aware(payload['created_at']).strftime('%Y%m%d')
    return f"D-{stamp}-{payload['id'][:6]}"


def audit_record(payload: dict) -> dict:
    """The §52 reproducibility summary of one decision."""
    return {'decision_id': record_id(payload), 'id': payload['id'], 'symbol': payload['symbol'],
            'category': payload.get('category', payload['direction']), 'score': payload['score'],
            'generated': iso(aware(payload['created_at'])), 'based_on': payload.get('basis_counts', {}),
            'versions': payload.get('versions', {}), 'ai_models': payload.get('versions', {}).get('models', {}),
            'auditor': payload.get('audit', {}).get('status', payload.get('audit_status')), 'input_hash': payload.get('input_hash')}
