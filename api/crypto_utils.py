"""API密钥加密存储：Fernet加密 + PBKDF2口令派生
"""

import os, base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


class CredentialManager:
    def __init__(self, config: dict):
        ex = config.get("exchange", {}).get("okx", {})
        self._key_file = ex.get("key_file", "data/keys.enc")
        self._password = None

    def set_password(self, pw: str):
        self._password = pw

    def _derive_key(self, salt: bytes) -> bytes:
        if not self._password:
            raise ValueError("未设置密码，请先调用 set_password()")
        kdf = PBKDF2HMAC(hashes.SHA256(), 32, salt, 600000)
        return base64.urlsafe_b64encode(kdf.derive(self._password.encode()))

    def save(self, api_key: str, api_secret: str, passphrase: str):
        if not self._password:
            self._password = input("设置加密口令: ").strip()
        salt = os.urandom(16)
        key = self._derive_key(salt)
        data = f"{api_key}|{api_secret}|{passphrase}".encode()
        encrypted = Fernet(key).encrypt(data)
        os.makedirs(os.path.dirname(self._key_file), exist_ok=True)
        with open(self._key_file, "wb") as f:
            f.write(salt + encrypted)

    def load(self) -> dict:
        if not os.path.exists(self._key_file):
            return None
        if not self._password:
            import getpass
            self._password = getpass.getpass("解密口令: ").strip()
        with open(self._key_file, "rb") as f:
            data = f.read()
        salt, encrypted = data[:16], data[16:]
        key = self._derive_key(salt)
        decrypted = Fernet(key).decrypt(encrypted).decode()
        parts = decrypted.split("|", 2)
        return {"api_key": parts[0], "api_secret": parts[1], "passphrase": parts[2]}

    def exists(self) -> bool:
        return os.path.exists(self._key_file)
