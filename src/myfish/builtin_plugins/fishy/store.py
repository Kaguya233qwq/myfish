import asyncio
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, TypeVar

from cryptography.fernet import Fernet

T = TypeVar("T")
Parameters = Sequence[object] | Mapping[str, object]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
 role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
 csrf TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (key TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS login_key ON login_attempts(key, created);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, price TEXT NOT NULL,
 account_id TEXT NOT NULL, item_id TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL,
 content TEXT NOT NULL, images TEXT NOT NULL DEFAULT '[]', auto_delivery INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'draft',
 created REAL NOT NULL, updated REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS product_listing ON products(account_id,item_id) WHERE item_id != '';
CREATE TABLE IF NOT EXISTS orders (
 key TEXT PRIMARY KEY, account_id TEXT NOT NULL, order_id TEXT NOT NULL, product_id INTEGER,
 item_id TEXT NOT NULL, buyer_id TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
 chat_id TEXT NOT NULL DEFAULT '', platform_status TEXT NOT NULL,
 delivery_state TEXT NOT NULL DEFAULT 'pending', content TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, account_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', run_at REAL NOT NULL, error TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL, updated REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_job ON jobs(kind,target) WHERE status IN ('pending','running');
CREATE TABLE IF NOT EXISTS sale_reshelves (
 order_key TEXT PRIMARY KEY REFERENCES orders(key),
 job_id INTEGER REFERENCES jobs(id), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS polish_accounts (
 account_id TEXT PRIMARY KEY, next_attempt REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS product_edits (
 product_id INTEGER PRIMARY KEY REFERENCES products(id), revision TEXT NOT NULL,
 candidate TEXT NOT NULL, baseline TEXT NOT NULL, expected TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL DEFAULT 'draft', job_id INTEGER, error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_logs (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
 message TEXT NOT NULL, location TEXT NOT NULL DEFAULT '', created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS job_log_task ON job_logs(job_id,id);
"""


class Store:
    """在线程中使用独立 SQLite 连接。"""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.path = directory / "fishy.sqlite3"
        key_path = directory / "encryption.key"
        if not key_path.exists():
            if self.path.exists():
                raise RuntimeError(
                    "已有数据库缺少 encryption.key，请恢复原密钥后再启动"
                )
            with key_path.open("xb") as stream:
                stream.write(Fernet.generate_key())
            key_path.chmod(0o600)
        self.key = key_path.read_bytes()
        self.cipher = Fernet(self.key)

    async def initialize(self):
        def initialize(db):
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 9:
                raise RuntimeError("数据库版本高于当前插件，请使用匹配版本")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(products)")}
            for name, declaration in {
                "product_type": "TEXT NOT NULL DEFAULT 'virtual'",
                "category": "TEXT NOT NULL DEFAULT '{}'",
                "quantity": "INTEGER NOT NULL DEFAULT 1",
                "shipping": "TEXT NOT NULL DEFAULT 'free'",
                "postage": "TEXT NOT NULL DEFAULT '0'",
                "draft_id": "TEXT NOT NULL DEFAULT ''",
                "draft_state": "TEXT NOT NULL DEFAULT 'local'",
                "draft_synced_at": "REAL NOT NULL DEFAULT 0",
                "auto_polish": "INTEGER NOT NULL DEFAULT 0",
                "polish_interval_hours": "INTEGER NOT NULL DEFAULT 24",
                "polish_next_at": "REAL NOT NULL DEFAULT 0",
                "polish_last_at": "REAL NOT NULL DEFAULT 0",
                "polish_status": "TEXT NOT NULL DEFAULT 'never'",
                "polish_error": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE products ADD COLUMN {name} {declaration}")
            order_columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
            for name in ("amount", "buyer_name"):
                if name not in order_columns:
                    db.execute(
                        f"ALTER TABLE orders ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            if version < 9:
                db.execute(
                    "UPDATE jobs SET status='cancelled',error='商品交付方式已停用',updated=? "
                    "WHERE status='pending' AND "
                    "((kind='deliver' AND target IN (SELECT o.key FROM orders o JOIN products p "
                    "ON p.id=o.product_id WHERE p.mode='stock')) OR "
                    "(kind IN ('publish','save_draft','reshelf') AND target IN "
                    "(SELECT CAST(id AS TEXT) FROM products WHERE mode='stock')))",
                    (time.time(),),
                )
                db.execute(
                    "UPDATE products SET mode='disabled',auto_delivery=0,"
                    "draft_state=CASE WHEN draft_state='queued' THEN "
                    "CASE WHEN draft_id='' THEN 'local' ELSE 'dirty' END "
                    "ELSE draft_state END WHERE mode='stock'"
                )
            db.execute("PRAGMA user_version=9")
            db.execute(
                "UPDATE product_edits SET state='uncertain',error='服务在编辑期间退出，请核对平台内容' WHERE state='running' OR (state='queued' AND job_id IN (SELECT id FROM jobs WHERE status='running'))"
            )
            db.execute(
                "INSERT INTO polish_accounts(account_id,next_attempt) SELECT DISTINCT account_id,? FROM jobs WHERE kind='polish' AND status='running' ON CONFLICT(account_id) DO UPDATE SET next_attempt=MAX(next_attempt,excluded.next_attempt)",
                (time.time() + 3600,),
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS polish_due ON products(auto_polish,status,polish_next_at)"
            )
            db.execute(
                "UPDATE products SET polish_status='uncertain',polish_error='服务在擦亮期间退出，请核对平台结果' WHERE polish_status='running' OR EXISTS (SELECT 1 FROM jobs j WHERE j.target=CAST(products.id AS TEXT) AND j.kind='polish' AND j.status='running')"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS platform_draft ON products(account_id,draft_id) WHERE draft_id!=''"
            )
            db.execute(
                "UPDATE products SET draft_state='uncertain' WHERE draft_state='syncing'"
            )
            db.execute(
                "UPDATE jobs SET status='uncertain', error='服务在任务执行期间退出，请核对平台结果' WHERE status='running'"
            )

        await self.call(initialize, write=True)

    async def call(
        self, function: Callable[[sqlite3.Connection], T], *, write: bool = False
    ) -> T:
        def work() -> T:
            with closing(sqlite3.connect(self.path, timeout=15)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA journal_mode=WAL")
                with db:
                    if write:
                        db.execute("BEGIN IMMEDIATE")
                    return function(db)

        return await asyncio.to_thread(work)

    async def rows(self, sql: str, params: Parameters = ()) -> list[dict[str, Any]]:
        return await self.call(
            lambda db: [dict(row) for row in db.execute(sql, params)]
        )

    async def execute(self, sql: str, params: Parameters = ()) -> int | None:
        return await self.call(lambda db: db.execute(sql, params).lastrowid, write=True)

    def encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode() if value else ""

    def setup_token(self) -> str:
        path = self.directory / "setup.token"
        if not path.exists():
            with path.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(32))
            path.chmod(0o600)
        return path.read_text(encoding="utf-8").strip()
