"""Deterministic, versioned causal knowledge graph (spec §17).

Rules are multi-step chains over intermediate variables; each variable node maps to asset exposures. Competing
rules for one trigger are all returned as separate paths. Regime dependencies and machine-evaluable exceptions
condition each path at compute time. Triggers are derived from source, keyword, macro-surprise and market-move
rules, never from an LLM (the AI may only propose; see backend.ai.accept_extraction).
"""
import math
import operator
import re
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.config import rules

OPERATORS = {'>': operator.gt, '<': operator.lt, '>=': operator.ge, '<=': operator.le}
Sign = Literal[-1, 1]
Condition = Literal['regime_active', 'event_confirmed', 'posture']
# Engineering cadence for noticing edited config files, not a market threshold.
CONFIG_RECHECK_SECONDS = 1.0
_snapshot: tuple | None = None
_checked_at = 0.0


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True, frozen=True)


class Node(Strict):
    label: str
    driver: str | None = None
    assets: dict[str, Sign] = Field(default_factory=dict)


class Step(Strict):
    variable: str
    direction: Sign
    transmission: float = Field(gt=0, le=1)
    parent: str | None = Field(None, alias='from')


class RuleException(Strict):
    id: str
    when: dict[Condition, str] = Field(min_length=1)
    action: Literal['suppress', 'invert', 'scale']
    factor: float = 1.0
    variables: list[str] = Field(default_factory=list)


class Rule(Strict):
    id: str
    label: str | None = None  # plain-English name shown in the UI instead of the id
    trigger: str
    variable: str
    direction: Sign
    strength: float = Field(gt=0, le=100)
    half_life_hours: float = Field(gt=0)
    persistence: float = Field(ge=0, le=1)
    version: str
    steps: list[Step] = Field(min_length=1)
    regime_dependencies: dict[str, float] = Field(default_factory=dict)
    exceptions: list[RuleException] = Field(default_factory=list)
    effects: dict[str, Sign]

    @model_validator(mode='after')
    def _chain(self) -> 'Rule':
        seen: list[str] = []
        for step in self.steps:
            if step.variable in seen:
                raise ValueError(f'{self.id}: variable {step.variable} repeated')
            if step.parent is not None and step.parent not in seen:
                raise ValueError(f'{self.id}: step {step.variable} links to unknown earlier variable {step.parent}')
            seen.append(step.variable)
        if self.steps[0].variable != self.variable or self.steps[0].direction != self.direction:
            raise ValueError(f'{self.id}: variable/direction must describe the first step')
        if any(v < 0 for v in self.regime_dependencies.values()):
            raise ValueError(f'{self.id}: regime multipliers must be >= 0')
        return self


class Graph(Strict):
    version: str
    max_depth: int = Field(ge=1)
    max_regime_multiplier: float = Field(gt=0)
    conflict_net_ratio: float = Field(ge=0, le=1)
    min_active_strength: float = Field(ge=0)
    drivers: list[str]
    other_driver: str
    nodes: dict[str, Node]
    rules: list[Rule]


class Exposure(BaseModel):
    """Compiled asset exposure of one rule: sign, cumulative transmission and the variable lineage."""
    model_config = ConfigDict(frozen=True)
    sign: int
    weight: float
    lineage: tuple[str, ...]


def _compile(rule: Rule, nodes: dict[str, Node], max_depth: int) -> dict[str, Exposure]:
    cumulative: dict[str, float] = {}
    lineage: dict[str, tuple[str, ...]] = {}
    exposures: dict[str, Exposure] = {}
    previous: str | None = None
    for step in rule.steps:
        if step.variable not in nodes:
            raise ValueError(f'{rule.id}: unknown variable {step.variable}')
        parent = step.parent or previous
        cumulative[step.variable] = (cumulative[parent] if parent else 1.0) * step.transmission
        lineage[step.variable] = (lineage[parent] if parent else ()) + (step.variable,)
        if len(lineage[step.variable]) > max_depth:
            raise ValueError(f'{rule.id}: chain deeper than max_depth {max_depth}')
        previous = step.variable
        for asset, exposure in nodes[step.variable].assets.items():
            candidate = Exposure(sign=step.direction * exposure, weight=cumulative[step.variable], lineage=lineage[step.variable])
            current = exposures.get(asset)
            if current and current.sign != candidate.sign:
                raise ValueError(f'{rule.id}: {asset} has opposite signs inside one rule; split it into competing rules')
            if not current or candidate.weight > current.weight:
                exposures[asset] = candidate
    return exposures


