from datetime import timedelta

import pytest
from test_pipeline import hub as hub
from test_pipeline import quote

from backend.database import Decision, utcnow
from backend.schemas import DocumentInput


def test_manual_official_import_is_discovery_until_retrieved(hub):
    doc = DocumentInput(source_id='fed', title='Policy announcement', content='unverified imported body',
                        url='https://federalreserve.gov/test', published_at=utcnow())
    event = hub.ingest_document(doc)
    assert event['verification_status'] == 'UNCONFIRMED'
    retrieved = hub.ingest_document(doc, trusted_transport=True)
    assert retrieved['verification_status'] == 'CONFIRMED'
    assert retrieved['id'] != event['id']
    assert hub.event(event['id'])['verification_status'] == 'UNCONFIRMED'

def test_old_decision_cannot_revive_with_fresh_quote(hub):
    hub.ingest_quote(quote())
    old = utcnow()-timedelta(hours=1)
    payload = {'id':'expired', 'symbol':'BTC', 'direction':'LONG', 'created_at':old.isoformat(),
               'score':70, 'audit_status':'PASS', 'reasons':[], 'input_snapshot':{}}
    with hub.store.session() as session:
        session.add(Decision(id='expired', symbol='BTC', direction='LONG', score=70, created_at=old, payload=payload))
        session.commit()
    decision = hub.assets()[0]['decision']
    assert decision['direction']=='WAIT'
    assert 'expired' in str(decision['reasons'])

@pytest.mark.asyncio
async def test_auditor_rejects_invented_numerical_claim(tmp_path):
    from test_ai import FakeProvider

    from backend.ai import AIService
    from backend.database import Store
    store = Store(f'sqlite:///{tmp_path}/numeric.db')
    store.initialize()
    store.put('ai_models', 'test', {'availability': True})
    store.put('ai_role_assignments', 'auditor', {'primary':'test'})
    service = AIService(store, FakeProvider(['{"status":"PASS","reasons":["BTC 999999"]}']*2))
    assert await service.run('auditor', {'price':100}) is None
