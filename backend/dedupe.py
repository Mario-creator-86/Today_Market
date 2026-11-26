"""Deterministic story deduplication (spec §12): Reuters, AP, X, company releases and reposts of one story
become ONE event with all evidence attached.

Signals: canonical URL, headline normalised by stripping wire decorations, character n-gram cosine similarity
(a lexical embedding computed locally; no remote model), shared entities and publication-time proximity.
Merges need strong lexical agreement or lexical + entity + time agreement; weaker matches stay separate. Headlines
whose long numbers differ (licence or case numbers) never merge, and another page of the same official source joins an
event only under an identical headline (each release, licence or statement has its own page)."""
import math
import re
from collections import Counter
from datetime import datetime
from functools import lru_cache

from backend.config import rules_view
from backend.engines import aware

ENTITY = re.compile(r'\$[A-Z]{1,6}\b|\b[A-Z]{2,6}\b|\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b')


def _config() -> dict:
    # The shared read-only view: rules() copies the whole configuration on every call (about a millisecond), and
    # this is read for every headline compared.
    return rules_view()['dedupe']


@lru_cache(maxsize=4)
def _decorations(patterns: tuple[str, ...]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def clean_headline(title: str) -> str:
    text = title
    for pattern in _decorations(tuple(_config()['decorations'])):
        text = pattern.sub(' ', text)
    return re.sub(r'\W+', ' ', text.casefold()).strip()


def vector(text: str) -> Counter:
    """Character n-gram term frequencies of normalised text (a deterministic lexical embedding)."""
    n = _config()['ngram']
    padded = f' {re.sub(r"\s+", " ", text.casefold()).strip()} '
    return Counter(padded[i:i + n] for i in range(max(0, len(padded) - n + 1)))


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


def entities(text: str) -> set[str]:
    stop = set(_config()['stop_entities'])
    found = set()
    for match in ENTITY.finditer(text):
        words = match.group(0).lstrip('$').casefold().split()
        while words and words[0] in stop:  # sentence-initial capitals: "The Commission" is "commission"
            words.pop(0)
        found.add(' '.join(words))
    return {e for e in found if e not in stop and len(e) >= 2}


@lru_cache(maxsize=4)
def _identifier(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def generic_url(url: str) -> bool:
    """Whether the URL is a page many unrelated publications share (config generic_url_patterns: Benzinga's quote page
    of a stock, linked by every wire item without an article). Such a URL never identifies a publication."""
    return any(p.search(url) for p in _decorations(tuple(_config().get('generic_url_patterns') or ())))


def identifiers(title: str) -> set[str]:
    """Long numbers of a headline (licence, accession or case numbers; config identifier_pattern)."""
    return set(_identifier(_config()['identifier_pattern']).findall(title))


def features(title: str, content: str = '') -> dict:
    headline = clean_headline(title)
    return {'headline': headline, 'vector': vector(headline), 'identifiers': identifiers(title),
            'body': vector(content[:_config()['content_chars']]) if content else Counter(),
            'entities': entities(f'{title} {content[:_config()["content_chars"]]}')}


def score(document: dict, candidate: dict, published: datetime, candidate_time: datetime, *,
          same_source_page: bool = False) -> dict:
    """How strongly a document matches a candidate event, with the signals that decided it. Even an identical headline
    published more than headline_time_window_hours apart is a new publication (a recurring release such as a monthly
    indicator table), never the same story. Headlines whose long numbers differ (identifier_pattern: licence
    INT/2025/7895596 vs INT/2025/8031092) are different documents. same_source_page: the candidate event already holds
    another page of the document's own official source, which it joins only under an identical headline
    (official_pages_distinct)."""
    config = _config()
    lexical = cosine(document['vector'], candidate['vector'])
    shared = document['entities'] & candidate['entities']
    hours = abs((aware(published) - aware(candidate_time)).total_seconds()) / 3600
    same = (lexical >= config['headline_match'] and hours <= config['headline_time_window_hours']) or (
        lexical >= config['entity_headline_match'] and len(shared) >= config['min_shared_entities']
        and hours <= config['entity_time_window_hours'])
    result = {'match': same, 'lexical': round(lexical, 4), 'shared_entities': sorted(shared), 'hours_apart': round(hours, 2)}
    own, other = document.get('identifiers') or set(), candidate.get('identifiers') or set()
    if same and own and other and not own & other:
        result.update(match=False, distinct_identifiers=True)
    elif same and same_source_page and config.get('official_pages_distinct') and document['headline'] != candidate['headline']:
        result.update(match=False, same_source_other_page=True)
    return result


def best_match(document: dict, published: datetime, candidates: list[tuple[str, dict, datetime]],
               same_source: set[str] | frozenset[str] = frozenset()) -> tuple[str | None, dict]:
    """candidates: (event_id, features, first_seen); same_source: ids of the candidate events that already hold another
    page of the document's own official source (score same_source_page). Returns the best matching event id and its
    signals."""
    best: tuple[str | None, dict] = (None, {'match': False, 'lexical': 0.0})
    for event_id, candidate, when in candidates:
        result = score(document, candidate, published, when, same_source_page=event_id in same_source)
        if result['match'] and result['lexical'] > best[1]['lexical']:
            best = (event_id, result)
    return best


def syndicated(body: Counter, others: list[tuple[str, Counter]]) -> str | None:
    """Source id whose text this document republishes (near-identical body), so it is not an independent confirmation."""
    threshold = _config()['syndication_similarity']
    return next((source for source, other in others if body and cosine(body, other) >= threshold), None)


def novelty(document: dict, recent: list[dict]) -> float:
    """1 - highest similarity to recent stories: a rehash of known news scores near zero."""
    return round(1 - max((cosine(document['vector'], other['vector']) for other in recent), default=0.0), 4)
