from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from backend import briefs as brief_rules
from backend import calibration, decision, event_risk, notifications, positioning, refresh
from backend.analytics import relative_policy
from backend.api_admin import admin_router
from backend.config import rules, rules_view
from backend.database import Decision, utcnow
from backend.engines import aware, confidence
from backend.event_risk import calendar_context
from backend.event_risk import history as event_history
from backend.llm_limiter import LIMITER
from backend.service import HubService, event_risk_error

SOCIAL_STATUS = {'CONTRADICTED': 'CONTRADICTED', 'FALSE': 'CONTRADICTED', 'CONFIRMED': 'CONFIRMED',
                 'PARTIALLY_CONFIRMED': 'CORROBORATED'}


def independent_groups(event: dict) -> int:
    """Independent source groups currently supporting the event, counted by the verification rule itself."""
    evidence = event.get('evidence') or []
    return confidence(evidence, utcnow(), event_type=event.get('event_type'))['independent_sources'] if evidence else 0


def social_status(post: dict, event: dict | None) -> str:
    """X display status (spec §9): derived from the linked event and the official-account registry. CORROBORATED
    means independent sources partially agree, so a PARTIALLY_CONFIRMED event backed by a single independence group
    stays UNVERIFIED for the post."""
    status = (event or {}).get('verification_status')
    if status in {'CONTRADICTED', 'FALSE'}:
        return 'CONTRADICTED'
    if post.get('official'):
        return 'OFFICIAL STATEMENT'
    if status == 'PARTIALLY_CONFIRMED' and independent_groups(event or {}) < rules_view()['ui']['social_corroboration_min_groups']:
        return 'UNVERIFIED'
    return SOCIAL_STATUS.get(status or '', 'UNVERIFIED')


def _upcoming(row: dict, now: datetime, date_only: timedelta, inactive: set[str]) -> bool:
    """Still ahead and still scheduled; a date-only row (time_known false) counts until its local day is over."""
    if str(row.get('status') or 'SCHEDULED').upper() in inactive or not row.get('scheduled_at'):
        return False
    at = aware(row['scheduled_at'])
    return (at + date_only if row.get('time_known') is False else at) >= now


def with_date_window(row: dict) -> dict:
    """A date-only calendar row (time_known false) with its release window, as the event layer times it
    (event_risk.date_window: the class window, else the row's session such as after the US close, else the publisher's
    local day): date_window {start_utc, end_utc, label, zone}. Timed rows and rows without a date are unchanged."""
    if row.get('time_known') is not False or not row.get('scheduled_at'):
        return row
    try:
        found = event_risk.date_window(row, event_risk.classify_row(row), aware(row['scheduled_at']))
    except (KeyError, TypeError, ValueError):  # a malformed row keeps the UI's 24 h fallback
        return row
    return dict(row, date_window={'start_utc': found['start'].astimezone(UTC).isoformat(),
                                  'end_utc': found['end'].astimezone(UTC).isoformat(), 'label': found['label'],
                                  'zone': found['zone']})


def next_rate_decisions(rows: list[dict], banks: list[str], now: datetime) -> list[dict]:
    """Each bank's next RATE DECISION among calendar rows (never a speech, testimony, minutes or statement): rows with
    is_decision of that bank or of its configured event class. A bank without a decision calendar source, or without a
    decision inside the fetched calendar horizon, gets scheduled_at None and a plain reason instead of a guess."""
    config = rules_view()  # shared read-only view: called once per pair
    specs = config['official_data']['central_bank_meetings']['banks']
    days = config['provider_routing']['calendars']['fetch_days']
    date_only = timedelta(hours=config['ui']['calendar_date_only_hours'])
    inactive = {str(s).upper() for s in config['refresh']['watch']['inactive_statuses']}
    decisions = [r for r in rows if r.get('is_decision') and _upcoming(r, now, date_only, inactive)]
    result = []
    for bank in banks:
        spec = specs.get(bank) or {}
        short, full = spec.get('short') or bank.upper(), spec.get('name') or bank.upper()
        entry: dict = {'bank': bank, 'bank_name': short, 'name': None, 'scheduled_at': None, 'time_known': None,
                       'reason': None}
        found = next((r for r in decisions if r.get('bank') == bank
                      or (spec.get('event_class') and r.get('event_class') == spec['event_class'])), None)
        if found:
            entry.update(name=found.get('name'), scheduled_at=found['scheduled_at'], time_known=found.get('time_known') is not False)
        elif not spec.get('event_class'):
            entry['reason'] = f'No rate-decision calendar source for the {full}'
        else:
            entry['reason'] = f'No {short} rate decision in the calendar for the next {days} days'
        result.append(entry)
    return result


