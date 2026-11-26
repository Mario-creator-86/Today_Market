import hashlib
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select

from backend import dedupe
from backend.config import Settings, rules, rules_view
from backend.database import RECORDS, Document, Event, Evidence, Store, Tick, uid, utcnow
from backend.engines import aware, confidence, headline_key, normalize_url, page_key, url_variants
from backend.providers.base import plain_text
from backend.providers.discovery import date_only_day, restate_legacy
from backend.schemas import DocumentInput, Quote, iso
from backend.security import screen_text

logger = logging.getLogger(__name__)

# Quote fields a provider may restate when it re-publishes an observation already stored: its presentation and its
# close classification (a last trade called real-time while fresh is the session's close once the session has closed).
RESTATED_QUOTE_FIELDS = ('as_of_label', 'label', 'attribution', 'daily_close', 'data_class')
EXCERPT_CHARS = 4000
RESPLIT_KEY = 'dedupe:resplit'  # configuration_versions row: the dedupe_version events were last re-split with
REPAIR_KEY = 'dedupe:repair'  # configuration_versions row: the dedupe_version the start-up repair last ran for
MERGED_EVENT = 'merged-event:'  # event_relationships key prefix: the id of a merged duplicate -> the event kept
MERGED_ROW = 'merged:'  # event_relationships key prefix: a reaction row of a merged duplicate the kept event already had
REFERENCE_TABLES = ('research_jobs', 'social_posts')  # record tables whose payload.event_id names an event
# Event fields an AI role derived from the event's whole document set: reset when a split takes documents away.
AI_FIELDS = ('extraction', 'ai_enriched', 'ai_acceptance', 'fact_check', 'interpretation', 'interpretation_role',
             'analysed_status')
UNCLASSIFIED: dict[str, Any] = {'trigger': None, 'event_type': None, 'topic': None, 'trigger_basis': 'none',
                                'classification': 'none', 'matched': [], 'affected_assets': [], 'assets': []}


def readable(text: str | None) -> str:
    """Stored text as people read it (event summaries, evidence titles and excerpts): tags removed and entities decoded
    (providers.base.plain_text; documents stored before feeds were cleaned hold raw RSS HTML and '&amp;'), and the
    wording of earlier adapter versions restated (discovery.restate_legacy: topic codes, ISO dates). Stored rows are
    never rewritten."""
    return restate_legacy(plain_text(text))


def distinct_pages(source: dict) -> bool:
    """Whether each page of this source is its own publication (dedupe official_pages_distinct): an official website,
    not an official social account, whose posts of one story are several pages."""
    config = rules_view()['dedupe']
    return bool(config.get('official_pages_distinct') and source.get('official')
                and source.get('domain') not in (config.get('official_pages_shared_domains') or []))


def classify(title: str, content: str, source_id: str | None, tier: Any, stored_type: str) -> tuple[dict, str]:
    """Deterministic classification of a document's readable text (causal.derive_trigger) and the event type it gives
    an event of `stored_type` (causal.classified_type). Causal rules unavailable: unclassified, never an error."""
    try:
        from backend import causal
        derived = causal.derive_trigger(readable(title), readable(content), source_id,
                                        tier=tier if isinstance(tier, int) else None)
        return derived, causal.classified_type(stored_type, derived) or stored_type
    except Exception:  # a malformed causal graph must not block ingestion or a read
        return dict(UNCLASSIFIED), stored_type


def document_assets(assets: list[str], affected: list[str]) -> list[str]:
    """The assets a stored event had from its documents, without the causal reach merged in at creation: creation wrote
    the documents' assets first, then the reach in configuration order minus those already listed, so the documents'
    assets are the shortest prefix that reproduces the rest. When the list was edited later (assets accepted from the
    extractor), every asset outside the reach is kept."""
    for cut in range(len(assets) + 1):
        if assets[cut:] == [a for a in affected if a not in assets[:cut]]:
            return assets[:cut]
    return [a for a in assets if a not in affected]


