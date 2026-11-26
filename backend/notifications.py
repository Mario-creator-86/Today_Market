"""Alert builders, dedupe and delivery (in-app, Telegram, SMTP email, Web Push).
Every number in an alert comes from stored observations. Channels without credentials report NOT CONFIGURED
and never send; secrets and request URLs never reach logs or delivery records."""
import asyncio
import hashlib
import json
import logging
import re
import smtplib
import ssl
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select

from backend import config as app_config
from backend.config import ROOT, rules, rules_view
from backend.database import Store, Tick, utcnow
from backend.engines import aware

logger = logging.getLogger(__name__)
KINDS = ('SETUP', 'LEVEL_BREAK', 'DIVERGENCE', 'ANOMALY', 'EVENT', 'MACRO_RELEASE', 'PROVIDER')
TELEGRAM_API = 'https://api.telegram.org'


def _config() -> dict:
    return rules_view()['alerts']  # shared read-only view: read for every alert candidate


def rank(priority: str) -> int:
    return _config()['priorities'].index(priority)


class NotificationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    telegram_bot_token: SecretStr = SecretStr('')
    telegram_chat_id: str = ''
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_username: str = ''
    smtp_password: SecretStr = SecretStr('')
    smtp_from: str = ''
    smtp_to: str = ''  # comma-separated recipients
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    webpush_vapid_public_key: str = ''
    webpush_vapid_private_key: SecretStr = SecretStr('')
    webpush_subject: str = ''  # mailto: or https: contact required by VAPID


# --- alert construction ---------------------------------------------------------------------

