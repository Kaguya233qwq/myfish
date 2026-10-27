import asyncio
import os
import re
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

import httpx
import qrcode
from pydantic import BaseModel, Field, SecretStr, field_serializer

from myfish.core.logger import logger

from .api import BilibiliAPI, BilibiliProtocolError
from .sign import correspond_path


class AuthState(BaseModel):
    cookies: dict[str, str] = Field(repr=False)
    device_id: str
    refresh_token: SecretStr | None = None
    confirmation_token: SecretStr | None = None

    @field_serializer("refresh_token", "confirmation_token", when_used="json")
    def serialize_token(self, value: SecretStr | None) -> str | None:
        return value.get_secret_value() if value is not None else None


def load_state(path: Path) -> AuthState | None:
    if not path.exists():
        return None
    return AuthState.model_validate_json(path.read_bytes())


def save_state(api: BilibiliAPI, path: Path) -> None:
    cookies = api.cookie_values()
    if not all(cookies.get(name) for name in ("SESSDATA", "bili_jct", "DedeUserID")):
        raise BilibiliProtocolError("Login cookies are incomplete")
    state = AuthState(
        cookies=cookies,
        device_id=api.device_id,
        refresh_token=api.refresh_token,
        confirmation_token=api.confirmation_token,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".auth-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(state.model_dump_json())
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def show_qrcode(url: str, path: Path | None = None) -> None:
    code = qrcode.QRCode(border=2)
    code.add_data(url)
    code.make(fit=True)
    code.print_ascii(invert=True)
    if path is not None:
        descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            code.make_image().save(stream)
        logger.info("[Bilibili] 登录二维码已保存: {}", path)


class _RefreshPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._collect = False
        self.token = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div" and dict(attrs).get("id") == "1-name":
            self._collect = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            self._collect = False

    def handle_data(self, data: str) -> None:
        if self._collect:
            self.token += data


async def refresh_credentials(api: BilibiliAPI, path: Path) -> bool:
    if api.confirmation_token is not None:
        await api.confirm_cookie_refresh(api.confirmation_token)
        api.confirmation_token = None
        await asyncio.to_thread(save_state, api, path)
        return True
    token = api.refresh_token
    if token is None or not token.get_secret_value():
        return False
    info = await api.get_cookie_info()
    if not info.refresh:
        return False
    async with BilibiliAPI(api.cookie_values(), transport=api._transport) as candidate:
        candidate.device_id = api.device_id
        response = await candidate._client.get(
            "https://www.bilibili.com/correspond/1/" + correspond_path(info.timestamp),
            headers={
                "origin": "https://www.bilibili.com",
                "referer": "https://www.bilibili.com/",
            },
        )
        if response.status_code != 200:
            raise BilibiliProtocolError("Cookie refresh page was not available")
        parser = _RefreshPage()
        parser.feed(response.text)
        refresh_csrf = parser.token.strip()
        if re.fullmatch(r"[0-9a-fA-F]{32}", refresh_csrf) is None:
            raise BilibiliProtocolError("Cookie refresh token was not found")
        await candidate.refresh_cookie(token, refresh_csrf)
        account = await candidate.get_self_info()
        if api.account is None or account.mid != api.account.mid:
            raise BilibiliProtocolError("Cookie refresh changed the account")
        candidate.confirmation_token = token
        await asyncio.to_thread(save_state, candidate, path)
        api._client.cookies = httpx.Cookies(candidate._client.cookies)
        api.refresh_token = candidate.refresh_token
        api.confirmation_token = token
        api.account = account
    await api.confirm_cookie_refresh(token)
    api.confirmation_token = None
    await asyncio.to_thread(save_state, api, path)
    logger.success("[Bilibili] 登录凭据已刷新")
    return True


async def login(api: BilibiliAPI, path: Path) -> None:
    qr = await api.get_login_qrcode()
    path.parent.mkdir(parents=True, exist_ok=True)
    qr_path = path.with_name("login.png")
    await asyncio.to_thread(show_qrcode, qr.url, qr_path)
    logger.info("[Bilibili] 请使用 B站 App 扫描终端二维码并确认登录")
    deadline = time.monotonic() + 180
    previous: int | None = None
    try:
        while time.monotonic() < deadline:
            status = await api.poll_login_qrcode(qr.qrcode_key.get_secret_value())
            if status.code == 0:
                await api.get_self_info()
                await asyncio.to_thread(save_state, api, path)
                logger.success("[Bilibili] 登录成功")
                return
            if status.code == 86038:
                raise TimeoutError("Bilibili login QR code expired")
            if status.code not in (86101, 86090):
                raise BilibiliProtocolError(f"Unknown login status: {status.code}")
            if status.code == 86090 and previous != status.code:
                logger.info("[Bilibili] 已扫码，请在手机上确认")
            previous = status.code
            await asyncio.sleep(2)
        raise TimeoutError("Bilibili QR login timed out")
    finally:
        qr_path.unlink(missing_ok=True)
