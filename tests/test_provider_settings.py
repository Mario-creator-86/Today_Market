import asyncio
import contextlib
import json
import logging
import os
import smtplib
import stat
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend import config, notifications, workers
from backend import provider_settings as ps
from backend.config import Settings, get_settings
from backend.database import Store, utcnow
from backend.notifications import NotificationSettings
from backend.runtime import Runtime
from backend.service import HubService, disable

HEADERS = {'X-Admin-Token': 'test-admin-token'}
# conftest blanks these ('' in the OS environment beats .env); settings under test come from a temporary .env.
BLANKED = ['OLLAMA_API_KEY', 'MASSIVE_API_KEY', 'FMP_API_KEY', 'X_BEARER_TOKEN', 'REUTERS_API_KEY', 'AP_API_KEY',
           'OPENAI_API_KEY', 'CONGRESS_API_KEY', 'FINNHUB_API_KEY', 'FRED_API_KEY', 'BLS_API_KEY', 'TELEGRAM_BOT_TOKEN',
           'SMTP_PASSWORD', 'WEBPUSH_VAPID_PRIVATE_KEY']
GROUP_KEYS = {'id', 'label', 'category', 'description', 'signup_url', 'pricing', 'status', 'providers', 'fields', 'test'}
FIELD_KEYS = {'name', 'env', 'label', 'kind', 'secret', 'required', 'options', 'min', 'max', 'help', 'placeholder',
              'configured', 'source', 'value', 'editable'}
FRED_OK = {'observations': [{'date': '2026-09-25', 'value': '4.10', 'realtime_start': '2026-09-26',
                             'realtime_end': '2026-09-26'}]}


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    monkeypatch.setattr(config, 'ENV_FILE', path)
    for name in BLANKED:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    ps._tested.clear()
    yield path
    ps._tested.clear()
    get_settings.cache_clear()


@pytest.fixture
def start(env_file, tmp_path, monkeypatch):
    """start(dotenv text) -> TestClient of an app whose settings come from the temporary .env."""
    from backend.main import create_app
    stack = contextlib.ExitStack()

    def run(text: str = '') -> TestClient:
        env_file.write_text(text, encoding='utf-8')
        monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/api.db')
        monkeypatch.setenv('ENABLE_INGESTION', 'false')
        monkeypatch.setenv('ADMIN_TOKEN', 'test-admin-token')
        get_settings.cache_clear()
        return stack.enter_context(TestClient(create_app()))
    with stack:
        yield run


@pytest.fixture
def hub(env_file, tmp_path):
    store = Store(f'sqlite:///{tmp_path}/hub.db')
    store.initialize()
    result = HubService(store, get_settings())
    result.initialize()
    return result


@pytest.fixture
def runtime(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/loops.db')
    store.initialize()
    service = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    service.initialize()
    return Runtime(service)


def respond(monkeypatch, status=200, body=None, delay=0.0, calls=None):
    async def request(self, method, url, **kwargs):
        if calls is not None:
            calls.append(url)
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(status, json=body if body is not None else {}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)


def no_network(monkeypatch):
    async def request(self, method, url, **kwargs):
        raise AssertionError('no network call expected')
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)


def group(client, group_id):
    return next(g for g in client.get('/api/admin/provider-settings', headers=HEADERS).json()['groups'] if g['id'] == group_id)


def audit_rows(client, action):
    return [r for r in client.get('/api/admin/audit-logs', headers=HEADERS).json() if r['action'] == action]


async def run_fresh(target, service, **options):
    ps._tested.pop(target, None)  # the 10 s limit is covered by its own test
    return await ps.run_test(service, target, channel_options=options)


# --- registry ------------------------------------------------------------------------------------------------------

def test_registry_fields_exist_on_their_settings_class_and_ids_are_unique():
    registry = ps.registry()
    groups = registry['groups']
    ids = [g['id'] for g in groups]
    assert len(ids) == len(set(ids)) == 18
    categories = [c['id'] for c in registry['categories']]
    assert categories == ['ai', 'market', 'macro', 'calendars', 'filings', 'news', 'alerts', 'general']
    classes = {'main': Settings, 'notifications': NotificationSettings}
    names = [f['name'] for g in groups for f in g['fields']]
    assert len(names) == len(set(names))
    for spec in groups:
        assert spec['category'] in categories and bool(spec.get('providers') is not None) != bool(spec.get('channel'))
        assert spec['test']['label'] in {'Test connection', 'Send test message'}
        for field in spec['fields']:
            assert field['name'] in classes[field['settings']].model_fields, field['name']
            assert field['kind'] in {'secret', 'url', 'text', 'email', 'email_list', 'int', 'bool', 'select'}
            assert (field['kind'] == 'secret') == (classes[field['settings']].model_fields[field['name']].annotation is SecretStr)
    assert {g['channel'] for g in groups if g.get('channel')} == set(ps.CHANNELS)
    assert not {'admin_token', 'database_url', 'redis_url', 'cors_origins'} & set(names)


def test_affected_providers_follow_groups_mode_and_llm():
    names, llm = ps.affected(['provider_mode'])
    assert {'massive', 'massive-stub', 'x-stub', 'calendar', 'congress_api-stub', 'reuters', 'ap-stub'} <= names
    assert 'fred' not in names and not llm
    # The BLS release-schedule calendar identifies itself with the same SEC contact: a new contact rebuilds it too.
    assert ps.affected(['sec_user_agent']) == ({'sec_edgar', 'sec_fulltext', 'bls_release_calendar'}, False)
    assert ps.affected(['llm_provider']) == (set(), True) and ps.affected(['ollama_api_key']) == (set(), True)
    assert ps.affected(['smtp_host']) == (set(), False)


# --- validation ----------------------------------------------------------------------------------------------------

FIELDS = ps.fields()


