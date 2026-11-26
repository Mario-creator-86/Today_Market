"""Deterministic morning brief and intraday update composition. No LLM writes brief content."""
import math
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from backend import refresh
from backend.analytics import unit_for
from backend.config import rules
from backend.engines import aware

ACTIONABLE = {'LONG', 'SHORT', 'STRONG LONG', 'STRONG SHORT'}
# Version of the composed text (labels, sections). A stored brief is text: one composed by an older formatter keeps its
# old labels, so a reader (GET /api/brief) can tell it apart (RI-5). 2: daily closes labelled by their own close date and
# close_status; each fact in one section; an event whose importance was never assessed needs an asset impact.
BRIEF_FORMAT = 2
REPORTABLE = {'CONFIRMED', 'PARTIALLY_CONFIRMED'}
DAILY_CLASSES = ('DAILY_CLOSE', 'DERIVED_DAILY')
Gate = Callable[[datetime], tuple[bool, str | None]]
# AFP-05: the readiness gate of the process that composes the briefs (the Runtime registers morning_ready over its
# refresh state; None: no background refresh in this process, nothing to wait for). morning_due consults it, so every
# caller of publish_briefs (the analysis cycle, the brief schedule job) waits the same way.
_gate: Gate | None = None


def set_gate(value: Gate | None) -> None:
    global _gate
    _gate = value


def gate() -> Gate | None:
    return _gate


def morning_state(now: datetime) -> tuple[bool, str | None]:
    """(ready, reason it waits) for composing the morning brief now, from the registered gate."""
    return _gate(now) if _gate is not None else (True, None)


def _config() -> dict:
    return rules()['brief']


def local_now(now: datetime) -> datetime:
    return aware(now).astimezone(ZoneInfo(_config()['timezone']))


def morning_schedule(now: datetime) -> dict | None:
    """Today's morning brief once its local time has passed (IANA DST rules, never a fixed offset).
    Inside the window it is on time; later it is labelled late, or skipped by policy or past the late limit."""
    config, local = _config(), local_now(now)
    hour, minute = (int(v) for v in config['morning_local_time'].split(':'))
    scheduled = datetime.combine(local.date(), time(hour, minute), tzinfo=ZoneInfo(config['timezone']))
    if local < scheduled:
        return None
    minutes_late = (local - scheduled).total_seconds() / 60
    late = minutes_late > config['morning_window_minutes']
    if late and (config['morning_late_policy'] == 'skip' or minutes_late > config['morning_late_limit_minutes']):
        return None
    return {'date': local.date().isoformat(), 'scheduled_at': aware(scheduled).isoformat(),
            'scheduled_local': scheduled.strftime('%H:%M'), 'late': late, 'minutes_late': round(minutes_late)}


def morning_due(now: datetime) -> str | None:
    """Local date whose morning brief is due now, or None (before schedule, too late to publish, or while the
    process's gate says the data refresh and an analysis cycle on it are still pending: morning_state)."""
    schedule = morning_schedule(now)
    if not schedule or not morning_state(now)[0]:
        return None
    return schedule['date']


def morning_ready(now: datetime, refresh_state: dict | None, cycle_at: datetime | str | None) -> tuple[bool, str | None]:
    """Whether the morning brief may be composed now (AFP-05): only after the first market-data refresh since the app
    started AND from an analysis cycle that started after that refresh, never from the state the previous run left in
    the database. Returns (ready, reason it waits).
    refresh_state: {'started_at', 'refreshed_at'} kept by the runtime (refreshed_at: when every quote / daily-close
    poll finished its first attempt, None until then); None means no background refresh runs in this process (tests,
    one-off jobs), so there is nothing to wait for. A refresh that has not finished one analysis cadence after the
    start no longer blocks the brief (a provider that is down must not cancel the day's brief); the brief then still
    waits for a cycle started after that bound. cycle_at: start of the analysis cycle whose decisions the brief
    describes (the running cycle, else the last successful one)."""
    if refresh_state is None:
        return True, None
    started = aware(refresh_state['started_at'])
    bound = started + timedelta(seconds=float(refresh.cadence('interpretation')['seconds']))
    refreshed = aware(refresh_state['refreshed_at']) if refresh_state.get('refreshed_at') else None
    if refreshed is None and aware(now) >= bound:
        refreshed = bound
    if refreshed is None:
        return False, 'waiting for the first market data refresh after start'
    if cycle_at is None or aware(cycle_at) < refreshed:
        return False, 'waiting for an analysis cycle on the refreshed data'
    return True, None


