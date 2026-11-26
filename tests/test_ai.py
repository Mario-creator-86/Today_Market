import pytest

from backend.ai import AIService, SearchBudgetManager
from backend.database import Store
from backend.schemas import Audit


class FakeProvider:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = 0
    async def chat(self, model, messages, schema):
        self.calls += 1
        return {'message': {'content': next(self.outputs)}}

@pytest.mark.asyncio
async def test_ai_invalid_output_retried_once_and_logged(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/ai.db')
    store.initialize()
    provider = FakeProvider(['not json', '{"status":"PASS","reasons":[]}'])
    service = AIService(store, provider)
    store.put('ai_models', 'test', {'name':'test', 'availability':True})
    store.put('ai_role_assignments', 'auditor', {'primary':'test'})
    result = await service.run('auditor', {'verified':[]})
    assert result == Audit(status='PASS', reasons=[]).model_dump()
    assert provider.calls == 2
    assert len(store.list('ai_runs')) == 1

@pytest.mark.asyncio
async def test_ai_malformed_output_cannot_publish(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/ai.db')
    store.initialize()
    service = AIService(store, FakeProvider(['bad', 'bad']))
    store.put('ai_models','test',{'name':'test','availability':True})
    store.put('ai_role_assignments','auditor',{'primary':'test'})
    assert await service.run('auditor', {}) is None
    assert store.list('ai_runs')[0]['success'] is False

def test_search_budget_and_cooldown():
    budget = SearchBudgetManager(2, 60)
    assert budget.acquire('oil', 0)
    assert not budget.acquire('oil', 0)
    assert budget.acquire('fed', 0)
    assert not budget.acquire('other', 0)

@pytest.mark.asyncio
async def test_second_opinion_can_veto_audit(tmp_path):
    store=Store(f'sqlite:///{tmp_path}/second.db')
    store.initialize()
    for name in ['primary','second']:
        store.put('ai_models',name,{'name':name,'availability':True})
    store.put('ai_role_assignments','auditor',{'primary':'primary','second_opinion':'second'})
    service=AIService(store,FakeProvider(['{"status":"PASS","reasons":[]}',
                                        '{"status":"BLOCK","reasons":["Missing evidence"]}']))
    assert (await service.run('auditor',{}))['status']=='BLOCK'