@pytest.mark.parametrize('name, raw, expected', [
    ('fred_api_key', '  abc-123  ', 'abc-123'), ('fred_api_key', None, None),
    ('ollama_base_url', 'https://ollama.com/api', 'https://ollama.com/api'),
    ('ollama_base_url', 'http://localhost:11434/api', 'http://localhost:11434/api'),
    ('ollama_base_url', 'http://127.0.0.1:11434/api', 'http://127.0.0.1:11434/api'),
    ('ollama_base_url', 'http://[::1]:11434/api', 'http://[::1]:11434/api'),
    ('sec_user_agent', 'Jane Doe jane@firm.io', 'Jane Doe jane@firm.io'),
    ('webpush_subject', 'mailto:ops@firm.io', 'mailto:ops@firm.io'), ('webpush_subject', 'https://firm.io', 'https://firm.io'),
    ('smtp_from', 'alerts@firm.io', 'alerts@firm.io'), ('smtp_to', 'a@firm.io,b@firm.io', 'a@firm.io, b@firm.io'),
    ('smtp_port', 465, '465'), ('smtp_port', '2525', '2525'), ('smtp_starttls', False, 'false'),
    ('smtp_ssl', True, 'true'), ('llm_provider', 'openai', 'openai'), ('provider_mode', 'stub', 'stub'),
    ('telegram_chat_id', '-100123', '-100123')])
def test_valid_values_are_normalised(name, raw, expected):
    assert ps.validate_value(FIELDS[name], raw) == expected


@pytest.mark.parametrize('name, raw', [
    ('fred_api_key', ''), ('fred_api_key', '   '), ('fred_api_key', 'ab\ncd'), ('fred_api_key', 'ab\rcd'),
    ('fred_api_key', 'ab\x00cd'), ('fred_api_key', 'pre${HOME}post'), ('fred_api_key', 'x' * 4097), ('fred_api_key', 12345),
    ('ollama_base_url', 'http://ollama.com/api'), ('ollama_base_url', 'ftp://ollama.com'), ('ollama_base_url', 'ollama.com'),
    ('ollama_base_url', 'https://user:pw@ollama.com'), ('ollama_base_url', 'https://' + 'a' * 2050 + '.com'),
    ('sec_user_agent', 'MarketIntelligence contact@example.com'), ('sec_user_agent', 'No contact given'),
    ('webpush_subject', 'ops@firm.io'), ('smtp_from', 'not-an-address'), ('smtp_to', 'a@firm.io,,b@firm.io'),
    ('smtp_port', 0), ('smtp_port', 65536), ('smtp_port', True), ('smtp_port', 'abc'), ('smtp_port', 5.5),
    ('smtp_starttls', 'true'), ('llm_provider', 'claude'), ('provider_mode', 'LIVE')])
def test_invalid_values_name_the_field_never_the_value(name, raw):
    with pytest.raises(ValueError) as info:
        ps.validate_value(FIELDS[name], raw)
    message = str(info.value)
    assert message.startswith(f'{name}:')
    if isinstance(raw, str) and len(raw.strip()) > 5:  # (short words such as 'true' appear in the rule text)
        assert raw.strip() not in message


# --- .env writes ---------------------------------------------------------------------------------------------------

def test_write_values_keeps_other_lines_and_round_trips_special_characters(env_file):
    env_file.write_text('# Local settings\nADMIN_TOKEN=keep-me\n\n# FRED\nFRED_API_KEY=old\nSMTP_TO=a@firm.io\nLOG_LEVEL=INFO\n',
                        encoding='utf-8')
    tricky = "it's a \\ back#slash with  spaces\\"
    changed, removed = ps.write_values({'fred_api_key': tricky, 'openai_api_key': 'sk-new-123456', 'smtp_to': None,
                                        'smtp_port': 2525, 'smtp_starttls': False, 'bls_api_key': None})
    assert changed == ['fred_api_key', 'openai_api_key', 'smtp_port', 'smtp_starttls'] and removed == ['smtp_to']
    lines = env_file.read_text(encoding='utf-8').splitlines()
    assert lines[:4] == ['# Local settings', 'ADMIN_TOKEN=keep-me', '', '# FRED']
    assert lines[4].startswith('FRED_API_KEY=') and lines[5] == 'LOG_LEVEL=INFO'  # replaced in place; order kept
    values = dotenv_values(env_file)
    assert values['FRED_API_KEY'] == tricky and values['OPENAI_API_KEY'] == 'sk-new-123456' and 'SMTP_TO' not in values
    assert values['SMTP_PORT'] == '2525' and values['SMTP_STARTTLS'] == 'false' and values['ADMIN_TOKEN'] == 'keep-me'
    assert Settings(_env_file=env_file).fred_api_key.get_secret_value() == tricky
    assert NotificationSettings(_env_file=env_file).smtp_port == 2525
    assert not (env_file.parent / ps.TEMP_NAME).exists()


def test_write_values_creates_a_missing_env_file_and_keeps_key_spelling(env_file):
    assert ps.write_values({'fred_api_key': 'fred-123456'}) == (['fred_api_key'], [])
    assert dotenv_values(env_file) == {'FRED_API_KEY': 'fred-123456'}
    env_file.write_text('fred_api_key=lower\n', encoding='utf-8')
    ps.write_values({'fred_api_key': 'fred-654321'})
    assert dotenv_values(env_file) == {'fred_api_key': 'fred-654321'}  # no duplicate upper-case key


def test_rejected_writes_leave_the_env_file_untouched(env_file):
    original = '# keep\nQUOTE_STALE_SECONDS=0\nFRED_API_KEY=old\n'
    env_file.write_text(original, encoding='utf-8')
    with pytest.raises(ValueError, match='quote_stale_seconds') as info:  # the candidate must load in Settings
        ps.write_values({'fred_api_key': 'brand-new-value'})
    assert 'brand-new-value' not in str(info.value)
    for changes in ({'smtp_port': 0}, {'admin_token': 'x'}, {'fred_api_key': 'a\nb'}):
        with pytest.raises(ValueError):
            ps.write_values(changes)
    assert env_file.read_text(encoding='utf-8') == original
    assert not (env_file.parent / ps.TEMP_NAME).exists()
    env_file.write_text('CORS_ORIGINS=not-json\n', encoding='utf-8')
    with pytest.raises(ValueError, match='cors_origins'):
        ps.write_values({'fred_api_key': 'brand-new-value'})
    assert env_file.read_text(encoding='utf-8') == 'CORS_ORIGINS=not-json\n'


# --- GET / PUT -----------------------------------------------------------------------------------------------------

def test_every_endpoint_requires_the_admin_token(start):
    client = start()
    assert client.get('/api/admin/provider-settings').status_code == 401
    assert client.put('/api/admin/provider-settings/fred', json={'values': {}}).status_code == 401
    assert client.put('/api/admin/providers/fred/enabled', json={'enabled': False}).status_code == 401
    assert client.post('/api/admin/providers/fred/test').status_code == 401
    assert client.get('/api/admin/provider-settings', headers={'X-Admin-Token': 'wrong'}).status_code == 401


