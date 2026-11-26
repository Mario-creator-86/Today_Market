"""Pure deterministic calculations. No provider calls and no LLM arithmetic."""
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from statistics import stdev
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from backend.config import rules, rules_view


def aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def bucharest(value: datetime) -> datetime:
    return aware(value).astimezone(ZoneInfo('Europe/Bucharest'))


@lru_cache
def equity_calendar():
    return xcals.get_calendar('XNYS')


def market_status(asset_class: str, now: datetime) -> str:
    now = aware(now)
    if asset_class == 'crypto':
        return 'LIVE'
    local = now.astimezone(ZoneInfo('America/New_York'))
    if asset_class == 'fx':
        closed = local.weekday() == 5 or (local.weekday() == 4 and local.hour >= 17) or (
            local.weekday() == 6 and local.hour < 17)
        return 'LAST CLOSE' if closed else 'LIVE'
    if asset_class in {'equity', 'index'}:
        try:
            opened = equity_calendar().is_open_on_minute(pd.Timestamp(now).floor('min'))
            return 'LIVE' if opened else 'LAST CLOSE'
        except (ValueError, KeyError):
            return 'UNKNOWN'
    # Instrument-specific futures/bond sessions need venue metadata; do not guess.
    return 'UNKNOWN'


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.startswith('utm_') and k not in {'fbclid', 'gclid'}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), urlencode(sorted(query)), ''))


def url_variants(canonical: str) -> list[str]:
    """Spellings of one web document that differ only in scheme (http/https) or a leading 'www.', so the same page
    reached both ways is one document. The canonical URL itself (the link shown to people) is never rewritten."""
    parts = urlsplit(canonical)
    if parts.scheme not in {'http', 'https'} or not parts.netloc:
        return [canonical]
    host = parts.netloc.removeprefix('www.')
    variants = [urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
                for scheme in ('https', 'http') for netloc in (host, f'www.{host}')]
    return list(dict.fromkeys([canonical, *variants]))


def page_key(url: str) -> str:
    """One key for every spelling of a web document that url_variants treats as the same page: the normalized URL
    without its scheme and a leading 'www.' ('boj.or.jp/en/g260731.pdf'). Other URLs are only normalized."""
    canonical = normalize_url(url)
    parts = urlsplit(canonical)
    if parts.scheme not in {'http', 'https'} or not parts.netloc:
        return canonical
    return urlunsplit(('', parts.netloc.removeprefix('www.'), parts.path, parts.query, parts.fragment)).removeprefix('//')


def headline_key(text: str) -> str:
    return re.sub(r'\W+', ' ', text.casefold()).strip()


def _independence_key(item: dict, groups_by_source: dict[str, str]) -> str:
    """Syndicated/reposted copies count under their origin's independence group, never as extra confirmations."""
    origin = item.get('syndicated_from') or item.get('origin')
    return groups_by_source.get(origin, origin) if origin else item['group']


def confidence(evidence: list[dict], now: datetime, event_type: str | None = None) -> dict:
    """Independent, age-graded, source-weighted fact confidence. Thresholds live in rules.d/verification.json."""
    config = rules_view()
    policy = config['verification']
    max_age = policy['max_age_hours_by_event_type'].get(event_type or '', config['thresholds']['max_evidence_age_hours']) * 3600
    sources = {s['id']: s for s in config.get('sources', [])}
    groups_by_source = {k: s.get('independence_group', k) for k, s in sources.items()}
    current = []
    for item in evidence:
        age = (aware(now) - aware(item['published_at'])).total_seconds()
        if 0 <= age <= max_age and not item.get('discovery_only'):
            current.append((item, age / 3600))
    if not current:
        return {'status': 'STALE' if evidence and not any(e.get('discovery_only') for e in evidence) else 'UNCONFIRMED',
                'confidence': 0.0, 'independent_sources': 0}
    tiers: dict[str, float] = {}
    decayed: dict[str, float] = {}
    official = False
    contradictions: list[tuple[float, bool]] = []
    for item, hours in current:
        key = _independence_key(item, groups_by_source)
        direct = item.get('direct', True) and key == item['group']
        kind = item.get('kind') or sources.get(item.get('source_id', ''), {}).get('kind')
        half_life = policy['half_life_hours_by_kind'].get(kind, policy['default_half_life_hours'])
        weight = max(0, min(1, item['weight'])) * (1 if direct else policy['second_hand_factor'])
        if item['relation'] == 'contradicts':
            contradictions.append((weight, bool(item.get('official')) and direct))
            continue
        # Tier checks use the undecayed weight; the confidence number decays with age per source kind.
        tiers[key] = max(tiers.get(key, 0), weight)
        decayed[key] = max(decayed.get(key, 0), weight * 0.5 ** (hours / half_life))
        official = official or (item.get('official', False) and direct)
    conf = (1 - math.prod(1 - v for v in decayed.values())) * 100
    if contradictions:
        conf *= 1 - max(w for w, _ in contradictions)
    if any(w >= policy['false_min_weight'] and is_official for w, is_official in contradictions):
        status = 'FALSE'
    elif any(w >= policy['contradiction_min_weight'] for w, _ in contradictions):
        status = 'CONTRADICTED'
    elif conf >= config['thresholds']['fact_confirmation'] and (
            official or sum(v >= policy['confirm_min_weight'] for v in tiers.values()) >= policy['confirm_min_independent']):
        status = 'CONFIRMED'
    elif any(v >= policy['partial_min_weight'] for v in tiers.values()):
        status = 'PARTIALLY_CONFIRMED'
    else:
        status = 'UNCONFIRMED'
    return {'status': status, 'confidence': round(conf, 2), 'independent_sources': len(tiers)}