def build(config: dict) -> tuple[Graph, dict[str, dict[str, Exposure]]]:
    """Validates and compiles the graph of one merged configuration; a malformed graph raises."""
    graph = Graph(version=config['causal_version'], rules=config['causal_rules'], **config['causal_graph'])
    known_assets = {a[0] for a in config['assets']}
    known_regimes = {r['regime'] for r in config.get('regime', {}).get('rules', [])} | {'MIXED'}
    compiled = {}
    for rule in graph.rules:
        exposures = _compile(rule, graph.nodes, graph.max_depth)
        unknown = set(exposures) - known_assets
        if unknown:
            raise ValueError(f'{rule.id}: unknown assets {sorted(unknown)}')
        if rule.effects != {a: e.sign for a, e in exposures.items()}:
            raise ValueError(f'{rule.id}: declared effects differ from the compiled chain')
        if config.get('regime') and set(rule.regime_dependencies) - known_regimes:
            raise ValueError(f'{rule.id}: unknown regimes {sorted(set(rule.regime_dependencies) - known_regimes)}')
        for exception in rule.exceptions:
            if set(exception.variables) - {s.variable for s in rule.steps}:
                raise ValueError(f'{rule.id}: exception {exception.id} scopes unknown variables')
        compiled[rule.id] = exposures
    if len({r.id for r in graph.rules}) != len(graph.rules):
        raise ValueError('Duplicate causal rule id')
    for node in graph.nodes.values():
        if node.driver is not None and node.driver not in graph.drivers:
            raise ValueError(f'Unknown driver {node.driver}')
    return graph, compiled


def _state() -> tuple[dict, Graph, dict[str, dict[str, Exposure]]]:
    """Merged rules plus the compiled graph, rebuilt only when a config file changes.

    rules() deep-copies the whole configuration and stats every file on each call; scoring loops over events x assets
    must not pay that, so file stamps are re-checked at most every CONFIG_RECHECK_SECONDS.
    """
    global _snapshot, _checked_at
    clock = time.monotonic()
    if _snapshot is not None and clock - _checked_at < CONFIG_RECHECK_SECONDS:
        return _snapshot[1], _snapshot[2], _snapshot[3]
    # Keyed on rules()' own content hash: file stamps can change while rules() still serves its (briefly cached)
    # previous content, which would pin a stale graph under a fresh stamp.
    config = rules()
    if _snapshot is None or _snapshot[0] != config['content_hash']:
        _snapshot = (config['content_hash'], config, *build(config))
    _checked_at = clock
    return _snapshot[1], _snapshot[2], _snapshot[3]


def settings() -> dict:
    """Shared read-only view of the merged rules for hot loops. Callers must never mutate it."""
    return _state()[0]


def graph() -> Graph:
    """Validated graph for the current configuration content; a malformed graph raises at load."""
    return _state()[1]


def compiled_effects(rule_id: str) -> dict[str, Exposure]:
    return _state()[2][rule_id]


def triggers() -> set[str]:
    return {r.trigger for r in graph().rules}


def affected_assets(trigger: str | None) -> list[str]:
    """Assets reachable from a trigger through any rule, in configuration order."""
    order = [a[0] for a in settings()['assets']]
    reached = {a for r in graph().rules if r.trigger == trigger for a in r.effects}
    return [a for a in order if a in reached]


def build_context(regime: dict | None = None, events: list[dict] | None = None, now: datetime | None = None) -> dict:
    """Condition inputs for exceptions/regime dependencies: active regimes, posture and fresh confirmed triggers."""
    from backend.engines import aware
    max_age = settings()['thresholds']['max_evidence_age_hours'] * 3600
    confirmed: set[str] = set()
    for event in events or []:
        if event.get('verification_status') != 'CONFIRMED':
            continue
        if now is not None and not 0 <= (aware(now) - aware(event['first_seen_utc'])).total_seconds() <= max_age:
            continue
        trigger = event_trigger(event)
        confirmed.update(t for t in (trigger, event.get('event_type')) if t)
    regime = regime or {}
    active = [r for r in regime.get('regimes', []) if r != 'MIXED']
    return {'regimes': active, 'posture': regime.get('posture'), 'confirmed_triggers': sorted(confirmed)}


