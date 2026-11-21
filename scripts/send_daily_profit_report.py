#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from scripts.daily_profit_report import build_report
from scripts.oscillation_harvest_report import build as build_harvest_report, OUT as HARVEST_OUT
from utils.notifier import Notifier


def refresh_harvest_report(builder=build_harvest_report, output=HARVEST_OUT):
    payload = builder(72)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def main():
    parser = argparse.ArgumentParser(description='Send OKX quant daily profit report by email')
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--config', default=str(PROJECT / 'config' / 'config.yaml'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    days = max(1, args.days)
    refresh_harvest_report()
    report = build_report(days)
    report_day = (dt.datetime.now().date() - dt.timedelta(days=1)).isoformat()
    subject = f"[量化日报] {report_day}完整日收益与当前风险"

    if args.dry_run:
        print(subject)
        print(report)
        return 0

    ok = Notifier(args.config).send_email(subject, report)
    print('email_sent=1' if ok else 'email_sent=0')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
