#!/opt/okx_quant/venv/bin/python3
"""
OKX 量化交易系统 - 主入口
基于 Opbot 原生逻辑，MOCA 自适应引擎增强

用法:
  python main.py                          # 交互式输入密钥
  python main.py --key-file data/keys.enc # 使用加密密钥文件
  python main.py --demo                   # 演示模式（无API连接）
  python main.py --config path/config.yaml
"""

import os
import glob
import sys
import atexit
import argparse
import signal
import getpass

try:
    import fcntl
except ImportError:
    fcntl = None


_ENGINE_LOCK_HANDLE = None


def acquire_single_instance_lock(lock_path: str = "data/engine.lock"):
    global _ENGINE_LOCK_HANDLE
    if fcntl is None:
        return
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        print(f"[错误] 已有引擎实例在运行，拒绝重复启动: {lock_path}")
        sys.exit(1)
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _ENGINE_LOCK_HANDLE = handle

    def _release_lock():
        global _ENGINE_LOCK_HANDLE
        if _ENGINE_LOCK_HANDLE is None:
            return
        try:
            fcntl.flock(_ENGINE_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            _ENGINE_LOCK_HANDLE.close()
        except OSError:
            pass
        _ENGINE_LOCK_HANDLE = None

    atexit.register(_release_lock)


def parse_args():
    parser = argparse.ArgumentParser(
        description="OKX 量化交易系统 — MOCA 自适应引擎"
    )
    parser.add_argument(
        "--config", default="config/config.yaml",
        help="配置文件路径 (默认: config/config.yaml)"
    )
    parser.add_argument(
        "--password", default="",
        help="密钥文件解密口令"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="演示模式：不连接交易所，仅展示框架运行"
    )
    return parser.parse_args()


def load_api_keys(args, config: dict) -> dict:
    """加载API密钥（加密存储）"""
    # 演示模式
    if args.demo:
        print("[INFO] 演示模式：跳过API密钥加载")
        return None

    # 从加密文件加载
    from api.crypto_utils import CredentialManager
    cm = CredentialManager(config)

    if cm.exists():
        if args.password:
            cm.set_password(args.password)
        else:
            pw = os.environ.get("OKX_KEY_PASSWORD", "")
            if not pw:
                # try reading from web_config
                try:
                    import sys as _sys
                    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "web"))
                    from config_manager import load_api_password_secure
                    pw = load_api_password_secure()
                except Exception:
                    pw = ""
            if pw:
                cm.set_password(pw)
            elif not sys.stdin.isatty():
                print("[ERROR] 缺少解密口令: 通过 Web 面板设置，或使用 --password/OKX_KEY_PASSWORD")
                sys.exit(1)
            else:
                pw = getpass.getpass("Password: ")
                cm.set_password(pw)
        try:
            keys = cm.load()
            print("[OK] 密钥已从加密文件加载")
            return keys
        except ValueError as e:
            print(f"[错误] {e}")
            sys.exit(1)

    # 交互式输入
    if not sys.stdin.isatty():
        print("[WARN] Non-interactive, skip key input. Configure via Web admin.")
        return None
    print("\n")
    print("首次运行：请输入OKX API密钥")
    print("(建议仅授予 交易权限，禁止提币权限)\n")
    api_key = input("API Key: ").strip()
    api_secret = input("API Secret: ").strip()
    passphrase = input("Passphrase: ").strip()
    if not (api_key and api_secret and passphrase):
        print("[错误] API密钥不完整")
        sys.exit(1)

    # 询问是否加密保存
    save_choice = input("\n是否加密保存密钥到本地? (y/n): ").strip().lower()
    if save_choice == "y":
        if args.password:
            pw = args.password
        else:
            pw = getpass.getpass("设置加密口令: ")
            pw2 = getpass.getpass("确认加密口令: ")
            if pw != pw2:
                print("[错误] 两次口令不一致")
                sys.exit(1)
        cm.set_password(pw)
        cm.save(api_key, api_secret, passphrase)
        print(f"[OK] 密钥已加密保存到 {config['exchange']['okx']['key_file']}")

    return {"api_key": api_key, "api_secret": api_secret, "passphrase": passphrase}


def main():
    args = parse_args()
    acquire_single_instance_lock()

    print("=" * 50)
    print("  OKX 量化交易系统 v2.0")
    print("  基于 MOCA 自适应引擎")
    print("=" * 50)

    # 加载配置
    import yaml
    config_path = args.config
    if not os.path.exists(config_path):
        print(f"[错误] 配置文件不存在: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 加载API密钥
    api_keys = load_api_keys(args, config)

    # 启动引擎
    from core.engine import MainEngine
    extra_configs = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config/config_*.yaml")))
    engine = MainEngine(config_path, api_keys, extra_configs)

    # 注册信号处理
    def signal_handler(sig, frame):
        print("\n收到停止信号，正在关闭...")
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        engine.start()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        print(f"[FATAL] 引擎异常: {e}")
        traceback.print_exc()
        engine.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