def level_state(asset: dict, now: datetime | None = None) -> str:
    """ABOVE_RESISTANCE / BELOW_SUPPORT / INSIDE_RANGE for a fresh quote against the decision's levels; UNKNOWN without
    a fresh quote or levels. With `now`, levels that no longer describe the current market (an expired decision, a
    stale or hours-old reference: notifications.current_levels) are UNKNOWN as well, never a break."""
    from backend.notifications import break_margin, current_levels
    quote, levels = asset.get('quote'), (asset.get('decision') or {}).get('levels') or {}
    if now is not None and levels:
        levels = current_levels(asset, now) or {}
    if not quote or quote.get('freshness_status') != 'FRESH' or not levels:
        return 'UNKNOWN'
    resistance, support, atr = levels.get('resistance'), levels.get('support'), levels.get('atr')
    if resistance is None and support is None:  # no boundary selected (e.g. no current minute bar): nothing to be inside of
        return 'UNKNOWN'
    # Same margin as level-break alerts, so a price hovering at a level does not flap between states.
    if resistance is not None and quote['price'] > resistance + break_margin(resistance, atr):
        return 'ABOVE_RESISTANCE'
    if support is not None and quote['price'] < support - break_margin(support, atr):
        return 'BELOW_SUPPORT'
    return 'INSIDE_RANGE'


def event_importance(event: dict) -> float | None:
    """Contract importance (0..1), else extractor importance (0..100) scaled; None when never measured."""
    if isinstance(event.get('importance'), (int, float)):
        return float(event['importance'])
    extracted = (event.get('extraction') or {}).get('potential_importance')
    return float(extracted) / 100 if isinstance(extracted, (int, float)) else None


def _causal_trigger(event: dict) -> bool:
    from backend import causal
    return causal.event_trigger(event) is not None


def market_link(event: dict) -> bool:
    """An identified asset impact: the event's own mapped assets, or a causal trigger."""
    return bool(event.get('affected_assets') or event.get('assets')) or _causal_trigger(event)


def reportable(event: dict) -> bool:
    """Whether a verified event belongs in a brief (AFP-06). An ASSESSED importance decides: the extractor's
    potential_importance (0-100), else the contract importance (0-1), against min_event_importance. An importance that
    was never assessed is unknown, never the maximum: such an event is listed only with an identified asset impact
    (market_link). Everything else is filler."""
    minimum = _config()['min_event_importance']
    potential = (event.get('extraction') or {}).get('potential_importance')
    if _finite(potential):
        return bool(potential >= minimum)
    if _finite(event.get('importance')):
        return bool(event['importance'] * 100 >= minimum)
    return market_link(event)


def _event_moment(event: dict) -> datetime:
    """Publication time of the event (event_time, else first seen); the epoch when neither is readable."""
    for key in ('event_time', 'first_seen_utc'):
        try:
            if event.get(key):
                return aware(event[key])
        except (TypeError, ValueError):
            continue
    return datetime.fromtimestamp(0, ZoneInfo('UTC'))


def fact_rank(event: dict) -> tuple:
    """'Confirmed facts' order: an identified asset impact first, then assessed importance, then recency."""
    return market_link(event), event_importance(event) or 0.0, _event_moment(event)


def is_major(event: dict) -> bool:
    """Measured importance decides; without a measure only a known causal trigger qualifies."""
    importance = event_importance(event)
    if importance is not None:
        return importance >= _config()['major_event_importance']
    return _causal_trigger(event)


def snapshot(now: datetime, assets: list[dict], events: list[dict], calendar: list[dict], regime: dict) -> dict:
    minimum = _config()['min_calendar_impact']
    return {'generated_at': aware(now).isoformat(),
            'scores': {a['symbol']: a['decision']['score'] for a in assets if a.get('decision')},
            'directions': {a['symbol']: a['decision']['direction'] for a in assets if a.get('decision')},
            'regimes': sorted(regime.get('regimes', [])), 'regime_basis': regime.get('basis'),
            'confirmed_events': sorted(e['id'] for e in events if e['verification_status'] == 'CONFIRMED'),
            'released': sorted(c['id'] for c in calendar if c.get('actual') is not None and (c.get('impact') or 0) >= minimum),
            'levels': {a['symbol']: level_state(a, now) for a in assets}}


