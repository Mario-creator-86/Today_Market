import json

import httpx
import pytest

from backend.ai import AIService, OllamaProvider
from backend.database import Store


@pytest.mark.asyncio
async def test_public_catalog_does_not_require_key(monkeypatch):
    async def request(self, method, url, **kwargs):
        assert method == 'GET' and url == 'https://ollama.com/api/tags'
        assert 'Authorization' not in kwargs.get('headers', {})
        return httpx.Response(200, json={'models':[{'name':'discovered-model'}]}, request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', '')
    assert (await provider.models())[0]['name'] == 'discovered-model'
    assert provider.health['status'] == 'NOT CONFIGURED'


@pytest.mark.asyncio
async def test_cloud_chat_uses_bearer_and_no_unsupported_format(monkeypatch):
    async def request(self, method, url, **kwargs):
        assert method == 'POST' and url == 'https://ollama.com/api/chat'
        assert kwargs['headers']['Authorization'] == 'Bearer test-secret'
        assert kwargs['json']['stream'] is False
        assert 'format' not in kwargs['json']
        return httpx.Response(200, json={'done': True, 'message': {'content':'{"status":"PASS","reasons":[]}'}},
                              request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    result = await provider.chat('from-registry', [], {})
    assert result.content == '{"status":"PASS","reasons":[]}'


@pytest.mark.asyncio
async def test_removed_model_falls_back_without_opening_provider_circuit(monkeypatch,tmp_path):
    models_called = []
    async def request(self, method, url, **kwargs):
        model = kwargs['json']['model']
        models_called.append(model)
        if model == 'removed':
            return httpx.Response(404, json={'error':'model not found'}, request=httpx.Request(method,url))
        return httpx.Response(200, json={'done':True, 'message':{'content':'{"status":"PASS","reasons":[]}'}},
                              request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    store=Store(f'sqlite:///{tmp_path}/models.db')
    store.initialize()
    for name in ['removed','backup']:
        store.put('ai_models',name,{'name':name,'availability':True})
    store.put('ai_role_assignments','auditor',{'primary':'removed','backup':'backup'})
    service=AIService(store,OllamaProvider('https://ollama.com/api','test-secret'))
    result=await service.run('auditor',{})
    assert result == {'status':'PASS','reasons':[],'checks':[]}
    assert models_called == ['removed','backup']
    actions = [row['action'] for row in store.list('audit_logs')]
    assert 'model_unavailable' in actions and 'model_fallback' in actions
    assert 'test-secret' not in json.dumps(store.list('ai_runs'))


@pytest.mark.asyncio
async def test_invalid_catalog_does_not_retire_valid_models(monkeypatch,tmp_path):
    async def request(self, method, url, **kwargs):
        return httpx.Response(200,json={'error':'temporary issue'},request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.AsyncClient,'request',request)
    store=Store(f'sqlite:///{tmp_path}/models.db')
    store.initialize()
    store.put('ai_models','working',{'name':'working','availability':True})
    service=AIService(store,OllamaProvider('https://ollama.com/api','test-secret'))
    with pytest.raises(ValueError):
        await service.discover()
    assert store.get('ai_models','working')['availability'] is True