def window_start(kind: str, now: datetime) -> datetime:
    """Start of the dedupe window: identical conditions inside one cooldown share one alert id."""
    cooldown = _config()['cooldown_seconds'][kind]
    return datetime.fromtimestamp(int(aware(now).timestamp()) // cooldown * cooldown, UTC)


def dedupe_key(kind: str, symbol: str | None, signature: str) -> str:
    return f'{kind}:{symbol or "-"}:{signature}'


def make_alert(kind: str, priority: str, title: str, message: str, now: datetime, signature: str, *,
               symbol: str | None = None, decision_id: str | None = None, event_id: str | None = None,
               metrics: dict | None = None, once: bool = False) -> dict:
    """once: the alert id is the dedupe key itself (no cooldown window), so the same condition is stored and
    delivered a single time for as long as its alert row exists (EVENT alerts: one per event id)."""
    if kind not in KINDS or priority not in _config()['priorities']:
        raise ValueError(f'Invalid alert kind/priority: {kind}/{priority}')
    key = dedupe_key(kind, symbol, signature)
    start = window_start(kind, now)
    return {'id': key if once else f'{key}:{int(start.timestamp())}', 'dedupe_key': key, 'kind': kind, 'priority': priority,
            'title': title, 'message': message, 'symbol': symbol, 'created_at': aware(now).isoformat(),
            'window_start': start.isoformat(), 'decision_id': decision_id, 'event_id': event_id,
            'metrics': metrics or {}}


def fmt_change(change: float, unit: str) -> str:
    return f'{change:+.1f}bp' if unit == 'bp' else f'{change:+.2f}%'


def window_changes(store: Store, symbols: Iterable[str], window_seconds: int, now: datetime) -> dict[str, dict]:
    """Observed change per symbol over the window, in the symbol's unit (bp for yields). Only ticks known at
    `now` count, stub ticks never count, and a symbol without a fresh observation or baseline is absent."""
    from backend.analytics import change_in_unit, unit_for
    config, now = _config()['changes'], aware(now)
    start = now - timedelta(seconds=window_seconds)
    stub = config['exclude_provider_suffix']
    found: dict[str, dict] = {}
    with store.session() as session:
        for symbol in dict.fromkeys(symbols):
            known = (Tick.symbol == symbol, Tick.ingested_at <= now, ~Tick.provider.endswith(stub))
            observed = session.scalar(select(Tick).where(*known, Tick.timestamp <= now, Tick.timestamp >= now - timedelta(
                seconds=config['max_observation_age_seconds'])).order_by(Tick.timestamp.desc()).limit(1))
            baseline = session.scalar(select(Tick).where(*known, Tick.timestamp <= start, Tick.timestamp >= start - timedelta(
                seconds=config['baseline_tolerance_seconds'])).order_by(Tick.timestamp.desc()).limit(1))
            if observed is None or baseline is None:
                continue
            unit = unit_for(symbol)
            try:
                change = change_in_unit(baseline.price, observed.price, unit)
            except ValueError:
                continue
            found[symbol] = {'symbol': symbol, 'change': round(change, 4), 'unit': unit, 'window_seconds': window_seconds,
                             'baseline_price': baseline.price, 'observed_price': observed.price,
                             'baseline_timestamp': aware(baseline.timestamp).isoformat(),
                             'observed_timestamp': aware(observed.timestamp).isoformat()}
    return found


def driver_symbols(drivers: list[dict]) -> list[str]:
    return [s for d in drivers for s in d.get('symbols', [d.get('symbol')]) if s]


def assess_drivers(drivers: list[dict], changes: dict[str, dict]) -> list[dict]:
    """Each driver's implied direction for the asset: sign(change) x pressure once |change| >= threshold."""
    result = []
    for driver in drivers:
        symbol = next((s for s in driver.get('symbols', [driver.get('symbol')]) if s in changes), None)
        if symbol is None:
            result.append({'symbol': '/'.join(driver.get('symbols', [driver.get('symbol')])), 'status': 'missing',
                           'implied_direction': 0})
            continue
        change = changes[symbol]
        moved = abs(change['change']) >= driver['threshold']
        implied = ((change['change'] > 0) - (change['change'] < 0)) * driver['pressure'] if moved else 0
        result.append(dict(change, status='measured', threshold=driver['threshold'], pressure=driver['pressure'],
                           implied_direction=implied))
    return result


def _describe(items: list[dict]) -> str:
    return ', '.join(f"{d['symbol']} {fmt_change(d['change'], d['unit'])}" for d in items)


def setup_alert(decision: dict, now: datetime) -> dict | None:
    priority = _config()['setup']['priority_by_direction'].get(decision.get('direction', ''))
    if priority is None or decision.get('audit_status') != 'PASS':  # an unaudited decision never alerts
        return None
    symbol, label = decision['symbol'], decision.get('category') or decision['direction']
    return make_alert('SETUP', priority, f'{symbol}: {label}', decision.get('confirmation') or label, now,
                      decision['direction'].replace(' ', '_'), symbol=symbol, decision_id=decision.get('id'),
                      metrics={'score': decision.get('score'), 'direction': decision['direction'],
                               'category': decision.get('category'), 'levels': decision.get('levels')})


def break_margin(level: float, atr: float | None) -> float:
    """Minimum distance beyond a level for a break: a tick through the level is noise, not a break."""
    config = _config()['level_break']
    return max(config['min_break_pct'] / 100 * level, config['min_break_atr'] * atr if isinstance(atr, (int, float)) else 0.0)


def level_kind(basis: Any) -> str:
    """Kind of a support/resistance level from its basis ('previous_day_low' -> 'daily', 'swing_low' -> 'intraday'):
    the first configured prefix (alerts.level_break.level_kinds), else the default kind."""
    config = _config()['level_break']['level_kinds']
    text = str(basis or '')
    return next((str(kind) for prefix, kind in config['prefixes'].items() if text.startswith(prefix)), str(config['default']))


def current_levels(asset: dict, now: datetime) -> dict | None:
    """The asset's support/resistance levels while they describe the current market, else None. Never tested for a
    break (AFP-01): levels of a decision past its expires_at (the last cycle before a restart), levels whose reference
    is flagged stale, and levels anchored to a minute close older than level_break.max_reference_age_seconds (the first
    cycle after a data gap). Levels anchored to a daily close (a date, no clock time) carry no such age."""
    decision = asset.get('decision') or {}
    levels = decision.get('levels') or asset.get('technical_levels')
    if not levels:
        return None
    now = aware(now)
    expires = _moment(decision.get('expires_at')) if decision.get('levels') else None
    if expires is not None and now > expires:
        return None
    reference = levels.get('reference') if isinstance(levels.get('reference'), dict) else {}
    if reference.get('stale'):
        return None
    anchor = reference.get('time')  # a minute bar start; a daily basis carries a date only
    stamp = aware(anchor) if isinstance(anchor, datetime) else _moment(anchor) if 'T' in str(anchor or '') else None
    if stamp is not None and (now - stamp).total_seconds() > _config()['level_break']['max_reference_age_seconds']:
        return None
    return levels


def level_break_alert(symbol: str, quote: dict | None, levels: dict | None, changes: dict[str, dict],
                      now: datetime) -> dict | None:
    """Technical break of support/resistance; HIGH PRIORITY when enough macro drivers push the same way. Deduplicated
    per symbol, side and level kind (level_kind): the level value is re-anchored to every completed minute close, so a
    key holding it would defeat the cooldown (AFP-02). The level stays in the message and the metrics."""
    if not quote or quote.get('freshness_status') != 'FRESH' or not levels:
        return None
    price = quote['price']
    if levels.get('support') is not None and price < levels['support']:
        side, level, direction = 'support', levels['support'], -1
    elif levels.get('resistance') is not None and price > levels['resistance']:
        side, level, direction = 'resistance', levels['resistance'], 1
    else:
        return None
    config = _config()['level_break']
    if abs(price - level) < break_margin(level, levels.get('atr')):
        return None
    drivers = assess_drivers(config['drivers'].get(symbol, []), changes)
    aligned = [d for d in drivers if d['implied_direction'] == direction]
    high = len(aligned) >= config['min_aligned_drivers']
    minutes = config['window_seconds'] // 60
    message = f'{symbol} {price:,.6g} broke {side} {level:,.6g}'
    message += (f' while {_describe(aligned)} ({minutes} min): macro and technical signals aligned' if high
                else ' without enough aligned macro confirmation')
    basis = levels.get(f'{side}_basis')
    kind = level_kind(basis)
    return make_alert('LEVEL_BREAK', config['aligned_priority'] if high else config['technical_only_priority'],
                      f"{symbol}: {'HIGH PRIORITY ' if high else ''}break of {side}", message, now,
                      f'{side}:{kind}', symbol=symbol,
                      metrics={'price': price, 'quote_timestamp': quote.get('timestamp'), 'level': level, 'side': side,
                               'level_basis': basis, 'level_kind': kind,
                               'direction': direction, 'aligned': len(aligned), 'drivers': drivers})


def divergence_alerts(changes: dict[str, dict], now: datetime, windows: dict[int, dict[str, dict]] | None = None) -> list[dict]:
    """Asset moving against what its drivers imply, e.g. oil, yields and USD up yet BTC up: relative strength.
    `windows` maps window_seconds -> changes when rules use different windows; `changes` is the default."""
    found = []
    for rule in _config()['divergence']['rules']:
        observed = (windows or {}).get(rule['window_seconds'], changes)
        asset = observed.get(rule['symbol'])
        if asset is None or abs(asset['change']) < rule['min_asset_move']:
            continue
        drivers = assess_drivers(rule['drivers'], observed)
        for implied, label in ((-1, 'relative strength'), (1, 'relative weakness')):
            pushing = [d for d in drivers if d['implied_direction'] == implied]
            opposing = [d for d in drivers if d['implied_direction'] == -implied]
            moved_against = (asset['change'] > 0) if implied == -1 else (asset['change'] < 0)
            if len(pushing) >= rule['min_aligned_drivers'] and not opposing and moved_against:
                hours = rule['window_seconds'] / 3600
                message = (f"{_describe(pushing)} yet {rule['symbol']} {fmt_change(asset['change'], asset['unit'])} "
                           f"({hours:g}h): {rule['symbol']} showing unusual {label}")
                found.append(make_alert('DIVERGENCE', rule['priority'], f"{rule['symbol']}: {label}", message, now,
                                        f"{rule['id']}:{label.replace(' ', '_')}", symbol=rule['symbol'],
                                        metrics={'rule': rule['id'], 'asset': asset, 'drivers': drivers}))
    return found


def anomaly_alert(symbol: str, anomaly: dict, now: datetime) -> dict | None:
    """Wraps analytics.detect_anomaly (change/unit/threshold) or the legacy rolling_anomaly (change_pct)."""
    if not anomaly or not anomaly.get('triggered'):
        return None
    change = anomaly.get('change', anomaly.get('change_pct'))
    threshold = anomaly.get('threshold', anomaly.get('threshold_pct'))
    unit = anomaly.get('unit', 'pct')
    if not isinstance(change, (int, float)) or not isinstance(threshold, (int, float)):
        return None
    config = _config()['anomaly']
    high = threshold > 0 and abs(change) >= threshold * config['high_threshold_multiple']
    window = anomaly.get('window_seconds')
    span = f' over {window // 60} min' if isinstance(window, int) else ' across recent observed bars'
    return make_alert('ANOMALY', config['high_priority'] if high else config['priority'],
                      f'{symbol}: unusual observed move', f'{fmt_change(change, unit)}{span}', now,
                      f"{'up' if change > 0 else 'down'}:{window or 'bars'}", symbol=symbol, metrics=anomaly)


def event_importance(event: dict) -> float | None:
    if isinstance(event.get('importance'), (int, float)):
        return float(event['importance'])
    extracted = (event.get('extraction') or {}).get('potential_importance')
    return float(extracted) / 100 if isinstance(extracted, (int, float)) else None


def event_assets(event: dict) -> list[str]:
    """The event's identified asset impact: the tracked assets it was mapped to (affected_assets, else assets), else the
    assets its causal trigger reaches. [] when nothing ties the event to a tracked asset."""
    mapped = [str(a) for a in event.get('affected_assets') or event.get('assets') or []]
    if mapped:
        return mapped
    from backend import causal
    return causal.affected_assets(causal.event_trigger(event))


def event_alert(event: dict, now: datetime) -> dict | None:
    """Verified, important events only: a configured status (CONFIRMED), an ASSESSED importance of at least
    min_importance (a missing importance measure never promotes an event) and, with require_assets, an identified asset
    impact. One alert per event id, ever (make_alert once): the key is the event id, never the shared status, so two
    events confirmed on one day both alert and one event never alerts twice (AFP-04)."""
    config = _config()['event']
    importance = event_importance(event)
    if event.get('verification_status') not in config['statuses'] or importance is None \
            or importance < config['min_importance']:
        return None
    assets = event_assets(event)
    if config['require_assets'] and not assets:
        return None
    high = importance >= config['high_importance']
    return make_alert('EVENT', config['high_priority'] if high else config['priority'],
                      f"{event['verification_status'].replace('_', ' ').title()}: {event.get('topic') or event['event_type']}",
                      event['summary_fact'], now, str(event['id']), event_id=event['id'], once=True,
                      metrics={'importance': importance, 'fact_confidence': event.get('fact_confidence'), 'assets': assets})


def event_alerts(events: Iterable[dict], now: datetime) -> list[dict]:
    """EVENT alerts for the newly confirmed events of the current event list: only events first seen or last updated
    within event.max_age_hours (a restart never alerts the whole backlog), each at most once (event_alert). No AI
    request is made: an event whose importance was never assessed gives no alert."""
    limit = timedelta(hours=_config()['event']['max_age_hours'])
    now = aware(now)
    found = []
    for event in events:
        stamps = [s for s in (_moment(event.get('last_updated_utc')), _moment(event.get('first_seen_utc'))) if s is not None]
        if not stamps or not timedelta(0) <= now - max(stamps) <= limit:
            continue
        alert = event_alert(event, now)
        if alert:
            found.append(alert)
    return found


def macro_release_alert(row: dict, now: datetime) -> dict | None:
    """A released calendar row (an actual from a vendor, or the outcome attached from the official series: EVR-4,
    release_outcome). The message states the published numbers; without a consensus it says the surprise is not
    computable instead of inventing one."""
    config = _config()['macro_release']
    if row.get('actual') is None or (row.get('impact') or 0) < config['min_impact'] or not row.get('id'):
        return None
    z = row.get('z_score')
    high = row['impact'] >= config['high_impact'] or (isinstance(z, (int, float)) and abs(z) >= config['high_abs_z_score'])
    outcome = row.get('release_outcome') if isinstance(row.get('release_outcome'), dict) else None
    if outcome and outcome.get('text'):
        message = f"{row.get('name')} ({row.get('country') or 'n/a'}): {outcome['text']}"
    else:
        message = f"{row.get('name')} ({row.get('country') or 'n/a'}): actual {row['actual']}"
    if row.get('consensus') is not None:
        message += f", consensus {row['consensus']}"
    elif outcome:
        message += '; no consensus: surprise not computable'
    if isinstance(row.get('raw_surprise'), (int, float)):
        message += f", surprise {row['raw_surprise']:+g}"
    if isinstance(z, (int, float)):
        message += f', z {z:+.2f}'
    metrics = {k: row.get(k) for k in ('actual', 'consensus', 'previous', 'raw_surprise', 'z_score', 'impact', 'scheduled_at', 'unit')}
    if outcome:
        metrics.update(outcome_text=outcome.get('text'), period=outcome.get('period'), values_source=row.get('values_source'))
    return make_alert('MACRO_RELEASE', config['high_priority'] if high else config['priority'],
                      f"Released: {row.get('name')}", message, now, str(row['id']), metrics=metrics)


def status_text(status: str | None) -> str:
    """Plain words for a provider status ('rate limited', 'access rejected')."""
    return _config()['provider']['status_text'].get(status or '', str(status or 'unknown').replace('_', ' ').lower())


def provider_alert(previous: dict | None, current: dict, now: datetime) -> dict | None:
    """Status transitions only: failures, and recovery after a failure. Startup and NOT CONFIGURED are silent.
    Deduplicated per provider and status (one provider failing never hides another's alert in the same window);
    title and message use the provider's human label and plain-language status and cause, never exception names."""
    config = _config()['provider']
    before, after = (previous or {}).get('status'), str(current.get('status') or '')
    if after == before or after in config['ignored_statuses'] or after not in config['priority_by_status']:
        return None
    if after == 'ONLINE' and (before is None or before in config['ignored_statuses']):
        return None
    name = current['name']
    label = current.get('label') or name
    if after == 'ONLINE':
        title, message = f'{label}: recovered', f'Back online (was {status_text(before)})'
    else:
        title = f'{label}: {status_text(after)}'
        message = current.get('error') or (f'Was {status_text(before)}, now {status_text(after)}' if before
                                            else f'Now {status_text(after)}')
    return make_alert('PROVIDER', config['priority_by_status'][after], title, message, now, f'{name}:{after}',
                      symbol=None, metrics={'provider': name, 'label': label, 'previous_status': before, 'status': after,
                                            'error_type': current.get('error_type'),
                                            'last_success': current.get('last_success')})


# --- read-time presentation (stored rows are never rewritten) ---------------------------------------------------------

LEGACY_TRANSITION = re.compile(r'^Status ([A-Z][A-Z_ ]*?) -> ([A-Z][A-Z_ ]*)$')
LEGACY_EXCEPTION = re.compile(r'^([A-Z][A-Za-z0-9]*(?:Error|Exception|Timeout|Full|Redirects|Unavailable))\b:?\s*(.*)$',
                              re.DOTALL)
HTTP_CODE = re.compile(r'\bHTTP (\d{3})\b')
STATUS_TOKEN = re.compile(r'^[A-Z][A-Z_ ]*[A-Z]$')


def _plain_cause(message: str, provider: str, label: str) -> str:
    """A legacy provider-alert message in the words provider_alert now writes: 'HTTPStatusError: HTTP 429' ->
    'Rate limited (HTTP 429)', 'ConnectError' -> 'Connection failed', 'Status RATE_LIMITED -> ONLINE' -> 'Back online
    (was rate limited)', and a leading raw provider id -> its label. Anything else is returned unchanged."""
    from backend.providers.base import failure_text
    text = message.strip()
    transition = LEGACY_TRANSITION.match(text)
    if transition:
        before, after = (part.strip() for part in transition.groups())
        return (f'Back online (was {status_text(before)})' if after == 'ONLINE'
                else f'Was {status_text(before)}, now {status_text(after)}')
    exception = LEGACY_EXCEPTION.match(text)
    if exception:
        code = HTTP_CODE.search(exception[2])
        if code:
            return failure_text(Exception(), int(code[1]))
        found = getattr(httpx, exception[1], None)
        # failure_text only looks at the exception's class, so an uninitialised instance is enough.
        return failure_text(found.__new__(found) if isinstance(found, type) and issubclass(found, Exception) else Exception())
    if provider and text.startswith(f'{provider}: '):
        rest = text[len(provider) + 2:].strip()
        return status_text(rest).capitalize() if STATUS_TOKEN.match(rest) else f'{label}: {rest}'
    return message


def readable_alert(row: dict, current: dict[str, str] | None = None, recovered: dict[str, list[str]] | None = None) -> dict:
    """An alert as the API and the dashboard show it (contract C16); the stored row is never changed.
    PROVIDER alerts stored before provider_alert wrote labels (no metrics.label) get the provider's label and plain
    status and cause, as provider_alert writes them now. A PROVIDER failure alert also says whether it is resolved:
    `resolved` is true when a later recovery alert of the same provider is known (`recovered`: provider -> created_at of
    its recovery alerts), or the provider is ONLINE now, switched off / not configured / pending (ignored statuses) or
    gone (`current`: provider -> status), and `resolved_at` is the first later recovery (null when only the current
    status shows it)."""
    if row.get('kind') != 'PROVIDER':
        return row
    from backend.providers.base import provider_label
    metrics = dict(row.get('metrics') or {})
    title, message = str(row.get('title') or ''), str(row.get('message') or '')
    provider = str(metrics.get('provider') or title.partition(': ')[0] or '')
    status = str(metrics.get('status') or '')
    result = dict(row)
    if not metrics.get('label') and provider:
        label = provider_label(provider)
        metrics['label'] = label
        if status:
            title = f'{label}: recovered' if status == 'ONLINE' else f'{label}: {status_text(status)}'
        elif title.startswith(f'{provider}: '):
            title = f'{label}: {title[len(provider) + 2:]}'
        result.update(title=title, message=_plain_cause(message, provider, label), metrics=metrics)
    if status and status != 'ONLINE' and provider:
        created = _moment(row.get('created_at'))
        later = sorted((at, text) for text in (recovered or {}).get(provider, [])
                       if (at := _moment(text)) is not None and created is not None and at > created)
        # Switched off, not configured, still pending or no longer registered: it never sends a recovery alert
        # (alerts.provider.ignored_statuses), so the outage it reported is over as far as the alert is concerned.
        now_status = (current or {}).get(provider)
        gone = current is not None and provider not in current
        result.update(resolved=bool(later) or now_status == 'ONLINE' or gone
                      or now_status in _config()['provider']['ignored_statuses'],
                      resolved_at=later[0][1] if later else None)
    return result


def _moment(value: Any) -> datetime | None:
    try:
        return aware(value) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def readable_alerts(rows: Iterable[dict], current: dict[str, str] | None = None) -> list[dict]:
    """readable_alert for a list of stored alerts; recoveries are looked up within the same list (the newest rows), so
    a failure is resolved by any later recovery alert of its provider in the list or by its current ONLINE status."""
    rows = list(rows)
    recovered: dict[str, list[str]] = {}
    for row in rows:
        metrics = row.get('metrics') or {}
        if row.get('kind') == 'PROVIDER' and metrics.get('status') == 'ONLINE' and metrics.get('provider'):
            recovered.setdefault(str(metrics['provider']), []).append(str(row.get('created_at') or ''))
    return [readable_alert(row, current, recovered) for row in rows]


def market_alerts(store: Store, assets: list[dict], now: datetime) -> list[dict]:
    """LEVEL_BREAK and DIVERGENCE alerts for the current asset list (dashboard asset shape). A break is only tested
    against levels that still describe the current market (current_levels)."""
    config = _config()
    level_window = config['level_break']['window_seconds']
    needed = {level_window: set(driver_symbols([d for ds in config['level_break']['drivers'].values() for d in ds]))}
    for rule in config['divergence']['rules']:
        needed.setdefault(rule['window_seconds'], set()).update([rule['symbol'], *driver_symbols(rule['drivers'])])
    windows = {seconds: window_changes(store, sorted(symbols), seconds, now) for seconds, symbols in needed.items()}
    found = []
    for asset in assets:
        alert = level_break_alert(asset['symbol'], asset.get('quote'), current_levels(asset, now), windows[level_window], now)
        if alert:
            found.append(alert)
    return found + divergence_alerts(windows[level_window], now, windows)


# --- delivery channels ----------------------------------------------------------------------

class DeliveryError(Exception):
    def __init__(self, reason: str, retryable: bool = True, retry_after: float | None = None):
        super().__init__(reason)
        self.reason, self.retryable, self.retry_after = reason, retryable, retry_after


class Channel(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...
    def status(self) -> dict: ...
    async def send(self, alert: dict) -> None: ...


def plain_text(alert: dict) -> str:
    return f"[{alert['priority']}] {alert['title']}\n{alert['message']}\n{alert['created_at']}"


class InAppChannel:
    name = 'in_app'
    configured = True

    def __init__(self, store: Store):
        self.store = store

    def status(self) -> dict:
        return {'name': self.name, 'status': 'ONLINE', 'configured': True}

    async def send(self, alert: dict) -> None:
        self.store.put('alerts', alert['id'], alert)


class TelegramChannel:
    name = 'telegram'

    def __init__(self, token: str, chat_id: str, transport: httpx.AsyncBaseTransport | None = None):
        self.token, self.chat_id, self.transport = token, chat_id, transport

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def status(self) -> dict:
        return {'name': self.name, 'status': 'CONFIGURED' if self.configured else 'NOT CONFIGURED',
                'configured': self.configured}

    async def send(self, alert: dict) -> None:
        text = plain_text(alert)[:_config()['channels']['telegram']['max_chars']]
        try:
            async with httpx.AsyncClient(timeout=_config()['request_timeout_seconds'], transport=self.transport,
                                         follow_redirects=False) as client:
                response = await client.post(f'{TELEGRAM_API}/bot{self.token}/sendMessage', json={
                    'chat_id': self.chat_id, 'text': text, 'disable_web_page_preview': True})
        except httpx.HTTPError as exc:  # the URL holds the bot token: report the type only
            raise DeliveryError(f'transport:{type(exc).__name__}') from None
        body = _json(response)
        if response.status_code == 429:
            retry = (body.get('parameters') or {}).get('retry_after')
            raise DeliveryError('HTTP 429', retry_after=float(retry) if isinstance(retry, (int, float)) else None)
        if response.status_code >= 500:
            raise DeliveryError(f'HTTP {response.status_code}')
        if response.status_code >= 400 or not body.get('ok'):
            raise DeliveryError(f'HTTP {response.status_code}', retryable=False)


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


LOCAL_SMTP_HOSTS = {'localhost', '127.0.0.1', '::1'}
PLAINTEXT_LOGIN = 'SMTP login refused: no SSL/TLS or STARTTLS'


def plaintext_login(settings: NotificationSettings) -> bool:
    """A login (username set) over a connection without SSL/TLS or STARTTLS to a server other than this computer:
    the password would cross the network in clear text, so it is never attempted."""
    host = settings.smtp_host.strip().strip('[]').lower()
    return bool(settings.smtp_username) and not (settings.smtp_ssl or settings.smtp_starttls) and host not in LOCAL_SMTP_HOSTS


class EmailChannel:
    name = 'email'

    def __init__(self, settings: NotificationSettings, smtp_factory: Callable[..., Any] | None = None):
        self.settings, self.smtp_factory = settings, smtp_factory

    @property
    def recipients(self) -> list[str]:
        return [r.strip() for r in self.settings.smtp_to.split(',') if r.strip()]

    @property
    def configured(self) -> bool:
        return bool(self.settings.smtp_host and self.settings.smtp_from and self.recipients)

    def status(self) -> dict:
        return {'name': self.name, 'status': 'CONFIGURED' if self.configured else 'NOT CONFIGURED',
                'configured': self.configured}

    def message(self, alert: dict) -> EmailMessage:
        message = EmailMessage()
        message['Subject'] = f"[{alert['priority']}] {alert['title']}"
        message['From'], message['To'] = self.settings.smtp_from, ', '.join(self.recipients)
        message.set_content(plain_text(alert) + '\n\nMetrics:\n' + json.dumps(alert.get('metrics') or {}, indent=2,
                                                                                    default=str))
        return message

    def _send(self, message: EmailMessage) -> None:
        s = self.settings
        if plaintext_login(s):  # checked before connecting: the credentials never leave in clear text
            raise DeliveryError(PLAINTEXT_LOGIN, retryable=False)
        factory = self.smtp_factory or (smtplib.SMTP_SSL if s.smtp_ssl else smtplib.SMTP)
        with factory(s.smtp_host, s.smtp_port, timeout=_config()['request_timeout_seconds']) as smtp:
            if s.smtp_starttls and not s.smtp_ssl:
                smtp.starttls(context=ssl.create_default_context())
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)

    async def send(self, alert: dict) -> None:
        try:
            await asyncio.to_thread(self._send, self.message(alert))
        except smtplib.SMTPAuthenticationError:
            raise DeliveryError('SMTP authentication failed', retryable=False) from None
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f'smtp:{type(exc).__name__}') from None