def _matches(when: Mapping[Condition, str], context: dict) -> bool:
    checks = {'regime_active': lambda v: v in context.get('regimes', []),
              'event_confirmed': lambda v: v in context.get('confirmed_triggers', []),
              'posture': lambda v: v == context.get('posture')}
    return all(checks[key](value) for key, value in when.items())


def _path(rule: Rule, hours: float, context: dict, origin_symbol: str | None, config: Graph,
          compiled: dict[str, Exposure]) -> dict:
    nodes = config.nodes
    decay = 0.5 ** (max(0.0, hours) / rule.half_life_hours)
    multiplier = math.prod(rule.regime_dependencies.get(r, 1.0) for r in context.get('regimes', []))
    multiplier = min(config.max_regime_multiplier, multiplier)
    fired, suppressed_by, global_factor = [], None, 1.0
    asset_factor: dict[str, float] = {}
    exposures = {a: e for a, e in compiled.items() if a != origin_symbol}
    for exception in rule.exceptions:
        if not _matches(exception.when, context):
            continue
        fired.append(exception.id)
        factor = 0.0 if exception.action == 'suppress' else -1.0 if exception.action == 'invert' else exception.factor
        if not exception.variables:
            global_factor *= factor
            suppressed_by = exception.id if exception.action == 'suppress' else suppressed_by
            continue
        for asset, exposure in exposures.items():
            if set(exception.variables) & set(exposure.lineage):
                asset_factor[asset] = asset_factor.get(asset, 1.0) * factor
    strength = rule.strength * multiplier * abs(global_factor)
    flip = -1 if global_factor < 0 else 1
    effects, weights, drivers, lineage, contributions = {}, {}, {}, {}, {}
    for asset, exposure in exposures.items():
        factor = asset_factor.get(asset, 1.0)
        if factor == 0:
            continue
        sign = exposure.sign * flip * (-1 if factor < 0 else 1)
        effects[asset] = sign
        weights[asset] = round(exposure.weight * abs(factor), 6)
        lineage_drivers = [nodes[v].driver for v in exposure.lineage if nodes[v].driver]
        drivers[asset] = list(dict.fromkeys(lineage_drivers)) or [config.other_driver]
        lineage[asset] = list(exposure.lineage)
        contributions[asset] = round(sign * weights[asset] * strength * decay, 6)
    active = suppressed_by is None and strength * decay >= config.min_active_strength
    steps = [{'variable': s.variable, 'direction': s.direction, 'label': nodes[s.variable].label,
              'from': s.parent, 'transmission': s.transmission} for s in rule.steps]
    return {'rule_id': rule.id, 'label': rule.label, 'trigger': rule.trigger, 'version': rule.version, 'steps': steps,
            'effects': effects,
            'weights': weights, 'drivers': drivers, 'lineage': lineage, 'contributions': contributions if active else {},
            'strength': round(strength, 4), 'strength_prior': rule.strength, 'regime_multiplier': round(multiplier, 4),
            'decay': round(decay, 4), 'half_life_hours': rule.half_life_hours, 'persistence': rule.persistence,
            'active': active, 'suppressed_by': suppressed_by, 'exceptions_fired': fired, 'origin_symbol': origin_symbol}


def paths(trigger: str | None, hours: float = 0.0, context: dict | None = None, origin_symbol: str | None = None) -> list[dict]:
    """Every rule for the trigger as its own path; competing paths are never netted here."""
    _, config, compiled = _state()
    return [_path(r, hours, context or {}, origin_symbol, config, compiled[r.id]) for r in config.rules if r.trigger == trigger]


