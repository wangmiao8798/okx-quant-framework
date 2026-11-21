"""
进程管理器 —— 多租户策略进程启停管控
启动/停止/监控每个用户的独立策略子进程
"""
import os, sys, signal, time, subprocess, json, base64
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

class ProcessManager:
    def __init__(self):
        self._log_dir = PROJ / "logs" / "users"
        self._data_dir = PROJ / "data" / "users"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def start(self, strategy_id: int, user_id: int, db, api_key_enc: str,
              api_secret_enc: str, passphrase_enc: str, config: dict) -> dict:
        """启动用户策略子进程"""
        try:
            db.cleanup_orphan_runs("user_strategy")
        except Exception:
            pass
        # 创建独立的运行目录
        user_dir = self._data_dir / f"u{user_id}_s{strategy_id}"
        user_dir.mkdir(parents=True, exist_ok=True)

        # 检查是否已在运行
        pid_file = user_dir / "run.pid"
        if pid_file.exists():
            try:
                old_pid = int(pid_file.read_text().strip())
                os.kill(old_pid, 0)  # 进程还在
                return {"ok": False, "msg": f"策略已在运行 (PID {old_pid})"}
            except (OSError, ValueError):
                pid_file.unlink(missing_ok=True)

        # 写入策略配置JSON。客户策略按创建配置运行；客户平台当前默认实盘。
        config = dict(config or {})
        config["paper_mode"] = bool(config.get("paper_mode", False))
        cfg_file = user_dir / "config.json"
        with open(cfg_file, "w") as f:
            json.dump(config, f)

        key_file = user_dir / "keys.json"
        if key_file.exists():
            key_file.unlink(missing_ok=True)
        key_payload = json.dumps({
            "api_key": api_key_enc,
            "api_secret": api_secret_enc,
            "passphrase": passphrase_enc
        }).encode()
        child_env = os.environ.copy()
        child_env["OKX_USER_API_KEY_B64"] = base64.b64encode(key_payload).decode()

        # 启动子进程
        runner = str(PROJ / "run_user_strategy.py")
        log_file = self._log_dir / f"u{user_id}_s{strategy_id}.log"
        cmd = [
            sys.executable, runner,
            "--strategy-id", str(strategy_id),
            "--user-dir", str(user_dir),
            "--log-file", str(log_file)
        ]

        try:
            with open(log_file, "w") as lf:
                proc = subprocess.Popen(
                    cmd, stdout=lf, stderr=lf,
                    cwd=str(PROJ),
                    close_fds=True,
                    env=child_env
                )
            pid_file.write_text(str(proc.pid))

            # 更新数据库策略状态
            db.update_strategy_status(strategy_id, "running")
            ts = int(time.time())
            run_id = db.start_run(f"user_strategy", str(config.get("symbol", "?")),
                                  proc.pid, json.dumps({"strategy_id": strategy_id, "user_id": user_id}))
            return {"ok": True, "pid": proc.pid, "run_id": run_id}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def stop(self, strategy_id: int, db) -> dict:
        """停止用户策略子进程"""
        try:
            db.cleanup_orphan_runs("user_strategy")
        except Exception:
            pass
        # 从数据库查找PID
        runs = db.get_active_runs("user_strategy")
        for r in runs:
            run_cfg = json.loads(r.get("config", "{}"))
            if run_cfg.get("strategy_id") == strategy_id:
                pid = r.get("pid")
                if pid:
                    try:
                        os.kill(pid, signal.SIGTERM)
                        # 等5秒后强制杀
                        for _ in range(5):
                            time.sleep(1)
                            try:
                                os.kill(pid, 0)
                            except OSError:
                                break
                        else:
                            os.kill(pid, signal.SIGKILL)
                    except:
                        pass
                db.stop_run(r["id"])
                db.update_strategy_status(strategy_id, "stopped")
                return {"ok": True, "msg": f"进程 {pid} 已停止"}
        return {"ok": False, "msg": "未找到运行中的策略"}

    def status(self, strategy_id: int, db) -> dict:
        """查询策略运行状态"""
        try:
            db.cleanup_orphan_runs("user_strategy")
        except Exception:
            pass
        runs = db.get_active_runs("user_strategy")
        for r in runs:
            run_cfg = json.loads(r.get("config", "{}"))
            if run_cfg.get("strategy_id") == strategy_id:
                pid = r.get("pid")
                alive = False
                if pid:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except:
                        pass
                return {
                    "running": alive,
                    "pid": pid,
                    "start_ts": r.get("start_ts"),
                    "heartbeat_ts": r.get("heartbeat_ts"),
                    "config": run_cfg
                }
        return {"running": False, "pid": None}

    def list_user_strategies(self, user_id: int, db) -> list:
        """列出用户所有策略及运行状态"""
        try:
            db.cleanup_orphan_runs("user_strategy")
        except Exception:
            pass
        strategies = db.get_user_strategies(user_id)
        runs = {r["id"]: r for r in db.get_active_runs("user_strategy")}
        results = []
        for s in strategies:
            sid = s["id"]
            status = {"running": False, "pid": None}
            for rid, r in runs.items():
                rc = json.loads(r.get("config", "{}"))
                if rc.get("strategy_id") == sid:
                    status = {"running": True, "pid": r.get("pid")}
                    break
            s["runtime"] = status
            results.append(s)
        return results