PushSender = Callable[[dict, str, NotificationSettings, int], int]


def pywebpush_sender(subscription: dict, data: str, settings: NotificationSettings, ttl: int) -> int:
    """Encrypts (RFC 8291) and signs (VAPID) through pywebpush; returns the push service HTTP status."""
    from pywebpush import WebPushException, webpush
    try:
        response = webpush(subscription_info=subscription, data=data, ttl=ttl,
                           vapid_private_key=settings.webpush_vapid_private_key.get_secret_value(),
                           vapid_claims={'sub': settings.webpush_subject},
                           timeout=_config()['request_timeout_seconds'])
    except WebPushException as exc:
        return int(getattr(exc.response, 'status_code', 0) or 0)
    return int(getattr(response, 'status_code', 201))


def webpush_available() -> bool:
    import importlib.util
    return importlib.util.find_spec('pywebpush') is not None


class WebPushChannel:
    name = 'webpush'

    def __init__(self, store: Store, settings: NotificationSettings, sender: PushSender | None = None):
        self.store, self.settings = store, settings
        self.sender = sender or (pywebpush_sender if webpush_available() else None)

    @property
    def keys_present(self) -> bool:
        s = self.settings
        return bool(s.webpush_vapid_public_key and s.webpush_vapid_private_key.get_secret_value() and s.webpush_subject)

    @property
    def configured(self) -> bool:
        return self.keys_present and self.sender is not None

    def status(self) -> dict:
        if not self.keys_present:
            return {'name': self.name, 'status': 'NOT CONFIGURED', 'configured': False}
        if self.sender is None:
            return {'name': self.name, 'status': 'UNAVAILABLE', 'configured': False,
                    'error': 'pywebpush not installed (pip install .[notifications])'}
        return {'name': self.name, 'status': 'CONFIGURED', 'configured': True,
                'subscriptions': len(self.subscriptions()), 'public_key': self.settings.webpush_vapid_public_key}

    def subscriptions(self) -> list[dict]:
        return [s for s in self.store.list('push_subscriptions', 1000) if s.get('active', True)]

    async def send(self, alert: dict) -> None:
        subscriptions = self.subscriptions()
        if not subscriptions or self.sender is None:
            raise DeliveryError('no active push subscriptions', retryable=False)
        data = json.dumps({k: alert.get(k) for k in ('id', 'kind', 'priority', 'title', 'message', 'symbol', 'created_at')})
        ttl = _config()['channels']['webpush']['ttl_seconds']
        delivered, failures = 0, []
        for item in subscriptions:
            subscription = {'endpoint': item['endpoint'], 'keys': item['keys']}
            status = await asyncio.to_thread(self.sender, subscription, data, self.settings, ttl)
            if 200 <= status < 300:
                delivered += 1
            elif status in {404, 410}:  # expired subscription: deactivate, keep the audit trail
                self.store.put('push_subscriptions', item['id'], dict({k: v for k, v in item.items() if k != 'id'},
                               active=False, deactivated_at=utcnow().isoformat()))
            else:
                failures.append(status)
        if not delivered:
            raise DeliveryError(f"push HTTP {failures[-1] if failures else 'expired'}", retryable=bool(failures))