def test_summary_follows_the_contract_and_never_returns_secret_values(start, env_file):
    secrets = ['sekret-FRED-9f8e7d', '123456:sekret-TELEGRAM', 'sekret-SMTP-pw', 'sekret-OLLAMA-key']
    client = start(f"FRED_API_KEY={secrets[0]}\nTELEGRAM_BOT_TOKEN='{secrets[1]}'\nTELEGRAM_CHAT_ID=42\n"
                   f"SMTP_PASSWORD={secrets[2]}\nOLLAMA_API_KEY={secrets[3]}\nSMTP_HOST=smtp.firm.io\n")
    response = client.get('/api/admin/provider-settings', headers=HEADERS)
    assert response.status_code == 200
    assert not [s for s in secrets if s in response.text]
    body = response.json()
    assert body['env_file'] == str(env_file) and body['runtime'] == 'in_process'
    assert [g['id'] for g in body['groups']] == [g['id'] for g in ps.groups()]
    for view in body['groups']:
        assert set(view) == GROUP_KEYS and set(view['test']) == {'available', 'target', 'label', 'note'}
        assert all(set(f) == FIELD_KEYS for f in view['fields'])
    groups = {g['id']: g for g in body['groups']}
    fred = groups['fred']
    assert fred['providers'] == [{'name': 'fred', 'label': 'FRED (St. Louis Fed)', 'status': 'PENDING', 'enabled': True,
                                  'stub': False}]
    assert fred['fields'][0] | {'help': None} == {
        'name': 'fred_api_key', 'env': 'FRED_API_KEY', 'label': 'API key', 'kind': 'secret', 'secret': True,
        'required': True, 'options': None, 'min': None, 'max': None, 'help': None, 'placeholder': None,
        'configured': True, 'source': 'env_file', 'value': None, 'editable': True}
    assert groups['finnhub']['status'] == 'NOT CONFIGURED' and groups['llm']['status'] == 'CONFIGURED'
    assert groups['telegram']['status'] == 'CONFIGURED' and groups['webpush']['status'] == 'NOT CONFIGURED'
    email = {f['name']: f for f in groups['email']['fields']}
    assert email['smtp_host']['value'] == 'smtp.firm.io'
    assert email['smtp_port']['value'] == 587 and email['smtp_port']['source'] == 'default'
    assert email['smtp_starttls']['value'] is True and email['smtp_password']['value'] is None
    assert groups['fred']['test'] == {'available': True, 'target': 'fred', 'label': 'Test connection', 'note': None}
    agent = groups['sec']['fields'][0]  # no contact yet: empty, so the input shows its placeholder
    assert agent['value'] in (None, '') and agent['configured'] is False and agent['source'] == 'default'
    assert groups['mode']['test']['available'] is False
    rows = {p['name']: p for p in body['providers']}
    assert set(rows) == set(client.app.state.hub.providers)
    assert rows['fred'] == {'name': 'fred', 'label': 'FRED (St. Louis Fed)', 'status': 'PENDING', 'enabled': True,
                            'stub': False, 'group': 'fred', 'testable': True}
    assert rows['binance']['group'] is None and rows['binance']['testable']


def test_os_environment_wins_and_blocks_edits(start, env_file, monkeypatch):
    client = start('FRED_API_KEY=from-file-123\n')
    monkeypatch.setenv('FRED_API_KEY', 'os-level-key-999')
    field = group(client, 'fred')['fields'][0]
    assert field['source'] == 'environment' and field['editable'] is False and field['value'] is None
    response = client.put('/api/admin/provider-settings/fred', json={'values': {'fred_api_key': 'new-key-123456'}},
                          headers=HEADERS)
    assert response.status_code == 409 and 'FRED_API_KEY' in response.json()['detail']
    assert 'os-level-key-999' not in response.text
    assert env_file.read_text(encoding='utf-8') == 'FRED_API_KEY=from-file-123\n'


def test_put_rejects_unknown_groups_fields_and_bad_values_without_echoing_them(start, env_file):
    client = start()
    put = lambda group_id, body: client.put(f'/api/admin/provider-settings/{group_id}', json=body, headers=HEADERS)  # noqa: E731
    assert put('nope', {'values': {}}).status_code == 404
    assert put('fred', {'values': {'massive_api_key': 'x'}}).status_code == 422
    assert put('fred', {'fred_api_key': 'x'}).status_code == 422
    bad = put('fred', {'values': {'fred_api_key': 'leaked-SECRET\nvalue'}})
    assert bad.status_code == 422 and 'fred_api_key' in bad.json()['detail'] and 'leaked-SECRET' not in bad.text
    listed = put('fred', {'values': {'fred_api_key': ['leaked-SECRET']}})
    assert listed.status_code == 422 and 'leaked-SECRET' not in listed.text
    assert env_file.read_text(encoding='utf-8') == ''


def test_put_hot_applies_only_the_affected_instances_and_learns_the_secret(start, env_file):
    from backend import main
    client = start()
    hub = client.app.state.hub
    original, before = hub.providers, dict(hub.providers)
    assert hub.providers['fred'].health['status'] == 'NOT CONFIGURED'
    response = client.put('/api/admin/provider-settings/fred', json={'values': {'fred_api_key': 'fred-key-123456'}},
                          headers=HEADERS)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view['id'] == 'fred' and view['status'] == 'PENDING' and view['fields'][0]['configured']
    assert 'fred-key-123456' not in response.text
    assert hub.providers is not original and hub.providers['fred'] is not before['fred'] and hub.providers['fred'].configured
    assert all(hub.providers[name] is before[name] for name in before if name != 'fred')
    assert set(hub.providers) == set(before)
    assert dotenv_values(env_file) == {'FRED_API_KEY': 'fred-key-123456'} and hub.settings.fred_api_key.get_secret_value()
    [row] = audit_rows(client, 'provider_settings_change')
    assert row['group'] == 'fred' and row['changed'] == ['fred_api_key'] and row['removed'] == []
    assert 'fred-key-123456' not in json.dumps(row)
    record = logging.LogRecord('backend.test', logging.WARNING, __file__, 1, 'leak fred-key-123456 here', None, None)
    assert 'fred-key-123456' not in main.JsonFormatter().format(record) and 'fred-key-123456' in main._known_secrets
    removed = client.put('/api/admin/provider-settings/fred', json={'values': {'fred_api_key': None}}, headers=HEADERS)
    assert removed.json()['status'] == 'NOT CONFIGURED' and dotenv_values(env_file) == {}
    assert audit_rows(client, 'provider_settings_change')[0]['removed'] == ['fred_api_key']


