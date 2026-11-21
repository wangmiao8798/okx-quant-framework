#!/usr/bin/env python3
"""每日测试套件巡检：全量 pytest，失败时邮件告警并落盘日志。

由 okx-test-suite.timer 每日调度。SMTP 口令来自
/etc/okx-quant/notifications.env（经 systemd EnvironmentFile 注入）。
"""
import datetime
import os
import subprocess
import sys

ROOT = "/opt/okx_quant"
LOG = os.path.join(ROOT, "logs", "test_suite_ci.log")
TIMEOUT = 1800  # 30 分钟


def append_log(text):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def main():
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        proc = subprocess.run(
            [os.path.join(ROOT, "venv", "bin", "python"), "-m", "pytest",
             "tests/", "-q", "--tb=no"],
            capture_output=True, text=True, timeout=TIMEOUT, cwd=ROOT)
        code = proc.returncode
        out = proc.stdout or ""
    except subprocess.TimeoutExpired:
        code = 124
        out = f"TIMEOUT after {TIMEOUT}s"
    except Exception as e:
        code = 125
        out = f"runner error: {e}"

    lines = [x for x in out.strip().splitlines() if x.strip()]
    summary = lines[-1] if lines else "(no output)"
    append_log(f"[{stamp}] exit={code} {summary}")

    if code != 0:
        append_log("---- 失败详情(尾部) ----")
        append_log(out[-2000:])
        try:
            sys.path.insert(0, ROOT)
            from utils.notifier import Notifier
            Notifier().send_email(
                "[量化] 每日测试套件失败",
                f"pytest 退出码 {code}\n{summary}\n\n"
                f"详情见服务器 {LOG}",
            )
        except Exception as e:
            append_log(f"[{stamp}] notify failed: {e}")
    sys.exit(code)


if __name__ == "__main__":
    main()
