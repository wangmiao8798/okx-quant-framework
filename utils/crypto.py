"""用户API密钥AES加密存储 (Fernet + 服务端密钥文件)"""
import os, base64
from pathlib import Path

KEY_FILE = "data/server.key"

class UserCrypto:
    _key = None

    @classmethod
    def get_key_path(cls) -> Path:
        return Path(__file__).resolve().parent.parent / KEY_FILE

    @classmethod
    def init_key(cls):
        key_path = cls.get_key_path()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            cls._key = key_path.read_bytes()
        else:
            from cryptography.fernet import Fernet
            cls._key = Fernet.generate_key()
            key_path.write_bytes(cls._key)
            os.chmod(str(key_path), 0o600)
        return cls._key

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        if cls._key is None:
            cls.init_key()
        from cryptography.fernet import Fernet
        return Fernet(cls._key).encrypt(plaintext.encode()).decode()

    @classmethod
    def decrypt(cls, ciphertext: str) -> str:
        if cls._key is None:
            cls.init_key()
        from cryptography.fernet import Fernet
        return Fernet(cls._key).decrypt(ciphertext.encode()).decode()
