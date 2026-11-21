import importlib
import hashlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


def main():
    project_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(project_dir))
    sys.path.insert(0, str(project_dir / "web"))

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "sessions.db")
        os.environ["OKX_DB_PATH"] = db_path

        try:
            from utils.database import Database
        except ModuleNotFoundError:
            from database import Database
        db = Database(db_path)
        assert db.register_user("session@example.com", "CustomerTest2026!", "session@example.com")["ok"]
        stored_password = db._conn.execute(
            "SELECT password_hash FROM users WHERE username=?", ("session@example.com",)
        ).fetchone()[0]
        assert stored_password.startswith("pbkdf2_sha256$")

        legacy_hash = hashlib.sha256(b"LegacyPassword2026!").hexdigest()
        db._conn.execute(
            "INSERT INTO users(username,password_hash,email,created_ts) VALUES(?,?,?,0)",
            ("legacy@example.com", legacy_hash, "legacy@example.com"),
        )
        db._conn.commit()
        assert db.verify_user("legacy@example.com", "LegacyPassword2026!")["ok"]
        upgraded_hash = db._conn.execute(
            "SELECT password_hash FROM users WHERE username=?", ("legacy@example.com",)
        ).fetchone()[0]
        assert upgraded_hash.startswith("pbkdf2_sha256$") and upgraded_hash != legacy_hash
        db.close()

        import auth
        token = auth.login("session@example.com", "CustomerTest2026!")
        assert token and auth.check_token(token)

        conn = sqlite3.connect(db_path)
        stored_hash = conn.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
        conn.close()
        assert stored_hash != token
        assert stored_hash == auth._token_hash(token)

        auth = importlib.reload(auth)
        assert auth.check_token(token)
        assert auth.get_username(token) == "session@example.com"

        auth.revoke_token(token)
        assert not auth.check_token(token)

    os.environ.pop("OKX_DB_PATH", None)
    print("persistent session checks passed")


if __name__ == "__main__":
    main()


def test_persistent_sessions():
    main()
