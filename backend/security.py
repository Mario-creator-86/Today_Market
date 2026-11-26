"""Prompt-injection screening and deterministic acceptance of AI proposals.

External text (articles, filings, X posts, search results) is data. It is sanitised before it reaches
an LLM and AI classifications only change scoring inputs when the evidence itself corroborates them.
"""
import re
from functools import lru_cache
from typing import Any

from backend.config import rules

URL = re.compile(r'https?://\S+|www\.\S+', re.IGNORECASE)


def _config() -> dict:
    return rules()['security']


@lru_cache(maxsize=8)
def _patterns(source: tuple[str, ...]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in source]


REMOVED = '[instruction-like text removed]'
# Sentence or line boundaries of external text ("4.25" does not split): an instruction is removed with its sentence.
_BOUNDARY = re.compile(r'(?<=[.!?])\s+|\s*\n\s*')


def _redact(text: str, patterns: list[re.Pattern]) -> str:
    """Every sentence or line of `text` that matches an instruction-like pattern is replaced by REMOVED."""
    kept: list[str] = []
    for part in _BOUNDARY.split(text):
        piece = REMOVED if any(p.search(part) for p in patterns) else part
        if piece and not (piece == REMOVED and kept and kept[-1] == REMOVED):
            kept.append(piece)
    return ' '.join(kept)


def screen_text(text: str, redact: bool = False) -> tuple[str, list[str]]:
    """Remove invisible/bidi control characters and report instruction-like phrases. The text itself is kept, unless
    `redact` is set: then every sentence carrying an instruction-like phrase is removed (replaced by REMOVED) and the
    removal is reported."""
    config = _config()
    invisible = set(config['invisible_characters'])
    cleaned = ''.join(c for c in text if c not in invisible)
    findings = []
    if cleaned != text:
        findings.append('invisible_or_bidi_characters_removed')
    patterns = _patterns(tuple(config['injection_patterns']))
    matched = [pattern for pattern in patterns if pattern.search(cleaned)]
    findings += [f'instruction_like_text:{pattern.pattern[:60]}' for pattern in matched]
    if redact and matched:
        cleaned = _redact(cleaned, matched)
        # A pattern anchored to a line start can still match what is left: remove it whole rather than keep it.
        if any(pattern.search(cleaned) for pattern in patterns):
            cleaned = REMOVED
        findings.append('instruction_like_text_removed')
    return cleaned, findings


def screen_inputs(value: Any, redact: bool = False) -> tuple[Any, list[str]]:
    """Recursively sanitise and bound LLM inputs: strings are cleaned and capped, lists truncated with a note. With
    `redact`, instruction-like sentences are removed from the strings instead of only being reported."""
    config = _config()
    findings: list[str] = []

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            cleaned, found = screen_text(item, redact)
            findings.extend(found)
            if len(cleaned) > config['max_string_chars']:
                findings.append('string_truncated')
                cleaned = cleaned[:config['max_string_chars']] + ' [truncated]'
            return cleaned
        if isinstance(item, dict):
            return {key: walk(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            items = list(item)
            if len(items) > config['max_list_items']:
                findings.append('list_truncated')
                omitted = len(items) - config['max_list_items']
                return [walk(v) for v in items[:config['max_list_items']]] + [f'[{omitted} more items omitted]']
            return [walk(v) for v in items]
        return item

    return walk(value), list(dict.fromkeys(findings))


def instruction_like(text: str) -> bool:
    """True when `text` carries an instruction-like phrase (the configured injection patterns)."""
    return any(p.search(text) for p in _patterns(tuple(_config()['injection_patterns'])))


def bound_queries(queries: list[str]) -> list[str]:
    """Research-planner queries go to a web search: strip URLs and instruction text, cap count and length."""
    config = _config()
    result = []
    for query in queries:
        cleaned, findings = screen_text(URL.sub(' ', query))
        cleaned = ' '.join(cleaned.split())[:config['max_query_chars']]
        if cleaned and not any(f.startswith('instruction_like_text') for f in findings):
            result.append(cleaned)
    return list(dict.fromkeys(result))[:config['max_queries']]


def _mentions(text: str, needle: str) -> bool:
    return re.search(rf'(?<![A-Za-z0-9]){re.escape(needle.casefold())}(?![A-Za-z0-9])', text) is not None


def accept_extraction(current_type: str, current_assets: list[str], extraction: dict, evidence_texts: list[str],
                      trigger_keywords: dict[str, list[str]], asset_names: dict[str, list[str]]) -> dict:
    """AI event_type/assets are proposals. Apply them only when the evidence text literally corroborates them.

    trigger_keywords maps an event type to words that must appear in the evidence; asset_names maps a
    symbol to the names/tickers that count as a mention. Rejected proposals are reported, never applied.
    """
    corpus = ' '.join(evidence_texts).casefold()
    proposed_type = extraction.get('event_type')
    keywords = trigger_keywords.get(proposed_type or '', [])
    type_ok = bool(proposed_type) and proposed_type != current_type and any(_mentions(corpus, k) for k in keywords)
    accepted_assets, rejected_assets = [], []
    for symbol in extraction.get('mentioned_assets', []):
        names = asset_names.get(symbol)
        if names and any(_mentions(corpus, name) for name in names):
            accepted_assets.append(symbol)
        else:
            rejected_assets.append(symbol)
    return {'event_type': proposed_type if type_ok else current_type,
            'assets': list(dict.fromkeys(current_assets + accepted_assets)),
            'basis': 'ai_proposal_accepted' if type_ok or accepted_assets else 'rule',
            'rejected': {'event_type': None if type_ok or not proposed_type or proposed_type == current_type else proposed_type,
                         'assets': rejected_assets}}
