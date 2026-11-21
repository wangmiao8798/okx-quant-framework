import importlib.util
import json
import sqlite3
from pathlib import Path

MODULE=Path('/opt/okx_quant/scripts/dram_weighted_forward_research.py')
spec=importlib.util.spec_from_file_location('dram_weighted_forward_research',MODULE)
r=importlib.util.module_from_spec(spec); spec.loader.exec_module(r)


def report(okx, *, etf=100, mu=100, samsung=100, hynix=100, etf_status='Closed', kr_session='NXT'):
    return json.dumps({'quotes':{
        'okx':{'ok':True,'last':okx},
        'dram_etf':{'ok':True,'last':etf,'market_status':etf_status},
        'micron':{'ok':True,'last':mu,'market_status':etf_status},
        'samsung':{'ok':True,'last':samsung,'session':kr_session},
        'sk_hynix':{'ok':True,'last':hynix,'session':kr_session},
    }})


def seed(db, rows):
    with sqlite3.connect(db) as c:
        c.execute('create table snapshots(ts_ms integer primary key, payload_json text not null)')
        for ts,payload in rows:c.execute('insert into snapshots values(?,?)',(ts,payload))


def cfg():
    return {'weights':{'korea':{'sk_hynix':0.7,'samsung':0.3},'us':{'micron':0.6,'dram_etf':0.4}},'thresholds':{'lead_pct':0.5,'estimated_round_trip_cost_pct':0.2},'quality':{'window_tolerance_seconds':90}}


def test_korea_session_uses_hynix_70_samsung_30(tmp_path):
    db=tmp_path/'x.db'; t=1_000_000
    seed(db,[(t,report(100,hynix=100,samsung=100)),(t+300_000,report(101,hynix=104,samsung=102))])
    r.analyze(db,cfg(),horizons=[5])
    with sqlite3.connect(db) as c: row=c.execute('select session,round(reference_return_pct,3),round(okx_return_pct,3),round(divergence_pct,3),signal,data_quality from dram_aligned_returns').fetchone()
    # weighted reference = 3.4%, OKX = 1%, reference leads by 2.4%
    assert row == ('korea',3.4,1.0,-2.4,'reference_leads','ok')


def test_us_session_uses_mu_60_etf_40(tmp_path):
    db=tmp_path/'x.db'; t=1_000_000
    seed(db,[(t,report(100,mu=100,etf=100,etf_status='Market Open')),(t+300_000,report(104,mu=102,etf=101,etf_status='Market Open'))])
    r.analyze(db,cfg(),horizons=[5])
    with sqlite3.connect(db) as c: row=c.execute('select session,round(reference_return_pct,3),round(divergence_pct,3),signal,data_quality from dram_aligned_returns').fetchone()
    assert row == ('us',1.6,2.4,'okx_leads','ok')


def test_closed_or_mixed_sessions_force_neutral(tmp_path):
    db=tmp_path/'x.db'; t=1_000_000
    seed(db,[(t,report(100,etf_status='Closed',kr_session='unknown')),(t+300_000,report(105,etf_status='Closed',kr_session='unknown'))])
    r.analyze(db,cfg(),horizons=[5,15])
    with sqlite3.connect(db) as c: rows=c.execute('select window_minutes,signal,data_quality,reasons_json from dram_aligned_returns order by window_minutes').fetchall()
    assert rows[0][0:3] == (5,'neutral','bad') and 'no_active_reference_session' in rows[0][3]
    assert rows[1][0:3] == (15,'neutral','bad') and 'insufficient_history' in rows[1][3]


def test_forward_outcome_tracks_lagger_catchup_and_cost(tmp_path):
    db=tmp_path/'x.db'; t=1_000_000
    seed(db,[
      (t,report(100,hynix=100,samsung=100)),
      (t+300_000,report(101,hynix=104,samsung=102)),
      (t+600_000,report(104,hynix=105,samsung=103)),
    ])
    r.analyze(db,cfg(),horizons=[5],forward_horizons=[5]); r.analyze(db,cfg(),horizons=[5],forward_horizons=[5])
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from dram_forward_outcomes').fetchone()[0] == 1
        gap,net,good=c.execute('select lagger_catchup_pct,net_after_cost_pct,favorable from dram_forward_outcomes').fetchone()
    assert gap>0 and abs(net-(gap-0.2))<1e-9 and good==1


