from __future__ import annotations
import argparse, json, time, uuid
from pathlib import Path
from one_way.market.adapter import OkxPublicMarket
from one_way.signal.scoring import score_candidates
from one_way.state.store import OneWayShadowStore

DEFAULT_DB = "/opt/okx_quant/data/one_way/one_way_shadow.db"
def run(max_symbols: int, db_path: str):
    api=OkxPublicMarket(); symbols=api.universe(max_symbols=max_symbols)
    rows=[]; failures=[]; run_id=f"ow-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"; ts=int(time.time()*1000)
    for symbol in symbols:
        try:
            decisions=score_candidates(api.snapshot(symbol))
            for direction,d in decisions.items(): rows.append({"symbol":symbol,"decision":d})
        except Exception as exc:
            failures.append({"symbol":symbol,"error":type(exc).__name__,"message":str(exc)[:160]})
    eligible=[x for x in rows if x["decision"].eligible]
    selected=sorted(eligible,key=lambda x:x["decision"].score,reverse=True)[:1]
    chosen={x["decision"].signal_id for x in selected}
    store=OneWayShadowStore(db_path)
    try: store.save(run_id,ts,len(symbols),len(set(x["symbol"] for x in rows)),failures,rows,chosen)
    finally: store.close()
    return {"run_id":run_id,"mode":"read_only_shadow","execution_attempted":False,"universe_count":len(symbols),"scanned_count":len(set(x["symbol"] for x in rows)),"failure_count":len(failures),"selected":[x["decision"].to_dict() for x in selected],"db_path":db_path}
if __name__ == "__main__":
    p=argparse.ArgumentParser(); p.add_argument("--symbols",type=int,default=8); p.add_argument("--db",default=DEFAULT_DB); a=p.parse_args(); print(json.dumps(run(a.symbols,a.db),ensure_ascii=False))
