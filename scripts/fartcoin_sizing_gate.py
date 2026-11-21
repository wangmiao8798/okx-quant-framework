#!/usr/bin/env python3
import json
from pathlib import Path
import yaml

ROOT=Path('/opt/okx_quant')
SYMBOL='FARTCOIN-USDT-SWAP'


def evaluate_checks(report:dict,row:dict,monitor:dict,coverage:dict)->dict:
    required_monitor=('imbalance_pct','margin_load_pct','total_unrealized_pnl')
    monitor_complete=all(monitor.get(k) is not None for k in required_monitor)
    risk_present='risk_event_rows' in row and row.get('risk_event_rows') is not None
    net=row.get('total_realized_cashflow'); trading=row.get('trading_cashflow')
    fees=row.get('net_fee_cost'); gross=row.get('trading_pnl')
    fee_ratio=fees/abs(gross) if fees is not None and gross else 999
    cov_ok=all(isinstance(coverage.get(side),dict) and (coverage[side].get('coverage_pct') or 0)>=100 and coverage[side].get('status')=='covered' for side in ('long','short'))
    return {
      'data_complete':report.get('data_complete') is True,
      'minimum_14_day_window':float(report.get('window_hours') or 0)>=336,
      'monitor_complete':monitor_complete,
      'risk_event_data_present':risk_present,
      'total_realized_cashflow_positive':net is not None and net>0,
      'trading_cashflow_above_1usdt':trading is not None and trading>=1.0,
      'no_liquidation_or_adl':risk_present and int(row['risk_event_rows'])==0,
      'fee_to_pnl_below_10pct':fee_ratio<=0.10,
      'tp_coverage_100pct':cov_ok,
      'imbalance_below_25pct':monitor_complete and float(monitor['imbalance_pct'])<25,
      'margin_load_below_40pct':monitor_complete and float(monitor['margin_load_pct'])<40,
      'unrealized_loss_below_5usdt':monitor_complete and float(monitor['total_unrealized_pnl'])>-5,
    }


def main()->int:
    report=json.load(open(ROOT/'data/bill_cashflow_report.json'))
    status=json.load(open(ROOT/'data/strategies_status.json'))
    row=report['symbols'][SYMBOL]
    state=next(x for x in status['strategies'].values() if x.get('symbol')==SYMBOL)
    monitor=state.get('monitor') or {}
    cfg=yaml.safe_load(open(ROOT/'config/config_fartcoin.yaml'))['futures']
    coverage=json.load(open(ROOT/'data/tp_coverage_audit.json'))['symbols'][SYMBOL]['coverage']
    checks=evaluate_checks(report,row,monitor,coverage)
    fees=row.get('net_fee_cost');gross=row.get('trading_pnl');fee_ratio=fees/abs(gross) if fees is not None and gross else 999
    result={'symbol':SYMBOL,'window_hours':report.get('window_hours'),'current_base_position_usdt':cfg['base_position_usdt'],'candidate_base_position_usdt':round(cfg['base_position_usdt']*1.1,2),'total_realized_cashflow':row.get('total_realized_cashflow'),'trading_cashflow':row.get('trading_cashflow'),'bill_rows':row.get('bill_rows'),'data_complete':report.get('data_complete'),'fee_to_pnl_pct':round(fee_ratio*100,3),'imbalance_pct':monitor.get('imbalance_pct'),'margin_load_pct':monitor.get('margin_load_pct'),'unrealized_pnl':monitor.get('total_unrealized_pnl'),'checks':checks,'eligible':all(checks.values()),'note':'Observation-only sizing gate; never auto-edits config. Live expansion requires at least 14 days of complete data.'}
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
