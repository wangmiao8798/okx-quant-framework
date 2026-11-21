#!/usr/bin/env python3
"""Read-only FIFO round-trip accounting from OKX fills and funding bills."""
from __future__ import annotations
import argparse,json,statistics,sys,time
from collections import deque
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data'/'round_trip_accounting.json'
SYMBOLS=['CRV-USDT-SWAP','DRAM-USDT-SWAP','FARTCOIN-USDT-SWAP']

def num(v:Any)->float:
    try:return float(v or 0)
    except:return 0.0

def fee_cost(fill:dict)->float:
    # OKX fees are normally negative for costs and positive for rebates.
    return -num(fill.get('fee'))

def is_open(fill:dict)->bool:
    return (str(fill.get('posSide')).lower(),str(fill.get('side')).lower()) in {('long','buy'),('short','sell')}

def is_close(fill:dict)->bool:
    return (str(fill.get('posSide')).lower(),str(fill.get('side')).lower()) in {('long','sell'),('short','buy')}

def account_symbol(fills:list[dict],funding:float=0.0)->dict:
    seen=set(); rows=[]
    for x in sorted(fills or [],key=lambda r:(int(num(r.get('ts'))),str(r.get('tradeId') or r.get('billId') or ''))):
        key=str(x.get('tradeId') or x.get('billId') or f"{x.get('ordId')}:{x.get('ts')}:{x.get('fillSz')}:{x.get('fillPx')}")
        if key in seen:continue
        seen.add(key);rows.append(x)
    lots={'long':deque(),'short':deque()}; open_fee_alloc=close_fee=realized=matched=0.0; unmatched_close=0.0; close_rows=open_rows=0
    hold_hours=[];round_trip_nets=[];total_fee=sum(fee_cost(x) for x in rows)
    for x in rows:
        side=str(x.get('posSide') or '').lower();qty=abs(num(x.get('fillSz')));fee=fee_cost(x);ts=int(num(x.get('ts')))
        if qty<=0 or side not in lots:continue
        if is_open(x):
            open_rows+=1;lots[side].append({'qty':qty,'fee_per_qty':fee/qty,'ts':ts})
        elif is_close(x):
            close_rows+=1;close_fee+=fee;close_gross=num(x.get('fillPnl'));realized+=close_gross;remaining=qty
            while remaining>1e-12 and lots[side]:
                lot=lots[side][0];take=min(remaining,lot['qty']);open_fee=take*lot['fee_per_qty'];close_ratio=take/qty
                open_fee_alloc+=open_fee;matched+=take
                round_trip_nets.append(close_gross*close_ratio-open_fee-fee*close_ratio)
                if ts>0 and lot['ts']>0 and ts>=lot['ts']:hold_hours.append((ts-lot['ts'])/3600000)
                lot['qty']-=take;remaining-=take
                if lot['qty']<=1e-12:lots[side].popleft()
            unmatched_close+=remaining
    unmatched={side:round(sum(x['qty'] for x in q),8) for side,q in lots.items()}
    pre=realized-open_fee_alloc-close_fee;complete=pre+funding
    return {'fill_rows':len(rows),'open_fill_rows':open_rows,'close_fill_rows':close_rows,'close_open_ratio':round(close_rows/max(open_rows,1),8),'matched_round_segments':len(round_trip_nets),'matched_close_qty':round(matched,8),'unmatched_close_qty':round(unmatched_close,8),'unmatched_open_qty':unmatched,'average_holding_hours':round(sum(hold_hours)/len(hold_hours),8) if hold_hours else None,'median_holding_hours':round(statistics.median(hold_hours),8) if hold_hours else None,'average_round_trip_net':round(sum(round_trip_nets)/len(round_trip_nets),8) if round_trip_nets else None,'realized_pnl_before_fees':round(realized,8),'total_fill_fee_cost':round(total_fee,8),'open_fee_allocated':round(open_fee_alloc,8),'close_fee':round(close_fee,8),'round_trip_net_before_funding':round(pre,8),'funding_net':round(funding,8),'complete_net_after_fees_and_funding':round(complete,8),'accounting_complete':bool(rows) and unmatched_close<=1e-8}

def make_client():
    sys.path[:0]=[str(ROOT),str(ROOT/'web')]
    import yaml
    from api.crypto_utils import CredentialManager
    from api.okx_client import OKXClient
    from config_manager import load_api_password_secure
    cfg=yaml.safe_load((ROOT/'config/config.yaml').read_text()) or {}; cm=CredentialManager(cfg); cm.set_password(load_api_password_secure())
    return OKXClient(cm.load(),flag=str(cfg.get('exchange',{}).get('okx',{}).get('flag','0')))

def fetch_fills(client,symbol,begin_ms:int,max_pages:int=50)->tuple[list[dict],bool]:
    rows=[];before='';end_ms=int(time.time()*1000);complete=False
    for _ in range(max_pages):
        kwargs={'instType':'SWAP','instId':symbol,'begin':str(begin_ms),'end':str(end_ms),'limit':'100'}
        if before:kwargs['before']=before
        resp=client._call(client._trade.get_fills,**kwargs)
        if str(resp.get('code'))!='0':raise RuntimeError(f"fills failed {symbol}: {resp.get('code')} {resp.get('msg')}")
        page=resp.get('data',[]) or []
        if not page:complete=True;break
        rows.extend(page);oldest=min(int(num(x.get('ts'))) for x in page)
        if oldest<=begin_ms or len(page)<100:complete=True;break
        nxt=str(page[-1].get('tradeId') or page[-1].get('billId') or '')
        if not nxt or nxt==before:break
        before=nxt;time.sleep(0.25)
    return [x for x in rows if int(num(x.get('ts')))>=begin_ms],complete

def fetch_funding(client,symbol,begin_ms:int)->float:
    # type=8 is funding fee in OKX account bills. Positive pnl is received, negative paid.
    # Installed SDK exposes cursor pagination, not begin/end, so filter returned rows by ts.
    resp=client._call(client._account.get_account_bills,instType='SWAP',type='8',limit='100')
    if str(resp.get('code'))!='0':raise RuntimeError(f"funding bills failed {symbol}: {resp.get('code')} {resp.get('msg')}")
    return sum(num(x.get('pnl') or x.get('balChg')) for x in (resp.get('data') or []) if x.get('instId')==symbol and int(num(x.get('ts')))>=begin_ms)

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--hours',type=float,default=72);ap.add_argument('--json-out',type=Path,default=OUT);a=ap.parse_args();begin=int((time.time()-a.hours*3600)*1000);client=make_client();symbols={}
    for sym in SYMBOLS:
        fills,fill_data_complete=fetch_fills(client,sym,begin);funding=fetch_funding(client,sym,begin);symbols[sym]=account_symbol(fills,funding);symbols[sym]['fill_data_complete']=fill_data_complete
    payload={'generated_at':int(time.time()),'window_hours':a.hours,'readonly':True,'method':'FIFO within requested window; holding time and per-round net use matched FIFO segments; unmatched_close_qty means opening fills predate window','symbols':symbols};a.json_out.write_text(json.dumps(payload,ensure_ascii=False,indent=2));print(json.dumps(payload,ensure_ascii=False,indent=2));return 0
if __name__=='__main__':raise SystemExit(main())
