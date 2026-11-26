from datetime import UTC, datetime, timedelta

import pytest

from backend import backtest
from backend.config import rules
from backend.database import Document, Event, Evidence, Store, Tick

T0 = datetime(2026, 3, 2, 12, tzinfo=UTC)  # Monday; crypto is always LIVE


@pytest.fixture
def store():
    value = Store('sqlite://')
    value.initialize()
    value.put('sources', 'rumour', {'id': 'rumour', 'name': 'Rumour', 'domain': 'x.com', 'kind': 'social',
                                    'independence_group': 'rumour'})
    value.put('sources', 'fed', {'id': 'fed', 'name': 'Federal Reserve', 'domain': 'federalreserve.gov',
                                 'kind': 'official', 'official': True, 'independence_group': 'fed'})
    return value


def causal_target():
    """A (trigger, symbol, effect) from the configured causal rules, so the test follows the config."""
    for rule in rules()['causal_rules']:
        for symbol, effect in rule['effects'].items():
            if effect and symbol in {'BTC', 'ETH'}:
                return rule['trigger'], symbol
    pytest.skip('No crypto causal effect configured')


def add_document(store, key, source, event_id, *, published, ingested):
    with store.session() as session:
        session.add(Document(id=key, fingerprint=key, source_id=source, title=f'{key} headline', url=f'https://{key}',
                             content='body', published_at=published, created_at=ingested,
                             payload={'direct': True, 'discovery_only': False}))
        session.flush()
        session.add(Evidence(id=f'ev-{key}', event_id=event_id, document_id=key, relation='supports', created_at=ingested))
        session.commit()


def seed_event(store, trigger, symbol):
    with store.session() as session:
        session.add(Event(id='e1', summary_fact='Hot inflation print', event_type=trigger, created_at=T0,
                          updated_at=T0, payload={'assets': [symbol]}))
        for minute in range(0, 180):
            at = T0 - timedelta(minutes=60) + timedelta(minutes=minute)
            session.add(Tick(id=f't{minute}', symbol=symbol, provider='binance', price=100 + minute * .01,
                             timestamp=at, ingested_at=at, payload={'bid': 99.99, 'ask': 100.01}))
        session.commit()
    add_document(store, 'rumour-post', 'rumour', 'e1', published=T0, ingested=T0)


def test_later_ingested_backdated_article_cannot_affect_earlier_decision(store):
    trigger, symbol = causal_target()
    seed_event(store, trigger, symbol)
    simulated = T0 + timedelta(minutes=30)
    before = backtest.replay(store, simulated, simulated, 60, [symbol], evaluate=False)['decisions'][0]
    # Official confirmation published before the simulated time but only ingested an hour later.
    add_document(store, 'official-release', 'fed', 'e1', published=T0 - timedelta(minutes=30),
                 ingested=T0 + timedelta(minutes=90))
    after = backtest.replay(store, simulated, simulated, 60, [symbol], evaluate=False)['decisions'][0]
    assert after == before
    assert before['components']['event'] == 0 and 'No verified asset-specific catalyst' in before['blocks']
    later = T0 + timedelta(minutes=95)
    confirmed = backtest.replay(store, later, later, 60, [symbol], evaluate=False)['decisions'][0]
    assert confirmed['components']['event'] != 0 and confirmed['events'] == ['e1']


def test_ticks_are_point_in_time_and_outcomes_have_no_lookahead(store):
    trigger, symbol = causal_target()
    seed_event(store, trigger, symbol)
    with store.session() as session:
        # A print stamped at T0+30m but only ingested at T0+3h must not be the quote at T0+30m.
        session.add(Tick(id='late', symbol=symbol, provider='binance', price=500, timestamp=T0 + timedelta(minutes=30, seconds=1),
                         ingested_at=T0 + timedelta(hours=3), payload={'bid': 499, 'ask': 501}))
        session.commit()
    simulated = T0 + timedelta(minutes=30, seconds=5)
    run = backtest.replay(store, simulated, simulated, 60, [symbol], horizons=['5m'], now=T0 + timedelta(hours=4))
    decision = run['decisions'][0]
    assert decision['quote']['price'] < 200
    assert decision['outcomes']['5m']['end_price'] < 200  # the late print was not known by the horizon end
    assert run['summary']['5m']['observations'] == (1 if decision['outcomes']['5m']['side'] else 0)
    assert decision['ai_replayed'] is False and run['limitations']


def test_calendar_actual_hidden_until_first_seen(store):
    store.put('macro_calendar', 'cpi', {'name': 'CPI', 'ingested_at': (T0 - timedelta(days=1)).isoformat(),
                                        'actual': 3.4, 'actual_ingested_at': (T0 + timedelta(minutes=5)).isoformat()})
    store.put('macro_calendar', 'legacy', {'name': 'PPI', 'ingested_at': (T0 - timedelta(days=1)).isoformat(), 'actual': 1.0})
    store.put('macro_calendar', 'future', {'name': 'NFP', 'ingested_at': (T0 + timedelta(days=1)).isoformat()})
    known = {item['name']: item for item in backtest.calendar_as_of(store, T0)}
    assert set(known) == {'CPI', 'PPI'}
    assert known['CPI']['actual'] is None and known['CPI']['actual_status'] == 'NOT YET KNOWN'
    assert known['PPI']['actual'] is None and known['PPI']['actual_status'] == 'UNKNOWN INGESTION TIME'
    assert {i['name']: i for i in backtest.calendar_as_of(store, T0 + timedelta(minutes=6))}['CPI']['actual'] == 3.4


def test_replay_validates_window_and_step_budget(store):
    with pytest.raises(ValueError):
        backtest.replay(store, T0, T0 - timedelta(seconds=1), 60)
    with pytest.raises(ValueError):
        backtest.replay(store, T0, T0 + timedelta(days=365), 1)
    with pytest.raises(ValueError):  # a replay cannot simulate beyond what is known now
        backtest.replay(store, T0, T0 + timedelta(hours=1), 60, now=T0 + timedelta(minutes=30))


def test_challenger_weights_replay_on_identical_inputs(store):
    trigger, symbol = causal_target()
    seed_event(store, trigger, symbol)
    add_document(store, 'official-release', 'fed', 'e1', published=T0, ingested=T0 + timedelta(minutes=1))
    moment = T0 + timedelta(minutes=10)
    champion = backtest.replay(store, moment, moment, 60, [symbol], evaluate=False)['decisions'][0]
    challenger = backtest.replay(store, moment, moment, 60, [symbol], weights={'event': 1.0}, evaluate=False)['decisions'][0]
    assert challenger['components'] == champion['components']
    assert challenger['score'] == pytest.approx(champion['components']['event'], abs=.01)
    report = backtest.compare_weights(store, moment, moment, 60, 'catalyst-weighted', '5m', [symbol])
    assert {'champion', 'challenger', 'limitations'} <= report.keys()
