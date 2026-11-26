"""The frontend fixture frontend/tests/fixtures/event-risk.real.json is a REAL event_risk snapshot and interpretation block
(scripts/export_event_fixture.py runs the real code paths on deterministic inputs, a temporary store, no network and
no .env). It must match what the backend produces now, so the frontend tests exercise the actual payload."""
import json

from scripts.export_event_fixture import OUTPUT, REGENERATE, build, render


def test_frontend_event_fixture_is_up_to_date():
    current = OUTPUT.read_text(encoding='utf-8') if OUTPUT.exists() else ''
    assert current == render(), f'{OUTPUT.name} is stale: regenerate it with `{REGENERATE}`'


def test_frontend_event_fixture_covers_the_contract_scenarios():
    data = json.loads(OUTPUT.read_text(encoding='utf-8')) if OUTPUT.exists() else build()
    er, block = data['event_risk'], data['interpretation']
    fed = er['next_event']
    assert fed['event_class'] == 'FOMC' and fed['minutes_until_event'] == 45 and fed['is_decision'] is True
    assert fed['sub_events'][0]['sub_event'] == 'PRESS_CONFERENCE' and er['mode']['active'] and er['mode']['time_utc']
    events = {e['event_class']: e for e in er['events']}
    assert events['US_CPI']['consensus'] == 'NO CONSENSUS' and events['US_CPI']['data_incomplete_flag'] is True
    assert events['EARNINGS']['consensus']['label'] == 'Finnhub estimate' and events['EARNINGS']['time_known'] is False
    assert er['active_events'][0]['event_class'] == 'GEO_ENERGY' and er['active_events'][0]['timing'] == 'ANYTIME'
    assert er['potential_events'][0]['status'] == 'POTENTIAL EVENT · UNCONFIRMED'
    post = er['post_event'][0]
    assert post['divergence'] is True and post['reactions']['US2Y']['15m']['unit'] == 'bp'
    assert set(post['reactions']['NASDAQ']['15m']) == {'change', 'unit', 'status'}
    statuses = {c['asset']: c['trade_status'] for c in er['trade_watch']}
    assert statuses['BTC'] == 'WAIT FOR FED' and statuses['BRENT'] == 'CONDITIONAL LONG' and statuses['MSFT'] == 'LONG'
    assert block['source'] in {'ai', 'deterministic'} and block['output']['ai_view']