def _mentions(text: str, phrase: str) -> bool:
    return re.search(rf'(?<!\w){re.escape(phrase.casefold())}(?!\w)', text) is not None


def attribution_origin(title: str, content: str = '', source_id: str | None = None) -> str | None:
    """Second-hand detection: a document crediting another source ('(Reuters) -', 'via @handle') names its origin.

    Phrases match on word boundaries ('via ap' never matches 'via apple'). A source crediting itself, or another
    source of its own independence group, is not second-hand, so None is returned.
    """
    config = rules_view()
    attribution = config['verification']['attribution']
    text = f'{title}\n{content[:attribution["max_chars"]]}'.casefold()
    origin = next((source for source, patterns in attribution['patterns'].items() if any(_mentions(text, p) for p in patterns)),
                  None)
    if origin is None:
        handle = re.search(attribution['handle_pattern'], text)
        origin = f'x:@{handle.group(1)}' if handle else None
    if origin is None or source_id is None:
        return origin
    groups = {s['id']: s.get('independence_group', s['id']) for s in config.get('sources', [])}
    return None if groups.get(origin, origin) == groups.get(source_id, source_id) else origin


def _shingles(text: str, size: int) -> set[tuple[str, ...]]:
    words = re.findall(r'\w+', text.casefold())
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def shingle_similarity(left: str, right: str) -> float | None:
    """Jaccard similarity of word shingles; None when either text is too short to judge."""
    config = rules_view()['verification']['syndication']
    if min(len(re.findall(r'\w+', left)), len(re.findall(r'\w+', right))) < config['min_words']:
        return None
    a, b = _shingles(left, config['shingle_words']), _shingles(right, config['shingle_words'])
    return len(a & b) / len(a | b) if a | b else None


def syndicated_from(text: str, earlier: list[tuple[str, str]]) -> str | None:
    """Source id of the most similar earlier document when the text is a near-copy of it (reposted/syndicated)."""
    threshold = rules_view()['verification']['syndication']['min_similarity']
    scored = [(similarity, source) for source, other in earlier
              if (similarity := shingle_similarity(text, other)) is not None and similarity >= threshold]
    return max(scored)[1] if scored else None


def category_side(label: str) -> int:
    """+1/-1 for long/short setup or lean labels (category or legacy direction), 0 for WAIT variants."""
    for band in rules_view()['categories']['bands']:
        for side, key in ((1, 'long'), (-1, 'short')):
            if label in (band[key]['category'], band[key]['direction']):
                return side
    return 0


def is_setup(label: str) -> bool:
    """True for SETUP/STRONG SETUP categories and their legacy LONG/SHORT directions (actionable)."""
    return any(label in (band[k]['category'], band[k]['direction']) for band in rules_view()['categories']['bands'] if band['setup']
               for k in ('long', 'short'))


def _component(name: str, raw: float | dict) -> dict[str, Any]:
    item: dict[str, Any] = raw if isinstance(raw, dict) else {'value': raw, 'status': 'measured', 'basis': 'caller supplied'}
    value = item.get('value')
    if item.get('status') == 'measured':
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -100 <= value <= 100:
            raise ValueError(f'Invalid score component {name}')
    elif item.get('status') != 'missing':
        raise ValueError(f'Invalid component status for {name}')
    return item


def _band(score: float, config: dict) -> dict | None:
    thresholds = config['thresholds']
    bands = sorted(config['categories']['bands'], key=lambda b: -thresholds[b['threshold']])
    return next((b for b in bands if abs(score) >= thresholds[b['threshold']]), None)