def test_stale_korea_quote_forces_neutral_and_nonoverlap_samples(tmp_path):
    db=tmp_path/'x.db'; t=1_000_000
    stale=report(100,hynix=100,samsung=100)
    fresh=report(101,hynix=104,samsung=102)
    for raw,quote_ts in [(stale,t-600_000),(fresh,t+300_000)]:
        obj=json.loads(raw)
        for name in ('samsung','sk_hynix'): obj['quotes'][name]['quote_ts_ms']=quote_ts
        if raw is stale: stale=json.dumps(obj)
        else: fresh=json.dumps(obj)
    seed(db,[(t,stale),(t+300_000,fresh)])
    strict=cfg(); strict['quality']['require_korea_quote_timestamp']=True; strict['quality']['max_reference_age_seconds']=180
    r.analyze(db,strict,horizons=[5],forward_horizons=[5])
    with sqlite3.connect(db) as c:
        quality,reasons=c.execute('select data_quality,reasons_json from dram_aligned_returns').fetchone()
    assert quality=='bad' and 'stale_reference_quote' in reasons


def test_nonoverlapping_outcomes_are_deduplicated(tmp_path):
    db=tmp_path/'x.db'; t=1_000_000; rows=[]
    for i in range(8):
        obj=json.loads(report(100+i*1.0,hynix=100+i*2.0,samsung=100+i*1.0))
        for name in ('samsung','sk_hynix'): obj['quotes'][name]['quote_ts_ms']=t+i*300_000
        rows.append((t+i*300_000,json.dumps(obj)))
    seed(db,rows); r.analyze(db,cfg(),horizons=[5],forward_horizons=[15])
    with sqlite3.connect(db) as c:
        all_rows=c.execute('select count(*) from dram_forward_outcomes').fetchone()[0]
        independent=c.execute('select count(*) from dram_independent_outcomes').fetchone()[0]
        overlaps=c.execute('select count(*) from dram_independent_outcomes a join dram_independent_outcomes b on a.rowid<b.rowid and a.signal_window_minutes=b.signal_window_minutes and a.forward_minutes=b.forward_minutes and a.signal_ts_ms<b.outcome_ts_ms and b.signal_ts_ms<a.outcome_ts_ms').fetchone()[0]
    assert independent <= all_rows and overlaps == 0


def test_stale_forward_endpoint_is_rejected(tmp_path):
    db=tmp_path/'x.db'; t=1_000_000; rows=[]
    for i,(okx,hynix,samsung) in enumerate([(100,100,100),(101,104,102),(104,105,103)]):
        obj=json.loads(report(okx,hynix=hynix,samsung=samsung))
        quote_ts=t+i*300_000 if i<2 else t+i*300_000-600_000
        for name in ('samsung','sk_hynix'): obj['quotes'][name]['quote_ts_ms']=quote_ts
        rows.append((t+i*300_000,json.dumps(obj)))
    seed(db,rows); strict=cfg(); strict['quality'].update({'require_korea_quote_timestamp':True,'max_reference_age_seconds':180})
    r.analyze(db,strict,horizons=[5],forward_horizons=[5])
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from dram_forward_outcomes').fetchone()[0] == 0
        assert c.execute('select count(*) from dram_independent_outcomes').fetchone()[0] == 0


def test_service_runs_weighted_research_and_keeps_key_inaccessible():
    service=Path('/opt/okx_quant/deploy/systemd/okx-dram-cross-market.service').read_text(encoding='utf-8')
    assert 'ExecStartPost=/opt/okx_quant/venv/bin/python3 /opt/okx_quant/scripts/dram_weighted_forward_research.py' in service
    assert 'InaccessiblePaths=/opt/okx_quant/data/keys.enc' in service


def test_source_freshness_uses_latest_snapshot(tmp_path):
    db=tmp_path/'x.db'; t=1_000_000
    seed(db,[(t,report(100))])
    assert r.source_is_fresh(db,90,now_ms=t+89_000)
    assert not r.source_is_fresh(db,90,now_ms=t+91_000)
    assert r.source_is_fresh(db,90,now_ms=t+89_000,cfg=cfg())