def changes(previous: dict | None, current: dict, events: list[dict], calendar: list[dict]) -> list[dict]:
    """Material differences between two snapshots; without a baseline there is nothing to compare."""
    if not previous:
        return []
    found = []
    by_event = {e['id']: e for e in events}
    for event_id in sorted(set(current['confirmed_events']) - set(previous.get('confirmed_events', []))):
        event = by_event.get(event_id)
        if event and is_major(event):
            found.append({'trigger': 'MAJOR_EVENT', 'text': f"Newly confirmed: {event['summary_fact']}"})
    by_release = {c['id']: c for c in calendar}
    for release_id in sorted(set(current['released']) - set(previous.get('released', []))):
        item = by_release.get(release_id)
        if item is None:
            continue
        surprise = item.get('raw_surprise')
        outcome = item.get('release_outcome') if isinstance(item.get('release_outcome'), dict) else None
        # EVR-4: an outcome read from the official series is stated with its numbers ('payrolls +29k (prev +133k), ...').
        published = outcome['text'] if outcome and outcome.get('text') else f"actual {item['actual']}"
        found.append({'trigger': 'MACRO_RELEASE', 'text': f"{item['name']} released: {published}"
                      + (f", consensus {item['consensus']}, surprise {surprise:+g}" if isinstance(surprise, (int, float)) else '')})
    if current['regimes'] != sorted(previous.get('regimes', [])):
        bases = (previous.get('regime_basis'), current.get('regime_basis'))
        basis = f" (basis {_basis_name(bases[0])} → {_basis_name(bases[1])})" if all(bases) and bases[0] != bases[1] else ''
        found.append({'trigger': 'REGIME_CHANGE', 'text': f"Regime {' / '.join(previous.get('regimes', [])) or 'unavailable'}"
                      f" → {' / '.join(current['regimes']) or 'unavailable'}{basis}"})
    limit = _config()['intraday_score_change']
    for symbol, score in current['scores'].items():
        old = previous.get('scores', {}).get(symbol)
        if old is not None and abs(score - old) > limit:
            found.append({'trigger': 'SCORE_CHANGE', 'text': f"{symbol}: score {old:g} → {score:g}, "
                          f"{previous.get('directions', {}).get(symbol, 'unavailable')} → {current['directions'][symbol]}"})
    for symbol, state in current['levels'].items():
        if state in {'ABOVE_RESISTANCE', 'BELOW_SUPPORT'} and previous.get('levels', {}).get(symbol) != state:
            found.append({'trigger': 'LEVEL_BREAK', 'text': f"{symbol}: price {state.replace('_', ' ').lower()}"})
    return found


def _basis_name(basis: str | None) -> str:
    return str(basis or 'unavailable').replace('_', ' ')


def regime_line(regime: dict) -> str:
    """Regimes, posture, rule coverage and the data basis they were evaluated on ('live intraday', or 'last close
    Fri 25 Sep, markets closed' when the live basis is insufficient), so a weekend brief says what it is based on."""
    return (f"Regime: {' / '.join(regime.get('regimes') or ['unavailable']).replace('_', ' ')} · posture "
            f"{regime.get('posture', 'INSUFFICIENT_DATA').replace('_', ' ')}"
            + (f" · rule coverage {regime['confidence']:g}%" if regime.get('confidence') is not None else '')
            + (f" · basis: {regime['basis_label']}" if regime.get('basis_label') else ''))


def intraday(last_update: dict | None, current: dict, events: list[dict], calendar: list[dict]) -> tuple[list[dict], dict]:
    """Differences since the last PUBLISHED update (so slow drift accumulates) and the baseline to store next.
    The first cycle only records a baseline."""
    if last_update is None:
        return [], current
    found = changes(last_update, current, events, calendar)
    return found, (current if found else last_update)


