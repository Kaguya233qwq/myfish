import json
import os
from pathlib import Path

from loguru import logger


def generate_headers() -> dict[str, str]:
    """生成请求头的函数"""
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://www.goofish.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0",
    }


_default_data_dir = Path.cwd() / "data"
DATA_DIR = Path(os.getenv("MYFISH_DATA_DIR", str(_default_data_dir)))
AUTH_FILE = DATA_DIR / "myfish_auth.json"


def load_local_auth() -> dict:
    """从本地读取Cookie"""
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning(f"本地鉴权文件损坏 [{AUTH_FILE.name}]，将重新登录。")
    return {}


def save_local_auth(cookies: dict):
    """持久化Cookie到本地"""
    if not DATA_DIR.exists():
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            logger.info(f"已创建持久化数据目录: {DATA_DIR}")
        except PermissionError:
            logger.error(f"无法创建数据目录 {DATA_DIR}，请检查文件夹权限！")
            return

    try:
        AUTH_FILE.write_text(
            json.dumps(cookies, indent=4, ensure_ascii=False), encoding="utf-8"
        )
        logger.success(f"凭证已成功保存至: {AUTH_FILE.resolve()}")
    except Exception as e:
        logger.error(f"保存凭证失败: {e}")


def rm_local_auth():
    """删除本地Cookie文件"""
    if AUTH_FILE.exists():
        try:
            AUTH_FILE.unlink()
            logger.info(f"已删除本地凭证文件: {AUTH_FILE.name}")
        except Exception as e:
            logger.error(f"删除凭证文件失败: {e}")