def decide(components: Mapping[str, float | dict], blocks: list[str], caps: list[str] | None = None,
           weights: dict[str, float] | None = None) -> dict:
    """Weighted score in -100..100 mapped to the §23 category table. Gates give WAIT / DATA INSUFFICIENT; setup
    requirements or caller caps downgrade a SETUP to the lean band. `weights` lets a challenger re-score in shadow."""
    config = rules()
    weights = weights or config['weights']
    if abs(sum(weights.values()) - 1) > config['scoring']['weight_sum_tolerance']:
        raise ValueError('Component weights must sum to 1')
    if set(components) - set(weights):
        raise ValueError('Invalid score component')
    detail: dict[str, dict[str, Any]] = {k: _component(k, components[k]) if k in components else
              {'value': None, 'status': 'missing', 'basis': 'Not computed'} for k in weights}
    weighted = {k: round(detail[k]['value'] * w, 4) if detail[k]['status'] == 'measured' else 0.0 for k, w in weights.items()}
    score = round(max(-100.0, min(100.0, sum(weighted.values()))), 2)
    categories = config['categories']
    band = _band(score, config)
    downgrades = list(caps or [])
    if band and band['setup']:
        need = categories['setup_requirements']
        for name in need['aligned']:
            item = detail[name]
            if item['status'] != 'measured' or item['value'] * score <= 0:
                downgrades.append(f"{name.replace('_', ' ').capitalize()} confirmation absent or opposed")
        anchor = need['min_abs_any']
        if not any(detail[k]['status'] == 'measured' and detail[k]['value'] * score > 0 and abs(detail[k]['value']) >= anchor['value']
                   for k in anchor['components']):
            downgrades.append(f"No aligned {'/'.join(anchor['components'])} driver of at least {anchor['value']}")
    if band and band['setup'] and downgrades:
        band = next(b for b in categories['bands'] if not b['setup'])
    if blocks:
        label = categories['insufficient']
    elif band:
        label = band['long' if score > 0 else 'short']
    else:
        label = categories['neutral']
    return {'category': label['category'], 'direction': label['direction'], 'score': score, 'components': weighted,
            'component_detail': detail, 'components_missing': [k for k in weights if detail[k]['status'] != 'measured'],
            'reasons': list(dict.fromkeys(blocks + downgrades)), 'blocks': list(dict.fromkeys(blocks)),
            'downgrades': list(dict.fromkeys(downgrades)), 'rules_version': config['version'],
            'content_hash': config['content_hash']}


def surprise(actual: float, consensus: float, history: list[float]) -> dict:
    raw = actual - consensus
    deviation = stdev(history) if len(history) >= rules_view()['thresholds']['min_history'] else 0
    return {'raw_surprise': round(raw, 8), 'z_score': raw / deviation if deviation > 0 else None, 'observations': len(history)}


def technical_levels(bars: list[dict]) -> dict:
    if len(bars) < rules_view()['thresholds']['min_history']:
        return {}
    high, low = max(b['high'] for b in bars), min(b['low'] for b in bars)
    volume = sum(b['volume'] for b in bars)
    vwap = sum((b['high'] + b['low'] + b['close']) / 3 * b['volume'] for b in bars) / volume if volume else None
    tr = [max(b['high'] - b['low'], abs(b['high'] - p['close']), abs(b['low'] - p['close'])) for p, b in zip(bars, bars[1:], strict=False)]
    return {'support': low, 'resistance': high, 'vwap': vwap, 'atr': sum(tr[-14:]) / len(tr[-14:]), 'observations': len(bars), 'scope': 'available bars'}


def reaction(before: float, after: float, expected: int, tolerance_pct: float | None = None) -> dict:
    """Legacy single-window reaction; per-asset units/tolerances live in backend.analytics."""
    if before <= 0 or after <= 0 or expected not in {-1, 0, 1}:
        raise ValueError('Invalid reaction input')
    noise = rules_view()['reaction']['tolerances']['default']['pct']['noise'] if tolerance_pct is None else tolerance_pct
    change = (after / before - 1) * 100
    if not expected:
        return {'change_pct': change, 'status': 'NOT_EXPECTED'}
    return {'change_pct': change, 'status': 'NO_REACTION' if abs(change) < noise else 'CONFIRMED' if change * expected > 0 else 'DIVERGENT'}


def causal_impact(event: dict, symbol: str, now: datetime, context: dict | None = None) -> float:
    """Legacy scalar kept for existing callers; the causal graph lives in backend.causal."""
    from backend.causal import causal_impact as graph_impact
    return graph_impact(event, symbol, now, context)
