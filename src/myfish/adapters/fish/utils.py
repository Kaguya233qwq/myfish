import json
from pathlib import Path

from loguru import logger

from myfish.core.storage import StorageManager


def generate_headers() -> dict[str, str]:
    """生成请求头的函数"""
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://www.goofish.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0",
    }


def _get_auth_file() -> Path:
    """动态获取闲鱼适配器的鉴权文件路径"""
    return StorageManager.get_adapter_dir("fish") / "auth.json"


def load_local_auth() -> dict:
    """从本地读取 Cookie"""
    auth_file = _get_auth_file()
    if auth_file.exists():
        try:
            return json.loads(auth_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning(f"本地鉴权文件损坏 [{auth_file.name}]，将重新登录。")
    return {}


def save_local_auth(cookies: dict):
    """持久化 Cookie 到本地"""
    auth_file = _get_auth_file()

    try:
        auth_file.write_text(
            json.dumps(cookies, indent=4, ensure_ascii=False), encoding="utf-8"
        )
        logger.success(f"凭证已成功保存至: {auth_file.resolve()}")
    except Exception as e:
        logger.error(f"保存凭证失败: {e}")


def rm_local_auth():
    """删除本地 Cookie 文件"""
    auth_file = _get_auth_file()
    if auth_file.exists():
        try:
            auth_file.unlink()
            logger.info(f"已删除本地凭证文件: {auth_file.name}")
        except Exception as e:
            logger.error(f"删除凭证文件失败: {e}")
