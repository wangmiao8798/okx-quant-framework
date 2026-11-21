#!/usr/bin/env python3
"""Read-only realized cashflow report from OKX account bills."""
from __future__ import annotations
import argparse,json,sys,time
from collections import defaultdict
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data'/'bill_cashflow_report.json'
SYMBOLS=['CRV-USDT-SWAP','DRAM-USDT-SWAP','FARTCOIN-USDT-SWAP']

def n(v:Any)->float:
    try:return float(v or 0)
    except:return 0.0

def summarize_bills(bills:list[dict])->dict:
    out=defaultdict(lambda:{'bill_rows':0,'trading_pnl':0.0,'net_fee_cost':0.0,'trading_cashflow':0.0,'funding_cashflow':0.0,'risk_event_cashflow':0.0,'risk_event_rows':0,'other_cashflow':0.0,'total_realized_cashflow':0.0})
    seen=set()
    for b in bills or []:
        bid=str(b.get('billId') or '').strip()
        key=bid or tuple(str(b.get(k) or '') for k in ('instId','type','subType','ts','balChg','pnl','fee','sz','px','ordId'))
        if key in seen:continue
        seen.add(key);sym=b.get('instId') or 'account';r=out[sym];r['bill_rows']+=1;typ=str(b.get('type') or '');bal=n(b.get('balChg'));pnl=n(b.get('pnl'));fee=n(b.get('fee'))
        r['total_realized_cashflow']+=bal
        if typ=='2':
            r['trading_pnl']+=pnl;r['net_fee_cost']+=-fee;r['trading_cashflow']+=bal
        elif typ=='8':r['funding_cashflow']+=bal
        elif typ in {'5','9'}:
            r['risk_event_cashflow']+=bal;r['risk_event_rows']+=1
        else:r['other_cashflow']+=bal
    return {sym:{k:(round(v,8) if isinstance(v,float) else v) for k,v in r.items()} for sym,r in out.items()}

def make_client():
    sys.path[:0]=[str(ROOT),str(ROOT/'web')]
    import yaml
    from api.crypto_utils import CredentialManager
    from api.okx_client import OKXClient
    from config_manager import load_api_password_secure
    cfg=yaml.safe_load((ROOT/'config/config.yaml').read_text()) or {};cm=CredentialManager(cfg);cm.set_password(load_api_password_secure())
    return OKXClient(cm.load(),flag=str(cfg.get('exchange',{}).get('okx',{}).get('flag','0')))

def fetch_bills(client,begin_ms:int,max_pages:int=30)->list[dict]:
    rows=[];after=''
    for _ in range(max_pages):
        resp={}
        for attempt in range(4):
            resp=client._call(client._account.get_account_bills,instType='SWAP',after=after,limit='100')
            if str(resp.get('code'))=='0':break
            if str(resp.get('code'))=='50011':
                time.sleep(1.2*(attempt+1));continue
            raise RuntimeError(f"bills failed {resp.get('code')} {resp.get('msg')}")
        if str(resp.get('code'))!='0':raise RuntimeError(f"bills failed after retries {resp.get('code')} {resp.get('msg')}")
        page=resp.get('data',[]) or []
        if not page:break
        rows.extend(page);oldest=min(int(n(x.get('ts'))) for x in page)
        if oldest<begin_ms:break
        nxt=str(page[-1].get('billId') or '')
        if not nxt or nxt==after:break
        after=nxt
        time.sleep(0.35)
    filtered=[x for x in rows if int(n(x.get('ts')))>=begin_ms and (x.get('instId') in SYMBOLS)]
    complete=bool(rows) and min(int(n(x.get('ts'))) for x in rows)<begin_ms
    return filtered,complete

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--hours',type=float,default=72);ap.add_argument('--json-out',type=Path,default=OUT);a=ap.parse_args();begin=int((time.time()-a.hours*3600)*1000);rows,data_complete=fetch_bills(make_client(),begin);by=summarize_bills(rows);payload={'generated_at':int(time.time()),'window_hours':a.hours,'readonly':True,'source':'OKX account bills balChg; total_realized_cashflow includes every returned bill type; unrealized PnL and account-level bills without tracked instId are outside per-symbol totals','data_complete':data_complete,'symbols':{s:by.get(s,{'bill_rows':0,'trading_pnl':0.0,'net_fee_cost':0.0,'trading_cashflow':0.0,'funding_cashflow':0.0,'risk_event_cashflow':0.0,'risk_event_rows':0,'other_cashflow':0.0,'total_realized_cashflow':0.0}) for s in SYMBOLS}};a.json_out.write_text(json.dumps(payload,ensure_ascii=False,indent=2));print(json.dumps(payload,ensure_ascii=False,indent=2));return 0
if __name__=='__main__':raise SystemExit(main())
