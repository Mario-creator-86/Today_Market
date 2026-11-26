import json

import pytest
from fastapi.testclient import TestClient

from backend.api import social_status
from backend.config import get_settings
from backend.database import utcnow
from backend.main import create_app

HEADERS = {'X-Admin-Token': 'test-admin-token'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/api.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    monkeypatch.setenv('ADMIN_TOKEN', 'test-admin-token')
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


@pytest.mark.parametrize('path', ['/api/verification', '/api/social', '/api/positioning', '/api/positioning/BTC',
                                  '/api/autolearn/versions', '/api/notifications/push-key', '/api/assets/BTC/comparables',
                                  '/api/briefs', '/api/regime'])
def test_new_reads(client, path):
    assert client.get(path).status_code == 200, client.get(path).text


def test_performance_rejects_unknown_grouping(client):
    assert client.get('/api/performance', params={'group_by': 'not_a_dimension'}).status_code == 422
    assert client.get('/api/performance', params={'group_by': 'asset', 'population': 'actionable'}).status_code == 200


def test_dashboard_exposes_strip_watch_and_regime_drivers(client):
    body = client.get('/api/dashboard').json()
    assert body['market_strip'][:2] == ['BTC', 'ETH'] and len(body['market_strip']) == 13
    assert 'drivers' in body['regime'] and 'active' in body['watch']


def test_decision_audit_record_is_reproducible(client):
    client.app.state.hub.ingest_quote(__import__('backend.schemas', fromlist=['Quote']).Quote(
        symbol='BTC', price=100, timestamp=utcnow(), provider='binance', bid=99.9, ask=100.1))
    decisions = client.post('/api/admin/recalculate', headers=HEADERS).json()
    btc = next(d for d in decisions if d['symbol'] == 'BTC')
    record = client.get(f"/api/decisions/{btc['id']}/audit").json()
    assert record['decision_id'].startswith('D-') and record['input_hash'] == btc['input_hash']
    assert record['versions']['content_hash'] and 'bars' in record['based_on']


def test_source_override_is_audited_and_changes_trust(client):
    assert client.put('/api/admin/sources/fed', json={'trust_weight': 0.5, 'reason': 'test'}).status_code == 401
    response = client.put('/api/admin/sources/fed', json={'trust_weight': 0.5, 'reason': 'desk review'}, headers=HEADERS)
    assert response.status_code == 200
    fed = next(s for s in client.get('/api/sources').json() if s['id'] == 'fed')
    assert fed['trust_weight'] == 0.5
    logs = client.get('/api/admin/audit-logs', headers=HEADERS).json()
    assert any(row['action'] == 'source_override' and row['reason'] == 'desk review' for row in logs)


def test_config_edit_is_versioned_and_invalid_edits_are_rolled_back(config_copy, client):
    path = config_copy / 'rules.json'  # a private copy: the running app never sees the edit
    original = path.read_text(encoding='utf-8')
    try:
        weights = json.loads(original)['weights']
        bad = dict(weights, event=weights['event'] + 0.1)
        rejected = client.put('/api/admin/config/scoring', json={'weights': bad}, headers=HEADERS)
        assert rejected.status_code == 422 and path.read_text(encoding='utf-8') == original
        assert client.put('/api/admin/config/scoring', json={'nope': 1}, headers=HEADERS).status_code == 422
        accepted = client.put('/api/admin/config/scoring', json={'weights': weights}, headers=HEADERS)
        assert accepted.status_code == 200 and accepted.json()['version'] != json.loads(original)['version']
    finally:
        path.write_text(original, encoding='utf-8')


def evidence(group):
    return {'group': group, 'source_id': group, 'relation': 'supports', 'weight': 0.8, 'direct': True,
            'discovery_only': False, 'published_at': utcnow().isoformat(), 'kind': 'wire'}


# PARTIALLY_CONFIRMED maps to CORROBORATED only with two independent supporting groups (UI audit EV-24): one trusted
# source alone is not 'independent sources partially agree'.
@pytest.mark.parametrize('post, status, groups, expected', [
    ({'official': True}, 'UNCONFIRMED', [], 'OFFICIAL STATEMENT'),
    ({}, 'PARTIALLY_CONFIRMED', ['reuters', 'ap'], 'CORROBORATED'),
    ({}, 'PARTIALLY_CONFIRMED', ['reuters'], 'UNVERIFIED'),
    ({}, 'PARTIALLY_CONFIRMED', ['reuters', 'reuters'], 'UNVERIFIED'),
    ({}, 'CONFIRMED', [], 'CONFIRMED'), ({'official': True}, 'CONTRADICTED', [], 'CONTRADICTED'),
    ({}, 'UNCONFIRMED', [], 'UNVERIFIED')])
def test_x_status_labels(post, status, groups, expected):
    event = {'verification_status': status, 'event_type': 'NEWS', 'evidence': [evidence(g) for g in groups]}
    assert social_status(post, event) == expected


def test_fx_rows_compare_both_central_banks(client):
    store = client.app.state.hub.store
    now = utcnow().isoformat()
    store.put('central_bank_state', 'fed', {'bank': 'fed', 'rate': 4.5, 'yield_2y': 4.8, 'expected_next_rate': 4.25,
                                             'as_of': now, 'verified': True})
    store.put('central_bank_state', 'ecb', {'bank': 'ecb', 'rate': 2.0, 'yield_2y': 2.1, 'expected_next_rate': 2.0,
                                             'as_of': now, 'verified': True})
    rows = {r['pair']: r for r in client.get('/api/fx').json()}
    eurusd = rows['EURUSD']
    assert eurusd['other_bank'] == 'ecb' and eurusd['other_rate'] == 2.0 and eurusd['fed_rate'] == 4.5
    # Base minus quote: EUR 2.0 - USD 4.5.
    assert eurusd['policy_differential'] == pytest.approx(-2.5) and eurusd['yield_differential'] == pytest.approx(-2.7)
    usdjpy = rows['USDJPY']
    assert usdjpy['other_bank'] == 'boj' and usdjpy['policy_differential'] is None  # BoJ state absent: nothing inferred


def test_each_analysis_cycle_records_the_configuration_it_uses(client):
    from backend.config import rules
    from backend.database import RECORDS
    config = rules()
    key = f"{config['version']}:{config['content_hash'][:16]}"
    store = client.app.state.hub.store
    with store.session() as session:  # as if the files were edited by hand after startup
        session.delete(session.get(RECORDS['configuration_versions'], key))
        session.commit()
    client.post('/api/admin/recalculate', headers=HEADERS)
    assert store.get('configuration_versions', key)['content_hash'] == config['content_hash']


def test_config_edits_keep_the_structure_and_types(config_copy, client):
    path = config_copy / 'rules.d' / 'refresh.json'  # a private copy: the running app never sees the edit
    original = path.read_text(encoding='utf-8')
    try:
        cadences = json.loads(original)['refresh']['cadences']
        kind = next(k for k, v in cadences.items() if isinstance(v.get('seconds'), int))
        for bad in ({kind: {'seconds': 'fast'}}, {kind: {'seconds': -5}}, {'not_a_kind': {'seconds': 10}}):
            response = client.put('/api/admin/config/refresh', json={'cadences': bad}, headers=HEADERS)
            assert response.status_code == 422 and path.read_text(encoding='utf-8') == original, bad
        ok = client.put('/api/admin/config/refresh', json={'cadences': {kind: {'seconds': cadences[kind]['seconds']}}}, headers=HEADERS)
        assert ok.status_code == 200 and not path.with_suffix('.json.tmp').exists()
    finally:
        path.write_text(original, encoding='utf-8')


def test_every_admin_write_is_audited(client):
    client.post('/api/admin/recalculate', headers=HEADERS)
    client.post('/api/admin/calibration/BTC/candidate', headers=HEADERS)
    actions = {row['action'] for row in client.get('/api/admin/audit-logs', headers=HEADERS).json()}
    assert {'recalculate', 'calibration_candidate'} <= actions


def test_oversized_request_bodies_are_rejected(client):
    huge = {'endpoint': 'https://fcm.googleapis.com/fcm/send/' + 'x' * 1_100_000, 'keys': {}}
    assert client.post('/api/notifications/push-subscriptions', json=huge).status_code == 413
    assert client.get('/api/dashboard').status_code == 200