def test_stub_mode_swaps_the_stub_for_the_real_adapter_when_a_key_is_saved(start):
    client = start('PROVIDER_MODE=stub\n')
    hub = client.app.state.hub
    assert 'massive-stub' in hub.providers and 'massive' not in hub.providers
    x_stub = hub.providers['x-stub']
    response = client.put('/api/admin/provider-settings/massive', json={'values': {'massive_api_key': 'massive-key-123456'}},
                          headers=HEADERS)
    assert response.status_code == 200
    assert 'massive' in hub.providers and 'massive-stub' not in hub.providers and hub.providers['massive'].configured
    assert hub.providers['x-stub'] is x_stub
    assert [p['name'] for p in response.json()['providers']] == ['massive']
    client.put('/api/admin/provider-settings/mode', json={'values': {'provider_mode': 'live'}}, headers=HEADERS)
    assert 'x' in hub.providers and 'x-stub' not in hub.providers and hub.settings.provider_mode == 'live'


def test_llm_settings_swap_the_bound_ai_provider(start):
    client = start()
    hub = client.app.state.hub
    assert hub.ai.provider is hub.ollama and 'openai' not in hub.providers
    put = lambda group_id, values: client.put(f'/api/admin/provider-settings/{group_id}', json={'values': values},  # noqa: E731
                                              headers=HEADERS)
    assert put('llm', {'llm_provider': 'openai'}).status_code == 200
    assert hub.ai.provider is hub.openai and hub.providers['openai'] is hub.openai and not hub.openai.configured
    old = hub.openai
    assert put('openai', {'openai_api_key': 'sk-test-123456'}).json()['status'] == 'PENDING'
    assert hub.openai is not old and hub.openai.configured and hub.ai.provider is hub.openai
    assert put('llm', {'llm_provider': 'ollama'}).status_code == 200
    assert hub.ai.provider is hub.ollama and 'openai' not in hub.providers and hub.providers['ollama'] is hub.ollama


def test_saved_alert_settings_reach_the_runtime_dispatcher(hub, env_file):
    runtime = Runtime(hub)
    dispatcher = runtime.dispatcher
    assert not {c.name: c for c in dispatcher.channels}['telegram'].configured
    env_file.write_text("TELEGRAM_BOT_TOKEN='123456:tok-abcdef'\nTELEGRAM_CHAT_ID=42\n", encoding='utf-8')
    hub.apply_settings(get_settings(), ['telegram_bot_token', 'telegram_chat_id'])
    assert runtime.dispatcher is dispatcher and {c.name: c for c in dispatcher.channels}['telegram'].configured


def test_celery_task_hubs_read_the_saved_settings(env_file, tmp_path, monkeypatch):
    monkeypatch.setattr(workers, 'settings', Settings(_env_file=None, database_url=f'sqlite:///{tmp_path}/worker.db'))
    env_file.write_text('FRED_API_KEY=worker-key-123\n', encoding='utf-8')
    assert workers._with_hub(lambda hub, _runtime: hub.providers['fred'].configured)
    env_file.write_text('', encoding='utf-8')
    assert not workers._with_hub(lambda hub, _runtime: hub.providers['fred'].configured)


# --- enable / disable ----------------------------------------------------------------------------------------------

def test_disable_and_enable_a_provider(start):
    client = start()
    hub = client.app.state.hub
    original = hub.providers['deribit']
    original.health['last_success'] = '2026-09-27T08:00:00+00:00'
    switch = lambda name, body: client.put(f'/api/admin/providers/{name}/enabled', json=body, headers=HEADERS)  # noqa: E731
    assert switch('nope', {'enabled': False}).status_code == 404
    assert switch('deribit', {'enabled': False, 'reason': 'x' * 201}).status_code == 422
    response = switch('deribit', {'enabled': False, 'reason': 'not needed'})
    assert response.json() == {'name': 'deribit', 'enabled': False, 'status': 'DISABLED'}
    assert hub.providers['deribit'] is original and not original.enabled and not original.configured
    identity = {'label', 'category', 'error_type'}  # descriptive fields a disabled row may keep
    assert {k: v for k, v in original.health.items() if k not in identity} == {
        'name': 'deribit', 'status': 'DISABLED', 'latency_ms': None, 'last_success': '2026-09-27T08:00:00+00:00',
        'error': None}
    original.health.update(status='OFFLINE', error='late answer of an in-flight call')
    assert next(r for r in hub.health() if r['name'] == 'deribit')['status'] == 'DISABLED'
    assert hub.store.get('providers', 'deribit')['reason'] == 'not needed'
    assert audit_rows(client, 'provider_disabled')[0]['provider'] == 'deribit'
    assert group(client, 'fred')['providers'][0]['enabled']
    row = next(p for p in client.get('/api/admin/provider-settings', headers=HEADERS).json()['providers'] if p['name'] == 'deribit')
    assert row['enabled'] is False and row['status'] == 'DISABLED'
    assert not HubService(hub.store, hub.settings).providers['deribit'].enabled  # Celery per-task hubs honour it
    assert switch('deribit', {'enabled': True}).json() == {'name': 'deribit', 'enabled': True, 'status': 'PENDING'}
    assert hub.providers['deribit'] is not original and getattr(hub.providers['deribit'], 'enabled', True)
    assert audit_rows(client, 'provider_enabled')[0]['provider'] == 'deribit'


