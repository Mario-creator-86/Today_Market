"""Deterministic pre-publication audit (spec §4J). The AI auditor may add BLOCK reasons but can never
turn a deterministic failure into PASS."""
import json
import re
from datetime import datetime
from typing import Any

from backend.config import freshness_seconds, rules
from backend.engines import aware
from backend.technicals import is_live_quote

CHECKS = ('fresh_prices', 'valid_timestamps', 'market_open', 'sources_attached', 'old_article_as_new',
          'claim_beyond_source', 'contradictions_visible', 'direction_matches_score', 'probabilities_valid',
          'invented_numbers', 'timezone_conversion', 'unverified_x_as_fact')
NUMBER = re.compile(r'(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)*')
ACTIONABLE_SIGN = {'LONG': 1, 'STRONG LONG': 1, 'SHORT': -1, 'STRONG SHORT': -1}


def numbers(text: str) -> set[str]:
    return set(NUMBER.findall(text))


def _rendered(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def untraceable(output: Any, inputs: Any, allowlist: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """Numbers cited in `output` that `inputs` do not contain. Both sides are rendered unescaped, so an arrow or a minus
    sign is never read as the digits of its \\u escape (json.dumps would turn '↓' into the number 193)."""
    return numbers(_rendered(output)) - numbers(_rendered(inputs)) - set(allowlist)


# --- numbers in context: a value must exist in the input with the same unit (percent, basis points, clock time) -----
_VALUE = r'(?<![\w.,])[-+−]?\d+(?:[.,]\d+)?'
PERCENT = re.compile(rf'({_VALUE})\s*(?:%|(?:per\s*cent|percent|pct)\b)', re.IGNORECASE)
BASIS_POINTS = re.compile(rf'({_VALUE})\s*(?:bps?|basis[\s-]points?)\b', re.IGNORECASE)
CLOCK = re.compile(r'(?<![\w:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])')
# Spelled-out quantities ("ninety percent", "a quarter-point cut", "fifty basis points") escape the digit checks.
_WORDS = (r'zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|twenty-five|thirty|forty|'
          r'fifty|sixty|seventy|seventy-five|eighty|ninety|hundred|a hundred|one hundred|half|a half|quarter|a quarter|'
          r'three-quarters?')
SPELLED_QUANTITY = re.compile(rf'\b(?:{_WORDS})[\s-](?:per\s*cent|percent|percentage[\s-]points?|basis[\s-]points?|bps?|'
                              r'points?)\b|\b(?:half|quarter)\s+(?:of\s+)?an?\s+(?:percentage\s+)?point\b',
                              re.IGNORECASE)
UNIT_NAMES = {'%': 'pct', 'pct': 'pct', 'percent': 'pct', 'bp': 'bp', 'bps': 'bp', 'basis points': 'bp'}


def value(token: str) -> float:
    """A cited number as a magnitude: signs (+, -, U+2212) are direction words, and '4,25' is 4.25 while '64,800' is 64800."""
    token = token.strip().lstrip('+-−')
    if ',' in token and '.' not in token:
        head, _, tail = token.partition(',')
        token = head + tail if len(tail) == 3 else f'{head}.{tail}'
    return round(abs(float(token.replace(',', ''))), 6)


def clock(hours: str, minutes: str) -> str:
    return f'{int(hours):02d}:{minutes}'


def quantities(text: str) -> dict[str, set[float]]:
    """Percent ('pct') and basis-point ('bp') values written in `text`."""
    return {'pct': {value(m.group(1)) for m in PERCENT.finditer(text)},
            'bp': {value(m.group(1)) for m in BASIS_POINTS.finditer(text)}}


def clocks(text: str) -> set[str]:
    """HH:MM clock times written in `text`, zero-padded."""
    return {clock(m.group(1), m.group(2)) for m in CLOCK.finditer(text)}


def unit_values(item: Any, unit: str | None = None, found: dict[str, set[float]] | None = None) -> dict[str, set[float]]:
    """Numeric values of structured data by unit: a dict's own `unit` ('%', 'pct', 'bp'), a `units` map for its
    `reactions` ({SYM: 'bp'|'pct'}), and keys ending in _pct / _bp tag the numbers beneath them."""
    found = found if found is not None else {'pct': set(), 'bp': set()}
    if isinstance(item, dict):
        own = UNIT_NAMES.get(str(item.get('unit') or '').strip().lower(), unit)
        units = item.get('units') if isinstance(item.get('units'), dict) else {}
        for key, child in item.items():
            if key in ('unit', 'units'):
                continue
            name = str(key).lower()
            tagged = 'pct' if name.endswith(('_pct', 'pct', 'percent')) else 'bp' if name.endswith(('_bp', 'bps')) else own
            if key == 'reactions' and isinstance(child, dict) and units:
                for symbol, cells in child.items():
                    unit_values(cells, UNIT_NAMES.get(str(units.get(symbol) or '').lower(), tagged), found)
            else:
                unit_values(child, tagged, found)
    elif isinstance(item, (list, tuple)):
        for child in item:
            unit_values(child, unit, found)
    elif isinstance(item, (int, float)) and not isinstance(item, bool) and unit in found:
        found[unit].add(round(abs(float(item)), 6))
    return found


def _parse(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _check(name: str, passed: bool, detail: str) -> dict:
    return {'name': name, 'passed': passed, 'detail': detail}


def pre_audit(decision: dict, inputs: dict, now: datetime, stale_seconds: float, max_future_seconds: int,
              numeric_allowlist: set[str] | None = None) -> dict:
    """stale_seconds is the default freshness limit; the quote's own provider limit applies
    (config.freshness_seconds, the lookup HubService.quote used). A quote stamped up to max_future_seconds after `now`
    counts as age 0 (the same tolerance as valid_timestamps), further ahead it fails both checks."""
    config = rules()
    thresholds = config['thresholds']
    social = {s['id'] for s in config['sources'] if s.get('kind') in {'social', 'anonymous'}}
    now = aware(now)
    quote = inputs.get('quote') or {}
    events = inputs.get('events') or []
    evidence = [e for event in events for e in event.get('evidence', [])]
    checks = []

    stamp = _parse(quote.get('timestamp'))
    age = (now - stamp).total_seconds() if stamp else None
    limit = freshness_seconds(quote.get('provider'), stale_seconds)
    checks.append(_check('fresh_prices', bool(quote) and quote.get('freshness_status') == 'FRESH'
                         and is_live_quote(quote) and age is not None and -max_future_seconds <= age <= limit,
                         f'quote age {age if age < -max_future_seconds else max(0.0, age):.1f}s, limit {limit:g}s, live data {is_live_quote(quote)}'
                         if age is not None else 'no timestamped quote'))

    future = [v for v in [quote.get('timestamp'), *(e.get('first_seen_utc') for e in events),
                          *(x.get('published_at') for x in evidence)]
              if (parsed := _parse(v)) and (parsed - now).total_seconds() > max_future_seconds]
    inverted = [x['id'] for x in evidence if (p := _parse(x.get('published_at'))) and (i := _parse(x.get('ingested_at')))
                and (p - i).total_seconds() > max_future_seconds]
    checks.append(_check('valid_timestamps', not future and not inverted,
                         'ok' if not future and not inverted else f'future={len(future)} published_after_ingest={len(inverted)}'))

    checks.append(_check('market_open', quote.get('market_status') == 'LIVE', f"market status {quote.get('market_status')}"))

    # Facts behind a decision are verified events with linked evidence and/or official macro releases (provider + time).
    unsourced = [e['id'] for e in events if not any(x.get('url') for x in e.get('evidence', []))]
    releases = inputs.get('macro_releases') or []
    unsourced += [r.get('name') for r in releases if not r.get('provider') or not _parse(r.get('released_at'))]
    sourced = bool(events or releases) and not unsourced
    checks.append(_check('sources_attached', sourced, 'every fact has a linked source' if sourced
                         else f'unsourced facts {unsourced or "none supplied"}'))

    # Same age windows as fact verification (engines.confidence): per event type, else the global maximum.
    by_type = config['verification']['max_age_hours_by_event_type']
    default_age = thresholds['max_evidence_age_hours']
    old = [x['id'] for e in events for x in e.get('evidence', [])
           if x.get('relation') == 'supports' and (p := _parse(x.get('published_at')))
           and (now - p).total_seconds() > by_type.get(e.get('event_type') or '', default_age) * 3600]
    checks.append(_check('old_article_as_new', not old, 'all supporting evidence current' if not old else f'stale evidence {old}'))

    explanation = decision.get('explanation') or {}
    allowed = set(inputs.get('evidence_ids', []))
    cited = set(explanation.get('evidence_ids', []))
    checks.append(_check('claim_beyond_source', cited <= allowed,
                         'explanation cites supplied evidence only' if cited <= allowed else f'unknown citations {sorted(cited - allowed)}'))

    contradicted = [e['id'] for e in events if e.get('verification_status') == 'CONTRADICTED'
                    or any(x.get('relation') == 'contradicts' for x in e.get('evidence', []))]
    visible = not contradicted or any('contradict' in r.lower() for r in decision.get('reasons', []))
    checks.append(_check('contradictions_visible', visible, 'no hidden contradiction' if visible else f'contradicted events {contradicted}'))

    score, direction = decision.get('score', 0), decision.get('direction', 'WAIT')
    sign = ACTIONABLE_SIGN.get(direction, 0)
    matches = sign == 0 or (score * sign >= thresholds['setup'] and (abs(score) >= thresholds['strong']) == direction.startswith('STRONG'))
    checks.append(_check('direction_matches_score', matches, f'{direction} with score {score}'))

    # A published probability needs a promoted calibration validated on >= minimum_calibration out-of-sample
    # observations, and >= min_comparable comparable signals in its score bucket.
    probability = decision.get('historical_probability')
    validated = ((probability or {}).get('calibration') or {}).get('validation_observations') if isinstance(probability, dict) else None
    valid_probability = probability is None or (isinstance(probability, dict) and (
        probability.get('status') == 'NOT ENOUGH DATA' or
        (isinstance(probability.get('p'), (int, float)) and 0 <= probability['p'] <= 1
         and isinstance(probability.get('n'), int) and probability['n'] >= config['autolearn']['min_comparable']
         and isinstance(validated, int) and validated >= thresholds['minimum_calibration'])))
    checks.append(_check('probabilities_valid', valid_probability, 'no uncalibrated probability published'
                         if valid_probability else f'invalid probability {probability}'))

    known = numbers(str(inputs)) | (numeric_allowlist or set())
    invented = numbers(' '.join([*explanation.get('reasons', []), explanation.get('confirmation', ''),
                                 explanation.get('invalidation', '')])) - known
    checks.append(_check('invented_numbers', not invented, 'all numbers traceable to inputs' if not invented else f'untraceable {sorted(invented)}'))

    naive = [v for v in [quote.get('timestamp'), decision.get('created_at'), *(e.get('first_seen_utc') for e in events)]
             if v is not None and _parse(v) is None]
    checks.append(_check('timezone_conversion', not naive, 'all timestamps timezone-aware UTC' if not naive else f'naive timestamps {naive}'))

    rumour = [e['id'] for e in events if e.get('evidence') and all(x.get('source_id') in social for x in e['evidence'])]
    checks.append(_check('unverified_x_as_fact', not rumour, 'no social-only event treated as fact' if not rumour else f'social-only events {rumour}'))

    failed = [c for c in checks if not c['passed']]
    return {'status': 'BLOCK' if failed else 'PASS', 'checks': checks,
            'reasons': [f"{c['name']}: {c['detail']}" for c in failed]}


def combine(deterministic: dict, ai_audit: dict | None) -> dict:
    """Final audit: any deterministic failure or missing/blocking AI audit blocks publication."""
    reasons = list(deterministic['reasons'])
    if ai_audit is None:
        reasons.append('AI auditor unavailable')
    elif ai_audit.get('status') != 'PASS':
        reasons.extend(f'AI auditor: {r}' for r in ai_audit.get('reasons', []) or ['blocked'])
    return {'status': 'BLOCK' if reasons else 'PASS', 'checks': deterministic['checks'],
            'ai_checks': (ai_audit or {}).get('checks', []), 'reasons': reasons}
