from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import AsyncIterator
from http.cookiejar import Cookie
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Literal, Self, TypeAlias, TypeVar

import httpx
from pydantic import BaseModel, JsonValue, ValidationError

from myfish.core.adapter import BaseAPI
from myfish.core.logger import logger

from .device import (
    LEGACY_USER_AGENT,
    DeviceFingerprint,
    DeviceProfile,
    generate_fields,
    generate_telemetry,
)
from .message import encode_text
from .model import (
    Account,
    Chat,
    ChatPage,
    ClientState,
    CookieState,
    Message,
    MessagePage,
    NotificationCategory,
    NotificationPage,
    PushToken,
    SendReceipt,
    SendResult,
    SigningSession,
    UnreadCounts,
    XhsResponse,
)
from .notes import (
    Comment,
    CommentCreated,
    CommentPage,
    FeedItem,
    FeedPage,
    Note,
    NoteAction,
    NoteRef,
    UserNote,
    UserNotePage,
)
from .sign import (
    current_headers,
    encode_b1,
    encrypt_message,
    sign_common,
    sign_xys,
    sign_xyw,
)
from .utils import (
    IDENTITY_ALPHABET,
    QueryValue,
    build_url,
    compact_json,
    generate_a1,
    generate_web_id,
    now_ms,
    write_private_json,
)

Model = TypeVar("Model", bound=BaseModel)


def parse_data(response: httpx.Response, model: type[Model]) -> Model:
    try:
        return model.model_validate(response.json()["data"])
    except (ValidationError, ValueError, KeyError, TypeError):
        raise XhsAPIError(status=response.status_code) from None


SignFormat: TypeAlias = Literal["xys", "xyw"]


class XhsAPIError(RuntimeError):
    def __init__(self, *, status: int, code: int | None = None) -> None:
        self.status = status
        self.code = code
        super().__init__(f"XHS request failed: HTTP {status}, code={code}")


class XhsVerificationRequired(XhsAPIError):
    def __init__(self, *, status: int, verify_uuid: str, verify_type: str) -> None:
        super().__init__(status=status)
        self.verify_uuid = verify_uuid
        self.verify_type = verify_type

    @property
    def verification_method(self) -> Literal["sms", "qrcode", "unknown"]:
        if self.verify_type == "201":
            return "sms"
        if self.verify_type == "124":
            return "qrcode"
        return "unknown"