def test_disabled_llm_is_never_called_and_leaves_no_ai_runs(runtime):
    hub = runtime.hub
    hub.ollama.configured = True
    hub.store.put('ai_models', 'm1', {'name': 'm1', 'availability': True, 'preferred_roles': []})
    hub.store.put('ai_role_assignments', 'research_planner', {'role': 'research_planner', 'primary': 'm1'})
    assert hub.ai.available('research_planner')
    hub.set_enabled('ollama', False)
    assert hub.ai.provider is hub.ollama and not hub.ai.available('research_planner')

    async def boom(*args, **kwargs):
        raise AssertionError('disabled LLM called')
    hub.ollama.chat = boom
    hub.ai.discover = boom
    assert asyncio.run(hub.ai.run_with_id('research_planner', {'evidence_ids': []})) == (None, None)
    assert hub.store.list('ai_runs') == []
    asyncio.run(runtime.discover())
    hub.set_enabled('ollama', True)
    assert hub.ai.provider is hub.ollama and getattr(hub.ollama, 'enabled', True)


class Boom:
    """A provider that fails the test if any loop calls it."""
    configured = True

    def __init__(self, name):
        self.name, self.health = name, {'name': name, 'status': 'ONLINE', 'last_success': 'earlier'}

    async def _boom(self, *args, **kwargs):
        raise AssertionError(f'{self.name} called while disabled')
    quotes = policy_rates = yields = hicp = hicp_flash = tga = inventories = series = latest = observations = _boom
    policy_rate = batch = documents = metrics = futures = search = positioning = calendar = _boom


@pytest.mark.asyncio
async def test_loops_never_call_disabled_providers_and_log_nothing(runtime, caplog):
    names = ['deribit', 'ecb_data', 'fred', 'eurostat', 'treasury_fiscal', 'eia_wpsr', 'bls_data', 'nyfed_rates',
             'gdelt_events', 'ofac_actions', 'ofac_sls', 'sec_fulltext', 'congress', 'x', 'finnhub', 'binance', 'coinbase',
             'treasury_yields', 'massive']
    runtime.hub.providers = {name: Boom(name) for name in names}
    for provider in runtime.hub.providers.values():
        disable(provider)
    runtime.previous_health = {name: {'name': name, 'status': 'ONLINE'} for name in names}
    caplog.set_level(logging.WARNING)
    for loop in (runtime.derivatives, runtime.poll_official, runtime.poll_discovery, runtime.poll_congress, runtime.social,
                 runtime.poll_equities, runtime.poll_market, runtime.poll_treasury_yields, runtime.poll_breaking):
        await loop()
    assert await runtime._call(runtime.hub.providers['fred'].latest) == []
    assert await runtime.ingest_from(runtime.hub.providers['congress']) == []
    assert runtime.hub.store.get('central_bank_state', 'fed') is not None  # computed without calling anyone
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert runtime.hub.store.list('alerts') == []  # ONLINE -> DISABLED is silent
    assert {row['status'] for row in runtime.hub.health()} == {'DISABLED'}


@pytest.mark.asyncio
async def test_disabled_binance_stream_never_connects_until_enabled(runtime, monkeypatch, caplog):
    from backend import runtime as loops
    connects = []

    def connect(*args, **kwargs):
        connects.append(args)
        raise OSError('offline in tests')
    monkeypatch.setattr(loops.websockets, 'connect', connect)
    monkeypatch.setattr(loops, 'STREAM_RECHECK_SECONDS', 0.01)
    runtime.hub.set_enabled('binance', False)
    caplog.set_level(logging.WARNING)
    task = asyncio.create_task(runtime.crypto_stream())
    await asyncio.sleep(0.1)
    assert connects == [] and runtime.hub.providers['binance'].health['status'] == 'DISABLED'
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    runtime.hub.set_enabled('binance', True)
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert len(connects) == 1 and runtime.hub.providers['binance'].health['status'] == 'DEGRADED'


def test_provider_alerts_stay_silent_into_and_out_of_disabled():
    now = utcnow()
    assert notifications.provider_alert({'status': 'ONLINE'}, {'name': 'x', 'status': 'DISABLED'}, now) is None
    assert notifications.provider_alert({'status': 'OFFLINE'}, {'name': 'x', 'status': 'DISABLED'}, now) is None
    assert notifications.provider_alert({'status': 'DISABLED'}, {'name': 'x', 'status': 'ONLINE'}, now) is None
    assert notifications.provider_alert({'status': 'DISABLED'}, {'name': 'x', 'status': 'PENDING'}, now) is None
    assert notifications.provider_alert({'status': 'ONLINE'}, {'name': 'x', 'status': 'OFFLINE'}, now) is not None


# --- connection tests ----------------------------------------------------------------------------------------------

def test_connection_test_uses_a_fresh_instance_and_maps_failures(start, monkeypatch):
    client = start('FRED_API_KEY=fred-key-123456\n')
    hub = client.app.state.hub
    running = hub.providers['fred']
    calls: list = []
    respond(monkeypatch, 200, FRED_OK, calls=calls)
    test = lambda target: client.post(f'/api/admin/providers/{target}/test', headers=HEADERS)  # noqa: E731
    ok = test('fred')
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body['ok'] and body['status'] == 'ONLINE' and body['target'] == 'fred' and body['note'] is None
    assert isinstance(body['latency_ms'], int) and body['checked_at'] and len(calls) == 1
    assert hub.providers['fred'] is running and running.health['status'] == 'PENDING'  # the running one is untouched
    for status, expected, words in [(401, 'AUTH_ERROR', 'HTTP 401'), (400, 'AUTH_ERROR', 'FRED rejected'),
                                    (429, 'RATE_LIMITED', 'HTTP 429'), (404, 'DEGRADED', 'HTTP 404')]:
        ps._tested.clear()
        respond(monkeypatch, status, {'error_message': 'api_key fred-key-123456 is bad'})
        result = test('fred').json()
        assert result['status'] == expected and not result['ok'] and words in result['detail']
        assert 'fred-key-123456' not in json.dumps(result) and 'stlouisfed' not in result['detail']
    ps._tested.clear()
    monkeypatch.setattr(ps, 'TEST_TIMEOUT_SECONDS', 0.2)
    respond(monkeypatch, 200, FRED_OK, delay=5)
    assert test('fred').json()['status'] == 'TIMEOUT'
    rows = audit_rows(client, 'provider_test')
    assert {(r['target'], r['ok'], r['status']) for r in rows} >= {('fred', True, 'ONLINE'), ('fred', False, 'AUTH_ERROR')}
    assert 'fred-key-123456' not in json.dumps(rows)