def _event_line(event: dict) -> str:
    return f"{event['summary_fact']} ({event['verification_status'].replace('_', ' ').lower()}, {event['fact_confidence']:g}% confidence)"


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _daily(quote: dict) -> bool:
    """An official or derived daily close (rows stored before data_class existed carry the daily_close flag)."""
    klass = quote.get('data_class')
    return klass in DAILY_CLASSES or (not klass and quote.get('daily_close') is True)


def _close_day(quote: dict) -> str:
    """' 25 Sep' for the close's own observation date, '' when it is unknown."""
    try:
        day = date.fromisoformat(str(quote.get('close_date') or '')[:10])
    except ValueError:
        return ''
    return f' {day.day} {day:%b}'


def quote_label(quote: dict) -> str:
    """The dashboard strip's classes. A daily close is labelled by its own publication status, never by the venue's
    current session: 'DAILY CLOSE 25 Sep' ('DERIVED 25 Sep' for a value this app computes) while it is the latest
    expected close, 'STALE close 22 Sep' once a newer one is overdue. Otherwise LIVE only for a fresh quote in a live
    session; a closed venue shows its session state for a fresh extended-hours print and LAST PRINT for an older one;
    a development stub is STUB; anything else is STALE."""
    if quote.get('stub'):
        return 'STUB'
    close = quote.get('close_status')
    if _daily(quote):
        # RI-5: a daily close follows its own close date and close_status. Without a publication calendar (close_status
        # unknown) it is still a daily close of its own date: never 'STALE' by assumption and never the venue's session
        # ('FRIDAY CLOSE' for a Tuesday close).
        if close == 'STALE':
            return f'STALE close{_close_day(quote)}'
        derived = quote.get('derived') is True or quote.get('data_class') == 'DERIVED_DAILY'
        return f"{'DERIVED' if derived else 'DAILY CLOSE'}{_close_day(quote)}"
    status, fresh = quote.get('market_status'), quote.get('freshness_status') == 'FRESH'
    if fresh and status == 'LIVE':
        return 'LIVE'
    if not _daily(quote) and status in _config()['closed_market_states']:
        return str(status) if fresh else 'LAST PRINT'
    return 'STALE'


def change_text(symbol: str, quote: dict) -> str:
    """' (-6 bp)' for yields (the provider's bp change, else derived from level and percent change, as the regime
    engine does), ' (+0.32%)' otherwise, '' when no change was reported."""
    if unit_for(symbol) == 'bp':
        bp: float | None = float(quote['change_bp']) if _finite(quote.get('change_bp')) else None
        if bp is None and _finite(quote.get('price')) and _finite(quote.get('change_pct')) and quote['change_pct'] > -100:
            previous = quote['price'] / (1 + quote['change_pct'] / 100)
            bp = (quote['price'] - previous) * rules()['market_units']['bp_per_unit']
        return f' ({round(bp, 1) or 0.0:+g} bp)' if bp is not None else ''
    return f" ({quote['change_pct']:+.2f}%)" if _finite(quote.get('change_pct')) else ''


def local_time(moment: datetime | str, now: datetime, zone: ZoneInfo) -> str:
    """'02:10 (Bucharest)' on the brief's own local day, '26 Sep 23:10 (Bucharest)' on another one."""
    local, today = aware(moment).astimezone(zone), local_now(now).date()
    city = zone.key.rsplit('/', 1)[-1].replace('_', ' ')
    return f'{local:%H:%M} ({city})' if local.date() == today else f'{local.day} {local:%b %H:%M} ({city})'


def _decided_at(decision: dict) -> datetime | None:
    created = decision.get('created_at')
    try:
        return aware(created) if isinstance(created, (str, datetime)) else None
    except ValueError:
        return None


def _current(decided: datetime, now: datetime) -> bool:
    """Decided within one analysis cadence (refresh cadences.interpretation, the cadence the dashboard's analysis
    status uses), i.e. by the latest analysis cycle."""
    return (aware(now) - decided).total_seconds() <= float(refresh.cadence('interpretation')['seconds'])


