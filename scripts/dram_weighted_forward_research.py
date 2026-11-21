#!/usr/bin/env python3
"""Weighted, session-aware DRAM cross-market aligned and forward research."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_DB=ROOT/'data/dram_cross_market.db'
DEFAULT_CONFIG=ROOT/'config/dram_cross_market_research.yaml'
DEFAULT_REPORT=ROOT/'data/dram_weighted_forward_report.json'
DEFAULT_HORIZONS=[5,15,30,60]


def source_is_fresh(db:Path,max_age_seconds:int,now_ms:int|None=None,cfg:dict|None=None)->bool:
    if not db.exists():return False
    with sqlite3.connect(db,timeout=30) as conn:row=conn.execute('select ts_ms,payload_json from snapshots order by ts_ms desc limit 1').fetchone()
    latest=int(row[0] or 0) if row else 0
    age=(now_ms if now_ms is not None else int(time.time()*1000))-latest
    if not (latest>0 and 0<=age<=max_age_seconds*1000):return False
    if cfg is None:return True
    payload=json.loads(row[1]); session=session_for(latest,payload,latest,payload)
    return session is not None and (session!='korea' or korea_quotes_fresh(latest,payload,latest,payload,cfg))


def pct(a:float,b:float)->float:
    return (b/a-1)*100 if a>0 and b>0 else 0.0


def korea_open(ts_ms:int)->bool:
    dt=datetime.fromtimestamp(ts_ms/1000,timezone.utc).astimezone(timezone(timedelta(hours=9)))
    minutes=dt.hour*60+dt.minute
    return dt.weekday()<5 and 9*60<=minutes<=15*60+30


def load_rows(conn:sqlite3.Connection)->list[tuple[int,dict[str,Any]]]:
    return [(int(ts),json.loads(payload)) for ts,payload in conn.execute('select ts_ms,payload_json from snapshots order by ts_ms')]


def prior(rows,end_i:int,minutes:int,tolerance_sec:int):
    target=rows[end_i][0]-minutes*60_000
    candidates=[(abs(ts-target),ts,payload) for ts,payload in rows[:end_i]]
    if not candidates:return None
    distance,ts,payload=min(candidates,key=lambda x:x[0])
    return (ts,payload) if distance<=tolerance_sec*1000 else None


def session_for(start_ts:int,start:dict,end_ts:int,end:dict)->str|None:
    sq,eq=start['quotes'],end['quotes']
    us_open=all(str(q.get('market_status','')).lower() in {'market open','open','regular market'} for q in (sq['dram_etf'],sq['micron'],eq['dram_etf'],eq['micron']))
    kr_sessions=(sq['samsung'].get('session'),sq['sk_hynix'].get('session'),eq['samsung'].get('session'),eq['sk_hynix'].get('session'))
    kr_open=korea_open(start_ts) and korea_open(end_ts) and all(str(x).upper() in {'KRX','NXT'} for x in kr_sessions)
    if us_open:return 'us'
    if kr_open:return 'korea'
    return None


def korea_quotes_fresh(start_ts:int,start:dict,end_ts:int,end:dict,cfg:dict)->bool:
    if not cfg.get('quality',{}).get('require_korea_quote_timestamp'):
        return True
    max_age=int(cfg['quality'].get('max_reference_age_seconds',180))*1000
    quotes=[start['quotes'][name] for name in ('samsung','sk_hynix')]+[end['quotes'][name] for name in ('samsung','sk_hynix')]
    timestamps=[int(q.get('quote_ts_ms') or 0) for q in quotes]
    return all(ts>0 for ts in timestamps) and all(abs(point-ts)<=max_age for point,ts in zip((start_ts,start_ts,end_ts,end_ts),timestamps))


def weighted_return(start:dict,end:dict,session:str,cfg:dict)->float:
    weights=cfg['weights'][session]
    return sum(float(weight)*pct(float(start['quotes'][name]['last']),float(end['quotes'][name]['last'])) for name,weight in weights.items())


def aligned_row(end_ts:int,start_ts:int|None,start:dict|None,end:dict,minutes:int,cfg:dict)->dict:
    reasons=[]
    if start is None:reasons.append('insufficient_history')
    session=session_for(start_ts,start,end_ts,end) if start is not None else None
    if session is None:reasons.append('no_active_reference_session')
    if session=='korea' and not korea_quotes_fresh(start_ts,start,end_ts,end,cfg):
        reasons.append('stale_reference_quote')
    okx_return=reference_return=divergence=0.0
    if not reasons:
        if not start['quotes']['okx'].get('ok') or not end['quotes']['okx'].get('ok'):
            reasons.append('missing_okx')
        else:
            reference_return=weighted_return(start,end,session,cfg)
            okx_return=pct(float(start['quotes']['okx']['last']),float(end['quotes']['okx']['last']))
            divergence=okx_return-reference_return
    signal='neutral'; threshold=float(cfg['thresholds'].get('lead_pct',0.5))
    if not reasons:
        if divergence>=threshold:signal='okx_leads'
        elif divergence<=-threshold:signal='reference_leads'
        else:reasons.append('inside_band')
    reasons.append('advisory_only')
    quality='ok' if reasons in (['advisory_only'],['inside_band','advisory_only']) else 'bad'
    return {'end_ts_ms':end_ts,'start_ts_ms':start_ts,'window_minutes':minutes,'session':session,
            'okx_return_pct':okx_return,'reference_return_pct':reference_return,'divergence_pct':divergence,
            'signal':signal,'data_quality':quality,'reasons':reasons,'advisory_only':True}


def ensure_schema(conn):
    conn.executescript('''
    create table if not exists dram_aligned_returns(
      end_ts_ms integer,window_minutes integer,start_ts_ms integer,session text,
      okx_return_pct real,reference_return_pct real,divergence_pct real,signal text,
      data_quality text,reasons_json text,payload_json text,primary key(end_ts_ms,window_minutes));
    create table if not exists dram_forward_outcomes(
      signal_ts_ms integer,signal_window_minutes integer,forward_minutes integer,outcome_ts_ms integer,
      session text,signal text,initial_divergence_pct real,outcome_divergence_pct real,
      lagger_catchup_pct real,mfe_pct real,mae_pct real,estimated_cost_pct real,
      net_after_cost_pct real,favorable integer,payload_json text,
      primary key(signal_ts_ms,signal_window_minutes,forward_minutes));
    create table if not exists dram_independent_outcomes(
      signal_ts_ms integer,signal_window_minutes integer,forward_minutes integer,outcome_ts_ms integer,
      session text,signal text,initial_divergence_pct real,outcome_divergence_pct real,
      lagger_catchup_pct real,mfe_pct real,mae_pct real,estimated_cost_pct real,
      net_after_cost_pct real,favorable integer,payload_json text,
      primary key(signal_ts_ms,signal_window_minutes,forward_minutes));
    ''')


def write_aligned(conn,row):
    conn.execute('''insert or replace into dram_aligned_returns
      (end_ts_ms,window_minutes,start_ts_ms,session,okx_return_pct,reference_return_pct,divergence_pct,signal,data_quality,reasons_json,payload_json)
      values(?,?,?,?,?,?,?,?,?,?,?)''',(row['end_ts_ms'],row['window_minutes'],row['start_ts_ms'],row['session'],row['okx_return_pct'],row['reference_return_pct'],row['divergence_pct'],row['signal'],row['data_quality'],json.dumps(row['reasons']),json.dumps(row,sort_keys=True)))


def build_forward(conn,rows,cfg,forward_horizons):
    index={ts:i for i,(ts,_) in enumerate(rows)}; tolerance=int(cfg['quality'].get('window_tolerance_seconds',90)); cost=float(cfg['thresholds'].get('estimated_round_trip_cost_pct',0.2)); written=0
    signals=conn.execute("select end_ts_ms,window_minutes,session,divergence_pct,signal from dram_aligned_returns where data_quality='ok' and signal!='neutral'").fetchall()
    for signal_ts,window,session,initial,signal in signals:
        start_i=index.get(signal_ts)
        if start_i is None:continue
        for forward in forward_horizons:
            target=signal_ts+forward*60_000
            candidates=[(abs(ts-target),i) for i,(ts,_) in enumerate(rows) if i>start_i]
            if not candidates:continue
            distance,out_i=min(candidates)
            if distance>tolerance*1000:continue
            points=[]; signal_payload=rows[start_i][1]
            for i in range(start_i+1,out_i+1):
                ts,payload=rows[i]
                if session_for(signal_ts,signal_payload,ts,payload)!=session or (session=='korea' and not korea_quotes_fresh(signal_ts,signal_payload,ts,payload,cfg)):
                    points=[]; break
                okx_forward=pct(float(signal_payload['quotes']['okx']['last']),float(payload['quotes']['okx']['last']))
                ref_forward=weighted_return(signal_payload,payload,session,cfg)
                points.append(okx_forward-ref_forward if signal=='reference_leads' else ref_forward-okx_forward)
            if not points:continue
            catchup=points[-1]; outcome=initial+catchup if signal=='reference_leads' else initial-catchup
            data={'signal_ts_ms':signal_ts,'outcome_ts_ms':rows[out_i][0],'signal_window_minutes':window,'forward_minutes':forward,'session':session,'signal':signal,'initial_divergence_pct':initial,'outcome_divergence_pct':outcome,'lagger_catchup_pct':catchup,'mfe_pct':max([0.0,*points]),'mae_pct':min([0.0,*points]),'estimated_cost_pct':cost,'net_after_cost_pct':catchup-cost,'favorable':catchup-cost>0,'advisory_only':True}
            conn.execute('''insert or replace into dram_forward_outcomes values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(signal_ts,window,forward,rows[out_i][0],session,signal,initial,outcome,catchup,data['mfe_pct'],data['mae_pct'],cost,data['net_after_cost_pct'],int(data['favorable']),json.dumps(data,sort_keys=True))); written+=1
    return written


def build_independent(conn):
    """Select non-overlapping outcomes for honest aggregate statistics."""
    conn.execute('delete from dram_independent_outcomes')
    rows=conn.execute('''select signal_ts_ms,signal_window_minutes,forward_minutes,outcome_ts_ms,session,signal,
      initial_divergence_pct,outcome_divergence_pct,lagger_catchup_pct,mfe_pct,mae_pct,
      estimated_cost_pct,net_after_cost_pct,favorable,payload_json
      from dram_forward_outcomes order by session,signal_window_minutes,forward_minutes,signal_ts_ms''').fetchall()
    last_outcome={}; written=0
    for row in rows:
        key=(row[4],row[1],row[2])
        if row[0] < last_outcome.get(key,0):
            continue
        conn.execute('insert into dram_independent_outcomes values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row)
        last_outcome[key]=row[3]; written+=1
    return written


def analyze(db:Path,cfg:dict,horizons=None,forward_horizons=None)->dict:
    horizons=horizons or DEFAULT_HORIZONS; forward_horizons=forward_horizons or DEFAULT_HORIZONS
    with sqlite3.connect(db,timeout=30) as conn:
        ensure_schema(conn); conn.execute('delete from dram_independent_outcomes'); conn.execute('delete from dram_forward_outcomes'); conn.execute('delete from dram_aligned_returns'); rows=load_rows(conn); written=0
        if rows:
            tolerance=int(cfg['quality'].get('window_tolerance_seconds',90)); latest=len(rows)-1
            for i,(ts,payload) in enumerate(rows):
                for minutes in horizons:
                    start=prior(rows,i,minutes,tolerance)
                    if start or i==latest:
                        write_aligned(conn,aligned_row(ts,start[0] if start else None,start[1] if start else None,payload,minutes,cfg)); written+=1
            forward_written=build_forward(conn,rows,cfg,forward_horizons)
            independent_written=build_independent(conn)
        else:forward_written=independent_written=0
        conn.commit(); directional=conn.execute("select count(*) from dram_aligned_returns where data_quality='ok' and signal!='neutral'").fetchone()[0]; bad=conn.execute("select count(*) from dram_aligned_returns where data_quality='bad'").fetchone()[0]
    return {'horizons':horizons,'forward_horizons':forward_horizons,'aligned_rows_written':written,'forward_rows_written':forward_written,'independent_forward_rows':independent_written,'directional_signals':directional,'pending_or_bad_windows':bad,'advisory_only':True}


def main()->int:
    p=argparse.ArgumentParser(); p.add_argument('--db',type=Path,default=DEFAULT_DB); p.add_argument('--config',type=Path,default=DEFAULT_CONFIG); p.add_argument('--report',type=Path,default=DEFAULT_REPORT); p.add_argument('--max-source-age-sec',type=int,default=90); p.add_argument('--force',action='store_true'); args=p.parse_args()
    cfg=yaml.safe_load(args.config.read_text(encoding='utf-8'))
    if not args.force and not source_is_fresh(args.db,max(1,args.max_source_age_sec),cfg=cfg):
        print(json.dumps({'advisory_only':True,'skipped':'no_fresh_source_snapshot'},sort_keys=True)); return 0
    result=analyze(args.db,cfg); args.report.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n'); print(json.dumps(result,sort_keys=True)); return 0


if __name__=='__main__':raise SystemExit(main())
