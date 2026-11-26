from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.ai import AIService, SearchBudgetManager
from backend.audit import CHECKS, combine, pre_audit
from backend.database import Store
from backend.schemas import CryptoAnalysis, FXAnalysis

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def evidence(eid, source='fed', hours=1, relation='supports'):
    stamp = (NOW - timedelta(hours=hours)).isoformat()
    return {'id': eid, 'source_id': source, 'url': f'https://x.test/{eid}', 'published_at': stamp,
            'ingested_at': stamp, 'relation': relation}


def good():
    decision = {'direction': 'LONG', 'score': 40, 'created_at': NOW.isoformat(), 'reasons': [],
                'historical_probability': None,
                'explanation': {'reasons': ['BTC above 100'], 'evidence_ids': ['e1'], 'confirmation': 'hold 100', 'invalidation': 'lose 100'}}
    inputs = {'quote': {'price': 100, 'timestamp': (NOW - timedelta(seconds=3)).isoformat(), 'freshness_status': 'FRESH',
                        'market_status': 'LIVE'},
              'events': [{'id': 'ev', 'verification_status': 'CONFIRMED', 'first_seen_utc': NOW.isoformat(), 'evidence': [evidence('e1')]}],
              'evidence_ids': ['e1']}
    return decision, inputs


def test_all_twelve_checks_pass_for_a_clean_setup():
    decision, inputs = good()
    result = pre_audit(decision, inputs, NOW, 15, 5)
    assert [c['name'] for c in result['checks']] == list(CHECKS) and result['status'] == 'PASS'


@pytest.mark.parametrize('mutate, failing', [
    (lambda d, i: i['quote'].update(timestamp=(NOW - timedelta(minutes=5)).isoformat()), 'fresh_prices'),
    (lambda d, i: i['quote'].update(market_status='LAST CLOSE'), 'market_open'),
    (lambda d, i: i['events'][0]['evidence'][0].update(url=None), 'sources_attached'),
    (lambda d, i: i['events'][0]['evidence'][0].update(published_at=(NOW - timedelta(hours=48)).isoformat()), 'old_article_as_new'),
    (lambda d, i: d['explanation'].update(evidence_ids=['forged']), 'claim_beyond_source'),
    (lambda d, i: i['events'][0]['evidence'].append(evidence('c1', 'ap', relation='contradicts')), 'contradictions_visible'),
    (lambda d, i: d.update(direction='STRONG LONG'), 'direction_matches_score'),
    (lambda d, i: d.update(historical_probability={'p': 0.7, 'n': 12}), 'probabilities_valid'),
    (lambda d, i: d['explanation'].update(reasons=['BTC to 250000']), 'invented_numbers'),
    (lambda d, i: d.update(created_at='2026-09-26T12:00:00'), 'timezone_conversion'),
    (lambda d, i: i['events'][0].update(evidence=[evidence('x1', 'x-unknown')]), 'unverified_x_as_fact'),
    (lambda d, i: i['events'][0]['evidence'][0].update(published_at=(NOW + timedelta(hours=1)).isoformat()), 'valid_timestamps'),
])
def test_each_check_blocks_its_failure(mutate, failing):
    decision, inputs = good()
    mutate(decision, inputs)
    result = pre_audit(decision, inputs, NOW, 15, 5)
    assert result['status'] == 'BLOCK' and failing in {c['name'] for c in result['checks'] if not c['passed']}


def test_a_probability_needs_a_validated_calibration_and_enough_comparables():
    def probability_check(value):
        decision, inputs = good()
        decision['historical_probability'] = value
        return next(c for c in pre_audit(decision, inputs, NOW, 15, 5)['checks'] if c['name'] == 'probabilities_valid')['passed']
    calibrated = {'p': 0.61, 'n': 40, 'calibration': {'validation_observations': 250}}
    assert probability_check(calibrated)
    assert not probability_check(dict(calibrated, n=12))  # below min_comparable
    assert not probability_check(dict(calibrated, calibration={'validation_observations': 150}))  # below minimum_calibration
    assert probability_check({'status': 'NOT ENOUGH DATA', 'n': 3})


def failed_checks(decision, inputs):
    return {c['name'] for c in pre_audit(decision, inputs, NOW, 15, 5)['checks'] if not c['passed']}


