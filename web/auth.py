# Web auth - multi-user
import hashlib, secrets, os, sys, time
from contextvars import ContextVar
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    from utils.database import Database
except ModuleNotFoundError:
    from database import Database

_recent_registers = {}
_request_token = ContextVar("request_token", default="")

def get_db():
    return Database(os.getenv("OKX_DB_PATH", "data/trading.db"))

def _token_hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()

def set_request_token(token):
    return _request_token.set(token or "")

def reset_request_token(marker):
    _request_token.reset(marker)

def _effective_token(token):
    return token or _request_token.get()

def _session_info(token):
    token = _effective_token(token)
    if not token:
        return {}
    db = get_db()
    info = db.get_auth_session(_token_hash(token))
    if info and (info["expires_ts"] < int(time.time()) or info["is_disabled"]):
        db.revoke_auth_session(_token_hash(token))
        info = {}
    db.close()
    return info

def login(username, password):
    username = (username or "").strip().lower()
    db = get_db()
    result = db.verify_user(username, password)
    if result["ok"]:
        token = secrets.token_hex(32)
        ttl = int(os.getenv("OKX_TOKEN_TTL_SECONDS", "86400"))
        db.create_auth_session(_token_hash(token), result["user_id"], int(time.time()) + ttl)
        db.close()
        return token
    db.close()
    return None

def check_token(token):
    return bool(_session_info(token))

def get_user_id(token):
    info = _session_info(token)
    return info["user_id"] if info else None

def get_username(token):
    info = _session_info(token)
    return info["username"] if info else None

def revoke_token(token):
    token = _effective_token(token)
    if not token:
        return
    db = get_db()
    db.revoke_auth_session(_token_hash(token))
    db.close()

def _invite_codes():
    raw = os.getenv("OKX_INVITE_CODES", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def registration_enabled() -> bool:
    """Require an explicit opt-in and invite codes for public registration."""
    enabled = str(os.getenv("OKX_ALLOW_PUBLIC_REGISTRATION", "false")).strip().lower()
    return enabled in {"1", "true", "yes", "on"} and bool(_invite_codes())

def register(username, password, email="", invite_code=""):
    if not registration_enabled():
        return {"ok": False, "msg": "public registration disabled"}
    username = (username or "").strip().lower()
    email = (email or username).strip().lower()
    codes = _invite_codes()
    if codes and (not invite_code or invite_code.strip() not in codes):
        return {"ok": False, "msg": "invalid invite code"}
    bucket = str(int(time.time()) // 3600)
    key = f"{bucket}:{email}"
    attempts = _recent_registers.get(key, 0)
    if attempts >= int(os.getenv("OKX_REGISTER_HOURLY_LIMIT", "3")):
        return {"ok": False, "msg": "register rate limited"}
    _recent_registers[key] = attempts + 1
    db = get_db()
    result = db.register_user(username, password, email)
    db.close()
    if result["ok"]:
        return {"ok": True, "msg": "OK"}
    msg = result.get("msg", "")
    if "UNIQUE" in msg or "unique" in msg:
        return {"ok": False, "msg": "email already registered"}
    return {"ok": False, "msg": msg or "register failed"}

def change_password(username, old_pw, new_pw):
    db = get_db()
    verify = db.verify_user(username, old_pw)
    if not verify["ok"]:
        db.close()
        return False
    result = db.update_user_password(username, new_pw)
    if result["ok"]:
        db.revoke_user_sessions(verify["user_id"])
    db.close()
    return result["ok"]

def change_password_by_token(token, old_pw, new_pw):
    username = get_username(token)
    if not username:
        return False
    return change_password(username, old_pw, new_pw)


def is_admin_token(token):
    return get_username(token) == "admin"