def summary(trigger: str | None) -> str | None:
    """One-sentence mechanism for the UI ('why it matters'): every path for the trigger, competing ones labelled."""
    nodes = graph().nodes
    chains = [' -> '.join(f"{nodes[s.variable].label} {'up' if s.direction > 0 else 'down'}" for s in r.steps)
              for r in graph().rules if r.trigger == trigger]
    if not chains:
        return None
    return chains[0] + ''.join(f'; competing path: {c}' for c in chains[1:])


def event_trigger(event: dict) -> str | None:
    known = triggers()
    return next((t for t in (event.get('trigger'), event.get('event_type')) if t in known), None)


def event_paths(event: dict, now: datetime, context: dict | None = None) -> list[dict]:
    from backend.engines import aware
    start = event.get('event_time') or event['first_seen_utc']
    hours = (aware(now) - aware(start)).total_seconds() / 3600
    return [dict(p, event_id=event.get('id')) for p in paths(event_trigger(event), hours, context)]


def net_effect(items: list[dict], symbol: str) -> float:
    return round(sum(p['contributions'].get(symbol, 0.0) for p in items if p['active']), 6)


def conflict(items: list[dict], symbol: str) -> dict:
    """Competing-path summary for one asset; conflicting when paths disagree and the net is a small share of the gross."""
    values = [(p['rule_id'], p['contributions'].get(symbol, 0.0)) for p in items if p['active']]
    values = [(rule_id, value) for rule_id, value in values if value]
    gross = sum(abs(v) for _, v in values)
    net = sum(v for _, v in values)
    opposed = any(v > 0 for _, v in values) and any(v < 0 for _, v in values)
    conflicting = opposed and gross > 0 and abs(net) / gross < graph().conflict_net_ratio
    return {'symbol': symbol, 'net': round(net, 6), 'gross': round(gross, 6), 'conflicting': conflicting,
            'supporting': [r for r, v in values if v * net > 0], 'opposing': [r for r, v in values if v * net < 0]}


def conflict_gate(items: list[dict], symbol: str) -> str | None:
    state = conflict(items, symbol)
    if not state['conflicting']:
        return None
    return f"Competing causal paths leave {symbol} unresolved ({', '.join(sorted(state['supporting'] + state['opposing']))})"


def _phrase(text: str, phrase: str) -> bool:
    return re.search(rf'(?<!\w){re.escape(phrase.casefold())}(?!\w)', text) is not None


def derive_trigger(title: str, content: str = '', source_id: str | None = None, *, tier: int | None = None) -> dict:
    """Keyword/source rules (config order, first match wins). Returns trigger, event_type, topic, the affected assets
    (the trigger's causal reach) and the linked assets of a rule that classifies without a trigger.

    A trigger gives an event a direction and causal reach over many assets, so the text has to state it (config
    causal_triggers.note): a rule fires for one of its `authorities` (the source or its independence group) on the
    whole text, the authority itself standing for the rule's actor group; for any other source only when the source's
    tier is at most the rule's `max_tier` and, under `headline_only`, every group is hit in the headline. `tier`
    overrides the registry tier (an administrator override); a source that is not registered has unknown_source_tier.
    The publisher's own name (publisher_names) never counts as a hit in its own items. `direction_words` is one more
    required group. A matching rule whose trigger is null classifies only: event_type, topic and linked assets, with no
    trigger and no affected assets. `classification` says what decided: 'rule', 'source' (source_event_types) or 'none'.
    Without a source_id the call is a text-only match (does this text state a trigger at all?): no tier or headline
    restriction applies. Ingestion and the read-time classification always pass the document's source."""
    merged = settings()
    config = merged['causal_triggers']
    source = next((s for s in merged.get('sources', []) if s['id'] == source_id), None) if source_id else None
    group = source.get('independence_group', source_id) if source else source_id
    if tier is None:
        tier = source.get('tier') if source else None
    if not isinstance(tier, (int, float)) or isinstance(tier, bool):
        tier = config.get('unknown_source_tier', 5)
    names = config.get('publisher_names') or {}
    own = {p.casefold() for key in {source_id, group} if key for p in names.get(key, [])}
    headline = title.casefold()
    text = f'{title}\n{content[:config["max_text_chars"]]}'.casefold()
    for rule in config['keyword_rules']:
        if rule['sources'] and source_id not in rule['sources']:
            continue
        authority = bool({source_id, group} & set(rule.get('authorities') or [])) if source_id else False
        restricted = source_id is not None and not authority
        if restricted and rule.get('max_tier') is not None and tier > rule['max_tier']:
            continue
        scope = headline if restricted and rule.get('headline_only') else text
        groups = [*rule['all'], *([rule['direction_words']] if rule.get('direction_words') else [])]
        hits = [f'source:{source_id}' if authority and index == rule.get('actor_group')
                else next((p for p in phrases if p.casefold() not in own and _phrase(scope, p)), None)
                for index, phrases in enumerate(groups)]
        if all(hits) and not any(_phrase(scope, p) for p in rule['none']):
            trigger = rule['trigger']
            return {'trigger': trigger, 'event_type': rule['event_type'], 'topic': rule['topic'],
                    'trigger_basis': 'rule' if trigger else 'none', 'classification': 'rule', 'matched': hits,
                    'affected_assets': affected_assets(trigger) if trigger else [], 'assets': list(rule.get('assets') or [])}
    types = config['source_event_types']
    fallback = types.get(source_id or '') or types.get(group or '')
    return {'trigger': None, 'event_type': fallback['event_type'] if fallback else None,
            'topic': fallback['topic'] if fallback else None, 'trigger_basis': 'none',
            'classification': 'source' if fallback else 'none', 'matched': [], 'affected_assets': [], 'assets': []}