def router(hub: HubService, admin) -> APIRouter:
    api = APIRouter(prefix='/api')

    @api.get('/dashboard')
    def dashboard():
        return hub.dashboard()

    @api.get('/assets')
    def assets():
        return hub.assets()  # entries carry the per-asset event_risk overlay beside the published decision

    @api.get('/assets/{symbol}')
    def asset(symbol: str):
        try:
            return hub.asset(symbol.upper())
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @api.get('/assets/{symbol}/comparables')
    def comparables(symbol: str, side: int | None = Query(None, ge=-1, le=1), limit: int = Query(50, ge=1, le=500)):
        from backend.evaluation import comparables as find
        return find(hub.store, symbol.upper(), side, limit)

    @api.get('/events')
    def events(limit: int = Query(100, ge=1, le=500)):
        return hub.events(limit)

    @api.get('/events/upcoming')
    def upcoming_events(min_relevance: float = Query(0, ge=0, le=100)):
        """Every scheduled event not yet resolved, in time order, including those below the dashboard threshold
        (docs/EVENT_AWARENESS.md §4). Declared before /events/{event_id} so 'upcoming' is not read as an id. An event-risk
        failure is logged and answered 503 with a short reason (never a stack trace)."""
        try:
            return hub.upcoming_events(min_relevance)
        except Exception as exc:
            raise HTTPException(503, event_risk_error('events_upcoming', exc)) from None

    @api.get('/event-risk')
    def event_risk():
        """The dashboard event_risk block on its own (503 with a short reason when the event layer fails)."""
        try:
            return hub.event_risk_snapshot()
        except Exception as exc:
            raise HTTPException(503, event_risk_error('event_risk', exc)) from None

    @api.get('/events/{event_id}')
    def event(event_id: str):
        try:
            return hub.event_detail(event_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @api.get('/verification')
    def verification(status: str | None = None, limit: int = Query(50, ge=1, le=200)):
        """Per-event transparency (spec §32): source, times, trust, result, supporting/contradicting counts."""
        rows = []
        for item in hub.events(limit):
            if status and item['verification_status'] != status:
                continue
            evidence = item['evidence']
            rows.append({'event_id': item['id'], 'summary_fact': item['summary_fact'], 'verification_status': item['verification_status'],
                         'fact_confidence': item['fact_confidence'], 'first_seen_utc': item['first_seen_utc'],
                         'supporting': sum(e['relation'] == 'supports' for e in evidence),
                         'contradicting': sum(e['relation'] == 'contradicts' for e in evidence),
                         'injection_suspected': item.get('injection_suspected', False),
                         'evidence': [{k: e.get(k) for k in ('source', 'source_id', 'tier', 'kind', 'weight', 'official', 'published_at',
                                                            'ingested_at', 'relation', 'direct', 'discovery_only', 'syndicated_from', 'url')}
                                      for e in evidence]})
        return rows

    @api.get('/providers')
    def providers():
        from backend import metrics
        detail = {row['name']: row for row in metrics.provider_rows(hub.providers)}
        return [dict(p, metrics=detail.get(p['name'])) for p in hub.health()]

    @api.get('/sources')
    def sources():
        """The source registry with each source's `homepage` (a working https URL from config/rules.d/sources.json, null
        when none is known: the registry domain is an ingestion match suffix, not a site) and the `status` and
        `last_success` of the provider that actually delivers it (config `provider`, e.g. benzinga -> alpaca, x-* -> x;
        its own id otherwise; null when no running provider delivers it)."""
        config = rules()
        weights = config['source_weights']
        configured = {s['id']: s for s in config['sources']}
        health = {p['name']: p for p in hub.health()}
        overrides = {r['id']: r for r in hub.store.list('source_reliability')}
        result = []
        for source in hub.store.list('sources'):
            override = overrides.get(source['id'], {})
            spec = configured.get(source['id'], source)
            home = spec.get('homepage')
            delivered = health.get(str(spec.get('provider') or source['id']), {})
            result.append(dict(source, trust_weight=override.get('trust_weight', weights.get(source.get('kind'), 0.1)),
                               tier=override.get('tier', source.get('tier')), enabled=override.get('enabled', True),
                               homepage=home if isinstance(home, str) and home.startswith('https://') else None,
                               status=delivered.get('status'), last_success=delivered.get('last_success')))
        return result

    @api.get('/calendar')
    def calendar(window: bool = True, min_impact: int = Query(0, ge=0, le=10), category: str | None = None,
                 days: int | None = Query(None, ge=1, le=120)):
        # The calendar page shows the whole fetched horizon; the dashboard uses the shorter UI window.
        rows = hub.calendar(window=window, upcoming_hours=(days or rules()['provider_routing']['calendars']['fetch_days']) * 24)
        rows = [with_date_window(r) for r in rows
                if (r.get('impact') or 0) >= min_impact and (category is None or r.get('category') == category)]
        # EVR-7: one event shows one relevance and level everywhere. The calendar scores its rows with the inputs the
        # dashboard's event layer uses (display regime, news events, stored reaction history); without them the regime
        # sensitivity fell back to its static mean and the same event read 39 LOW here and 40.4 MODERATE on the dashboard.
        now = utcnow()
        try:
            inputs = hub.event_risk_inputs(now)
            context = {'regime': inputs['regime'], 'news': inputs['events'], 'stored_history': event_history(hub.store)}
        except Exception as exc:  # the calendar never fails with the event layer's inputs: static relevance then
            event_risk_error('calendar', exc)
            context = {}
        return calendar_context(rows, now, **context)

    @api.get('/fx')
    def fx():
        """Per pair: price, published bias, both central banks (display names, rates with their basis) and each bank's
        next rate decision. next_meeting is the soonest of the pair's decisions (DXY: the Fed); next_meetings lists
        every bank of the pair, with a reason when its next decision is unknown."""
        config = rules()
        banks_by_currency = config['decision']['fx_banks']
        names = config['official_data']['central_bank_meetings']['banks']
        banks = {str(row['bank']): row for row in hub.store.list('central_bank_state') if row.get('bank')}
        now = utcnow()
        # The whole fetched calendar horizon: rate decisions are weeks apart, beyond the dashboard's 168 h window.
        calendar = hub.calendar(window=True, upcoming_hours=config['provider_routing']['calendars']['fetch_days'] * 24)
        result = []
        for asset in hub.assets(event_overlay=False):
            symbol = asset['symbol']
            if asset['asset_class'] != 'fx' and symbol != 'DXY':
                continue
            base_ccy, quote_ccy = (symbol[:3], symbol[3:]) if len(symbol) == 6 else ('USD', None)
            base = banks.get(banks_by_currency.get(base_ccy, ''))
            counter = banks.get(banks_by_currency.get(quote_ccy or '', ''))
            # "Other" is the non-USD bank of the pair (the one compared with the Fed).
            other_ccy = quote_ccy if base_ccy == 'USD' else base_ccy
            other_bank = banks_by_currency.get(other_ccy or '') if quote_ccy else None
            other = banks.get(other_bank or '')
            relative = relative_policy(base, counter) if quote_ccy else {'reason': 'DXY is a basket; compare component pairs'}

            def diff(field: str, left: dict | None = base, right: dict | None = counter) -> float | None:
                a, b = (left or {}).get(field), (right or {}).get(field)
                return round(a - b, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
            fed = banks.get('fed')
            pair_banks = [b for b in dict.fromkeys([banks_by_currency.get(base_ccy), banks_by_currency.get(quote_ccy or '')]) if b]
            meetings = next_rate_decisions(calendar, pair_banks, now)
            scheduled = sorted((m for m in meetings if m['scheduled_at']), key=lambda m: aware(m['scheduled_at']))
            latest = asset.get('decision') or {}
            result.append({'pair': symbol, 'symbol': symbol, 'price': asset['quote']['price'] if asset['quote'] else None,
                           'quote': asset['quote'], 'bias': latest.get('category', 'WAIT'), 'score': latest.get('score'),
                           'fed_rate': (fed or {}).get('rate'), 'fed_rate_range': (fed or {}).get('policy_rate_range'),
                           'fed_rate_basis': (fed or {}).get('basis'), 'other_bank': other_bank,
                           'other_bank_name': (names.get(other_bank or '') or {}).get('short') if other_bank else None,
                           'other_rate': (other or {}).get('rate'), 'other_rate_basis': (other or {}).get('basis'),
                           # Differentials are base minus quote currency, in percentage points.
                           'policy_differential': diff('rate'), 'yield_differential': diff('yield_2y'),
                           'repricing_differential': relative.get('repricing_differential'), 'relative_basis': relative.get('reason'),
                           'next_meeting': {k: scheduled[0][k] for k in ('bank', 'bank_name', 'name', 'scheduled_at', 'time_known')}
                           if scheduled else None, 'next_meetings': meetings,
                           'confirmation': latest.get('confirmation'), 'invalidation': latest.get('invalidation'),
                           'as_of': max(filter(None, [(base or {}).get('as_of'), (counter or {}).get('as_of')]), default=None)})
        return result

    @api.get('/positioning/{currency}')
    def positioning_summary(currency: str):
        currency = currency.upper()
        if currency not in rules()['provider_routing']['deribit']['currencies']:
            raise HTTPException(404, 'Unsupported currency')
        options, futures, as_of = positioning.stored_rows(hub.store, currency)
        return positioning.summarize(currency, options, futures, utcnow(), as_of)

    @api.get('/positioning')
    def positioning_reports(limit: int = Query(200, ge=1, le=2000)):
        return hub.store.list('positioning_reports', limit)

    @api.get('/social')
    def social(limit: int = Query(100, ge=1, le=500)):
        result = []
        for post in hub.store.list('social_posts', limit):
            linked = None
            if post.get('event_id'):
                try:
                    linked = hub.event(post['event_id'])
                except ValueError:
                    linked = None
            # A development-stub post has no real X status: it never gets a live-looking link.
            result.append({'id': post['id'], 'text': post.get('text'), 'author_username': post.get('author_username'),
                           'created_at': post.get('created_at'), 'url': None if post.get('stub') else post.get('url'),
                           'event_id': post.get('event_id'),
                           'official': bool(post.get('official')), 'stub': bool(post.get('stub')),
                           'verification_status': social_status(post, linked)})
        return result

    @api.get('/models')
    def models():
        return hub.store.list('ai_models')

    @api.get('/settings')
    def settings():
        channels = notifications.build_channels(hub.store)
        return {'scoring': {k: rules()[k] for k in ('version', 'weights', 'thresholds', 'categories', 'content_hash')},
                'refresh': refresh.cadence_table(), 'alerts': notifications.alert_settings(channels),
                'roles': hub.store.list('ai_role_assignments'), 'models': models(), 'sources': sources(),
                'providers': hub.health(), 'provider_mode': hub.settings.provider_mode, 'admin_mode': hub.settings.admin_auth,
                'autolearn': dict(rules().get('autolearn', {}), versions=calibration.versions(hub.store)),
                'editable': ['refresh', 'alerts', 'scoring', 'sources', 'roles', 'ai_budget']}

    @api.get('/ai/usage')
    def ai_usage():
        """AI requests of the current budget day (Europe/Bucharest) against the daily budget, per role, the pace
        window, the last 7 days and the LLM limiter (docs/AI_AGENTS.md)."""
        return hub.ai.budget.usage(limiter=LIMITER)

    @api.get('/performance')
    def performance(group_by: list[str] = Query(default=[]), population: Literal['all', 'actionable', 'counterfactual'] = 'all',
                    horizon: str | None = None):
        from backend.evaluation import performance as report
        try:
            return report(hub.store, group_by or None, population, horizon)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.get('/autolearn/versions')
    def autolearn_versions():
        return calibration.versions(hub.store)

    @api.get('/brief')
    def brief(kind: Literal['MORNING', 'INTRADAY'] | None = None):
        """The newest stored brief of the kind. A morning brief is the brief of ONE local day (RI-5): the one of an
        earlier day is never served as today's (a stored brief is text and keeps the labels of the day and of the code
        that composed it), so before today's brief exists the answer is the empty brief with the reason."""
        today = brief_rules.local_now(utcnow()).date().isoformat()
        kinds = {kind} if kind else {'MORNING', 'INTRADAY'}
        latest = next((b for b in hub.store.list('briefs', 200) if b.get('kind') in kinds
                       and (b.get('kind') != 'MORNING' or b.get('local_date') == today)), None)
        if latest:
            return latest
        waiting = brief_rules.morning_state(utcnow())[1]  # AFP-05: why the brief is not composed yet, when known
        reason = f'No morning brief for {today} yet' + (f': {waiting}' if waiting else '')
        return {'kind': kind, 'generated_at': None, 'sections': [], 'changes': [], 'local_date': today,
                **({'reason': reason} if kind == 'MORNING' else {})}

    @api.get('/briefs')
    def briefs(limit: int = Query(20, ge=1, le=100)):
        return [{k: b.get(k) for k in ('id', 'kind', 'generated_at', 'local_date', 'triggers', 'changes', 'late')}
                for b in hub.store.list('briefs', limit)]

    @api.get('/regime')
    def regime():
        return hub.regime(last_close=True)  # display: falls back on the labelled last-close basis

    @api.get('/alerts')
    def alerts(limit: int = Query(100, ge=1, le=500)):
        deliveries: dict[str, list[dict]] = {}
        for row in hub.store.list('notification_deliveries', 1000):
            deliveries.setdefault(row.get('alert_id', ''), []).append(row)
        return [dict(a, deliveries=deliveries.get(a['id'], [])) for a in hub.alerts(limit)]

    @api.get('/decisions/{decision_id}')
    def decision_record(decision_id: str):
        with hub.store.session() as session:
            row = session.get(Decision, decision_id)
            if not row:
                raise HTTPException(404, 'Decision not found')
            return row.payload

    @api.get('/decisions/{decision_id}/audit')
    def decision_audit(decision_id: str):
        """Spec §52: every published decision is reproducible from its versions, inputs and audit."""
        return decision.audit_record(decision_record(decision_id))

    @api.get('/notifications/push-key')
    def push_key():
        channel = next((c for c in notifications.build_channels(hub.store) if c.name == 'webpush'), None)
        return {'public_key': channel.status().get('public_key') if channel else None}

    @api.post('/notifications/push-subscriptions')
    def push_subscribe(subscription: dict):
        try:
            return {'key': notifications.register_push_subscription(hub.store, subscription)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    api.include_router(admin_router(hub, admin))
    return api
