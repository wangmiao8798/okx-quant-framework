"""SQLite 数据存储：交易记录 + 权益快照"""
import hashlib, hmac, sqlite3, json, threading, os, time
from typing import List, Dict, Optional


class Database:
    def __init__(self, db_path: str = "data/trading.db"):
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=15.0, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER, strategy TEXT, side TEXT,
                    symbol TEXT, price REAL, quantity REAL,
                    cost REAL, pnl REAL, fee REAL,
                    order_id TEXT,
                    fill_id TEXT,
                    pos_side TEXT,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    fill_type TEXT NOT NULL DEFAULT ''
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS equity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER, equity REAL,
                    usdt REAL, btc REAL, btc_price REAL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER, strategy TEXT, state TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT, symbol TEXT, pid INTEGER,
                    status TEXT DEFAULT "running",
                    start_ts INTEGER, heartbeat_ts INTEGER,
                    config TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)")
            trade_columns = {row[1] for row in cur.execute("PRAGMA table_info(trades)")}
            trade_migrations = {
                "fill_id": "TEXT",
                "pos_side": "TEXT",
                "reduce_only": "INTEGER NOT NULL DEFAULT 0",
                "fill_type": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in trade_migrations.items():
                if column not in trade_columns:
                    cur.execute(f"ALTER TABLE trades ADD COLUMN {column} {definition}")
            # One exchange order can have multiple partial fills. Idempotency
            # belongs on fill_id, never on order_id.
            cur.execute("DROP INDEX IF EXISTS idx_trades_order_id_unique")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_fill_id_unique "
                "ON trades(fill_id) WHERE fill_id IS NOT NULL AND fill_id != ''"
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts)")
            cur.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    email TEXT,
                    created_ts INTEGER,
                    last_login_ts INTEGER
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS user_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    exchange TEXT DEFAULT "okx",
                    api_key_enc TEXT NOT NULL,
                    api_secret_enc TEXT NOT NULL,
                    passphrase_enc TEXT NOT NULL,
                    alias TEXT,
                    created_ts INTEGER,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS user_strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    api_key_id INTEGER NOT NULL,
                    name TEXT,
                    symbol TEXT NOT NULL,
                    leverage INTEGER DEFAULT 3,
                    base_position_usdt REAL DEFAULT 5,
                    max_add_layers INTEGER DEFAULT 6,
                    add_step REAL DEFAULT 0.003,
                    add_multiplier REAL DEFAULT 1.25,
                    first_tp_ratio REAL DEFAULT 0.0065,
                    final_tp_ratio REAL DEFAULT 0.013,
                    max_margin_ratio REAL DEFAULT 0.5,
                    capital_budget_usdt REAL DEFAULT 0,
                    max_daily_loss REAL DEFAULT 20,
                    max_total_loss REAL DEFAULT 50,
                    max_drawdown REAL DEFAULT 15,
                    paper_mode INTEGER DEFAULT 1,
                    status TEXT DEFAULT "stopped",
                    pid INTEGER,
                    created_ts INTEGER,
                    started_ts INTEGER,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(api_key_id) REFERENCES user_api_keys(id)
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    title TEXT,
                    content TEXT,
                    strategy_id INTEGER DEFAULT 0,
                    is_read INTEGER DEFAULT 0
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL,
                    expires_ts INTEGER NOT NULL,
                    revoked_ts INTEGER,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            ''')
            cur.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_ts)")

            user_columns = {row[1] for row in cur.execute("PRAGMA table_info(users)")}
            if "is_disabled" not in user_columns:
                cur.execute("ALTER TABLE users ADD COLUMN is_disabled INTEGER DEFAULT 0")

            strategy_columns = {row[1] for row in cur.execute("PRAGMA table_info(user_strategies)")}
            migrations = {
                "paper_mode": "INTEGER DEFAULT 1",
                "max_daily_loss": "REAL DEFAULT 20",
                "max_total_loss": "REAL DEFAULT 50",
                "max_drawdown": "REAL DEFAULT 15",
                "capital_budget_usdt": "REAL DEFAULT 0",
                "capital_budget_usdt": "REAL DEFAULT 0",
            }
            for column, definition in migrations.items():
                if column not in strategy_columns:
                    cur.execute(f"ALTER TABLE user_strategies ADD COLUMN {column} {definition}")
            self._conn.commit()
        print(f"Database ready")

    def add_trade(self, ts: int, strategy: str, side: str, symbol: str,
                  price: float, quantity: float, cost: float = 0.0,
                  pnl: float = 0.0, fee: float = 0.0, order_id: str = "",
                  fill_id: str = "", pos_side: str = "",
                  reduce_only: bool = False, fill_type: str = ""):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO trades("
                "ts,strategy,side,symbol,price,quantity,cost,pnl,fee,order_id,"
                "fill_id,pos_side,reduce_only,fill_type) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, strategy, side, symbol, price, quantity, cost, pnl, fee,
                    order_id, fill_id, pos_side, int(bool(reduce_only)), fill_type,
                )
            )
            self._conn.commit()

    def add_equity(self, ts: int, equity: float,
                   usdt: float = 0, btc: float = 0, btc_price: float = 0):
        with self._lock:
            self._conn.execute(
                "INSERT INTO equity(ts,equity,usdt,btc,btc_price) VALUES(?,?,?,?,?)",
                (ts, equity, usdt, btc, btc_price)
            )
            self._conn.commit()

    def add_strategy_state(self, ts: int, strategy: str, state: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO strategy_status(ts,strategy,state) VALUES(?,?,?)",
                (ts, strategy, json.dumps(state))
            )
            self._conn.commit()

    def get_recent_trades(self, limit: int = 30) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        cols = [
            "id", "ts", "strategy", "side", "symbol", "price", "quantity",
            "cost", "pnl", "fee", "order_id", "fill_id", "pos_side",
            "reduce_only", "fill_type",
        ]
        return [dict(zip(cols, r)) for r in rows]

    def get_equity_curve(self, since_ts: int = 0) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT ts,equity FROM equity WHERE ts > ? ORDER BY ts", (since_ts,)
        ).fetchall()
        return [{"ts": r[0], "equity": r[1]} for r in rows]

    def get_trade_summary(self) -> Dict:
        total = self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        wins = self._conn.execute("SELECT COUNT(*) FROM trades WHERE pnl > 0").fetchone()[0]
        gross_pnl = self._conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades").fetchone()[0]
        return {
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "total_pnl": round(gross_pnl, 2),
        }

    def save_state(self, strategy: str, state: dict):
        import json
        with self._lock:
            self._conn.execute(
                "INSERT INTO strategy_status(ts,strategy,state) VALUES(?,?,?)",
                (int(time.time()*1000), strategy, json.dumps(state))
            )
            self._conn.commit()

    def load_state(self, strategy: str) -> dict:
        row = self._conn.execute(
            "SELECT state FROM strategy_status WHERE strategy=? ORDER BY ts DESC LIMIT 1",
            (strategy,)
        ).fetchone()
        import json
        return json.loads(row[0]) if row else {}

    def start_run(self, strategy: str, symbol: str, pid: int, config: str = "") -> int:
        pid = int(pid)
        """创建运行记录, 返回run_id"""
        ts = int(time.time() * 1000)
        cur = self._conn.execute(
            "INSERT INTO strategy_runs(strategy,symbol,pid,status,start_ts,heartbeat_ts,config) "
            "VALUES(?,?,?,'running',?,?,?)",
            (strategy, symbol, pid, ts, ts, config)
        )
        self._conn.commit()
        return cur.lastrowid

    def stop_run(self, run_id: int):
        """停止运行记录"""
        self._conn.execute(
            "UPDATE strategy_runs SET status='stopped',heartbeat_ts=? WHERE id=?",
            (int(time.time() * 1000), run_id)
        )
        self._conn.commit()

    def update_heartbeat(self, run_id: int):
        """更新心跳"""
        self._conn.execute(
            "UPDATE strategy_runs SET heartbeat_ts=? WHERE id=?",
            (int(time.time() * 1000), run_id)
        )
        self._conn.commit()

    def get_active_runs(self, strategy: str = "") -> list:
        """获取正在运行的实例"""
        if strategy:
            rows = self._conn.execute(
                "SELECT * FROM strategy_runs WHERE strategy=? AND status='running'", (strategy,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM strategy_runs WHERE status='running'"
            ).fetchall()
        cols = ["id","strategy","symbol","pid","status","start_ts","heartbeat_ts","config"]
        return [dict(zip(cols, r)) for r in rows]

    def stop_all_runs(self, strategy: str = ""):
        """停止所有运行实例"""
        ts = int(time.time() * 1000)
        if strategy:
            self._conn.execute(
                "UPDATE strategy_runs SET status='stopped',heartbeat_ts=? WHERE strategy=? AND status='running'",
                (ts, strategy)
            )
        else:
            self._conn.execute(
                "UPDATE strategy_runs SET status='stopped',heartbeat_ts=? WHERE status='running'",
                (ts,)
            )
        self._conn.commit()

    def cleanup_orphan_runs(self, strategy: str = "") -> int:
        """清理数据库中已经死亡但仍标记为 running 的运行记录"""
        rows = self.get_active_runs(strategy)
        cleaned = 0
        for r in rows:
            pid = r.get("pid")
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if not alive:
                self.stop_run(r["id"])
                cleaned += 1
        return cleaned


    def _hash_password(self, password: str) -> str:
        iterations = 600000
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"

    def _verify_password(self, password: str, stored_hash: str) -> tuple[bool, bool]:
        if stored_hash.startswith("pbkdf2_sha256$"):
            try:
                _name, iterations, salt, expected = stored_hash.split("$", 3)
                actual = hashlib.pbkdf2_hmac(
                    "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
                ).hex()
                return hmac.compare_digest(actual, expected), False
            except (TypeError, ValueError):
                return False, False
        legacy = hashlib.sha256(password.encode()).hexdigest()
        valid = hmac.compare_digest(legacy, stored_hash)
        return valid, valid

    def seed_admin(self):
        with self._lock:
            row = self._conn.execute("SELECT id FROM users WHERE username=?", ("admin",)).fetchone()
            if row:
                return
            import time
            self._conn.execute(
                "INSERT INTO users(username, password_hash, email, created_ts) VALUES(?,?,?,?)",
                ("admin", self._hash_password("q98056q"), "admin@local", int(time.time()))
            )
            self._conn.commit()

    def verify_user(self, username: str, password: str) -> dict:
        self.seed_admin()
        login_id = (username or "").strip().lower()
        row = self._conn.execute(
            "SELECT id, username, password_hash, COALESCE(is_disabled,0) FROM users WHERE lower(username)=? OR lower(COALESCE(email, ''))=?",
            (login_id, login_id),
        ).fetchone()
        if not row:
            return {"ok": False, "msg": "user not found"}
        if row[3]:
            return {"ok": False, "msg": "user disabled"}
        password_ok, needs_upgrade = self._verify_password(password, row[2])
        if password_ok:
            if needs_upgrade:
                self._conn.execute(
                    "UPDATE users SET password_hash=?, last_login_ts=? WHERE id=?",
                    (self._hash_password(password), int(time.time()), row[0]),
                )
            else:
                self._conn.execute("UPDATE users SET last_login_ts=? WHERE id=?", (int(time.time()), row[0]))
            self._conn.commit()
            return {"ok": True, "user_id": row[0], "username": row[1]}
        return {"ok": False, "msg": "wrong password"}

    def register_user(self, username: str, password: str, email: str = "") -> dict:
        self.seed_admin()
        username = (username or "").strip().lower()
        email = (email or username).strip().lower()
        try:
            import time
            existing = self._conn.execute(
                "SELECT id FROM users WHERE lower(username)=? OR lower(COALESCE(email, ''))=?",
                (username, email),
            ).fetchone()
            if existing:
                return {"ok": False, "msg": "email already registered"}
            self._conn.execute(
                "INSERT INTO users(username, password_hash, email, created_ts) VALUES(?,?,?,?)",
                (username, self._hash_password(password), email, int(time.time()))
            )
            self._conn.commit()
            return {"ok": True, "msg": "registered"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def update_user_password(self, username: str, password: str) -> dict:
        self.seed_admin()
        try:
            row = self._conn.execute(
                "SELECT id FROM users WHERE username=?",
                (username,)
            ).fetchone()
            if not row:
                return {"ok": False, "msg": "user not found"}
            self._conn.execute(
                "UPDATE users SET password_hash=?, last_login_ts=? WHERE username=?",
                (self._hash_password(password), int(time.time()), username)
            )
            self._conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_user(self, username: str) -> dict:
        row = self._conn.execute(
            "SELECT id, username, email, COALESCE(is_disabled,0) FROM users WHERE username=?", (username,)
        ).fetchone()
        if not row:
            return {"ok": False}
        return {"ok": True, "user_id": row[0], "username": row[1], "email": row[2], "is_disabled": bool(row[3])}

    def create_auth_session(self, token_hash: str, user_id: int, expires_ts: int) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute("DELETE FROM auth_sessions WHERE expires_ts < ?", (now,))
            self._conn.execute(
                "INSERT INTO auth_sessions(token_hash,user_id,created_ts,expires_ts) VALUES(?,?,?,?)",
                (token_hash, user_id, now, expires_ts),
            )
            self._conn.commit()

    def get_auth_session(self, token_hash: str) -> dict:
        row = self._conn.execute(
            """
            SELECT s.user_id, u.username, s.expires_ts, COALESCE(u.is_disabled,0)
            FROM auth_sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.revoked_ts IS NULL
            """,
            (token_hash,),
        ).fetchone()
        if not row:
            return {}
        return {
            "user_id": row[0],
            "username": row[1],
            "expires_ts": row[2],
            "is_disabled": bool(row[3]),
        }

    def revoke_auth_session(self, token_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE auth_sessions SET revoked_ts=? WHERE token_hash=? AND revoked_ts IS NULL",
                (int(time.time()), token_hash),
            )
            self._conn.commit()

    def revoke_user_sessions(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE auth_sessions SET revoked_ts=? WHERE user_id=? AND revoked_ts IS NULL",
                (int(time.time()), user_id),
            )
            self._conn.commit()

    def get_user_strategies(self, user_id: int) -> list:
        rows = self._conn.execute(
            "SELECT * FROM user_strategies WHERE user_id=?", (user_id,)
        ).fetchall()
        col_names = [info[1] for info in self._conn.execute("PRAGMA table_info(user_strategies)").fetchall()]
        items = [dict(zip(col_names, r)) for r in rows]
        for item in items:
            item["paper_mode"] = bool(item.get("paper_mode", 1))
        return items

    def create_strategy(self, user_id: int, api_key_id: int, symbol: str,
                        name: str = "", leverage: int = 3, base_position_usdt: float = 5,
                        max_add_layers: int = 6, add_step: float = 0.003,
                        add_multiplier: float = 1.25, first_tp_ratio: float = 0.0065,
                        final_tp_ratio: float = 0.013, max_margin_ratio: float = 0.5,
                        capital_budget_usdt: float = 0.0,
                        max_daily_loss: float = 20.0, max_total_loss: float = 50.0,
                        max_drawdown: float = 15.0) -> dict:
        try:
            import time
            ts = int(time.time())
            cur = self._conn.execute(
                "INSERT INTO user_strategies(user_id, api_key_id, name, symbol, leverage, "
                "base_position_usdt, max_add_layers, add_step, add_multiplier, "
                "first_tp_ratio, final_tp_ratio, max_margin_ratio, capital_budget_usdt, max_daily_loss, "
                "max_total_loss, max_drawdown, paper_mode, status, created_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'stopped',?)",
                (user_id, api_key_id, name, symbol, leverage, base_position_usdt,
                 max_add_layers, add_step, add_multiplier, first_tp_ratio, final_tp_ratio,
                 max_margin_ratio, capital_budget_usdt, max_daily_loss, max_total_loss, max_drawdown, ts)
            )
            self._conn.commit()
            return {"ok": True, "strategy_id": cur.lastrowid}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def update_strategy_status(self, strategy_id: int, status: str):
        self._conn.execute(
            "UPDATE user_strategies SET status=? WHERE id=?", (status, strategy_id)
        )
        self._conn.commit()

    def get_user_api_keys(self, user_id: int) -> list:
        rows = self._conn.execute(
            "SELECT id, exchange, alias, created_ts FROM user_api_keys WHERE user_id=?", (user_id,)
        ).fetchall()
        cols = ["id","exchange","alias","created_ts"]
        return [dict(zip(cols, r)) for r in rows]

    def create_api_key(self, user_id: int, exchange: str, api_key: str,
                       api_secret: str, passphrase: str, alias: str = "") -> dict:
        try:
            import time
            from utils.crypto import UserCrypto
            cur = self._conn.execute(
                "INSERT INTO user_api_keys(user_id, exchange, api_key_enc, api_secret_enc, "
                "passphrase_enc, alias, created_ts) VALUES(?,?,?,?,?,?,?)",
                (user_id, exchange, UserCrypto.encrypt(api_key), UserCrypto.encrypt(api_secret), UserCrypto.encrypt(passphrase), alias, int(time.time()))
            )
            self._conn.commit()
            return {"ok": True, "api_key_id": cur.lastrowid}
        except Exception as e:
            return {"ok": False, "msg": str(e)}


    def save_api_key(self, user_id: int, api_key: str, api_secret: str, passphrase: str, alias: str = "") -> dict:
        result = self.create_api_key(user_id, "okx", api_key, api_secret, passphrase, alias)
        if result.get("ok"):
            result["key_id"] = result.get("api_key_id")
        return result

    def get_user_api_key_data(self, key_id: int) -> dict:
        rows = self._conn.execute(
            "SELECT api_key_enc, api_secret_enc, passphrase_enc FROM user_api_keys WHERE id=?",
            (key_id,)
        ).fetchone()
        if not rows:
            return {"ok": False, "msg": "key not found"}
        try:
            from utils.crypto import UserCrypto
            return {"ok": True,
                    "api_key": UserCrypto.decrypt(rows[0]),
                    "api_secret": UserCrypto.decrypt(rows[1]),
                    "passphrase": UserCrypto.decrypt(rows[2])}
        except Exception as e:
            return {"ok": False, "msg": f"decrypt failed: {e}"}

    def list_users_admin(self) -> list:
        rows = self._conn.execute("""
            SELECT
                u.id, u.username, u.email, u.created_ts, u.last_login_ts, COALESCE(u.is_disabled,0),
                (SELECT COUNT(*) FROM user_api_keys k WHERE k.user_id=u.id),
                (SELECT COUNT(*) FROM user_strategies s WHERE s.user_id=u.id)
            FROM users u
            ORDER BY u.id DESC
        """).fetchall()
        cols = ["id","username","email","created_ts","last_login_ts","is_disabled","api_key_count","strategy_count"]
        return [dict(zip(cols, r)) for r in rows]

    def set_user_disabled(self, user_id: int, disabled: bool) -> dict:
        row = self._conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return {"ok": False, "msg": "user not found"}
        if row[0] == "admin" and disabled:
            return {"ok": False, "msg": "admin cannot be disabled"}
        self._conn.execute("UPDATE users SET is_disabled=? WHERE id=?", (1 if disabled else 0, user_id))
        self._conn.commit()
        return {"ok": True}

    def close(self):
        self._conn.close()
