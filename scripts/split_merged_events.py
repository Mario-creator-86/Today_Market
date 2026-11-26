"""Re-split events that earlier dedupe rules merged (Ingestion.split_merged_events; config dedupe).

A monthly release joined to last month's, several OFSI licences in one event or two Bank of Japan tables published
together become separate events, each with its own documents and verification. The application runs this itself at
start-up, once per dedupe_version, as part of Ingestion.repair_stored_events (behind a backup of the database file);
this script is for a dry run or a manual re-check, with the application stopped. Recorded in audit_logs.

    .venv/Scripts/python scripts/split_merged_events.py --dry-run   # list what would move, change nothing
    .venv/Scripts/python scripts/split_merged_events.py             # apply
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import get_settings  # noqa: E402
from backend.database import Store  # noqa: E402
from backend.ingestion import Ingestion  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dry-run', action='store_true', help='list the events that would be split; change nothing')
    parser.add_argument('--force', action='store_true', help='check again although this dedupe version was applied')
    args = parser.parse_args()
    settings = get_settings()
    store = Store(settings.database_url)
    store.initialize()
    result = Ingestion(store, settings).split_merged_events(dry_run=args.dry_run, force=args.force)
    if result.get('skipped'):
        print(f"Already applied for dedupe rules {result['dedupe_version']}; nothing to do (use --force to check again).")
        return 0
    for item in result['plan']:
        print(f"{item['summary']}")
        for group in item['split_off']:
            print(f"  -> separate event: {group['title']} ({count(len(group['document_ids']), 'document')})")
    verb = 'would move' if args.dry_run else 'moved'
    print(f"{count(result['events_checked'], 'event')} with several documents checked, {result['events_split']} split: "
          f"{verb} {count(result['documents_moved'], 'document')} into {count(result['events_created'], 'new event')}.")
    return 0


def count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


if __name__ == '__main__':
    raise SystemExit(main())
