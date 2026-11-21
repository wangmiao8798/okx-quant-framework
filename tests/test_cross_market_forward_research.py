import importlib.util
import json
import sqlite3
from pathlib import Path

MODULE = Path('/opt/okx_quant/scripts/cross_market_forward_research.py')
spec = importlib.util.spec_from_file_location('cross_market_forward_research', MODULE)
research = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(research)


def payload(symbol, price, market='Market Open'):
    return json.dumps({'symbol': symbol, 'price': price, 'ok': True, 'market_status': market})


def seed(db, rows):
    with sqlite3.connect(db) as c:
        c.execute('create table normalized_quotes(batch_id text,symbol text,source text,price real,change_pct real,age_seconds real,ok integer,payload_json text,primary key(batch_id,symbol))')
        for batch, symbol, price, market in rows:
            c.execute('insert into normalized_quotes values(?,?,?,?,?,?,?,?)', (batch, symbol, 'test', price, 0, 0, 1, payload(symbol, price, market)))


def cfg():
    return {'thresholds': {'lead_pct': 2.0, 'estimated_round_trip_cost_pct': 0.2}, 'quality': {'window_tolerance_seconds': 90}, 'themes': {'btc_equities': {'crypto': 'BTC', 'equities': ['IBIT', 'MSTR'], 'benchmark': 'QQQ'}}}


def test_builds_aligned_five_minute_signal(tmp_path):
    db = tmp_path/'x.db'
    seed(db, [
        ('2026-01-01T14:30:00+00:00','BTC',100,'Market Open'),('2026-01-01T14:30:00+00:00','IBIT',100,'Market Open'),('2026-01-01T14:30:00+00:00','MSTR',100,'Market Open'),('2026-01-01T14:30:00+00:00','QQQ',100,'Market Open'),
        ('2026-01-01T14:35:00+00:00','BTC',105,'Market Open'),('2026-01-01T14:35:00+00:00','IBIT',102,'Market Open'),('2026-01-01T14:35:00+00:00','MSTR',102,'Market Open'),('2026-01-01T14:35:00+00:00','QQQ',101,'Market Open'),
    ])
    result = research.analyze_database(db, cfg(), horizons=[5])
    assert result['aligned_rows_written'] == 1
    with sqlite3.connect(db) as c:
        row = c.execute('select signal,round(crypto_return_pct,3),round(equity_excess_pct,3),round(divergence_pct,3),data_quality from aligned_window_returns').fetchone()
    assert row == ('crypto_leads', 5.0, 1.0, 4.0, 'ok')


def test_closed_market_and_missing_window_are_neutral(tmp_path):
    db = tmp_path/'x.db'
    seed(db, [
        ('2026-01-01T14:30:00+00:00','BTC',100,'Closed'),('2026-01-01T14:30:00+00:00','IBIT',100,'Closed'),('2026-01-01T14:30:00+00:00','MSTR',100,'Closed'),('2026-01-01T14:30:00+00:00','QQQ',100,'Closed'),
        ('2026-01-01T14:35:00+00:00','BTC',105,'Closed'),('2026-01-01T14:35:00+00:00','IBIT',102,'Closed'),('2026-01-01T14:35:00+00:00','MSTR',102,'Closed'),('2026-01-01T14:35:00+00:00','QQQ',101,'Closed'),
    ])
    research.analyze_database(db, cfg(), horizons=[5,15])
    with sqlite3.connect(db) as c:
        rows = c.execute('select window_minutes,signal,data_quality,reasons_json from aligned_window_returns order by window_minutes').fetchall()
    assert rows[0][0:3] == (5,'neutral','bad') and 'equity_market_closed' in rows[0][3]
    assert rows[1][0:3] == (15,'neutral','bad') and 'insufficient_history' in rows[1][3]


