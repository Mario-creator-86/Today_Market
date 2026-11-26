"""Macro release history for surprise z-scores (spec §16) without lookahead (spec §38).

Each release is stored once per (country, indicator, scheduled time) in macro_releases. The first time an
actual value becomes known is recorded as actual_ingested_at and never overwritten, so a replay at time T
only sees surprises that were known at T."""
import hashlib
import re
from datetime import datetime

from backend.database import Store, utcnow
from backend.engines import aware, surprise

NOISE = re.compile(r'\b(yoy|mom|qoq|y/y|m/m|q/q|sa|nsa|prel|prelim|preliminary|final|flash|adv|advance)\b|\(.*?\)', re.IGNORECASE)


def indicator_key(country: str | None, name: str) -> str:
    """Stable indicator identity across months: 'US CPI YoY (Aug)' and 'US CPI YoY (Sep)' share a key."""
    months = r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b'
    base = re.sub(months, ' ', name, flags=re.IGNORECASE)
    measure = 'yoy' if re.search(r'\byoy\b|y/y', name, re.IGNORECASE) else 'mom' if re.search(r'\bmom\b|m/m', name, re.IGNORECASE) else 'level'
    base = re.sub(r'\W+', ' ', NOISE.sub(' ', base)).strip().casefold()
    return f"{(country or 'XX').upper()}:{base}:{measure}"


def record_release(store: Store, row: dict, now: datetime | None = None, *, known_at: datetime | str | None = None) -> dict:
    """Upsert one calendar row into the release history; keeps the first-known time of the actual value (`known_at`
    when the caller knows it: an outcome read from the official series was known when the app first stored that
    period, backend/releases.py). A row with an attached outcome (release_outcome) records it: the text, the period,
    every metric and the series provider as values_source. Without a numeric consensus no surprise is recorded."""
    now = aware(now or utcnow())
    indicator = indicator_key(row.get('country'), row['name'])
    key = hashlib.sha256(f"{indicator}:{row['scheduled_at']}".encode()).hexdigest()
    previous = store.get('macro_releases', key) or {}
    actual, consensus = row.get('actual'), row.get('consensus')
    record = dict(previous, indicator=indicator, name=row['name'], country=row.get('country'),
                  scheduled_at=aware(row['scheduled_at']).isoformat(), consensus=consensus, previous=row.get('previous'),
                  actual=actual, unit=row.get('unit'), provider=row.get('provider'), updated_at=now.isoformat())
    if isinstance(actual, (int, float)) and not previous.get('actual_ingested_at'):
        record['actual_ingested_at'] = aware(known_at).isoformat() if known_at else now.isoformat()
    if isinstance(actual, (int, float)) and isinstance(consensus, (int, float)):
        record['raw_surprise'] = actual - consensus
    found = row.get('release_outcome')
    if isinstance(found, dict) and found.get('text'):
        record.update(values_source=row.get('values_source') or found.get('values_source'), event_class=row.get('event_class'),
                      row_id=row.get('id'), outcome={k: found.get(k) for k in ('text', 'period', 'headline', 'metrics', 'series')})
    store.put('macro_releases', key, record)
    return record


def surprise_history(store: Store, indicator: str, before: datetime, known_at: datetime) -> list[float]:
    """Past surprises of one indicator released before `before` and known at `known_at` (no lookahead)."""
    rows = []
    for row in store.list('macro_releases', 5000):
        if row.get('indicator') != indicator or not isinstance(row.get('raw_surprise'), (int, float)):
            continue
        if aware(row['scheduled_at']) < aware(before) and row.get('actual_ingested_at') and aware(row['actual_ingested_at']) <= aware(known_at):
            rows.append((row['scheduled_at'], row['raw_surprise']))
    return [value for _, value in sorted(rows)]


def release_surprise(store: Store, row: dict, now: datetime | None = None) -> dict | None:
    """Raw surprise and z-score (std-dev of the indicator's own historical surprises) for a released row."""
    actual, consensus = row.get('actual'), row.get('consensus')
    if not isinstance(actual, (int, float)) or not isinstance(consensus, (int, float)):
        return None
    now = aware(now or utcnow())
    history = surprise_history(store, indicator_key(row.get('country'), row['name']), aware(row['scheduled_at']), now)
    return dict(surprise(actual, consensus, history), indicator=indicator_key(row.get('country'), row['name']))
