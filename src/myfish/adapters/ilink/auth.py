import json
from pathlib import Path
from typing import Optional
from myfish.core.logger import logger
from myfish.core.storage import StorageManager  # 引入统一存储管理器


def _get_cred_path() -> Path:
    """获取当前适配器的凭证路径"""
    return StorageManager.get_adapter_dir("ilink") / "auth.json"


def load_credentials() -> Optional[dict]:
    """读取本地凭证"""
    cred_path = _get_cred_path()
    if not cred_path.exists():
        return None
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        logger.error(f"[ILink] 读取凭证文件失败: {e}")
        return None


def save_credentials(token: str, base_url: str, account_id: str, user_id: str):
    """持久化保存凭证"""
    payload = {
        "token": token,
        "base_url": base_url,
        "account_id": account_id,
        "user_id": user_id,
    }
    try:
        _get_cred_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("[ILink] 登录凭证已持久化保存至数据目录")
    except Exception as e:
        logger.error(f"[ILink] 保存凭证失败: {e}")


def clear_credentials():
    """清除失效凭证"""
    cred_path = _get_cred_path()
    if cred_path.exists():
        cred_path.unlink()
        logger.info("[ILink] 本地凭证已清除")
