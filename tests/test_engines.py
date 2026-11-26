from datetime import UTC, datetime, timedelta

import pytest

from backend.config import rules
from backend.engines import (
    bucharest,
    confidence,
    decide,
    market_status,
    normalize_url,
    surprise,
    technical_levels,
)

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)

def test_saturday_stocks_are_not_live():
    assert market_status('equity', NOW) == 'LAST CLOSE'
    assert market_status('crypto', NOW) == 'LIVE'

def test_holiday_and_early_close():
    assert market_status('equity', datetime(2026, 12, 25, 16, tzinfo=UTC)) == 'LAST CLOSE'
    assert market_status('equity', datetime(2026, 11, 27, 19, tzinfo=UTC)) == 'LAST CLOSE'

def test_romania_dst():
    assert bucharest(datetime(2026, 1, 1, 12, tzinfo=UTC)).hour == 14
    assert bucharest(datetime(2026, 7, 1, 12, tzinfo=UTC)).hour == 15

def evidence(group='unknown', weight=.2, official=False, **kwargs):
    return dict(group=group, weight=weight, official=official, relation='supports',
                published_at=NOW.isoformat(), direct=True, discovery_only=False, **kwargs)

def test_rumors_never_confirm_and_reposts_not_independent():
    single = confidence([evidence()], NOW)
    assert single['status'] == 'UNCONFIRMED'
    assert confidence([evidence()] * 50, NOW) == single

def test_official_confirms_but_contradiction_blocks():
    assert confidence([evidence('fed', 1, True)], NOW)['status'] == 'CONFIRMED'
    contrary = evidence('ap', .97)
    contrary['relation'] = 'contradicts'
    assert confidence([evidence('fed', 1, True), contrary], NOW)['status'] == 'CONTRADICTED'

def test_old_news_is_stale():
    item = evidence('fed', 1, True)
    item['published_at'] = (NOW - timedelta(days=3)).isoformat()
    assert confidence([item], NOW)['status'] == 'STALE'

def test_search_snippet_never_confirms():
    item = evidence('fed', 1, True)
    item['discovery_only'] = True
    assert confidence([item], NOW)['status'] != 'CONFIRMED'

def test_gates_override_extreme_score():
    components = dict.fromkeys(rules()['weights'], -100)
    result = decide(components, ['critical data stale'])
    assert result['score'] == -100
    assert result['direction'] == 'WAIT'

def test_scoring_and_missing_components():
    assert decide({'event': 100}, [])['score'] == 30
    assert decide({'positioning': 100}, [])['direction'] == 'WAIT'
    assert decide({}, [])['score'] == 0
    with pytest.raises(ValueError):
        decide({'event': float('nan')}, [])

def test_surprise_is_calculated_and_requires_history():
    assert surprise(3.1, 2.8, [])['z_score'] is None
    assert surprise(3, 2, [-1, 1] * 10)['z_score'] > 0

def test_technical_levels_require_real_bars():
    assert technical_levels([]) == {}

def test_url_normalization_preserves_meaningful_query():
    assert normalize_url('https://example.com/a?utm_source=x&id=3#news') == 'https://example.com/a?id=3'

@pytest.mark.parametrize('components,category,direction', [
    (dict(event=100, technical=100, macro=100, reaction=100), 'STRONG LONG SETUP', 'STRONG LONG'),
    (dict(event=100, technical=100), 'LONG SETUP', 'LONG'),
    (dict(event=60), 'BULLISH LEAN / WAIT', 'BULLISH LEAN / WAIT'),
    (dict(event=40), 'WAIT', 'WAIT'),
    (dict(event=-60), 'BEARISH LEAN / WAIT', 'BEARISH LEAN / WAIT'),
    (dict(event=-100, technical=-100), 'SHORT SETUP', 'SHORT'),
    (dict(event=-100, technical=-100, macro=-100, reaction=-100), 'STRONG SHORT SETUP', 'STRONG SHORT'),
])
def test_category_table_matches_spec(components, category, direction):
    result = decide(components, [])
    assert (result['category'], result['direction']) == (category, direction)
    assert -100 <= result['score'] <= 100

def test_failed_gate_is_data_insufficient_not_neutral_wait():
    result = decide(dict.fromkeys(rules()['weights'], -100), ['Critical data missing or stale'])
    assert result['category'] == 'WAIT / DATA INSUFFICIENT' and result['direction'] == 'WAIT'
    assert result['blocks'] == ['Critical data missing or stale']
    assert decide({}, [])['category'] == 'WAIT'

def test_setup_needs_technical_confirmation_and_a_real_catalyst():
    no_price = decide(dict(event=-100, macro=-100), [])
    assert no_price['category'] == 'BEARISH LEAN / WAIT' and any('Technical' in r for r in no_price['downgrades'])
    # A tiny causal impact plus a level break and reaction must not become a setup.
    tiny = decide({'event': -1, 'technical': -100, 'reaction': -100}, [])
    assert tiny['category'] == 'BEARISH LEAN / WAIT' and tiny['score'] < -30
    capped = decide(dict(event=100, technical=100), [], caps=['Competing causal paths'])
    assert capped['category'] == 'BULLISH LEAN / WAIT' and 'Competing causal paths' in capped['reasons']