def register_push_subscription(store: Store, subscription: dict) -> str:
    """Validates a browser PushSubscription JSON ({endpoint, keys: {p256dh, auth}}) and stores it once."""
    endpoint = str(subscription.get('endpoint') or '')
    keys = subscription.get('keys') or {}
    if not endpoint.startswith('https://') or len(endpoint) > 2048:
        raise ValueError('Push endpoint must be an https URL')
    # The server later POSTs to this URL: only browser push services are accepted, never an arbitrary host.
    config = _config()['channels']['webpush']
    host = (urlsplit(endpoint).hostname or '').lower()
    if not any(host == allowed or host.endswith('.' + allowed) for allowed in config['allowed_push_hosts']):
        raise ValueError('Push endpoint is not a known browser push service')
    if not all(isinstance(keys.get(k), str) and 8 <= len(keys[k]) <= 256 for k in ('p256dh', 'auth')):
        raise ValueError('Push subscription keys missing or malformed')
    key = hashlib.sha256(endpoint.encode()).hexdigest()
    if not store.get('push_subscriptions', key) and len(store.list('push_subscriptions', config['max_subscriptions'] + 1))             >= config['max_subscriptions']:
        raise ValueError('Push subscription limit reached')
    store.put('push_subscriptions', key, {'endpoint': endpoint, 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']},
                                          'active': True, 'registered_at': utcnow().isoformat()})
    return key