def test_forward_outcome_backfills_directional_convergence_mfe_mae_and_cost(tmp_path):
    db = tmp_path/'x.db'
    batches = [
        ('2026-01-01T14:30:00+00:00',100,100,100,100),
        ('2026-01-01T14:35:00+00:00',105,102,102,101),
        # BTC led initially; during the forward window equities outperform BTC.
        ('2026-01-01T14:40:00+00:00',106,105,105,102),
    ]
    rows=[]
    for batch,btc,ibit,mstr,qqq in batches:
        rows += [(batch,'BTC',btc,'Market Open'),(batch,'IBIT',ibit,'Market Open'),(batch,'MSTR',mstr,'Market Open'),(batch,'QQQ',qqq,'Market Open')]
    seed(db, rows)
    research.analyze_database(db, cfg(), horizons=[5], forward_horizons=[5])
    research.analyze_database(db, cfg(), horizons=[5], forward_horizons=[5])
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from forward_outcomes').fetchone()[0] == 1
        convergence,mfe,mae,net,favorable = c.execute('select gap_reduction_pct,mfe_pct,mae_pct,net_after_cost_pct,favorable from forward_outcomes').fetchone()
    # Forward BTC ~=0.952%; equity excess ~=1.951%; lagger catch-up ~=+0.999%.
    assert abs(convergence - 0.9987) < 0.001
    assert mfe >= convergence and mae <= 0
    assert abs(net - (convergence - 0.2)) < 1e-9
    assert favorable == 1


def test_bad_target_endpoint_does_not_create_forward_outcome(tmp_path):
    db = tmp_path/'x.db'
    rows=[]
    for batch,btc,ibit,mstr,qqq,market in [
        ('2026-01-01T14:30:00+00:00',100,100,100,100,'Market Open'),
        ('2026-01-01T14:35:00+00:00',105,102,102,101,'Market Open'),
        ('2026-01-01T14:40:00+00:00',106,105,105,102,'Closed'),
    ]:
        rows += [(batch,'BTC',btc,market),(batch,'IBIT',ibit,market),(batch,'MSTR',mstr,market),(batch,'QQQ',qqq,market)]
    seed(db, rows)
    research.analyze_database(db, cfg(), horizons=[5], forward_horizons=[5])
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from forward_outcomes').fetchone()[0] == 0


def test_bad_target_endpoint_removes_previously_written_forward_outcome(tmp_path):
    db = tmp_path/'x.db'; rows=[]
    for batch,btc,ibit,mstr,qqq in [
        ('2026-01-01T14:30:00+00:00',100,100,100,100),
        ('2026-01-01T14:35:00+00:00',105,102,102,101),
        ('2026-01-01T14:40:00+00:00',106,105,105,102),
    ]:
        rows += [(batch,'BTC',btc,'Market Open'),(batch,'IBIT',ibit,'Market Open'),(batch,'MSTR',mstr,'Market Open'),(batch,'QQQ',qqq,'Market Open')]
    seed(db,rows); research.analyze_database(db,cfg(),horizons=[5],forward_horizons=[5])
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from forward_outcomes').fetchone()[0] == 1
        c.execute("update normalized_quotes set age_seconds=999 where batch_id='2026-01-01T14:40:00+00:00' and symbol='QQQ'")
    research.analyze_database(db,cfg(),horizons=[5],forward_horizons=[5])
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from forward_outcomes').fetchone()[0] == 0


