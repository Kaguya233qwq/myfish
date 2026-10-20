import asyncio
import hashlib
import hmac
import re
import secrets
import time

from typing_extensions import TypedDict

from .store import Store


class SessionUser(TypedDict):
    id: int
    username: str
    role: str
    session_id: int
    csrf: str
    expires: float


class AuthError(ValueError):
    pass


def password_hash(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("密码长度必须为 12 到 128 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > 128:
        return False
    _, salt, digest = encoded.split("$")
    actual = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    )
    return hmac.compare_digest(actual.hex(), digest)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, store: Store, hours: int = 12) -> None:
        self.store = store
        self.hours = hours
        self.dummy = password_hash(secrets.token_urlsafe(24))

    async def create_user(
        self, username, password, role="operator", *, setup_token=None
    ):
        username = username.lower().strip()
        if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username) or role not in {
            "admin",
            "operator",
        }:
            raise ValueError(
                "账号需为 3-40 位字母数字或 _.-，角色仅支持 admin/operator"
            )
        hashed = await asyncio.to_thread(password_hash, password)

        def create(db):
            if setup_token is not None:
                if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                    raise AuthError("初始化已完成")
                if not hmac.compare_digest(
                    setup_token.encode(), self.store.setup_token().encode()
                ):
                    raise AuthError("初始化令牌错误")
            return db.execute(
                "INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)",
                (username, hashed, role, time.time()),
            ).lastrowid

        user_id = await self.store.call(create, write=True)
        if setup_token is not None:
            (self.store.directory / "setup.token").unlink(missing_ok=True)
        return user_id

    async def login(self, username, password, ip):
        username = username.lower().strip()
        now = time.time()
        keys = [token_hash("ip:" + ip), token_hash("user:" + username)]

        def admit(db):
            db.execute("DELETE FROM login_attempts WHERE created<?", (now - 900,))
            attempts = db.execute(
                "SELECT key,COUNT(*) AS count FROM login_attempts WHERE key IN (?,?) GROUP BY key",
                keys,
            ).fetchall()
            if any(row["count"] >= 10 for row in attempts):
                raise AuthError("尝试次数过多，请 15 分钟后再试")
            db.executemany(
                "INSERT INTO login_attempts(key,created) VALUES(?,?)",
                [(key, now) for key in keys],
            )

        await self.store.call(admit, write=True)
        users = await self.store.rows(
            "SELECT * FROM users WHERE username=?", (username,)
        )
        user = users[0] if users else None
        valid = await asyncio.to_thread(
            verify_password, password, user["password"] if user else self.dummy
        )
        if not valid or not user or not user["active"]:
            raise AuthError("账号或密码错误")
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        expires = now + self.hours * 3600

        def record(db):
            current = db.execute(
                "SELECT password,active FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            if (
                not current
                or not current["active"]
                or current["password"] != user["password"]
            ):
                raise AuthError("账号凭证已更新，请重新登录")
            db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            db.execute("DELETE FROM login_attempts WHERE key=?", (keys[1],))
            db.execute(
                "DELETE FROM login_attempts WHERE key=? AND created=?", (keys[0], now)
            )
            db.execute(
                "INSERT INTO sessions(token,user_id,csrf,expires,created) VALUES(?,?,?,?,?)",
                (token_hash(token), user["id"], csrf, expires, now),
            )
            db.execute(
                "DELETE FROM sessions WHERE user_id=? AND id NOT IN (SELECT id FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 20)",
                (user["id"], user["id"]),
            )

        await self.store.call(record, write=True)
        return token, csrf, expires

    async def session(self, token: str) -> SessionUser:
        if not token or len(token) > 256:
            raise AuthError("请先登录")
        rows = await self.store.rows(
            "SELECT u.id,u.username,u.role,s.id AS session_id,s.csrf,s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1",
            (token_hash(token), time.time()),
        )
        if not rows:
            raise AuthError("登录已失效，请重新登录")
        row = rows[0]
        return SessionUser(
            id=int(row["id"]),
            username=str(row["username"]),
            role=str(row["role"]),
            session_id=int(row["session_id"]),
            csrf=str(row["csrf"]),
            expires=float(row["expires"]),
        )

    async def change_password(self, user_id, old, new):
        users = await self.store.rows(
            "SELECT password FROM users WHERE id=?", (user_id,)
        )
        if not users or not await asyncio.to_thread(
            verify_password, old, users[0]["password"]
        ):
            raise AuthError("原密码错误")
        await self.reset_password(user_id, new, expected=users[0]["password"])

    async def reset_password(self, user_id, new, *, expected=None):
        hashed = await asyncio.to_thread(password_hash, new)

        def reset(db):
            current = db.execute(
                "SELECT password FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not current:
                raise ValueError("账号不存在")
            if expected is not None and current["password"] != expected:
                raise AuthError("密码已更新，请重新登录")
            db.execute("UPDATE users SET password=? WHERE id=?", (hashed, user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

        await self.store.call(reset, write=True)