def decided_text(decision: dict, now: datetime, zone: ZoneInfo) -> str:
    """' · decided 02:10 (Bucharest)' for a decision older than one analysis cadence: a brief composed after it (the
    07:30 morning brief on a 02:10 cycle) says when the setup was decided instead of passing it off as current. ''
    for a decision of the latest cycle or one without a time."""
    decided = _decided_at(decision)
    return '' if decided is None or _current(decided, now) else f' · decided {local_time(decided, now, zone)}'


def analysis_note(analysis: dict | None, now: datetime, zone: ZoneInfo, assets: list[dict] | None = None) -> str | None:
    """A brief-level warning when the full analysis is not current (HubService.analysis_status 'stale': no cycle
    completed within its window, or the last attempt failed), so the setups below are read as of that time. A brief
    composed by the cycle that has just decided (a decision within one cadence) is current whatever the status says."""
    if not analysis or not analysis.get('stale'):
        return None
    if any((decided := _decided_at(a.get('decision') or {})) is not None and _current(decided, now) for a in assets or []):
        return None
    last = analysis.get('last_success_at')
    if not last:
        return 'Analysis not current: no analysis cycle has completed yet, so there are no current setups'
    return f'Analysis not current: last completed {local_time(last, now, zone)}; setups are from then'


def _asset_line(asset: dict, zone: ZoneInfo, now: datetime) -> str:
    quote, decision = asset['quote'], asset.get('decision') or {}
    label = quote_label(quote)
    # A daily close already names its own date; other values say when they were observed.
    dated = _daily(quote) and _close_day(quote)
    stamp = f" as of {aware(quote['timestamp']).astimezone(zone):%d %b %H:%M}" if quote.get('timestamp') and not dated else ''
    # Stale or closed prices are context only: no setup direction next to them. An old setup says when it was decided.
    setup = f" · {decision.get('direction', 'WAIT')}{decided_text(decision, now, zone)}" if label == 'LIVE' else ''
    return f"{asset['symbol']} {quote['price']:,.5g}{change_text(asset['symbol'], quote)} · {label}{stamp}{setup}"


def catalyst_rank(event: dict, now: datetime) -> float:
    config = _config()
    importance = event_importance(event)
    started = event.get('event_time') or event.get('first_seen_utc')
    hours = max(0.0, (aware(now) - aware(started)).total_seconds() / 3600) if started else 0.0
    recency = 0.5 ** (hours / config['catalyst_recency_half_life_hours'])
    bonus = 1 + config['catalyst_causal_bonus'] if _causal_trigger(event) else 1
    return (config['catalyst_default_importance'] if importance is None else importance) * \
        (event.get('fact_confidence') or 0) / 100 * recency * bonus


