"""本机后台密码重置工具。"""

import argparse
import asyncio
import getpass
from pathlib import Path

from .auth import Auth
from .store import Store


async def reset(directory, username):
    if not (directory / "fishy.sqlite3").is_file():
        raise SystemExit("数据库不存在，请确认插件数据目录")
    store = Store(directory)
    rows = await store.rows(
        "SELECT id FROM users WHERE username=?", (username.lower(),)
    )
    if not rows:
        raise SystemExit("账号不存在")
    password = getpass.getpass("新密码（12-128 字符）: ")
    if password != getpass.getpass("再次输入: "):
        raise SystemExit("两次密码不一致")
    await Auth(store).reset_password(rows[0]["id"], password)
    print("密码已重置，所有已有会话已撤销。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    asyncio.run(reset(args.data_dir, args.username))