def test_connection_tests_without_network_and_the_rate_limit(start, monkeypatch):
    client = start()
    no_network(monkeypatch)
    test = lambda target: client.post(f'/api/admin/providers/{target}/test', headers=HEADERS)  # noqa: E731
    assert test('nope').status_code == 404
    missing = test('finnhub').json()
    assert missing['status'] == 'NOT CONFIGURED' and not missing['ok'] and missing['latency_ms'] is None
    assert test('finnhub').status_code == 429
    client.put('/api/admin/providers/deribit/enabled', json={'enabled': False}, headers=HEADERS)
    assert test('deribit').json()['status'] == 'DISABLED'
    assert ps.note_for('bls_data').startswith('Uses 1 of your daily BLS queries')
    assert ps.note_for('email') == 'Sends one real e-mail to every recipient.' and ps.note_for('binance') is None


def test_connection_test_of_a_development_stub(start, monkeypatch):
    client = start('PROVIDER_MODE=stub\n')
    no_network(monkeypatch)
    for target in ('massive', 'x-stub'):
        result = client.post(f'/api/admin/providers/{target}/test', headers=HEADERS).json()
        assert result['status'] == 'DEVELOPMENT STUB' and result['ok']


@pytest.mark.asyncio
async def test_ollama_test_lists_the_catalog_then_searches(hub, env_file, monkeypatch):
    env_file.write_text('OLLAMA_API_KEY=ollama-key-123456\n', encoding='utf-8')
    calls: list = []

    async def request(self, method, url, **kwargs):
        calls.append((method, url.rsplit('/', 1)[-1]))
        body = {'models': [{'name': 'm1'}]} if url.endswith('/tags') else {'results': []}
        return httpx.Response(200, json=body, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    result = await run_fresh('ollama', hub)
    assert result['status'] == 'ONLINE' and calls == [('GET', 'tags'), ('POST', 'web_search')]
    assert 'web search' in result['note'] and hub.ollama.health['status'] == 'NOT CONFIGURED'


class Telegram:
    def __init__(self, status, body):
        self.status, self.body, self.requests = status, body, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


@pytest.mark.asyncio
async def test_telegram_test_sends_one_message_and_maps_errors(hub, env_file):
    refuse = Telegram(200, {'ok': True})
    assert (await run_fresh('telegram', hub, transport=httpx.MockTransport(refuse)))['status'] == 'NOT CONFIGURED'
    assert refuse.requests == []
    env_file.write_text("TELEGRAM_BOT_TOKEN='123456:tok-abcdef'\nTELEGRAM_CHAT_ID=42\n", encoding='utf-8')
    telegram = Telegram(200, {'ok': True})
    result = await run_fresh('telegram', hub, transport=httpx.MockTransport(telegram))
    assert result['status'] == 'SENT' and result['ok'] and result['note'].startswith('Sends one real message')
    [sent] = telegram.requests
    assert 'Test message from Market Intelligence' in json.loads(sent.content)['text']
    assert hub.store.list('alerts') == [] and hub.store.list('notification_deliveries') == []  # no dispatcher records
    for status, expected in [(401, 'AUTH_ERROR'), (404, 'AUTH_ERROR'), (400, 'DEGRADED'), (429, 'RATE_LIMITED'),
                             (502, 'OFFLINE')]:
        failed = await run_fresh('telegram', hub, transport=httpx.MockTransport(Telegram(status, {'ok': False})))
        assert failed['status'] == expected and not failed['ok'] and 'tok-abcdef' not in json.dumps(failed)


class FakeSMTP:
    sent: list = []
    fail_auth = False

    def __init__(self, host, port, timeout):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if FakeSMTP.fail_auth:
            raise smtplib.SMTPAuthenticationError(535, b'bad credentials')

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.mark.asyncio
async def test_email_test_sends_through_smtp_and_reports_bad_credentials(hub, env_file):
    env_file.write_text('SMTP_HOST=smtp.firm.io\nSMTP_FROM=alerts@firm.io\nSMTP_TO=a@firm.io, b@firm.io\n'
                        'SMTP_USERNAME=user\nSMTP_PASSWORD=sekret-smtp-pw\n', encoding='utf-8')
    FakeSMTP.sent, FakeSMTP.fail_auth = [], False
    result = await run_fresh('email', hub, smtp_factory=FakeSMTP)
    assert result['status'] == 'SENT' and FakeSMTP.sent[0]['Subject'] == '[LOW] Test message from Market Intelligence'
    FakeSMTP.fail_auth = True
    failed = await run_fresh('email', hub, smtp_factory=FakeSMTP)
    assert failed['status'] == 'AUTH_ERROR' and 'sekret-smtp-pw' not in json.dumps(failed)


@pytest.mark.asyncio
async def test_webpush_test_needs_a_subscribed_browser(hub, env_file):
    env_file.write_text('WEBPUSH_VAPID_PUBLIC_KEY=public-key\nWEBPUSH_VAPID_PRIVATE_KEY=private-key-123\n'
                        'WEBPUSH_SUBJECT=mailto:ops@firm.io\n', encoding='utf-8')
    pushed: list = []

    def sender(subscription, data, settings, ttl):
        pushed.append(json.loads(data)['title'])
        return 201
    assert (await run_fresh('webpush', hub, push_sender=sender))['status'] == 'NOT CONFIGURED'
    hub.store.put('push_subscriptions', 'k1', {'endpoint': 'https://fcm.googleapis.com/fcm/send/abc',
                                               'keys': {'p256dh': 'p' * 16, 'auth': 'a' * 16}, 'active': True})
    assert (await run_fresh('webpush', hub, push_sender=sender))['status'] == 'SENT'
    assert pushed == ['Test message from Market Intelligence']


# --- destinations: a stored secret never follows a new address ------------------------------------------------------

STORED = {'OPENAI_API_KEY': 'sk-stored-OPENAI-111', 'OLLAMA_API_KEY': 'stored-OLLAMA-222',
          'REUTERS_API_KEY': 'stored-REUTERS-333', 'AP_API_KEY': 'stored-AP-444', 'SMTP_PASSWORD': 'stored-SMTP-555'}
DESTINATIONS = ('REUTERS_BASE_URL=https://reuters-gw.firm.io\nAP_BASE_URL=https://ap-gw.firm.io\nSMTP_HOST=smtp.firm.io\n'
                'SMTP_USERNAME=alerts\nSMTP_FROM=alerts@firm.io\nSMTP_TO=a@firm.io\n')
ENV_TEXT = ''.join(f'{key}={value}\n' for key, value in STORED.items()) + DESTINATIONS


def recorder(monkeypatch) -> list[tuple[str, str]]:
    """Every outgoing request as (host, Authorization header); answers 200 without touching the network."""
    sent: list[tuple[str, str]] = []

    async def request(self, method, url, **kwargs):
        headers = {**{k.lower(): v for k, v in self.headers.items()},
                   **{k.lower(): v for k, v in (kwargs.get('headers') or {}).items()}}
        sent.append((httpx.URL(str(url)).host, headers.get('authorization', '')))
        return httpx.Response(200, json={'data': [], 'models': [], 'results': []}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    return sent


def test_destination_pairs_are_registered_fields_of_the_same_group():
    registered = ps.fields()
    pairs = ps.destination_secrets()
    assert pairs == {'ollama_base_url': 'ollama_api_key', 'openai_base_url': 'openai_api_key',
                     'reuters_base_url': 'reuters_api_key', 'ap_base_url': 'ap_api_key', 'smtp_host': 'smtp_password',
                     'smtp_port': 'smtp_password'}
    for destination, secret in pairs.items():
        assert registered[secret]['kind'] == 'secret' and registered[destination]['group'] == registered[secret]['group']


@pytest.mark.parametrize('group_id, values, named', [
    ('openai', {'openai_base_url': 'https://evil.example/v1'}, ['openai_base_url', 'openai_api_key']),
    ('ollama', {'ollama_base_url': 'https://evil.example/api'}, ['ollama_base_url', 'ollama_api_key']),
    ('reuters', {'reuters_base_url': 'https://evil.example'}, ['reuters_base_url', 'reuters_api_key']),
    ('ap', {'ap_base_url': 'https://evil.example'}, ['ap_base_url', 'ap_api_key']),
    ('email', {'smtp_host': 'evil.example'}, ['smtp_host', 'smtp_password']),
    ('email', {'smtp_port': 2525}, ['smtp_port', 'smtp_password']),
    ('email', {'smtp_host': 'evil.example', 'smtp_port': 25, 'smtp_username': 'x'}, ['smtp_host', 'smtp_port',
                                                                                    'smtp_password'])])
def test_a_new_destination_without_its_secret_is_refused_and_the_key_never_reaches_it(start, env_file, monkeypatch,
                                                                                       group_id, values, named):
    """Reproduces the exfiltration: PUT only the destination, then Test. The stored key must never reach the new host."""
    client = start(ENV_TEXT)
    sent = recorder(monkeypatch)
    response = client.put(f'/api/admin/provider-settings/{group_id}', json={'values': values}, headers=HEADERS)
    assert response.status_code == 422
    detail = response.json()['detail']
    assert all(name in detail for name in named) and 'never sent to a new address' in detail
    assert not [secret for secret in STORED.values() if secret in response.text] and 'evil.example' not in response.text
    assert env_file.read_text(encoding='utf-8') == ENV_TEXT
    assert not audit_rows(client, 'provider_settings_change')
    if group_id != 'email':  # (the e-mail send path is covered with a fake SMTP server below)
        client.post(f'/api/admin/providers/{group_id}/test', headers=HEADERS)
        assert sent and all(host != 'evil.example' for host, _ in sent)


def test_a_destination_moves_together_with_its_secret(start, env_file, monkeypatch):
    client = start(ENV_TEXT)
    sent = recorder(monkeypatch)
    put = lambda group_id, values: client.put(f'/api/admin/provider-settings/{group_id}', json={'values': values},  # noqa: E731
                                              headers=HEADERS)
    moved = put('openai', {'openai_base_url': 'https://gateway.firm.io/v1', 'openai_api_key': 'sk-new-OPENAI-999'})
    assert moved.status_code == 200, moved.text
    client.post('/api/admin/providers/openai/test', headers=HEADERS)
    assert sent == [('gateway.firm.io', 'Bearer sk-new-OPENAI-999')]  # the new key only, never the stored one
    assert put('reuters', {'reuters_base_url': 'https://other-gw.firm.io', 'reuters_api_key': None}).status_code == 200
    assert 'REUTERS_API_KEY' not in dotenv_values(env_file)  # removing the key with the move: nothing follows it
    assert put('ap', {'ap_base_url': 'https://ap-gw.firm.io'}).status_code == 200  # unchanged (whole form sent again)
    assert put('ap', {'ap_base_url': None}).status_code == 200  # no destination left: nothing is sent anywhere
    assert put('ollama', {'ollama_api_key': None}).status_code == 200
    assert put('ollama', {'ollama_base_url': 'http://localhost:11434/api'}).status_code == 200  # no key stored
    assert put('email', {'smtp_port': 465, 'smtp_ssl': True, 'smtp_password': 'new-SMTP-pw'}).status_code == 200
    assert STORED['OPENAI_API_KEY'] not in env_file.read_text(encoding='utf-8')


def test_a_secret_from_the_os_environment_pins_its_destination(start, env_file, monkeypatch):
    client = start(ENV_TEXT)
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-os-level-777')
    put = lambda values: client.put('/api/admin/provider-settings/openai', json={'values': values}, headers=HEADERS)  # noqa: E731
    refused = put({'openai_base_url': 'https://evil.example/v1'})
    assert refused.status_code == 422 and 'OS environment' in refused.json()['detail']
    assert 'sk-os-level-777' not in refused.text
    assert put({'openai_base_url': 'https://evil.example/v1', 'openai_api_key': 'sk-x-123456'}).status_code == 409
    assert env_file.read_text(encoding='utf-8') == ENV_TEXT


def test_an_smtp_login_without_tls_is_refused_at_save_time(start, env_file):
    client = start(ENV_TEXT)
    put = lambda values: client.put('/api/admin/provider-settings/email', json={'values': values}, headers=HEADERS)  # noqa: E731
    plain = put({'smtp_starttls': False})  # smtp_ssl is off by default: the login would go out in clear text
    assert plain.status_code == 422 and 'smtp_starttls' in plain.json()['detail'] and 'clear text' in plain.json()['detail']
    assert env_file.read_text(encoding='utf-8') == ENV_TEXT
    assert put({'smtp_starttls': False, 'smtp_ssl': True, 'smtp_port': 465, 'smtp_password': 'new-SMTP-pw'}).status_code == 200
    # a server on this computer may use a plain connection (the host moves, so the password is re-entered)
    assert put({'smtp_host': 'localhost', 'smtp_ssl': False, 'smtp_password': 'local-pw-123'}).status_code == 200
    env_file.write_text(ENV_TEXT + 'SMTP_STARTTLS=false\n', encoding='utf-8')  # an older insecure file
    fred = client.put('/api/admin/provider-settings/fred', json={'values': {'fred_api_key': 'fred-key-123456'}},
                      headers=HEADERS)
    assert fred.status_code == 200  # other groups are never blocked by the e-mail settings


class RecordingSMTP:
    connections: list = []

    def __init__(self, host, port, timeout):
        RecordingSMTP.connections.append((host, port))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        raise AssertionError('no login expected')

    def send_message(self, message):
        pass


@pytest.mark.asyncio
async def test_an_insecure_saved_email_setting_never_sends_the_password(hub, env_file):
    env_file.write_text(ENV_TEXT + 'SMTP_STARTTLS=false\n', encoding='utf-8')
    RecordingSMTP.connections = []
    result = await run_fresh('email', hub, smtp_factory=RecordingSMTP)
    assert result['status'] == 'DEGRADED' and not result['ok'] and 'clear text' in result['detail']
    assert RecordingSMTP.connections == [] and STORED['SMTP_PASSWORD'] not in json.dumps(result)


# --- .env integrity and permissions ---------------------------------------------------------------------------------

def legacy_set_key(path, key, value, quote_mode='always'):
    """python-dotenv 1.2.2 serialisation: only the single quote was escaped, so a trailing backslash escaped the closing
    quote and the parser swallowed the next line."""
    line = f"{key}='{value.replace(chr(39), chr(92) + chr(39))}'"
    lines = Path(path).read_text(encoding='utf-8').splitlines()
    edited = [line if current.split('=', 1)[0] == key else current for current in lines]
    Path(path).write_text('\n'.join(edited if line in edited else edited + [line]) + '\n', encoding='utf-8')


def test_a_value_that_would_not_read_back_is_rejected_and_the_env_file_is_untouched(env_file, monkeypatch):
    original = "FINNHUB_API_KEY='fh-123456'\nSMTP_PASSWORD='old'\nADMIN_TOKEN='tok-123456'\n"
    env_file.write_text(original, encoding='utf-8')
    real = ps.set_key
    monkeypatch.setattr(ps, 'set_key', legacy_set_key)
    with pytest.raises(ValueError, match='smtp_password') as info:
        ps.write_values({'smtp_password': 'pa\\'})
    message = str(info.value)
    assert 'admin_token' in message and 'read back' in message and 'tok-123456' not in message and 'pa\\' not in message
    assert env_file.read_text(encoding='utf-8') == original and not (env_file.parent / ps.TEMP_NAME).exists()
    monkeypatch.setattr(ps, 'set_key', real)  # the pinned python-dotenv (>= 1.2.3) keeps both the value and the next key
    ps.write_values({'smtp_password': 'pa\\'})
    assert dotenv_values(env_file) == {'FINNHUB_API_KEY': 'fh-123456', 'SMTP_PASSWORD': 'pa\\', 'ADMIN_TOKEN': 'tok-123456'}
    assert NotificationSettings(_env_file=env_file).smtp_password.get_secret_value() == 'pa\\'


def test_the_working_copy_is_private_and_a_new_env_file_is_0600(env_file, monkeypatch):
    opened: list[tuple[str, int, int]] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        opened.append((Path(path).name, flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)
    monkeypatch.setattr(ps.os, 'open', spy)
    (env_file.parent / ps.TEMP_NAME).write_text('LEFTOVER=from-a-killed-process\n', encoding='utf-8')
    ps.write_values({'fred_api_key': 'fred-123456'})
    _, flags, mode = next(item for item in opened if item[0] == ps.TEMP_NAME)
    assert mode == 0o600 and flags & os.O_CREAT and flags & os.O_EXCL  # private before any secret is written
    assert dotenv_values(env_file) == {'FRED_API_KEY': 'fred-123456'} and not (env_file.parent / ps.TEMP_NAME).exists()
    if os.name == 'posix':  # (Windows has no group/other permission bits)
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        env_file.chmod(0o640)
        ps.write_values({'fred_api_key': 'fred-654321'})
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o640  # an existing file keeps its mode


# --- a connection test never answers 500 ----------------------------------------------------------------------------

def test_a_connection_test_never_answers_500(start, monkeypatch):
    from backend.providers.market import TreasuryYieldProvider
    client = start()

    async def maintenance(self, method, url, **kwargs):
        return httpx.Response(200, text='<html><body>Scheduled maintenance&nbsp;<br></body></html>',
                              request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', maintenance)
    first = client.post('/api/admin/providers/treasury_yields/test', headers=HEADERS)
    assert first.status_code == 200 and first.json()['status'] == 'DEGRADED'
    assert first.json()['detail'] == 'Connected, but the response was not understood.'  # no exception names
    ps._tested.clear()

    async def unexpected(self, *args, **kwargs):
        raise RuntimeError('boom at https://internal.example/path?key=abc')
    monkeypatch.setattr(TreasuryYieldProvider, 'quotes', unexpected)
    second = client.post('/api/admin/providers/treasury_yields/test', headers=HEADERS)
    assert second.status_code == 200 and second.json() | {'latency_ms': 0, 'checked_at': ''} == {
        'target': 'treasury_yields', 'ok': False, 'status': 'DEGRADED', 'detail': 'The test failed unexpectedly; see the server log.',
        'latency_ms': 0, 'checked_at': '', 'note': None}


# --- admin token helper --------------------------------------------------------------------------------------------

def test_admin_token_helper_reads_the_given_env_file(env_file, monkeypatch, capsys):
    from scripts import admin_token
    monkeypatch.delenv('ADMIN_TOKEN', raising=False)
    env_file.write_text("ADMIN_TOKEN='local-owner-token'\n", encoding='utf-8')
    assert admin_token.admin_token(env_file) == 'local-owner-token'
    assert admin_token.main() == 0 and capsys.readouterr().out.strip() == 'local-owner-token'
    env_file.write_text('', encoding='utf-8')
    assert admin_token.main() == 1 and capsys.readouterr().out == ''