def compose(kind: str, now: datetime, assets: list[dict], events: list[dict], calendar: list[dict],
            regime: dict, changed: list[dict] | None = None, since: str | None = None,
            analysis: dict | None = None) -> dict:
    """analysis: HubService.analysis_status(now, assets); when it is stale the brief says so above the setups."""
    config = _config()
    limit = config['max_items_per_section']
    local = local_now(now)
    zone = ZoneInfo(config['timezone'])
    # AFP-06: an assessed importance decides; one that was never assessed is unknown (never the maximum), so such an
    # event needs an identified asset impact (reportable). Each fact appears in ONE section: the major confirmed events
    # under 'Top catalysts', the other confirmed ones under 'Confirmed facts' (asset impact, importance, recency), and
    # what is left (partially confirmed, overflow) under its topic. A fact already named under 'What changed' is not
    # repeated either.
    current = [e for e in events if e['verification_status'] in REPORTABLE and reportable(e)]
    names = {s['id']: s['name'] for s in rules()['sources']}
    shown: set[str] = {c['text'].removeprefix('Newly confirmed: ') for c in changed or [] if c.get('trigger') == 'MAJOR_EVENT'}

    def lines(candidates: list[dict]) -> list[str]:
        """Up to `limit` event lines, skipping every fact an earlier section (or an earlier line) already states."""
        found: list[str] = []
        for item in candidates:
            if len(found) >= limit:
                break
            if item['summary_fact'] in shown:
                continue
            shown.add(item['summary_fact'])
            found.append(_event_line(item))
        return found
    confirmed = sorted((e for e in current if e['verification_status'] == 'CONFIRMED'), key=fact_rank, reverse=True)
    catalysts = sorted((e for e in confirmed if is_major(e)), key=lambda e: catalyst_rank(e, now), reverse=True)
    quoted = [a for a in assets if a.get('quote')]
    fresh = [a for a in quoted if a['quote'].get('freshness_status') == 'FRESH']
    sections: list[dict] = []
    if kind == 'INTRADAY':
        sections.append({'title': 'What changed since last update', 'items': [c['text'] for c in changed or []]})
    sections.append({'title': 'Top catalysts', 'items': lines(catalysts)})
    sections.append({'title': 'Confirmed facts', 'items': lines(confirmed)})
    note = analysis_note(analysis, now, zone, assets)
    state = [regime_line(regime), f"Fresh quotes: {len(fresh)} of {len(assets)} tracked assets", *([note] if note else [])]
    sections.append({'title': 'Global market state', 'items': state})
    today = [c for c in calendar if (c.get('impact') or 0) >= config['min_calendar_impact'] and c.get('scheduled_at')
             and aware(c['scheduled_at']).astimezone(zone).date() == local.date()]
    sections.append({'title': f"Today's schedule ({config['timezone']})", 'items': [
        f"{aware(c['scheduled_at']).astimezone(zone):%H:%M} {c['name']} ({c.get('country') or 'n/a'}, impact {c['impact']})"
        + (f"; consensus {c['consensus']}" if c.get('consensus') is not None else '')
        for c in sorted(today, key=lambda c: c['scheduled_at'])][:limit]})
    for title, topic in config['topics'].items():
        sources = set(topic['sources']) | {names[s] for s in topic['sources'] if s in names}
        matched = [e for e in current if e['event_type'] in topic['types'] or set(e.get('sources', [])) & sources]
        sections.append({'title': title, 'items': lines(matched)})
    for title, group in config['asset_sections'].items():
        members = [a for a in quoted if a['asset_class'] in group['classes'] or a['symbol'] in group['symbols']]
        sections.append({'title': title, 'items': [_asset_line(a, zone, now) for a in members[:limit]]})
    reasons = Counter(r for a in assets for r in (a.get('decision') or {}).get('reasons', []))
    sections.append({'title': 'Risk map', 'items': [f"{reason} ({count} {'asset' if count == 1 else 'assets'})"
                                                    for reason, count in reasons.most_common(limit)]})
    decided = [a for a in assets if a.get('decision')]
    setups = sorted((a for a in decided if a['decision']['direction'] != 'WAIT'),
                    key=lambda a: abs(a['decision']['score']), reverse=True)
    sections.append({'title': 'Long / short / wait setups', 'items': [
        f"{a['symbol']}: {a['decision']['direction']} (score {a['decision']['score']:g}){decided_text(a['decision'], now, zone)}"
        for a in setups[:limit]]
        or ([f"No actionable setups: {len(decided)} {'asset' if len(decided) == 1 else 'assets'} WAIT"] if decided else [])})
    sections.append({'title': 'Confirmation / invalidation', 'items': [
        f"{a['symbol']}: confirm — {a['decision'].get('confirmation')}; invalidate — {a['decision'].get('invalidation')}"
        for a in setups if a['decision']['direction'] in ACTIONABLE][:limit]})
    # Empty sections are omitted: no low-impact filler and no invented placeholders.
    result = {'kind': kind, 'generated_at': aware(now).isoformat(), 'local_date': local.date().isoformat(),
              'timezone': config['timezone'], 'format': BRIEF_FORMAT,
              'regime': {k: regime.get(k) for k in ('regimes', 'posture', 'confidence', 'basis', 'basis_label')},
              'triggers': sorted({c['trigger'] for c in changed or []}), 'changes': [c['text'] for c in changed or []],
              'sections': [dict(s, items=list(dict.fromkeys(s['items']))) for s in sections if s['items']],
              'scores': {a['symbol']: a['decision']['score'] for a in decided}}
    if kind == 'INTRADAY':
        result['since'] = since
    schedule = morning_schedule(now) if kind == 'MORNING' else None
    if schedule:
        result.update(scheduled_at=schedule['scheduled_at'], scheduled_local=schedule['scheduled_local'],
                      late=schedule['late'], minutes_late=schedule['minutes_late'])
    return result