def test_macro_only_setups_are_sourced_by_their_official_release():
    decision, inputs = good()
    decision['explanation'].update(evidence_ids=[])
    inputs.update(events=[], evidence_ids=[],
                  macro_releases=[{'name': 'US CPI', 'provider': 'bls_data', 'released_at': NOW.isoformat(), 'z_score': 2.1}])
    assert 'sources_attached' not in failed_checks(decision, inputs)
    inputs['macro_releases'][0]['provider'] = None
    assert 'sources_attached' in failed_checks(decision, inputs)
    inputs['macro_releases'] = []
    assert 'sources_attached' in failed_checks(decision, inputs)  # a decision with no facts at all is not sourced


def test_evidence_age_follows_the_verification_window_of_the_event_type():
    decision, inputs = good()
    inputs['events'][0].update(event_type='CENTRAL_BANK', evidence=[evidence('e1', hours=30)])
    assert 'old_article_as_new' not in failed_checks(decision, inputs)  # 72 h window for central-bank facts
    inputs['events'][0].update(evidence=[evidence('e1', hours=80)])
    assert 'old_article_as_new' in failed_checks(decision, inputs)


def test_ai_cannot_override_a_deterministic_failure():
    decision, inputs = good()
    inputs['quote']['market_status'] = 'LAST CLOSE'
    final = combine(pre_audit(decision, inputs, NOW, 15, 5), {'status': 'PASS', 'reasons': [], 'checks': []})
    assert final['status'] == 'BLOCK'
    assert combine(pre_audit(*good(), NOW, 15, 5), None)['status'] == 'BLOCK'
    assert combine(pre_audit(*good(), NOW, 15, 5), {'status': 'PASS', 'reasons': []})['status'] == 'PASS'


def test_fx_must_be_relative_and_crypto_forbids_positioning_fallacies():
    view = {'pair': 'USDJPY', 'bias': 'USD-positive', 'base_bank': 'Fed', 'counter_bank': 'Fed', 'policy_comparison': '',
            'yield_differential_view': '', 'intervention_risk': '', 'causal_path': ['a', 'b'], 'evidence_ids': [],
            'confirmation': '', 'invalidation': ''}
    with pytest.raises(ValidationError):
        FXAnalysis.model_validate({'pairs': [view], 'uncertainties': []})
    FXAnalysis.model_validate({'pairs': [dict(view, counter_bank='BoJ')], 'uncertainties': []})
    crypto = {'asset': 'BTC', 'direction': 'positive', 'drivers': ['calls > puts'], 'positioning_note': '', 'causal_path': [],
              'evidence_ids': [], 'confirmation': '', 'invalidation': ''}
    with pytest.raises(ValidationError):
        CryptoAnalysis.model_validate({'assets': [crypto], 'uncertainties': []})


def test_search_budget_priorities_and_persistence(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/budget.db')
    store.initialize()
    budget = SearchBudgetManager(10, 60, store)
    assert all(budget.acquire(f'sweep{i}', 3) for i in range(4))
    assert not budget.acquire('sweep-extra', 3)  # P3 limited to 40% of the hourly allowance
    assert budget.acquire('breaking', 0)
    restored = SearchBudgetManager(10, 60, store)
    assert not restored.acquire('breaking', 0) and len(restored.calls) == 5  # state (and cooldowns) survive restarts


class Provider:
    def __init__(self):
        self.models_used = []

    async def chat(self, model, messages, schema):
        self.models_used.append(model)
        return {'message': {'content': '{"reasons":["US10Y rose to 4.2"],"evidence_ids":[],"confirmation":"DXY holds","invalidation":"US10Y below 4.2"}'}}


@pytest.mark.asyncio
async def test_chief_may_cite_input_values_and_fallback_uses_validated_models(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/chief.db')
    store.initialize()
    store.put('ai_models', 'gone', {'name': 'gone', 'availability': False})
    store.put('ai_models', 'spare', {'name': 'spare', 'availability': True, 'preferred_roles': ['chief_analyst']})
    store.put('ai_role_assignments', 'chief_analyst', {'primary': 'gone'})
    provider = Provider()
    output, run_id = await AIService(store, provider).run_with_id('chief_analyst', {'US10Y': 4.2})
    assert output and output['reasons'] == ['US10Y rose to 4.2'] and provider.models_used == ['spare']
    assert any(r['action'] == 'model_fallback' and r['basis'] == 'fallback' for r in store.list('audit_logs'))