class Ingestion:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self._features: dict[str, dict] = {}  # event id -> dedupe features of its (immutable) summary headline
        self._legacy: dict[str, dict] = {}  # event id -> its classification under the current rules (_derived)

    def _event_features(self, event: Event) -> dict:
        cached = self._features.get(event.id)
        if cached is None:
            cached = self._features[event.id] = dedupe.features(readable(event.summary_fact))
        return cached

    def ingest_quote(self, quote: Quote) -> bool:
        """Newest wins per symbol within its own kind: a real quote is compared only with the latest real tick, so a
        development-stub tick stamped 'now' never hides a real daily close or last trade, and a stub only with the latest
        stub tick. A stub never replaces or edits a real tick; which one is displayed is HubService.quote's choice."""
        now = utcnow()
        stamp = quote.timestamp.astimezone(UTC)  # stored as UTC whatever offset the provider used
        if stamp > now + timedelta(seconds=self.settings.max_future_seconds):
            raise ValueError('Future market timestamp')
        if quote.symbol not in {a[0] for a in rules()['assets']}:
            raise ValueError('Unknown asset')
        suffix = rules()['market_data']['exclude_provider_suffix']
        stub = quote.stub or quote.provider.endswith(suffix)
        is_stub_tick = Tick.provider.endswith(suffix, autoescape=True)
        with self.store.session() as session:
            previous = session.scalar(select(Tick).where(Tick.symbol == quote.symbol,
                                                         is_stub_tick if stub else ~is_stub_tick)
                                      .order_by(Tick.timestamp.desc()).limit(1))
            if previous and aware(previous.timestamp) == stamp and previous.price == quote.price \
                    and previous.provider == quote.provider:
                # The same observation re-published: fill the fields the stored copy lacks (e.g. a daily close stored
                # before the provider emitted previous_close) and take the provider's current label and close
                # classification of it (RESTATED_QUOTE_FIELDS), so no stored tick keeps a label its session has
                # outlived. The price, time and every other value are never changed.
                incoming = quote.model_dump(mode='json')
                updates = {k: v for k, v in incoming.items() if v is not None and previous.payload.get(k) is None}
                updates.update({k: incoming[k] for k in RESTATED_QUOTE_FIELDS
                                if incoming.get(k) is not None and incoming[k] != previous.payload.get(k)})
                if updates:
                    previous.payload = dict(previous.payload, **updates)
                    session.commit()
                return False
            if previous and aware(previous.timestamp) >= stamp:
                return False
            session.add(Tick(symbol=quote.symbol, price=quote.price, timestamp=stamp,
                             provider=quote.provider, payload=quote.model_dump(mode='json')))
            session.commit()
        return True

    @staticmethod
    def _other_pages(session, event_ids: list[str], source_id: str, canonical: str) -> set[str]:
        """Ids of the events that hold a document of `source_id` at a page other than `canonical`'s."""
        if not event_ids:
            return set()
        page = page_key(canonical)
        rows = session.execute(select(Evidence.event_id, Document.url).join(Document, Evidence.document_id == Document.id)
                               .where(Evidence.event_id.in_(event_ids), Document.source_id == source_id))
        return {event_id for event_id, url in rows if page_key(url) != page}

    def _match_event(self, session, document: DocumentInput, canonical: str, provenance: str, now,
                     source: dict) -> tuple[Event | None, dict]:
        """Same story -> same event: canonical URL first (http/https and www. spellings of it are the same page), then
        lexical/entity/time similarity (backend.dedupe). The page matches a document ingested inside the candidate
        window or published within headline_time_window_hours of this one (a page fetched again days later with a
        retouched text is still that publication); a generic URL (dedupe.generic_url: a quote page many headlines link
        to) never matches. An event stored before events carried a provenance has legacy_provenance. An event that
        already holds another page of the document's own official source is joined only under an identical headline
        (dedupe official_pages_distinct)."""
        config = rules()['dedupe']
        legacy = config.get('legacy_provenance')
        window = now - timedelta(hours=config['candidate_window_hours'])
        if not dedupe.generic_url(canonical):
            span = timedelta(hours=config['headline_time_window_hours'])
            published = document.published_at.astimezone(UTC)
            same_url = session.scalars(
                select(Evidence).join(Document, Evidence.document_id == Document.id)
                .where(Document.url.in_(url_variants(canonical)),
                       or_(Document.created_at >= window, Document.published_at.between(published - span, published + span)))
                .order_by(Document.created_at, Document.id)).all()
            for evidence in same_url:
                event = session.get(Event, evidence.event_id)
                if event and (event.payload.get('provenance') or legacy) == provenance:
                    return event, {'match': True, 'basis': 'canonical_url'}
        candidates = [e for e in session.scalars(select(Event).where(Event.created_at >= window))
                      if (e.payload.get('provenance') or legacy) == provenance]
        features = dedupe.features(readable(document.title), readable(document.content))
        own = self._other_pages(session, [e.id for e in candidates], document.source_id, canonical) \
            if distinct_pages(source) else set()
        event_id, signals = dedupe.best_match(features, document.published_at, [
            (e.id, self._event_features(e), aware(e.payload.get('event_time') or e.created_at)) for e in candidates], own)
        return (next(e for e in candidates if e.id == event_id), dict(signals, basis='similarity')) if event_id else (None, signals)

    def _new_event_payload(self, session, document: DocumentInput, source: dict, provenance: str, now,
                           injection: list[str], derived: dict | None = None) -> dict:
        """Deterministic event fields at creation; the AI may later propose, never impose, type/assets. `derived` is
        the classification of the document's readable text (classify). Novelty is measured against the newest
        novelty_scan_limit events of the look-back window before `now`. A document whose source publishes a date and
        no clock time (discovery.date_only_day) gives the event time_known false and its event_date."""
        if derived is None:
            derived, _ = classify(document.title, document.content, document.source_id, source.get('tier'),
                                  document.event_type)
        config = rules()['dedupe']
        features = dedupe.features(readable(document.title), readable(document.content))
        lookback = now - timedelta(hours=config['novelty_lookback_hours'])
        recent = [self._event_features(e) for e in session.scalars(
            select(Event).where(Event.created_at >= lookback, Event.created_at <= now)
            .order_by(Event.created_at.desc()).limit(config.get('novelty_scan_limit')))]
        tier_weight = {1: 1.0, 2: .9, 3: .7, 4: .5, 5: .2}.get(int(source.get('tier', 5)), .2)
        entities = sorted(features['entities'])[:20]
        # topic_basis: 'rule' = a configured causal/source rule named the topic; 'entities' = two extracted entities
        # joined (a heuristic, not a curated title; the UI then shows the headline); None = no topic.
        if derived.get('topic'):
            topic, topic_basis = derived['topic'], 'rule'
        elif entities:
            topic, topic_basis = '_'.join(e.upper() for e in entities[:2]), 'entities'
        else:
            topic, topic_basis = None, None
        reach = derived.get('affected_assets') or []
        published = document.published_at.astimezone(UTC)
        day = date_only_day(document.source_id, published)
        payload = {'assets': list(dict.fromkeys([*document.assets, *(derived.get('assets') or []), *reach])),
                   'affected_assets': reach, 'document_assets': list(document.assets),
                   'event_time': published.isoformat(), 'time_known': day is None, 'interpretation': None,
                   'novelty': dedupe.novelty(features, recent), 'importance': tier_weight, 'importance_basis': 'source tier',
                   'topic': topic, 'topic_basis': topic_basis, 'entities': entities, 'trigger': derived.get('trigger'),
                   'trigger_basis': derived.get('trigger_basis', 'none'), 'provenance': provenance,
                   'provider_event_type': document.event_type,
                   'injection_suspected': bool(injection), 'injection_findings': injection}
        if day:
            payload['event_date'] = day
        return payload

    def ingest_document(self, document: DocumentInput, *, trusted_transport: bool = False) -> dict:
        now = utcnow()
        published = document.published_at.astimezone(UTC)
        if published > now + timedelta(seconds=self.settings.max_future_seconds):
            raise ValueError('Future document timestamp')
        sources = {s['id']: s for s in self.store.list('sources')}
        source = sources.get(document.source_id)
        if not source:
            raise ValueError('Unknown source')
        host = urlsplit(str(document.url)).hostname or ''
        if host != source['domain'] and not host.endswith('.' + source['domain']):
            raise ValueError('Source domain mismatch')
        truncated = len(document.content) > self.settings.max_document_chars
        if truncated:  # evidence keeps the start of the text; the full page stays at its URL
            document = document.model_copy(update={'content': document.content[:self.settings.max_document_chars]})
        canonical = normalize_url(str(document.url))
        provenance = 'provider_retrieval' if trusted_transport else 'manual_import'

        def fingerprint_of(url: str) -> str:
            return hashlib.sha256((provenance + '\n' + document.source_id + '\n' + url + '\n' + document.title + '\n'
                                   + document.content).encode()).hexdigest()
        fingerprint = fingerprint_of(canonical)
        # The same text already stored under another spelling of the URL (http vs https, www.) is the same document.
        known = [fingerprint_of(url) for url in url_variants(canonical)]
        # Instruction-like text is kept verbatim as evidence but flagged; it never reaches an LLM unsanitised.
        _, injection = screen_text(f'{document.title}\n{document.content}')
        with self.store.session() as session:
            existing = session.scalar(select(Document).where(Document.fingerprint.in_(known)).limit(1)) \
                or self._stored_copy(session, document, canonical, provenance)
            if existing:
                evidence = session.scalar(select(Evidence).where(Evidence.document_id == existing.id))
                return self.event(evidence.event_id) if evidence else {}
            event = session.get(Event, document.event_id) if document.event_id else None
            if document.event_id and not event:
                raise ValueError('Unknown target event')
            match = {'match': False, 'basis': 'explicit_event'} if event else {}
            if not event:
                event, match = self._match_event(session, document, canonical, provenance, now, source)
            syndicated_from = None
            if event:
                others = session.execute(select(Document).join(Evidence, Evidence.document_id == Document.id)
                                         .where(Evidence.event_id == event.id, Document.source_id != document.source_id)).scalars()
                body = dedupe.vector(document.content[:rules()['dedupe']['content_chars']])
                syndicated_from = dedupe.syndicated(body, [(d.source_id, dedupe.vector(d.content[:rules()['dedupe']['content_chars']]))
                                                           for d in others])
                if injection and not event.payload.get('injection_suspected'):
                    event.payload = dict(event.payload, injection_suspected=True,
                                         injection_findings=list(dict.fromkeys(event.payload.get('injection_findings', []) + injection)))
            else:
                # created_at is the ingestion start like updated_at, so 'updated' never precedes 'first seen'.
                # The type is the classification's when the provider's only says how the document arrived (a Fed feed
                # item is CENTRAL_BANK, a BLS one MACRO), so the verification window documented per type applies.
                derived, event_type = classify(document.title, document.content, document.source_id, source.get('tier'),
                                               document.event_type)
                event = Event(id=uid(), summary_fact=document.title, event_type=event_type, created_at=now,
                              updated_at=now, payload=self._new_event_payload(session, document, source, provenance, now,
                                                                              injection, derived))
                session.add(event)
            payload = {'direct': document.direct and not syndicated_from,
                       'discovery_only': document.discovery_only or not trusted_transport, 'provenance': provenance,
                       'dedupe': match or {'match': False, 'basis': 'new_event'}, 'injection_suspected': bool(injection),
                       'truncated': truncated}
            day = date_only_day(document.source_id, published)
            if day:  # the source gives a date and no clock time: never show the start-of-day stamp as a time
                payload.update(time_known=False, published_date=day)
            if syndicated_from:
                # A republished copy is second-hand: it can never count as an independent confirmation.
                payload.update(syndicated_from=syndicated_from, origin=syndicated_from)
            raw = Document(id=uid(), source_id=document.source_id, fingerprint=fingerprint, title=document.title,
                           content=document.content, url=canonical, published_at=published, payload=payload)
            session.add(raw)
            session.flush()
            session.add(Evidence(event_id=event.id, document_id=raw.id, relation=document.relation))
            event.updated_at = now
            session.commit()
            event_id = event.id
        self.verify(event_id)
        return self.event(event_id)

    @staticmethod
    def _stored_copy(session, document: DocumentInput, canonical: str, provenance: str) -> Document | None:
        """The document already stored for this page (any http/https or www. spelling, same source and provenance)
        whose text reads the same (readable): a feed item stored as raw HTML before feeds were cleaned, or an adapter
        record stored under its earlier wording, is the same document when it is fetched again, not a revision."""
        title, content = readable(document.title), readable(document.content)
        rows = session.scalars(select(Document).where(Document.source_id == document.source_id,
                                                      Document.url.in_(url_variants(canonical))))
        return next((d for d in rows if (d.payload or {}).get('provenance', provenance) == provenance
                     and readable(d.title) == title and readable(d.content) == content), None)

    def source_context(self) -> dict:
        """Registry, admin overrides and weights, loaded once per request instead of once per event."""
        config = rules()
        return {'sources': {s['id']: s for s in self.store.list('sources')},
                'overrides': {r['id']: r for r in self.store.list('source_reliability')},
                'weights': config['source_weights'], 'unknown': config['verification']['unknown_kind_weight']}

    def evidence(self, event_id: str, as_of=None, context: dict | None = None) -> list[dict]:
        """Evidence rows, earliest ingested first; with as_of only documents ingested by then (no lookahead in replays).
        Trust weights come from versioned config; an administrator override (source_reliability) wins and is audited.
        One page is one document: copies stored before the http/https and www. spellings of a URL were recognised as
        the same page (the same source, headline, text and relation under another spelling, engines.page_key) are
        listed once, as the earliest copy with the others' ids in 'duplicate_document_ids', so no count shows them
        twice. Documents whose text differs (a revised page) stay separate. Title and excerpt are readable text.
        time_known is false, with published_date, for a document whose source publishes a date and no clock time
        (discovery.date_only_day): published_at is then the start of that day, kept for ordering and ageing only."""
        context = context or self.source_context()
        source_map, overrides = context['sources'], context['overrides']
        weights, unknown = context['weights'], context['unknown']
        with self.store.session() as session:
            query = (select(Evidence, Document).join(Document, Evidence.document_id == Document.id)
                     .where(Evidence.event_id == event_id).order_by(Document.created_at, Document.id))
            if as_of is not None:
                query = query.where(Document.created_at <= aware(as_of))
            result = []
            copies: dict[tuple, dict] = {}
            for evidence, doc in session.execute(query).all():
                page = (doc.source_id, page_key(doc.url), evidence.relation, doc.title,
                        hashlib.sha256(doc.content.encode()).hexdigest())
                if page in copies:
                    copies[page].setdefault('duplicate_document_ids', []).append(doc.id)
                    continue
                source = dict(source_map.get(doc.source_id, {}), **{k: v for k, v in overrides.get(doc.source_id, {}).items()
                                                                  if k in {'tier', 'trust_weight', 'enabled'}})
                weight = source.get('trust_weight', weights.get(source.get('kind'), unknown))
                if source.get('enabled') is False:
                    weight = 0.0
                copies[page] = {'id': evidence.id, 'document_id': doc.id, 'source_id': doc.source_id,
                                'source': source.get('name', doc.source_id), 'url': doc.url,
                                'title': readable(doc.title), 'excerpt': readable(doc.content)[:EXCERPT_CHARS],
                                'published_at': iso(doc.published_at),
                                'ingested_at': iso(doc.created_at), 'relation': evidence.relation,
                                'group': source.get('independence_group', doc.source_id), 'weight': weight,
                                'kind': source.get('kind'), 'tier': source.get('tier'),
                                'official': source.get('official', False), **doc.payload}
                day = date_only_day(doc.source_id, doc.published_at)
                if day:
                    copies[page].update(time_known=False, published_date=day)
                copies[page].setdefault('time_known', True)
                result.append(copies[page])
            return result

    def verify(self, event_id: str) -> dict:
        with self.store.session() as session:
            row = session.get(Event, event_id)
            event_type = self._typed(row, self._derived(session, row)) if row else None
        verified = confidence(self.evidence(event_id), utcnow(), event_type=event_type)
        with self.store.session() as session:
            event = session.get(Event, event_id)
            if event:
                event.verification_status, event.fact_confidence = verified['status'], verified['confidence']
                session.commit()
        return verified

    # --- start-up repair: one publication stored twice, and events merged under earlier dedupe rules -----------------

    def repair_stored_events(self, *, force: bool = False) -> dict:
        """Start-up repair of stored events (HubService.run_event_repair), once per dedupe_version (config dedupe
        repair_note); never raises, so it can never block start-up. Nothing is changed before the SQLite database is
        copied to '<database file>.pre-repair-<dedupe_version>' with the sqlite3 backup API; when that copy cannot be
        written, or the database is not a SQLite file, the repair is skipped, logged and tried again at the next start.
        Then: (1) events stored before events carried a provenance get legacy_provenance; (2) events that are one
        publication stored twice (_duplicate_groups) are merged into the oldest one (_merge_duplicates: evidence,
        reactions and references move, the merged id keeps opening the kept event); (3) events merged under earlier
        rules are re-split (split_merged_events). Old and new ids go to audit_logs (action events_repair). A second
        run does nothing (the dedupe_version is recorded; force runs again and finds nothing left). Returns the
        summary ('status': nothing_to_repair, repaired, skipped or failed)."""
        try:
            return self._repair_stored_events(force)
        except Exception as exc:  # a failed repair leaves the events as they were; the next start tries again
            logger.exception('events_repair_failed', extra={'error_type': type(exc).__name__})
            return {'status': 'failed', 'error_type': type(exc).__name__}

    def _repair_stored_events(self, force: bool) -> dict:
        config = rules()['dedupe']
        version = config['dedupe_version']
        done = self.store.get('configuration_versions', REPAIR_KEY) or {}
        if done.get('dedupe_version') == version and not force:
            return dict(done, skipped=True)
        now = utcnow()
        with self.store.session() as session:
            groups, legacy = self._duplicate_groups(session)
        to_split = self.split_merged_events(dry_run=True)['events_split']
        summary: dict[str, Any] = {'dedupe_version': version, 'applied_at': now.isoformat(), 'status': 'nothing_to_repair',
                                   'backup': None, 'provenance_filled': 0, 'duplicate_groups': 0, 'events_merged': 0,
                                   'documents_moved_by_merge': 0, 'events_split': 0, 'events_created': 0,
                                   'documents_moved_by_split': 0}
        if not groups and not legacy and not to_split:
            self.store.put('configuration_versions', REPAIR_KEY, summary)
            return summary
        with self.store.session() as session:
            summary['events_before'] = session.scalar(select(func.count(Event.id)))
        backup = self._backup_database(version)
        if backup is None:
            logger.warning('events_repair_skipped', extra={'reason': 'no SQLite backup could be written',
                                                           'dedupe_version': version})
            return {'status': 'skipped', 'reason': 'no SQLite backup could be written', 'dedupe_version': version}
        merged: list[dict] = []
        with self.store.session() as session:
            for event in session.scalars(select(Event).where(Event.id.in_(legacy))):
                event.payload = dict(event.payload, provenance=config.get('legacy_provenance'))
            session.commit()
            for ids in groups:
                merged.append(self._merge_duplicates(session, ids, now))
                session.commit()
        for item in merged:
            self._legacy.pop(item['kept'], None)
            self.verify(item['kept'])
        split = self.split_merged_events(force=True)
        with self.store.session() as session:
            events_after = session.scalar(select(func.count(Event.id)))
        summary.update(status='repaired', backup=backup, provenance_filled=len(legacy), duplicate_groups=len(merged),
                       events_merged=sum(len(m['merged']) for m in merged),
                       documents_moved_by_merge=sum(len(m['documents_moved']) for m in merged),
                       events_split=split['events_split'], events_created=split['events_created'],
                       documents_moved_by_split=split['documents_moved'], events_after=events_after)
        self.store.put('configuration_versions', REPAIR_KEY, summary)
        self.store.put('audit_logs', uid(), dict(summary, action='events_repair', timestamp=now.isoformat(),
                                                 provenance_filled_event_ids=legacy, merged=merged, split=split['plan']))
        logger.info('events_repaired', extra={k: summary[k] for k in ('dedupe_version', 'events_merged', 'events_created')})
        return summary

    def _backup_database(self, version: str) -> str | None:
        """Copies the SQLite database file to '<file>.pre-repair-<version>' with the sqlite3 backup API (a consistent
        copy even with a write-ahead log) and returns the copy's path; None when the database is not a SQLite file or
        the copy fails. A copy left by an earlier, interrupted repair of this version is the older state: it is kept."""
        url = self.store.engine.url
        path = url.database
        if url.get_backend_name() != 'sqlite' or not path or path == ':memory:' or not os.path.isfile(path):
            return None
        target = f'{path}.pre-repair-{version}'
        if os.path.isfile(target):
            return target
        partial = f'{target}.partial'
        try:
            source = sqlite3.connect(path)
            try:
                copy = sqlite3.connect(partial)
                try:
                    source.backup(copy)
                    copied = copy.execute('select count(*) from events').fetchone()[0]
                finally:
                    copy.close()
                stored = source.execute('select count(*) from events').fetchone()[0]
            finally:
                source.close()
            if copied != stored:
                raise sqlite3.DatabaseError('backup is not a complete copy')
            os.replace(partial, target)
            return target
        except (sqlite3.Error, OSError) as exc:
            logger.warning('events_repair_backup_failed', extra={'error_type': type(exc).__name__})
            try:
                os.remove(partial)
            except OSError:
                pass
            return None

    def _duplicate_groups(self, session) -> tuple[list[list[str]], list[str]]:
        """(groups of event ids that are one publication stored twice, oldest first; ids of the events stored without
        a provenance). Two events are one publication when the documents that created them come from the same source
        with the same provenance (an event without one has legacy_provenance), were published within
        headline_time_window_hours of each other, and are the same page (any spelling of the URL, never a generic
        one) or read the same (readable title and text). An identical text further apart (a weekly table) or another
        headline at a shared quote page is another publication."""
        config = rules()['dedupe']
        default, span = config.get('legacy_provenance'), timedelta(hours=config['headline_time_window_hours'])
        events = {e.id: e for e in session.scalars(select(Event))}
        legacy = sorted(e.id for e in events.values() if not e.payload.get('provenance'))
        buckets: dict[tuple, list[tuple[datetime, str]]] = {}
        seen: set[str] = set()
        rows = session.execute(select(Evidence.event_id, Document.source_id, Document.url, Document.title, Document.content,
                                      Document.published_at, Document.payload)
                               .join(Document, Evidence.document_id == Document.id).order_by(Document.created_at, Document.id))
        for event_id, source_id, url, title, content, published, payload in rows:
            if event_id in seen or event_id not in events:
                continue
            seen.add(event_id)  # the earliest document of an event is the one that created it
            provenance = events[event_id].payload.get('provenance') or (payload or {}).get('provenance') or default
            stamp = aware(published)
            if not dedupe.generic_url(url):
                buckets.setdefault(('page', provenance, source_id, page_key(url)), []).append((stamp, event_id))
            text = hashlib.sha256(f'{readable(title)}\n{readable(content)}'.encode()).hexdigest()
            buckets.setdefault(('text', provenance, source_id, text), []).append((stamp, event_id))
        parent: dict[str, str] = {}

        def root(item: str) -> str:
            while parent.get(item, item) != item:
                item = parent[item]
            return item
        for members in buckets.values():
            members.sort()
            for (earlier, first), (later, second) in zip(members, members[1:], strict=False):
                if later - earlier <= span and root(first) != root(second):
                    parent[root(second)] = root(first)
        clusters: dict[str, list[str]] = {}
        for event_id in seen:
            clusters.setdefault(root(event_id), []).append(event_id)
        groups = [sorted(ids, key=lambda i: (aware(events[i].created_at), i)) for ids in clusters.values() if len(ids) > 1]
        return sorted(groups, key=lambda ids: (aware(events[ids[0]].created_at), ids[0])), legacy

    def _merge_duplicates(self, session, ids: list[str], now: datetime) -> dict:
        """Merges the later events of one duplicate group into the oldest (ids[0]): their evidence moves to it; their
        reaction, basket and completion rows are re-keyed to it (a row the kept event already has stays as it is and
        the duplicate's row is kept under 'merged:<old key>', so no measurement is lost); payload.event_id references
        (REFERENCE_TABLES) follow; an alias row ('merged-event:<old id>') lets every other stored reference to the old
        id open the kept event; fields the kept event lacks are taken from the duplicate; the duplicate row is removed.
        Returns the audit entry."""
        relationships: Any = RECORDS['event_relationships']
        keep, *others = [session.get(Event, event_id) for event_id in ids]
        payload = dict(keep.payload)
        existing = set(session.scalars(select(relationships.id).where(or_(
            relationships.id.startswith(f'reaction:{keep.id}:', autoescape=True),
            relationships.id.startswith(f'basket:{keep.id}:', autoescape=True),
            relationships.id == f'reactions-complete:{keep.id}'))))
        entry: dict[str, Any] = {'kept': keep.id, 'summary': readable(keep.summary_fact)[:200], 'merged': [],
                                 'documents_moved': [], 'reaction_rows_moved': 0, 'reaction_rows_archived': 0,
                                 'references_updated': 0}
        for other in others:
            for evidence, doc in session.execute(select(Evidence, Document).join(Document, Evidence.document_id == Document.id)
                                                 .where(Evidence.event_id == other.id)).all():
                evidence.event_id = keep.id
                doc.payload = dict(doc.payload or {}, dedupe={'match': True, 'basis': 'merged_duplicate_event',
                                                              'merged_from': other.id,
                                                              'previous': (doc.payload or {}).get('dedupe')})
                entry['documents_moved'].append(doc.id)
            rows = session.scalars(select(relationships).where(or_(
                relationships.id.startswith(f'reaction:{other.id}:', autoescape=True),
                relationships.id.startswith(f'basket:{other.id}:', autoescape=True),
                relationships.id == f'reactions-complete:{other.id}'))).all()
            for row in rows:
                target = row.id.replace(other.id, keep.id, 1)
                if target in existing:
                    moved = relationships(id=MERGED_ROW + row.id, created_at=row.created_at,
                                          payload=dict(row.payload, merged_into=keep.id))
                    entry['reaction_rows_archived'] += 1
                else:
                    moved = relationships(id=target, created_at=row.created_at,
                                          payload=dict(row.payload, event_id=keep.id, merged_from_event=other.id))
                    existing.add(target)
                    entry['reaction_rows_moved'] += 1
                session.delete(row)
                session.add(moved)
            for table in REFERENCE_TABLES:
                cls: Any = RECORDS[table]
                for row in session.scalars(select(cls).where(cls.payload['event_id'].as_string() == other.id)).all():
                    row.payload = dict(row.payload, event_id=keep.id)
                    entry['references_updated'] += 1
            session.add(relationships(id=MERGED_EVENT + other.id, payload={
                'event_id': keep.id, 'merged_event_id': other.id, 'merged_at': now.isoformat(),
                'summary': readable(other.summary_fact)[:200], 'first_seen_utc': iso(other.created_at)}))
            for key, value in other.payload.items():
                if payload.get(key) is None:
                    payload[key] = value
            for key in ('assets', 'ai_disputed_evidence', 'injection_findings'):
                if payload.get(key) or other.payload.get(key):
                    payload[key] = list(dict.fromkeys([*(payload.get(key) or []), *(other.payload.get(key) or [])]))
            payload['injection_suspected'] = bool(payload.get('injection_suspected') or other.payload.get('injection_suspected'))
            payload['merged_event_ids'] = [*payload.get('merged_event_ids', []), other.id,
                                           *other.payload.get('merged_event_ids', [])]
            keep.updated_at = max(aware(keep.updated_at), aware(other.updated_at))
            entry['merged'].append({'id': other.id, 'first_seen_utc': iso(other.created_at),
                                    'summary': readable(other.summary_fact)[:200]})
            session.flush()  # the evidence points at the kept event before the duplicate row goes
            session.delete(other)
            self._features.pop(other.id, None)
            self._legacy.pop(other.id, None)
        keep.payload = payload
        return entry

    def split_merged_events(self, *, dry_run: bool = False, force: bool = False) -> dict:
        """Re-split, once per dedupe_version, the events that earlier and looser dedupe rules merged: a monthly release
        joined to last month's, nine OFSI licences in one event, two Bank of Japan tables published together, unrelated
        headlines that all link to one stock's quote page. In each event with several documents, taken in ingestion
        order, a document that joined by similarity, or by a generic URL (dedupe.generic_url), stays when it still
        matches the event's headline under the current rules (dedupe.score, official_pages_distinct); otherwise
        it moves, with every other copy of its page, to a split-off event it matches or to a new event of its own,
        created as ingestion creates one from the document's readable text (its first-seen time is the document's
        ingestion time; split_from names the original event). Documents that joined by any other URL or were attached
        to the event explicitly stay. The AI-derived fields of an event that loses documents are reset (AI_FIELDS: they
        described the merged set). Verification is recomputed for every touched event and the moves are written to
        audit_logs with the new event ids. dry_run returns the plan without writing; force re-checks a dedupe_version
        already applied. Returns {'dedupe_version', 'events_checked', 'events_split', 'documents_moved',
        'events_created', 'plan'}, with 'skipped' when this version was applied."""
        version = rules()['dedupe']['dedupe_version']
        done = self.store.get('configuration_versions', RESPLIT_KEY) or {}
        if done.get('dedupe_version') == version and not (dry_run or force):
            return dict(done, skipped=True)
        sources = {s['id']: s for s in self.store.list('sources')}
        now = utcnow()
        plan: list[dict] = []
        touched: set[str] = set()
        with self.store.session() as session:
            merged = list(session.scalars(select(Evidence.event_id).group_by(Evidence.event_id)
                                          .having(func.count(Evidence.id) > 1).order_by(Evidence.event_id)))
            for event_id in merged:
                event = session.get(Event, event_id)
                groups = self._resplit_groups(session, event, sources) if event else []
                if not event or not groups:
                    continue
                split_off: list[dict] = [
                    {'title': readable(g['items'][0][1].title)[:200], 'published_at': iso(g['items'][0][1].published_at),
                     'document_ids': [doc.id for _, doc in g['items']]} for g in groups]
                plan.append({'event_id': event_id, 'summary': readable(event.summary_fact)[:200], 'split_off': split_off})
                if dry_run:
                    continue
                created = self._apply_resplit(session, event, groups, sources, now)
                session.commit()
                for group, new_id in zip(split_off, created, strict=True):
                    group['event_id'] = new_id
                touched.update([event_id, *created])
        for event_id in sorted(touched):
            self._legacy.pop(event_id, None)
            self.verify(event_id)
        summary = {'dedupe_version': version, 'applied_at': now.isoformat(), 'events_checked': len(merged),
                   'events_split': len(plan), 'events_created': sum(len(p['split_off']) for p in plan),
                   'documents_moved': sum(len(g['document_ids']) for p in plan for g in p['split_off'])}
        if not dry_run:
            self.store.put('configuration_versions', RESPLIT_KEY, summary)
            if plan:
                self.store.put('audit_logs', uid(), dict(summary, action='events_resplit', timestamp=now.isoformat(),
                                                         plan=plan))
        return dict(summary, plan=plan)

    def _resplit_groups(self, session, event: Event, sources: dict[str, dict]) -> list[dict]:
        """The split-off groups of one event: [{'features', 'time', 'pages', 'items': [(evidence, document), ...]}],
        empty when every document still belongs to the event. A page is (source, URL); at a generic URL each headline
        is its own page."""
        rows = session.execute(select(Evidence, Document).join(Document, Evidence.document_id == Document.id)
                               .where(Evidence.event_id == event.id).order_by(Document.created_at, Document.id)).all()
        original: dict = {'features': self._event_features(event), 'pages': set(), 'items': [],
                          'time': aware(event.payload.get('event_time') or event.created_at)}
        groups = [original]
        for index, (evidence, doc) in enumerate(rows):
            generic = dedupe.generic_url(doc.url)
            page = (doc.source_id, page_key(doc.url), headline_key(readable(doc.title)) if generic else '')
            home = next((g for g in groups if page in g['pages']), None)  # a copy of a page follows that page
            basis = ((doc.payload or {}).get('dedupe') or {}).get('basis')
            if home is None and index and (basis == 'similarity' or (generic and basis == 'canonical_url')):
                features = dedupe.features(readable(doc.title), readable(doc.content))
                official = distinct_pages(sources.get(doc.source_id, {}))
                best = -1.0
                for group in groups:  # the original event first: it wins a tie
                    other_page = official and any(s == doc.source_id and p != page[1] for s, p, _ in group['pages'])
                    result = dedupe.score(features, group['features'], doc.published_at, group['time'],
                                          same_source_page=other_page)
                    if result['match'] and result['lexical'] > best:
                        home, best = group, result['lexical']
                if home is None:
                    home = {'features': dedupe.features(readable(doc.title)), 'time': aware(doc.published_at),
                            'pages': set(), 'items': []}
                    groups.append(home)
            home = home or original
            home['pages'].add(page)
            home['items'].append((evidence, doc))
        return groups[1:]

    def _apply_resplit(self, session, event: Event, groups: list[dict], sources: dict[str, dict], now) -> list[str]:
        """Moves each split-off group to a new event built from the readable text of its first document; resets the
        AI-derived fields of the original event; returns the new event ids, one per group."""
        created: list[str] = []
        moved: set[str] = set()
        provider_type = event.payload.get('provider_event_type') or event.event_type
        for group in groups:
            first = group['items'][0][1]
            document = DocumentInput.model_construct(
                source_id=first.source_id, title=readable(first.title), content=readable(first.content), url=first.url,
                published_at=aware(first.published_at), event_type=provider_type, assets=[])
            source = sources.get(first.source_id, {})
            provenance = ((first.payload or {}).get('provenance') or event.payload.get('provenance')
                          or rules()['dedupe'].get('legacy_provenance') or 'provider_retrieval')
            _, injection = screen_text(f'{first.title}\n{first.content}')
            derived, event_type = classify(document.title, document.content, first.source_id, source.get('tier'), provider_type)
            payload = self._new_event_payload(session, document, source, provenance, aware(first.created_at), injection,
                                              derived)
            split = Event(id=uid(), summary_fact=document.title, event_type=event_type, created_at=first.created_at,
                          updated_at=now, payload=dict(payload, split_from=event.id))
            session.add(split)
            for index, (evidence, doc) in enumerate(group['items']):
                evidence.event_id = split.id
                moved.add(evidence.id)
                doc.payload = dict(doc.payload or {}, dedupe={'match': index > 0, 'basis': 'split_from_merged_event',
                                                              'split_from': event.id,
                                                              'previous': (doc.payload or {}).get('dedupe')})
            created.append(split.id)
        payload = dict(event.payload)
        if payload.get('ai_disputed_evidence'):
            payload['ai_disputed_evidence'] = [e for e in payload['ai_disputed_evidence'] if e not in moved]
        reset = [key for key in AI_FIELDS if payload.get(key) is not None]
        if reset:  # what an AI role wrote described the merged set of documents, not this event
            payload = {key: value for key, value in payload.items() if key not in AI_FIELDS}
            payload.update(interpretation=None, ai_reset={'at': now.isoformat(), 'reason': 'documents split off',
                                                          'fields': reset})
            if isinstance(payload.get('tier_weight'), (int, float)):
                payload.update(importance=payload['tier_weight'], importance_basis='source tier')
        event.payload = payload
        event.updated_at = now
        return created

    # --- reading ------------------------------------------------------------------------------------------------------

    def event(self, event_id: str, context: dict | None = None) -> dict:
        """One event with its evidence, verification and classification as the current rules read it (_classified,
        _typed). The id of a duplicate merged by the start-up repair opens the event that was kept. time_known (served
        to the web client as event_time_known too) is false, with event_date, when the creating document's source
        publishes a date and no clock time: event_time is then the start of that day (ordering and ageing only), never
        a time to show."""
        from backend import causal
        with self.store.session() as session:
            event = session.get(Event, event_id)
            if not event:
                alias: Any = session.get(RECORDS['event_relationships'], MERGED_EVENT + event_id)
                event = session.get(Event, alias.payload.get('event_id')) if alias else None
            if not event:
                raise ValueError('Event not found')
            evidence = self.evidence(event.id, context=context)
            derived = self._derived(session, event)
            event_type = self._typed(event, derived)
            verified = confidence(evidence, utcnow(), event_type=event_type)
            payload = self._classified(event, derived)
            day = date_only_day(evidence[0]['source_id'], payload.get('event_time')) if evidence else None
            if day:
                payload.update(time_known=False, event_date=day)
            payload.setdefault('time_known', True)
            payload['event_time_known'] = payload['time_known']  # the name the web client reads (lib/events.ts)
            # Rows stored before created_at was set explicitly got it at flush time, seconds after updated_at.
            updated = max(aware(event.updated_at), aware(event.created_at))
            return {'id': event.id, 'event_id': event.id, 'summary_fact': readable(event.summary_fact),
                    'event_type': event_type, 'verification_status': verified['status'],
                    'fact_confidence': verified['confidence'], 'first_seen_utc': iso(event.created_at),
                    'last_updated_utc': iso(updated), 'sources': sorted({e['source'] for e in evidence}),
                    # Evidence published further from event_time than this is another publication, never the same story.
                    'same_story_window_hours': rules_view()['dedupe']['headline_time_window_hours'],
                    'evidence': evidence, 'why_it_matters': causal.summary(payload.get('trigger')), **payload}

    def _derived(self, session, event: Event) -> dict:
        """Classification of a stored event under the current rules, from the same input as at ingestion (its headline
        plus the text of the document that created it, both readable). Cached per event and configuration content: the
        headline and the first document are fixed, the rules are not. {} when the causal rules cannot be loaded."""
        try:
            from backend import causal
            stamp = causal.settings()['content_hash']
        except Exception:  # causal rules unavailable: leave the event as stored rather than fail the read
            return {}
        cached = self._legacy.get(event.id)
        if cached is None or cached.get('config') != stamp:
            first = session.execute(select(Document.content, Document.source_id)
                                    .join(Evidence, Evidence.document_id == Document.id)
                                    .where(Evidence.event_id == event.id)
                                    .order_by(Document.created_at, Document.id).limit(1)).first()
            # An event without a document has no source to justify a trigger: it stays unclassified.
            derived = dict(UNCLASSIFIED)
            if first:
                derived, _ = classify(event.summary_fact, first.content, first.source_id, None, event.event_type)
            cached = self._legacy[event.id] = {'config': stamp, 'derived': derived}
        return cached['derived']

    @staticmethod
    def _typed(event: Event, derived: dict) -> str:
        """The event type as read: the classification's when the stored one only says how the document arrived
        (causal.classified_type), so the verification window documented per type applies to stored events too."""
        if not derived:
            return event.event_type
        from backend import causal
        return causal.classified_type(event.event_type, derived) or event.event_type

    def _classified(self, event: Event, derived: dict) -> dict:
        """The event payload as the current rules read it. Read-only: the stored row is never rewritten.

        A deterministic classification (no 'trigger' key: stored before classification existed; trigger_basis 'rule' or
        'none': derived by the keyword and source rules at ingestion) is re-derived with the current rules (_derived).
        When the trigger differs from the stored one (a legacy event a rule now matches, or a stored trigger the rules
        no longer justify, e.g. a keyword hit on the publisher's own name), the payload carries the current trigger,
        its causal reach in affected_assets, 'assets' rebuilt as the documents' own assets plus the rule's linked assets
        and reach, the topic of the current classification, stored_trigger (when there was one) and classified_on_read:
        reactions measured before were graded against another expectation, or none. A trigger with any other basis is
        kept. A topic stored without topic_basis gets one ('rule' for a configured rule/source topic, else 'entities')."""
        payload = dict(event.payload)
        legacy = 'trigger' not in payload
        stored = payload.get('trigger')
        if derived and (legacy or payload.get('trigger_basis') in (None, 'none', 'rule')):
            current = derived['trigger']
            if legacy or current != stored:
                reach = payload.get('affected_assets') or []
                own = payload['document_assets'] if 'document_assets' in payload \
                    else document_assets(list(payload.get('assets') or []), list(reach))
                payload.update(trigger=current, trigger_basis=derived['trigger_basis'],
                               affected_assets=derived['affected_assets'],
                               assets=list(dict.fromkeys([*own, *derived['assets'], *derived['affected_assets']])))
                if current is not None or stored is not None:  # only a changed classification un-grades reactions
                    payload['classified_on_read'] = True
                if not legacy:
                    payload['stored_trigger'] = stored
                if derived.get('topic') and (not payload.get('topic') or not legacy):
                    payload.update(topic=derived['topic'], topic_basis='rule')
                elif not legacy and payload.get('topic_basis') == 'rule':  # the rule that named the topic is gone
                    entities = payload.get('entities') or []
                    payload.update(topic='_'.join(str(e).upper() for e in entities[:2]) or None,
                                   topic_basis='entities' if entities else None)
            elif current is None and derived['assets'] and 'document_assets' not in payload:
                # Stored before neutral rules linked assets: the same classification, now with its linked assets.
                payload['assets'] = list(dict.fromkeys([*(payload.get('assets') or []), *derived['assets']]))
                if derived.get('topic') and payload.get('topic_basis') != 'rule':
                    payload.update(topic=derived['topic'], topic_basis='rule')
        if payload.get('topic') and 'topic_basis' not in payload:
            from backend import causal
            config = causal.settings()['causal_triggers']
            topics = {r['topic'] for r in config['keyword_rules']} | {s['topic'] for s in config['source_event_types'].values()}
            payload['topic_basis'] = 'rule' if payload['topic'] in topics else 'entities'
        return payload

    def events(self, limit: int = 100) -> list[dict]:
        with self.store.session() as session:
            ids = list(session.scalars(select(Event.id).order_by(Event.created_at.desc()).limit(limit)))
        context = self.source_context()
        events = [self.event(event_id, context) for event_id in ids]
        # Newest publication first: a backfilled old release must not look like breaking news.
        return sorted(events, key=lambda e: e.get('event_time') or e['first_seen_utc'], reverse=True)
