#!/usr/bin/env python3
"""Read-only baseline for realized oscillation harvesting and current inventory risk."""
from __future__ import annotations
import argparse, csv, json, sqlite3, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'
CSV_FILES=[DATA/'pnl_records.csv',DATA/'pnl_records_dram.csv',DATA/'pnl_records_fartcoin.csv']
STATUS=DATA/'strategies_status.json'
TP_AUDIT=DATA/'tp_coverage_audit.json'
OUT=DATA/'oscillation_harvest_report.json'
DB=DATA/'trading.db'

def f(v:Any)->float:
    try:return float(v or 0)
    except:return 0.0

def parse_ts(raw:str)->float:
    raw=str(raw or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S','%Y-%m-%dT%H:%M:%S'):
        try:return datetime.strptime(raw[:19],fmt).timestamp()
        except ValueError:pass
    try:
        v=float(raw);return v/1000 if v>10_000_000_000 else v
    except:return 0.0

def read_rows(hours:float)->list[dict]:
    cutoff=time.time()-hours*3600;rows=[]
    for path in CSV_FILES:
        if not path.exists():continue
        with path.open(newline='',encoding='utf-8-sig') as fh:
            for r in csv.DictReader(fh):
                if parse_ts(r.get('time') or r.get('ts'))<cutoff:continue
                gross=f(r.get('gross') or r.get('pnl'));fee=f(r.get('fee'));net=f(r.get('net') if r.get('net') not in (None,'') else gross-fee)
                rows.append({'symbol':r.get('symbol') or path.stem,'side':r.get('side') or 'unknown','gross':gross,'fee':fee,'net':net,'qty':f(r.get('qty'))})
    return rows

def summarize(rows:list[dict])->dict:
    agg=defaultdict(lambda:{'close_fill_rows':0,'gross_close_pnl':0.0,'close_fee':0.0,'close_net_before_open_fee':0.0,'qty_closed':0.0,'by_side':defaultdict(lambda:{'close_fill_rows':0,'gross_close_pnl':0.0,'close_fee':0.0,'close_net_before_open_fee':0.0,'qty_closed':0.0})})
    for r in rows:
        a=agg[r['symbol']];b=a['by_side'][r['side']]
        for x in (a,b):
            x['close_fill_rows']+=1;x['gross_close_pnl']+=r['gross'];x['close_fee']+=r['fee'];x['close_net_before_open_fee']+=r['net'];x['qty_closed']+=r['qty']
    out={}
    for sym,a in agg.items():
        sides={}
        for side,b in a['by_side'].items():
            sides[side]={k:(round(v,6) if isinstance(v,float) else v) for k,v in b.items()}
        gross=a['gross_close_pnl']
        out[sym]={
          'close_fill_rows':a['close_fill_rows'],'gross_close_pnl':round(gross,6),'close_fee':round(a['close_fee'],6),'close_net_before_open_fee':round(a['close_net_before_open_fee'],6),'qty_closed':round(a['qty_closed'],6),
          'close_fee_to_gross_pct':round(a['close_fee']/abs(gross)*100,4) if gross else None,'by_side':sides,
          'both_sides_have_close_fills':all(side in sides and sides[side]['close_fill_rows']>0 for side in ('long','short'))}
    return out

def status_overlay(status:dict)->dict:
    out={}
    for runtime,s in (status.get('strategies') or {}).items():
        m=s.get('monitor') or {};sym=s.get('symbol') or runtime
        out[sym]={'runtime':runtime,'long_qty':f(s.get('long_qty')),'short_qty':f(s.get('short_qty')),'unrealized_pnl':f(m.get('total_unrealized_pnl')),'imbalance_pct':f(m.get('imbalance_pct')),'margin_load_pct':f(m.get('margin_load_pct')),'net_exposure_usdt':f(m.get('net_exposure_usdt')),'block_long':m.get('long_add_block_reason',''),'block_short':m.get('short_add_block_reason','')}
    return out

def harvest_coverage_metrics(realized_net:float,unrealized_pnl:float)->dict:
    open_loss=max(0.0,-f(unrealized_pnl))
    realized=f(realized_net)
    ratio=realized/open_loss if open_loss else None
    return {
      'harvest_to_open_loss_ratio':round(ratio,4) if ratio is not None else None,
      'open_loss_covered_by_harvest':open_loss==0 or realized>=open_loss,
      'harvest_surplus_after_open_loss':round(realized-open_loss,6),
    }

def _sample_at_or_before(samples:list[dict],target:float)->dict:
    eligible=[row for row in samples if row['ts']<=target]
    return eligible[-1] if eligible else samples[0]

def inventory_history_metrics(samples:list[dict],now_sec:float|None=None)->dict:
    if not samples:return {'change_6h':None,'change_24h':None,'change_72h':None,'skew_over_50h':0.0,'history_hours':0.0}
    samples=sorted(samples,key=lambda row:row['ts']);now_sec=f(now_sec if now_sec is not None else samples[-1]['ts']);current=samples[-1]
    def change(hours:float)->dict|None:
        target=now_sec-hours*3600
        if samples[0]['ts']>target:return None
        old=_sample_at_or_before(samples,target)
        return {'long':round(f(current['long_qty'])-f(old['long_qty']),6),'short':round(f(current['short_qty'])-f(old['short_qty']),6),'net':round((f(current['long_qty'])-f(current['short_qty']))-(f(old['long_qty'])-f(old['short_qty'])),6)}
    skew_start=None
    for row in reversed(samples):
        gross=f(row['long_qty'])+f(row['short_qty']);skew=abs(f(row['long_qty'])-f(row['short_qty']))/gross*100 if gross else 0
        if skew>=50:skew_start=row['ts']
        else:break
    return {'change_6h':change(6),'change_24h':change(24),'change_72h':change(72),'skew_over_50h':round((now_sec-skew_start)/3600,2) if skew_start is not None else 0.0,'history_hours':round((now_sec-samples[0]['ts'])/3600,2)}

def read_inventory_history(hours:float)->dict[str,list[dict]]:
    if not DB.exists():return {}
    cutoff=int((time.time()-hours*3600)*1000);out=defaultdict(list)
    with sqlite3.connect(DB) as conn:
        rows=conn.execute('select ts,state from strategy_status where ts>=? order by ts asc',(cutoff,)).fetchall()
    for ts,raw in rows:
        try:state=json.loads(raw)
        except Exception:continue
        sym=state.get('symbol')
        if sym:out[sym].append({'ts':f(ts)/1000,'long_qty':f(state.get('long_qty')),'short_qty':f(state.get('short_qty'))})
    return out

def load_json(path:Path)->dict:
    try:return json.loads(path.read_text(encoding='utf-8'))
    except:return {}

def build(hours:float)->dict:
    realized=summarize(read_rows(hours));current=status_overlay(load_json(STATUS));tp=load_json(TP_AUDIT).get('symbols',{});history=read_inventory_history(max(72,hours))
    symbols={}
    for sym in sorted(set(realized)|set(current)):
        r=realized.get(sym,{'close_fill_rows':0,'gross_close_pnl':0.0,'close_fee':0.0,'close_net_before_open_fee':0.0,'qty_closed':0.0,'close_fee_to_gross_pct':None,'by_side':{},'both_sides_have_close_fills':False})
        c=current.get(sym,{})
        symbols[sym]={'realized':r,'current':c,'tp_coverage':tp.get(sym,{}).get('coverage',{}),'harvest_coverage':harvest_coverage_metrics(r.get('close_net_before_open_fee'),c.get('unrealized_pnl')),'inventory_history':inventory_history_metrics(history.get(sym,[])),'readonly':True}
    return {'generated_at':int(time.time()),'window_hours':hours,'readonly':True,'coverage_note':'PnL CSV rows are close fills. close_net_before_open_fee subtracts close fee only; it excludes opening fee, funding and full open-to-TP linkage, so it is not complete round-trip net profit.','symbols':symbols}

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--hours',type=float,default=72);ap.add_argument('--json-out',type=Path,default=OUT);args=ap.parse_args()
    p=build(args.hours);args.json_out.write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"Oscillation harvest baseline window={args.hours}h (read-only)")
    for sym,x in p['symbols'].items():
        r=x['realized'];c=x['current'];cov=x['tp_coverage']
        print(f"{sym}: close_fill_rows={r['close_fill_rows']} sides={r['by_side']} close_net_before_open_fee={r['close_net_before_open_fee']} close_fee/gross={r['close_fee_to_gross_pct']}% upnl={c.get('unrealized_pnl')} skew={c.get('imbalance_pct')}% margin={c.get('margin_load_pct')}% TP=L{(cov.get('long') or {}).get('coverage_pct')}/S{(cov.get('short') or {}).get('coverage_pct')}")
    return 0
if __name__=='__main__':raise SystemExit(main())