def build_channels(store: Store, settings: NotificationSettings | None = None, *,
                   transport: httpx.AsyncBaseTransport | None = None, smtp_factory: Callable[..., Any] | None = None,
                   push_sender: PushSender | None = None) -> list[Channel]:
    settings = settings or NotificationSettings(_env_file=app_config.ENV_FILE)
    return [InAppChannel(store),
            TelegramChannel(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, transport),
            EmailChannel(settings, smtp_factory), WebPushChannel(store, settings, push_sender)]


class Dispatcher:
    """Dedupes alerts, stores them in-app and fans out to external channels with per-channel minimum priority,
    hourly rate limits and retries with exponential backoff. Every attempt outcome is recorded."""

    def __init__(self, store: Store, channels: list[Channel],
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, clock: Callable[[], datetime] = utcnow):
        self.store, self.channels, self.sleep, self.clock = store, channels, sleep, clock
        self.sent: dict[str, deque[float]] = {}

    def duplicate(self, alert: dict) -> bool:
        if self.store.get('alerts', alert['id']):
            return True
        cooldown = _config()['cooldown_seconds'][alert['kind']]
        previous_id = f"{alert['dedupe_key']}:{int(aware(alert['window_start']).timestamp()) - cooldown}"
        previous = self.store.get('alerts', previous_id)
        return bool(previous and (aware(alert['created_at']) - aware(previous['created_at'])).total_seconds() < cooldown)

    def _rate_limited(self, channel: str, limit: int) -> bool:
        window = self.sent.setdefault(channel, deque())
        now = time.monotonic()
        while window and now - window[0] > 3600:
            window.popleft()
        return len(window) >= limit

    async def _deliver(self, channel: Channel, alert: dict) -> dict:
        config = _config()['channels'][channel.name]
        record = {'alert_id': alert['id'], 'channel': channel.name, 'priority': alert['priority'], 'attempts': 0,
                  'error': None}
        if not channel.configured:
            return dict(record, status='NOT_CONFIGURED')
        if rank(alert['priority']) < rank(config['min_priority']):
            return dict(record, status='SKIPPED_PRIORITY')
        if self._rate_limited(channel.name, config['max_per_hour']):
            return dict(record, status='RATE_LIMITED')
        for attempt in range(config['retries'] + 1):
            record['attempts'] = attempt + 1
            try:
                await channel.send(alert)
                self.sent[channel.name].append(time.monotonic())
                return dict(record, status='SENT', error=None, sent_at=self.clock().isoformat())
            except DeliveryError as exc:
                record['error'] = exc.reason
                if not exc.retryable or attempt == config['retries']:
                    break
                await self.sleep(exc.retry_after if exc.retry_after is not None else config['backoff_seconds'] * 2 ** attempt)
        logger.warning('alert_delivery_failed', extra={'channel': channel.name, 'alert_id': alert['id'],
                                                       'error_type': record['error']})
        return dict(record, status='FAILED')

    async def dispatch(self, alert: dict) -> dict:
        if self.duplicate(alert):
            return {'alert_id': alert['id'], 'status': 'DUPLICATE', 'deliveries': []}
        deliveries = []
        for channel in self.channels:  # in-app first: a failing external channel never hides the alert
            record = await self._deliver(channel, alert)
            record['recorded_at'] = self.clock().isoformat()
            self.store.put('notification_deliveries', f"{alert['id']}:{channel.name}", record)
            deliveries.append(record)
        return {'alert_id': alert['id'], 'status': 'NEW', 'deliveries': deliveries}

    async def dispatch_many(self, alerts: Iterable[dict | None]) -> list[dict]:
        return [await self.dispatch(a) for a in alerts if a]


def alert_settings(channels: list[Channel]) -> dict:
    """Payload for /api/settings `alerts`: channel status (never credentials) and the active rules."""
    config = _config()
    return {'version': rules()['alerts_version'], 'channels': [
                dict(c.status(), min_priority=config['channels'][c.name]['min_priority'],
                     max_per_hour=config['channels'][c.name]['max_per_hour']) for c in channels],
            'cooldown_seconds': config['cooldown_seconds'], 'kinds': list(KINDS), 'priorities': config['priorities']}