def classified_type(stored: str | None, derived: dict | None) -> str | None:
    """Event type of an event whose stored type only says how the document arrived (causal_triggers.generic_event_types:
    NEWS, OFFICIAL_RELEASE): the type of its classification (derive_trigger: keyword rule, else the source), so the
    per-type verification windows apply. Any other stored type, set by a provider on purpose or accepted from the
    extractor, is kept."""
    generic = settings()['causal_triggers'].get('generic_event_types') or []
    return (derived or {}).get('event_type') or stored if stored in generic else stored


def macro_trigger(release: dict) -> dict | None:
    """Maps a released indicator's normalized surprise (z) to a trigger. No z-score means no trigger."""
    z = release.get('surprise_z_score', release.get('z_score'))
    if not isinstance(z, (int, float)) or isinstance(z, bool) or not math.isfinite(z):
        return None
    name = str(release.get('name', '')).casefold()
    country = str(release.get('country') or '')
    for rule in settings()['causal_triggers']['macro']:
        if country not in rule['countries'] or not any(_phrase(name, m) for m in rule['match']):
            continue
        if any(_phrase(name, x) for x in rule['exclude']) or abs(z) < rule['min_abs_z']:
            continue
        trigger = rule['up'] if z > 0 else rule['down']
        if trigger:
            return {'trigger': trigger, 'rule_id': rule['id'], 'z_score': z, 'trigger_basis': 'macro_surprise',
                    'affected_assets': affected_assets(trigger)}
    return None


def market_triggers(observed: dict[str, dict[str, float]]) -> list[dict]:
    """Causal activation from observed moves (e.g. Brent +2.8% -> inflation chain). The origin asset is excluded."""
    result = []
    for rule in settings()['causal_triggers']['market']:
        value = observed.get(rule['symbol'], {}).get(rule['field'])
        if value is not None and OPERATORS[rule['op']](value, rule['value']):
            result.append({'trigger': rule['trigger'], 'rule_id': rule['id'], 'origin_symbol': rule['symbol'],
                           'observed': value, 'trigger_basis': 'market_move'})
    return result


def market_paths(observed: dict[str, dict[str, float]], context: dict | None = None) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for item in market_triggers(observed):
        if item['trigger'] in seen:
            continue
        seen.add(item['trigger'])
        items += [dict(p, market_trigger=item['rule_id']) for p in paths(item['trigger'], 0.0, context, item['origin_symbol'])]
    return items


def causal_impact(event: dict, symbol: str, now: datetime, context: dict | None = None) -> float:
    """Legacy scalar: confirmed event net effect x fact confidence, clamped to -100..100."""
    if event.get('verification_status') != 'CONFIRMED':
        return 0.0
    value = net_effect(event_paths(event, now, context), symbol) * event.get('fact_confidence', 0) / 100
    return max(-100.0, min(100.0, value))