class XhsAPI(BaseAPI):
    def __init__(
        self,
        cookies: dict[str, str],
        *,
        sign_format: SignFormat = "xys",
        transport: httpx.AsyncBaseTransport | None = None,
        session: SigningSession | None = None,
    ) -> None:
        if not cookies.get("a1"):
            raise ValueError("a1 cookie is required")
        self.session = session or SigningSession()
        self.fingerprint = DeviceFingerprint.create(cookies)
        self.session_secret: bytes | None = None
        self.use_current_signing = False
        self.profile_sdk_version = "4.3.5"
        self._sign_format = sign_format
        self._client = httpx.AsyncClient(
            cookies=cookies,
            timeout=20,
            transport=transport,
            follow_redirects=False,
            headers={
                "user-agent": LEGACY_USER_AGENT,
                "origin": "https://www.xiaohongshu.com",
                "referer": "https://www.xiaohongshu.com/",
            },
        )

    @classmethod
    def create(
        cls,
        *,
        profile: DeviceProfile | None = None,
        fields: dict[str, JsonValue] | None = None,
        sign_format: SignFormat = "xys",
    ) -> Self:
        timestamp = now_ms()
        a1 = generate_a1(
            timestamp, "".join(secrets.choice(IDENTITY_ALPHABET) for _ in range(30))
        )
        api = cls(
            {
                "a1": a1,
                "webId": generate_web_id(a1),
                "xsecappid": "xhs-pc-web",
                "webBuild": "6.3.0",
                "loadts": str(timestamp),
            },
            sign_format=sign_format,
        )
        if profile is not None:
            generated = generate_fields(profile, a1, timestamp)
            generated["x83"] = fields.get("x83", "") if fields is not None else ""
            generated["x84"] = generate_telemetry(api.session.loaded_at_ms)
            api.set_profile_fields(generated)
        elif fields is not None:
            api.set_profile_fields(fields)
        return api

    def save_state(self, path: Path) -> None:
        if (
            not self.use_current_signing
            or self.session_secret is None
            or self.session.dsl is None
            or self.session.tiga_timestamp is None
        ):
            raise ValueError("Current signing is not initialized")
        state = ClientState(
            cookies=[
                CookieState(
                    name=cookie.name,
                    value=cookie.value,
                    domain=cookie.domain,
                    path=cookie.path,
                    secure=cookie.secure,
                    expires=cookie.expires,
                    domain_specified=cookie.domain_specified,
                    domain_initial_dot=cookie.domain_initial_dot,
                    path_specified=cookie.path_specified,
                )
                for cookie in self._client.cookies.jar
                if cookie.value is not None
            ],
            fingerprint=self.fingerprint.fields,
            secret=self.session_secret.hex(),
            loaded_at_ms=self.session.loaded_at_ms,
            sequence=self.session.sequence,
            dsl=self.session.dsl,
            tiga_timestamp=self.session.tiga_timestamp,
            tab_device_id=self.session.tab_device_id,
            rwp_fingerprint=self.session.rwp_fingerprint,
        )
        write_private_json(path, state.model_dump_json())

    @classmethod
    def from_state(
        cls,
        path: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        try:
            state = ClientState.model_validate_json(path.read_bytes())
            a1 = next(cookie.value for cookie in state.cookies if cookie.name == "a1")
            session = SigningSession()
            session.loaded_at_ms = state.loaded_at_ms
            session.sequence = state.sequence
            session.dsl = state.dsl
            session.tiga_timestamp = state.tiga_timestamp
            session.tab_device_id = state.tab_device_id
            session.rwp_fingerprint = state.rwp_fingerprint or str(state.loaded_at_ms)
            api = cls({"a1": a1}, session=session, transport=transport)
            api.set_profile_fields(state.fingerprint)
        except (ValidationError, StopIteration, ValueError):
            raise ValueError("Invalid XHS client state") from None
        api._client.cookies.clear()
        for cookie in state.cookies:
            api._client.cookies.jar.set_cookie(
                Cookie(
                    version=0,
                    name=cookie.name,
                    value=cookie.value,
                    port=None,
                    port_specified=False,
                    domain=cookie.domain,
                    domain_specified=cookie.domain_specified,
                    domain_initial_dot=cookie.domain_initial_dot,
                    path=cookie.path,
                    path_specified=cookie.path_specified,
                    secure=cookie.secure,
                    expires=cookie.expires,
                    discard=cookie.expires is None,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
            )
        api.session_secret = bytes.fromhex(state.secret)
        api.use_current_signing = True
        return api

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(
        self,
        path: str,
        params: dict[str, QueryValue] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self._request("GET", path, params=params, extra_headers=headers)

    def cookie_values(self) -> dict[str, str]:
        return {
            cookie.name: cookie.value
            for cookie in self._client.cookies.jar
            if cookie.value is not None
        }

    def cookies_for_url(self, url: str) -> dict[str, str]:
        request = self._client.build_request("GET", url)
        cookies = SimpleCookie()
        cookies.load(request.headers.get("cookie", ""))
        return {name: value.value for name, value in cookies.items()}

    async def initialize_navigation(self) -> None:
        for url in (
            "https://www.xiaohongshu.com/",
            "https://www.xiaohongshu.com/explore",
        ):
            request = self._client.build_request(
                "GET", url, headers={"accept": "text/html"}
            )
            request.headers.pop("origin", None)
            response = await self._client.send(request)
            if not 200 <= response.status_code < 400:
                raise XhsAPIError(status=response.status_code)
        if not self.cookies_for_url("https://www.xiaohongshu.com/").get("abRequestId"):
            raise RuntimeError("Initial navigation did not issue abRequestId")

    async def initialize_ds(self) -> None:
        response = await self._client.get(
            "https://as.xiaohongshu.com/api/sec/v1/ds?appId=xhs-pc-web"
        )
        if response.status_code != 200:
            raise XhsAPIError(status=response.status_code)
        match = re.search(
            r"function\s+getdss\s*\(\s*\)\s*\{\s*return\s+['\"](\d+)['\"]",
            response.text,
        )
        if match is None:
            raise ValueError("Unsupported DS script format")
        self.session.dsl = match[1]

    def set_profile_fields(self, fields: dict[str, JsonValue]) -> None:
        if set(fields) != set(self.fingerprint.fields) | {"x83", "x84"}:
            raise ValueError("Current profile requires a complete field map")
        user_agent = fields["x1"]
        if not isinstance(user_agent, str) or not user_agent:
            raise ValueError("Profile user agent is missing")
        self.fingerprint.fields = dict(fields)
        self.profile_sdk_version = "4.4.3"
        self.set_cookie("webBuild", "6.47.2")
        self._client.headers["user-agent"] = user_agent

    def set_cookie(self, name: str, value: str) -> None:
        self._client.cookies.delete(name)
        self._client.cookies.set(name, value, domain=".xiaohongshu.com", path="/")

    async def post(
        self,
        path: str,
        payload: dict[str, JsonValue],
        *,
        security: bool = False,
        signed: bool = True,
        tier: Literal["0201", "0301"] = "0301",
    ) -> httpx.Response:
        return await self._request(
            "POST", path, payload=payload, security=security, signed=signed, tier=tier
        )

    async def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        params: dict[str, QueryValue] | None = None,
        payload: dict[str, JsonValue] | None = None,
        extra_headers: dict[str, str] | None = None,
        security: bool = False,
        signed: bool = True,
        tier: Literal["0201", "0301"] = "0301",
    ) -> httpx.Response:
        origin = (
            "https://as.xiaohongshu.com"
            if security
            else "https://edith.xiaohongshu.com"
        )
        url = build_url(f"{origin}{path}", params)
        signed_path = url.removeprefix(origin)
        cookies = self.cookies_for_url(url)
        body = compact_json(payload or {}) if method == "POST" else ""
        content = body.encode("utf-8") if method == "POST" else None
        headers: dict[str, str] = {}
        if signed:
            if self.use_current_signing:
                if self.session_secret is None:
                    raise ValueError("Current signing requires a negotiated SSK")
                context = self.session.current_context(
                    signed_path, security=tier == "0201"
                )
                self.fingerprint.update(cookies, context.timestamp_ms)
                nonce = (
                    context.timestamp_ms + self.session.random_bits(32)
                ) & 0xFFFFFFFF
                headers = current_headers(
                    signed_path,
                    body,
                    self.fingerprint.fields,
                    context,
                    a1=cookies["a1"],
                    web_build=cookies["webBuild"],
                    secret=self.session_secret,
                    nonce=nonce,
                    ds_anchor=self.session.ds_anchor(),
                    has_body=method == "POST",
                    tier=tier,
                )
            else:
                if self._sign_format == "xys" or tier == "0201":
                    context = (
                        self.session.current_context(signed_path, security=True)
                        if tier == "0201"
                        else self.session.legacy_context()
                    )
                    timestamp = context.timestamp_ms
                    signature = sign_xys(
                        signed_path, body, cookies["a1"], context, tier=tier
                    )
                else:
                    timestamp = self.session.clock()
                    signature = sign_xyw(signed_path, body, cookies["a1"], timestamp)
                self.fingerprint.update(cookies, timestamp)
                random_bits = self.session.random_bits
                headers = {
                    "x-s": signature,
                    "x-t": str(timestamp),
                    "x-s-common": sign_common(
                        cookies["a1"], encode_b1(self.fingerprint.fields)
                    ),
                    "x-b3-traceid": f"{random_bits(64):016x}",
                    "x-xray-traceid": f"{(timestamp << 23) | random_bits(23):016x}{random_bits(64):016x}",
                    "x-mns": "unload",
                    "xy-direction": str(10 + self.session.random_below(91)),
                }
        if method == "POST":
            headers["content-type"] = "application/json;charset=UTF-8"
        headers.update(extra_headers or {})
        request = self._client.build_request(
            method, url, headers=headers, content=content
        )
        if request.url.raw_path != url.removeprefix(origin).encode():
            raise ValueError("HTTP URL normalization changed the signed request")
        response = await self._client.send(request)
        logger.debug(
            "[XHS][HTTP] {} {} -> {}",
            method,
            path.split("?", 1)[0],
            response.status_code,
        )
        if response.status_code in (461, 471):
            logger.warning(
                "[XHS] 请求需要人工验证（HTTP {}，type={}）",
                response.status_code,
                response.headers.get("verifytype", "unknown"),
            )
            if verify_uuid := response.headers.get("verifyuuid"):
                challenge = XhsVerificationRequired(
                    status=response.status_code,
                    verify_uuid=verify_uuid,
                    verify_type=response.headers.get("verifytype", ""),
                )
                if challenge.verification_method == "qrcode":
                    logger.warning(
                        "[XHS] 需要扫码验证，可运行 python -m myfish.adapters.xhs.auth --verify"
                    )
                elif challenge.verification_method == "sms":
                    logger.warning("[XHS] 需要网页短信验证码验证，不能使用扫码验证流程")
                raise challenge
        if response.status_code != 200:
            raise XhsAPIError(status=response.status_code)
        try:
            result = XhsResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise XhsAPIError(status=response.status_code) from exc
        if not result.success or result.code != 0:
            logger.debug(
                "[XHS][HTTP] 业务请求被拒绝: path={}, code={}",
                path.split("?", 1)[0],
                result.code,
            )
            if path == "/api/sns/web/v1/comment/post" and result.code == -9043:
                logger.warning(
                    "[XHS] 评论请求被拒绝（code=-9043），账号可能处于发言限制状态，"
                    "请在官方 App 中检查账号状态，勿自动重试。"
                )
            elif path == "/api/sns/web/v1/user/follow" and result.code == -10000:
                logger.warning(
                    "[XHS] 您被系统限制关注他人（code=-10000），"
                    "请在官方 App 中检查账号状态，勿自动重试。"
                )
            elif path in {
                "/api/sns/web/v1/user/follow",
                "/api/sns/web/v1/user/unfollow",
            }:
                action = "关注" if path.endswith("/follow") else "取消关注"
                logger.warning(
                    "[XHS] {}请求被拒绝（code={}），请检查目标用户及账号限制状态，勿自动重试。",
                    action,
                    result.code,
                )
            raise XhsAPIError(status=response.status_code, code=result.code)
        return response

    async def get_self_info(self) -> Account:
        return parse_data(await self._get("/api/sns/web/v2/user/me"), Account)

    async def get_notifications(
        self, category: NotificationCategory, *, cursor: str = "", limit: int = 20
    ) -> NotificationPage:
        if category not in {"mentions", "likes", "connections"}:
            raise ValueError("Unsupported notification category")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1–100")
        response = await self._get(
            f"/api/sns/web/v1/you/{category}", {"num": limit, "cursor": cursor}
        )
        return parse_data(response, NotificationPage)

    async def get_chats(
        self, *, page: int = 0, limit: int = 20, next_ts: int | None = None
    ) -> ChatPage:
        if page < 0 or not 1 <= limit <= 100:
            raise ValueError("page must be nonnegative and limit must be 1–100")
        params: dict[str, QueryValue] = {
            "limit": limit,
            "complete": "true",
            "page": page,
            "source": "pc",
        }
        if next_ts is not None:
            if next_ts < 0:
                raise ValueError("next_ts must be nonnegative")
            params["next_ts"] = next_ts
        response = await self._get("/api/im/web/v3/chats", params)
        return parse_data(response, ChatPage)

    async def get_message_history(
        self, user_id: str, *, last_id: int = 0, start_id: int = 0, limit: int = 20
    ) -> MessagePage:
        if not user_id or not user_id.isascii() or not user_id.isalnum():
            raise ValueError("user_id must be an ASCII alphanumeric identifier")
        if min(last_id, start_id) < 0 or not 1 <= limit <= 100:
            raise ValueError(
                "message cursors must be nonnegative and limit must be 1–100"
            )
        response = await self._get(
            "/api/im/web/messages/history",
            {
                "chat_user_id": user_id,
                "last_id": last_id,
                "start_id": start_id,
                "limit": limit,
            },
        )

        result = parse_data(response, MessagePage)
        if not result.result.success or result.result.code != 0:
            raise XhsAPIError(status=response.status_code, code=result.result.code)
        return result

    async def get_unread(self) -> UnreadCounts:
        return parse_data(await self._get("/api/im/web/chat/get_unread"), UnreadCounts)

    async def iter_chats(self, *, limit: int = 20) -> AsyncIterator[Chat]:
        seen: set[str] = set()
        cursor = None
        while True:
            result = await self.get_chats(next_ts=cursor, limit=limit)
            fresh = [chat for chat in result.chats if chat.chat_user_id not in seen]
            if not fresh:
                return
            for chat in fresh:
                if chat.chat_user_id not in seen:
                    seen.add(chat.chat_user_id)
                    yield chat
            if result.next_ts < 0:
                return
            if result.next_ts == cursor:
                raise ValueError("Chat pagination did not advance")
            cursor = result.next_ts

    async def iter_message_history(
        self, user_id: str, *, limit: int = 20
    ) -> AsyncIterator[Message]:
        cursor = 0
        seen: set[str] = set()
        while True:
            result = await self.get_message_history(
                user_id, last_id=cursor, limit=limit
            )
            for message in result.out_message_list:
                if message.id not in seen:
                    seen.add(message.id)
                    yield message
            next_cursor = result.next_cursor
            if next_cursor is None or (cursor and next_cursor >= cursor):
                return
            cursor = next_cursor

    async def send_text(
        self, receiver: str, text: str, *, message_id: str | None = None
    ) -> SendReceipt:
        if not receiver or not receiver.isascii() or not receiver.isalnum():
            raise ValueError("Receiver must be an ASCII alphanumeric identifier")
        if not text.strip():
            raise ValueError("Message text must not be empty")
        if len(text.encode()) > 16 * 1024:
            raise ValueError("Message text exceeds local size limit")
        account = await self.get_self_info()
        if account.guest:
            raise ValueError("Sending messages requires an authenticated account")
        client_id = str(uuid.uuid4()) if message_id is None else message_id
        payload = encode_text(
            account.user_id, receiver, text, client_id, self.session.clock()
        )
        response = await self.post(
            "/api/im/web/short_link/send_message", {"message": encrypt_message(payload)}
        )
        result = parse_data(response, SendResult)
        return SendReceipt(
            client_id=client_id, message_id=result.message_id, chat_ack=result.chat_ack
        )

    async def get_push_token(self) -> PushToken:
        response = await self._get(
            "/api/sns/web/v1/celestial/lt",
            headers={"c_device_id": self.session.tab_device_id},
        )
        return parse_data(response, PushToken)

    async def get_recommendations(
        self,
        *,
        category: str = "homefeed_recommend",
        cursor: str = "",
        limit: int = 20,
        note_index: int = 0,
    ) -> FeedPage:
        if not 1 <= limit <= 50 or note_index < 0:
            raise ValueError("limit must be 1–50 and note_index must be nonnegative")
        if not category or len(category) > 128:
            raise ValueError("Category must contain 1–128 characters")
        response = await self.post(
            "/api/sns/web/v1/homefeed",
            {
                "cursor_score": cursor,
                "num": limit,
                "refresh_type": 3 if cursor else 1,
                "note_index": note_index,
                "unread_begin_note_id": "",
                "unread_end_note_id": "",
                "unread_note_count": 0,
                "category": category,
                "search_key": "",
                "need_num": limit,
                "image_formats": ["jpg", "webp", "avif"],
                "need_filter_image": False,
            },
        )
        return parse_data(response, FeedPage)

    async def iter_recommendations(
        self,
        *,
        pages: int = 1,
        limit: int = 20,
        category: str = "homefeed_recommend",
    ) -> AsyncIterator[FeedItem]:
        if not 1 <= pages <= 100:
            raise ValueError("pages must be 1–100")
        seen: set[str] = set()
        cursor, index = "", 0
        for _ in range(pages):
            result = await self.get_recommendations(
                category=category, cursor=cursor, limit=limit, note_index=index
            )
            fresh = False
            for item in result.items:
                if item.id not in seen:
                    seen.add(item.id)
                    if (
                        not item.ignore
                        and item.model_type == "note"
                        and item.note_card is not None
                    ):
                        fresh = True
                        yield item
            if not fresh or not result.cursor_score or result.cursor_score == cursor:
                return
            cursor = result.cursor_score
            index += len(result.items)

    async def get_user_notes(
        self,
        user_id: str,
        *,
        cursor: str = "",
        limit: int = 30,
        xsec_token: str = "",
        xsec_source: str = "pc_user",
    ) -> UserNotePage:
        self._validate_identifier(user_id)
        if not 1 <= limit <= 30:
            raise ValueError("limit must be 1–30")
        response = await self._get(
            "/api/sns/web/v1/user_posted",
            {
                "num": limit,
                "cursor": cursor,
                "user_id": user_id,
                "image_formats": "jpg,webp,avif",
                "xsec_token": xsec_token,
                "xsec_source": xsec_source,
            },
        )
        return parse_data(response, UserNotePage)

    async def iter_user_notes(
        self,
        user_id: str,
        *,
        pages: int = 1,
        limit: int = 30,
        cursor: str = "",
        xsec_token: str = "",
        xsec_source: str = "pc_user",
    ) -> AsyncIterator[UserNote]:
        if not 1 <= pages <= 100:
            raise ValueError("pages must be 1–100")
        seen: set[str] = set()
        cursors = {cursor}
        for _ in range(pages):
            result = await self.get_user_notes(
                user_id,
                cursor=cursor,
                limit=limit,
                xsec_token=xsec_token,
                xsec_source=xsec_source,
            )
            for item in result.notes:
                if item.note_id not in seen:
                    seen.add(item.note_id)
                    yield item
            if (
                not result.has_more
                or not result.notes
                or not result.cursor
                or result.cursor in cursors
            ):
                return
            cursor = result.cursor
            cursors.add(cursor)

    async def get_note(self, note: NoteRef) -> Note:
        response = await self.post(
            "/api/sns/web/v1/feed",
            {
                "source_note_id": note.note_id,
                "image_formats": ["jpg", "webp", "avif"],
                "extra": {"need_body_topic": "1"},
                "xsec_source": note.xsec_source,
                "xsec_token": note.xsec_token,
            },
        )
        result = parse_data(response, FeedPage)
        for item in result.items:
            if (
                item.id == note.note_id
                and not item.ignore
                and item.note_card is not None
            ):
                return Note(ref=note, card=item.note_card)
        raise ValueError("Requested note was not available in the response")

    async def get_comments(
        self,
        note: NoteRef,
        *,
        cursor: str = "",
        root_id: str | None = None,
    ) -> CommentPage:
        params: dict[str, QueryValue] = {"note_id": note.note_id}
        path = "/api/sns/web/v2/comment/page"
        if root_id is not None:
            self._validate_identifier(root_id)
            path = "/api/sns/web/v2/comment/sub/page"
            params.update(root_comment_id=root_id, num="10")
        params.update(
            cursor=cursor,
            top_comment_id="",
            image_formats="jpg,webp,avif",
            xsec_token=note.xsec_token,
        )
        return parse_data(await self._get(path, params), CommentPage)

    async def iter_comments(
        self,
        note: NoteRef,
        *,
        root_id: str | None = None,
        pages: int = 10,
    ) -> AsyncIterator[Comment]:
        if not 1 <= pages <= 100:
            raise ValueError("pages must be 1–100")
        seen: set[str] = set()
        cursors: set[str] = set()
        cursor = ""
        for _ in range(pages):
            result = await self.get_comments(note, cursor=cursor, root_id=root_id)
            for comment in result.comments:
                if comment.id not in seen:
                    seen.add(comment.id)
                    yield comment
            if not result.has_more:
                return
            if not result.cursor or result.cursor in cursors or result.cursor == cursor:
                raise ValueError("Comment pagination did not advance")
            cursors.add(cursor)
            cursor = result.cursor

    @staticmethod
    def _validate_identifier(identifier: str) -> None:
        if not identifier or not identifier.isascii() or not identifier.isalnum():
            raise ValueError("Expected an ASCII alphanumeric identifier")

    async def set_note_like(self, note_id: str, enabled: bool) -> NoteAction:
        self._validate_identifier(note_id)
        path = (
            "/api/sns/web/v1/note/like" if enabled else "/api/sns/web/v1/note/dislike"
        )
        await self.post(path, {"note_oid": note_id})
        return NoteAction(note_id=note_id, action="like", enabled=enabled)

    async def set_note_collect(self, note_id: str, enabled: bool) -> NoteAction:
        self._validate_identifier(note_id)
        path = (
            "/api/sns/web/v1/note/collect"
            if enabled
            else "/api/sns/web/v1/note/uncollect"
        )
        await self.post(path, {"note_id" if enabled else "note_ids": note_id})
        return NoteAction(note_id=note_id, action="collect", enabled=enabled)

    async def post_comment(
        self, note_id: str, content: str, *, reply_to: str | None = None
    ) -> Comment:
        self._validate_identifier(note_id)
        if not content.strip() or len(content.encode()) > 16 * 1024:
            raise ValueError("Comment must be nonempty and at most 16 KiB")
        payload: dict[str, JsonValue] = {
            "note_id": note_id,
            "content": content,
            "at_users": [],
        }
        if reply_to is not None:
            self._validate_identifier(reply_to)
            payload["target_comment_id"] = reply_to
        response = await self.post("/api/sns/web/v1/comment/post", payload)
        return parse_data(response, CommentCreated).comment

    async def delete_comment(self, note_id: str, comment_id: str) -> None:
        self._validate_identifier(note_id)
        self._validate_identifier(comment_id)
        await self.post(
            "/api/sns/web/v1/comment/delete",
            {"note_id": note_id, "comment_id": comment_id},
        )

    async def follow_user(self, user_id: str) -> None:
        self._validate_identifier(user_id)
        await self.post("/api/sns/web/v1/user/follow", {"target_user_id": user_id})

    async def unfollow_user(self, user_id: str) -> None:
        self._validate_identifier(user_id)
        await self.post("/api/sns/web/v1/user/unfollow", {"target_user_id": user_id})