def test_start_endpoint_closed_or_stale_forces_bad_window(tmp_path):
    db = tmp_path/'x.db'
    rows=[]
    for batch,market in [('2026-01-01T14:30:00+00:00','Closed'),('2026-01-01T14:35:00+00:00','Market Open')]:
        rows += [(batch,'BTC',100,market),(batch,'IBIT',100,market),(batch,'MSTR',100,market),(batch,'QQQ',100,market)]
    seed(db, rows)
    research.analyze_database(db, cfg(), horizons=[5])
    with sqlite3.connect(db) as c:
        quality,reasons=c.execute('select data_quality,reasons_json from aligned_window_returns').fetchone()
    assert quality == 'bad' and 'equity_market_closed' in reasons

    stale_db = tmp_path/'stale.db'
    seed(stale_db, [
        ('2026-01-01T14:30:00+00:00','BTC',100,'Market Open'),('2026-01-01T14:30:00+00:00','IBIT',100,'Market Open'),('2026-01-01T14:30:00+00:00','MSTR',100,'Market Open'),('2026-01-01T14:30:00+00:00','QQQ',100,'Market Open'),
        ('2026-01-01T14:35:00+00:00','BTC',101,'Market Open'),('2026-01-01T14:35:00+00:00','IBIT',101,'Market Open'),('2026-01-01T14:35:00+00:00','MSTR',101,'Market Open'),('2026-01-01T14:35:00+00:00','QQQ',101,'Market Open'),
    ])
    with sqlite3.connect(stale_db) as c:
        c.execute("update normalized_quotes set age_seconds=999 where batch_id='2026-01-01T14:35:00+00:00' and symbol='QQQ'")
    research.analyze_database(stale_db, cfg(), horizons=[5])
    with sqlite3.connect(stale_db) as c:
        quality,reasons=c.execute('select data_quality,reasons_json from aligned_window_returns').fetchone()
    assert quality == 'bad' and 'stale_quote' in reasons


def test_equities_lead_forward_convergence_direction(tmp_path):
    db=tmp_path/'x.db'; rows=[]
    for batch,btc,eq,qqq in [
        ('2026-01-01T14:30:00+00:00',100,100,100),
        ('2026-01-01T14:35:00+00:00',100,105,101),
        ('2026-01-01T14:40:00+00:00',103,106,102),
    ]:
        rows += [(batch,'BTC',btc,'Market Open'),(batch,'IBIT',eq,'Market Open'),(batch,'MSTR',eq,'Market Open'),(batch,'QQQ',qqq,'Market Open')]
    seed(db,rows); research.analyze_database(db,cfg(),horizons=[5],forward_horizons=[5])
    with sqlite3.connect(db) as c:
        signal,convergence,outcome=c.execute('select a.signal,f.gap_reduction_pct,f.outcome_divergence_pct from aligned_window_returns a join forward_outcomes f on f.signal_batch_id=a.end_batch_id and f.theme=a.theme and f.signal_window_minutes=a.window_minutes').fetchone()
    assert signal == 'equities_lead' and convergence > 0
    assert abs(outcome) < abs(-3.0)


def test_summary_reports_pending_until_enough_history(tmp_path):
    db=tmp_path/'x.db'; seed(db,[('2026-01-01T14:30:00+00:00','BTC',100,'Market Open')])
    result=research.analyze_database(db,cfg(),horizons=[5,15,30,60])
    assert result['horizons'] == [5,15,30,60]
    assert result['directional_signals'] == 0


def test_systemd_runs_forward_research_after_collection():
    service = Path('/opt/okx_quant/deploy/systemd/okx-cross-market-opportunity.service').read_text(encoding='utf-8')
    assert 'ExecStartPost=/opt/okx_quant/venv/bin/python3 /opt/okx_quant/scripts/cross_market_forward_research.py' in service


def test_source_freshness_uses_latest_collected_batch(tmp_path):
    db=tmp_path/'x.db'; batch='2026-01-01T14:30:00+00:00'
    seed(db,[(batch,'BTC',100,'Market Open')])
    assert research.source_is_fresh(db,60,now=research._ts(batch)+30)
    assert not research.source_is_fresh(db,60,now=research._ts(batch)+61)


def test_latest_benchmark_must_be_open(tmp_path):
    db=tmp_path/'x.db'; batch='2026-01-01T14:30:00+00:00'
    seed(db,[(batch,'QQQ',100,'Closed')])
    assert not research.latest_benchmarks_open(db,{'QQQ'})
    with sqlite3.connect(db) as c:
        c.execute("update normalized_quotes set payload_json=? where symbol='QQQ'",(payload('QQQ',100,'Market Open'),))
    assert research.latest_benchmarks_open(db,{'QQQ'})
