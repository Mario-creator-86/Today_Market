from copy import deepcopy
from datetime import UTC, datetime

import pytest

from backend import event_risk as er
from backend import interpretation, regime, scoring
from backend.service import importance_fields

NOW = datetime(2026, 9, 29, 17, 30, tzinfo=UTC)


@pytest.mark.parametrize('bad', [{'stub': True}, {'session': {'state': 'CLOSED'}},
                                 {'market_status': 'CLOSED'}, {'data_class': 'DAILY_CLOSE'}])
def test_live_features_reject_ineligible_quotes(bad):
    asset = {'symbol': 'VIX', 'quote': {'freshness_status': 'FRESH', 'price': 15, **bad}}
    assert regime.features([asset]) == {}


def test_live_features_preserve_level_only_quote():
    assert regime.features([{'symbol': 'VIX', 'quote': {'freshness_status': 'FRESH', 'price': 15}}]) == {
        'VIX': {'price': 15.0}}


@pytest.mark.parametrize('basis', ['live', 'last_close'])
def test_stale_quote_and_old_decision_cannot_confirm_current_market(basis):
    asset = {'symbol': 'SP500', 'session': {'state': 'LIVE'},
             'quote': {'freshness_status': 'STALE', 'change_pct': 1.5},
             'decision': {'category': 'LONG', 'created_at': '2026-09-28T15:00:00+00:00'}}
    context = {'regimes': ['RISK_ON'], 'posture': 'CONSTRUCTIVE', 'basis': basis}
    view = er.market_view([asset], context, {})
    assert not view['favored']
    assert any(i['symbol'] == 'SP500' for i in view['non_confirming'])


def test_ai_reuse_revalidates_cited_numbers():
    context = {'regimes': ['RISK_ON'], 'posture': 'CONSTRUCTIVE', 'basis': 'last_close',
               'drivers': [{'driver': 'Risk appetite', 'direction': 'up',
                            'inputs': [{'feature': 'SP500', 'field': 'change_pct', 'value': 0.5}]}]}
    risk = {'regime': context, 'favored': [], 'pressured': [], 'non_confirming': []}
    digest = interpretation.basis_hash(interpretation.bundle(risk, context, [], NOW))
    stored = {'source': 'ai', 'stale': False, 'basis_hash': digest, 'output': {'ai_view': 'SP500 is up 0.50%.'}}
    changed = deepcopy(context)
    changed['drivers'][0]['inputs'][0]['value'] = 2.5
    current = interpretation.for_snapshot(stored, dict(risk, regime=changed), changed, [], NOW)
    assert current['source'] == 'deterministic'


def test_prose_identity_binds_numbers_to_their_asset_and_keeps_unchanged_ai():
    context = {'drivers': [{'driver': 'Risk appetite', 'direction': 'up', 'inputs': [
        {'feature': 'SP500', 'field': 'change_pct', 'value': 0.5},
        {'feature': 'NASDAQ', 'field': 'change_pct', 'value': 2.5}]}]}
    risk = {'regime': context}
    data = interpretation.bundle(risk, context, [], NOW)
    stored = {'source': 'ai', 'stale': False, 'basis_hash': interpretation.basis_hash(data),
              'prose_hash': interpretation.prose_hash(data), 'output': {'ai_view': 'SP500 is up 0.50%.'}}
    assert interpretation.for_snapshot(stored, risk, context, [], NOW) is stored
    changed = deepcopy(context)
    changed['drivers'][0]['inputs'][0]['value'] = 2.5
    changed['drivers'][0]['inputs'][1]['value'] = 0.5
    assert interpretation.for_snapshot(stored, {'regime': changed}, changed, [], NOW)['source'] == 'deterministic'


def test_source_authority_is_not_unmeasured_market_importance():
    result = importance_fields({'tier_weight': 1.0})
    assert result['importance'] is None
    assert result['tier_weight'] == 1.0
    assert importance_fields({'tier_weight': 1, 'extraction': {'potential_importance': 0}})['importance'] == 0


def test_wait_explains_neutral_measurements_and_missing_evidence():
    components = {'technical': {'status': 'measured', 'value': 0, 'basis': 'Price inside support and resistance'},
                  'macro': {'status': 'missing', 'value': None, 'basis': 'No released macro surprise'}}
    text = ' '.join(scoring.why_bullets('BTC', components))
    assert 'Price inside support and resistance' in text
    assert 'No released macro surprise' in text


def test_trade_watch_includes_blocks_of_existing_wait_decisions():
    assets = [{'symbol': 'SP500', 'decision': {'category': 'WAIT', 'why': [],
               'blocks': ['Critical data missing or stale'], 'component_detail': {
                   'technical': {'status': 'missing', 'value': None, 'basis': 'No measured support'}}}}]
    lists = {'favored': [{'symbol': 'SP500'}], 'pressured': []}
    cards = er.trade_watch(assets, {}, lists, [], None)
    assert 'Critical data missing or stale' in cards[0]['why']
    assert any('No measured support' in text for text in cards[0]['why'])