def test_component_dicts_missing_listed_and_weights_validated():
    components = {'event': {'value': 50, 'status': 'measured', 'basis': 'x'},
                  'macro': {'value': None, 'status': 'missing', 'basis': 'no release'}}
    result = decide(components, [])
    assert result['score'] == 15 and result['components']['macro'] == 0
    assert result['components_missing'] == ['macro', 'reaction', 'technical', 'positioning', 'cross_asset', 'regime']
    assert result['component_detail']['macro']['status'] == 'missing' and result['content_hash'] == rules()['content_hash']
    with pytest.raises(ValueError):
        decide({'event': {'value': 150, 'status': 'measured', 'basis': 'x'}}, [])
    with pytest.raises(ValueError):
        decide({'bogus': 1}, [])
    with pytest.raises(ValueError):
        decide({'event': 10}, [], weights={'event': 0.5})
    challenger = decide({'event': 100, 'technical': 100}, [], weights={'event': 0.5, 'technical': 0.5})
    assert challenger['score'] == 100 and challenger['category'] == 'STRONG LONG SETUP'

def test_syndicated_copies_count_under_their_origin():
    reuters = evidence('reuters', .97)
    copies = [evidence(f'site{i}', .85, syndicated_from='reuters') for i in range(5)]
    alone = confidence([reuters], NOW)
    with_copies = confidence([reuters, *copies], NOW)
    assert with_copies['independent_sources'] == 1 and with_copies['confidence'] == alone['confidence']
    assert with_copies['status'] != 'CONFIRMED'
    ap = evidence('ap', .97)
    assert confidence([reuters, ap], NOW)['status'] == 'CONFIRMED'
    press_origin = [evidence(f'blog{i}', .85, origin='x-unknown') for i in range(4)]
    assert confidence(press_origin, NOW)['independent_sources'] == 1

def test_age_is_graded_by_kind_and_event_type():
    fresh, aged = evidence('reuters', .97, kind='wire'), evidence('reuters', .97, kind='wire')
    aged['published_at'] = (NOW - timedelta(hours=20)).isoformat()
    assert confidence([aged], NOW)['confidence'] < confidence([fresh], NOW)['confidence']
    fed = evidence('fed', 1, True, kind='official')
    fed['published_at'] = (NOW - timedelta(hours=40)).isoformat()
    assert confidence([fed], NOW)['status'] == 'STALE'
    assert confidence([fed], NOW, event_type='CENTRAL_BANK')['status'] == 'CONFIRMED'
    rumor = evidence('x-unknown', .2, kind='social')
    rumor['published_at'] = (NOW - timedelta(hours=6)).isoformat()
    assert confidence([rumor], NOW)['confidence'] == pytest.approx(10, abs=.01)

def test_reliability_thresholds_come_from_config(monkeypatch):
    import backend.engines as engines
    config = rules()
    config['verification']['second_hand_factor'] = 1.0
    monkeypatch.setattr(engines, 'rules_view', lambda: config)
    second_hand = evidence('reuters', .97)
    second_hand['direct'] = False
    assert confidence([second_hand], NOW)['confidence'] == pytest.approx(97)
    config['verification']['false_min_weight'] = 0.5
    denial = evidence('fed', 1, True)
    denial['relation'] = 'contradicts'
    assert confidence([evidence('reuters', .97), denial], NOW)['status'] == 'FALSE'

def test_second_hand_attribution_and_near_duplicates_are_detected():
    from backend.engines import attribution_origin, syndicated_from
    assert attribution_origin('LONDON (Reuters) - Oil jumped after the attack') == 'reuters'
    assert attribution_origin('Oil spikes', 'according to the Associated Press, tankers were hit') == 'ap'
    assert attribution_origin('Tanker hit, via @SomeDesk') == 'x:@somedesk'
    assert attribution_origin('Original reporting from our correspondent') is None
    assert attribution_origin('Chipmaker rallies', 'Shares rose via Apple supplier deals') is None
    assert attribution_origin('LONDON (Reuters) - Oil jumped', source_id='reuters') is None
    assert attribution_origin('LONDON (Reuters) - Oil jumped', source_id='ap') == 'reuters'
    story = ('Oil prices jumped more than three percent on Monday after reports of an explosion near the Strait of '
             'Hormuz disrupted tanker traffic and raised supply fears across global energy markets')
    other = ('The Federal Reserve left its policy rate unchanged on Wednesday and signalled that officials want more '
             'evidence that inflation is moving sustainably toward the two percent objective')
    assert syndicated_from(story + ' traders said', [('ap', other), ('reuters', story)]) == 'reuters'
    assert syndicated_from(other, [('reuters', story)]) is None
    assert syndicated_from('too short', [('reuters', 'too short')]) is None

def test_category_helpers_and_legacy_reaction():
    from backend.engines import category_side, is_setup, reaction
    assert category_side('SHORT SETUP') == category_side('STRONG SHORT') == -1
    assert category_side('BULLISH LEAN / WAIT') == 1 and category_side('WAIT / DATA INSUFFICIENT') == 0
    assert is_setup('LONG') and is_setup('STRONG SHORT SETUP') and not is_setup('BEARISH LEAN / WAIT')
    assert reaction(100, 101, 0)['status'] == 'NOT_EXPECTED'
    assert reaction(100, 101, 1)['status'] == 'CONFIRMED' and reaction(100, 99, 1)['status'] == 'DIVERGENT'


def test_every_source_kind_has_an_age_half_life():
    policy = rules()['verification']
    kinds = set(rules()['source_weights']) | {s.get('kind') for s in rules()['sources']}
    assert kinds <= set(policy['half_life_hours_by_kind'])
    fed = evidence('fed', 1, True, source_id='fed')
    fed['published_at'] = (NOW - timedelta(hours=20)).isoformat()
    # Registry kind central_bank decays on the official half-life, not the 24h default.
    assert confidence([fed], NOW)['confidence'] > 95
