from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Literal, Self, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, JsonValue, SecretStr, ValidationError

from myfish.core.adapter import BaseAPI
from myfish.core.logger import logger

from .model import (
    Account,
    ApiResponse,
    Comment,
    CommentPage,
    CommentReceipt,
    CommentRef,
    CommentReplies,
    CookieInfo,
    CookieRefresh,
    DeviceCookies,
    FansPage,
    FavoriteFolders,
    FavoritePage,
    FavoriteStatus,
    LikeNotificationPage,
    LiveConnection,
    LiveRoom,
    LiveSearchPage,
    LiveSendReceipt,
    LoginQRCode,
    LoginStatus,
    MentionNotificationPage,
    MessagePage,
    PlayUrls,
    ReplyNotificationPage,
    SearchKind,
    SearchOrder,
    SearchPage,
    SearchQuery,
    SendReceipt,
    SessionPage,
    Subtitle,
    SubtitleList,
    SubtitleTrack,
    UpVideoPage,
    User,
    UserCard,
    UserRelation,
    VideoDetails,
    WbiConfig,
)
from .sign import sign_wbi

_API = "https://api.bilibili.com"
_LIVE = "https://api.live.bilibili.com"
_IM = "https://api.vc.bilibili.com"
_LOGIN = "https://passport.bilibili.com/x/passport-login/web/qrcode/"
_AUTH_HEADERS = {
    "origin": "https://www.bilibili.com",
    "referer": "https://www.bilibili.com/",
}
_MEDIA_HEADERS = {"referer": "https://www.bilibili.com/", "user-agent": "Mozilla/5.0"}
_LIVE_HEADERS = {
    "referer": "https://live.bilibili.com/",
    "origin": "https://live.bilibili.com",
}
_SUBTITLE_LIMIT = 10 * 1024 * 1024
_Model = TypeVar("_Model", bound=BaseModel)
_COMMENT_ERRORS = {
    -509: "评论操作过于频繁",
    12002: "评论区已关闭",
    12003: "禁止回复",
    12004: "评论点赞操作被限制",
    12006: "目标评论不存在",
    12015: "评论需要验证码验证",
    12016: "评论内容被拒绝",
    12035: "账号被该评论区限制",
    12051: "重复评论被拒绝",
    12052: "评论区已关闭",
}
_INTERACTION_ERRORS = {
    10031: "弹幕发送过于频繁",
    1003212: "弹幕超过长度限制",
    -403: "账号异常或请求被拒绝",
    -352: "请求需要安全验证",
    -412: "请求被平台拦截",
    -509: "操作过于频繁",
    10003: "视频不存在",
    65004: "取消点赞失败",
    65006: "视频已点赞",
    22001: "不能关注自己",
    22002: "对方隐私设置不允许关注",
    22003: "关注失败，请先移除黑名单",
    22009: "已达到关注上限",
    22013: "账号已注销",
    22014: "已关注该用户",
    40061: "用户不存在",
}


class BilibiliAPIError(RuntimeError):
    def __init__(self, *, status: int, code: int | None = None) -> None:
        self.status = status
        self.code = code
        super().__init__(f"Bilibili request failed: HTTP {status}, code={code}")


class BilibiliProtocolError(RuntimeError):
    pass


def _validation_details(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in issue['loc']) or '<root>'}: {issue['type']}"
        for issue in error.errors(include_input=False, include_url=False)[:3]
    )


class BilibiliAPI(BaseAPI):
    def __init__(
        self,
        cookies: Mapping[str, str] | None = None,
        *,
        timeout: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={
                "user-agent": "Mozilla/5.0",
                "referer": "https://message.bilibili.com/",
                "origin": "https://message.bilibili.com",
            },
        )
        self._media_client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers=_MEDIA_HEADERS,
        )
        for name, value in (cookies or {}).items():
            self._client.cookies.set(name, value, domain=".bilibili.com", path="/")
        self.account: Account | None = None
        self.refresh_token: SecretStr | None = None
        self.confirmation_token: SecretStr | None = None
        self._transport = transport
        self.device_id = str(uuid.uuid4())
        self._wbi: tuple[str, str] | None = None
        self._wbi_loaded = 0.0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            await self._client.aclose()
        finally:
            await self._media_client.aclose()

    def cookie_values(self) -> dict[str, str]:
        return {
            cookie.name: cookie.value
            for cookie in self._client.cookies.jar
            if cookie.value is not None
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        data: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonValue:
        response = await self._client.request(
            method, url, params=params, data=data, headers=headers
        )
        if response.status_code != 200:
            raise BilibiliAPIError(status=response.status_code)
        try:
            result = ApiResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise BilibiliProtocolError(
                f"Invalid API response at {urlsplit(url).path}: {_validation_details(exc)}"
            ) from None
        if result.code != 0:
            path = urlsplit(url).path
            if path.startswith("/x/v2/reply") and result.code in _COMMENT_ERRORS:
                logger.warning(
                    "[Bilibili] 评论请求失败: code={}, {}",
                    result.code,
                    _COMMENT_ERRORS[result.code],
                )
            elif (
                path
                in (
                    "/x/web-interface/wbi/search/type",
                    "/x/web-interface/archive/like",
                    "/x/v3/fav/resource/deal",
                    "/x/relation/modify",
                    "/msg/send",
                )
                and result.code in _INTERACTION_ERRORS
            ):
                logger.warning(
                    "[Bilibili] 请求失败: code={}, {}",
                    result.code,
                    _INTERACTION_ERRORS[result.code],
                )
            raise BilibiliAPIError(status=response.status_code, code=result.code)
        return result.data

    @staticmethod
    def _parse(value: JsonValue, model: type[_Model]) -> _Model:
        try:
            return model.model_validate(value)
        except ValidationError as exc:
            raise BilibiliProtocolError(
                f"Invalid {model.__name__} response: {_validation_details(exc)}"
            ) from None

    async def get_self_info(self) -> Account:
        data = await self._request("GET", _API + "/x/web-interface/nav")
        account = self._parse(data, Account)
        if not account.is_login:
            raise BilibiliAPIError(status=200, code=-101)
        self.account = account
        return account

    async def get_login_qrcode(self) -> LoginQRCode:
        return self._parse(await self._request("GET", _LOGIN + "generate"), LoginQRCode)

    async def poll_login_qrcode(self, key: str) -> LoginStatus:
        result = self._parse(
            await self._request("GET", _LOGIN + "poll", params={"qrcode_key": key}),
            LoginStatus,
        )
        if result.code == 0:
            self.refresh_token = result.refresh_token
            self.confirmation_token = None
        return result

    def _csrf(self) -> str:
        token = self.cookie_values().get("bili_jct")
        if not token:
            raise ValueError("A CSRF token is required")
        return token

    async def get_cookie_info(self) -> CookieInfo:
        return self._parse(
            await self._request(
                "GET",
                "https://passport.bilibili.com/x/passport-login/web/cookie/info",
                params={"csrf": self._csrf()},
                headers=_AUTH_HEADERS,
            ),
            CookieInfo,
        )

    async def refresh_cookie(self, token: SecretStr, refresh_csrf: str) -> None:
        result = self._parse(
            await self._request(
                "POST",
                "https://passport.bilibili.com/x/passport-login/web/cookie/refresh",
                data={
                    "csrf": self._csrf(),
                    "refresh_csrf": refresh_csrf,
                    "source": "main_web",
                    "refresh_token": token.get_secret_value(),
                },
                headers=_AUTH_HEADERS,
            ),
            CookieRefresh,
        )
        if result.status != 0 or not result.refresh_token.get_secret_value():
            raise BilibiliProtocolError("Cookie refresh was not accepted")
        self.refresh_token = result.refresh_token

    async def confirm_cookie_refresh(self, token: SecretStr) -> None:
        await self._request(
            "POST",
            "https://passport.bilibili.com/x/passport-login/web/confirm/refresh",
            data={"csrf": self._csrf(), "refresh_token": token.get_secret_value()},
            headers=_AUTH_HEADERS,
        )

    async def get_reply_notifications(
        self, *, cursor_id: int = 0, reply_time: int = 0
    ) -> ReplyNotificationPage:
        if cursor_id < 0 or reply_time < 0:
            raise ValueError("Notification cursor must not be negative")
        params: dict[str, str | int] = {
            "build": 0,
            "mobi_app": "web",
            "platform": "web",
        }
        if cursor_id or reply_time:
            params.update({"id": cursor_id, "reply_time": reply_time})
        return self._parse(
            await self._request(
                "GET",
                _API + "/x/msgfeed/reply",
                params=params,
            ),
            ReplyNotificationPage,
        )

    async def get_mention_notifications(
        self, *, cursor_id: int = 0, at_time: int = 0
    ) -> MentionNotificationPage:
        if cursor_id < 0 or at_time < 0:
            raise ValueError("Notification cursor must not be negative")
        params: dict[str, str | int] = {
            "build": 0,
            "mobi_app": "web",
            "platform": "web",
        }
        if cursor_id or at_time:
            params.update({"id": cursor_id, "at_time": at_time})
        return self._parse(
            await self._request("GET", _API + "/x/msgfeed/at", params=params),
            MentionNotificationPage,
        )

    async def get_like_notifications(
        self, *, cursor_id: int = 0, like_time: int = 0
    ) -> LikeNotificationPage:
        if cursor_id < 0 or like_time < 0:
            raise ValueError("Notification cursor must not be negative")
        params: dict[str, str | int] = {
            "build": 0,
            "mobi_app": "web",
            "platform": "web",
        }
        if cursor_id or like_time:
            params.update({"id": cursor_id, "like_time": like_time})
        return self._parse(
            await self._request(
                "GET",
                _API + "/x/msgfeed/like",
                params=params,
            ),
            LikeNotificationPage,
        )

    async def get_new_sessions(self, begin_ts: int) -> SessionPage:
        if begin_ts < 0:
            raise ValueError("Session timestamp must not be negative")
        return self._parse(
            await self._request(
                "GET",
                _IM + "/session_svr/v1/session_svr/new_sessions",
                params={
                    "begin_ts": begin_ts,
                    "size": 100,
                    "build": 0,
                    "mobi_app": "web",
                },
            ),
            SessionPage,
        )

    async def get_sessions(
        self, *, begin_ts: int = 0, end_ts: int | None = None
    ) -> SessionPage:
        if begin_ts < 0 or (end_ts is not None and end_ts <= begin_ts):
            raise ValueError("Invalid session timestamp bounds")
        params: dict[str, str | int] = {
            "session_type": 1,
            "unfollow_fold": 0,
            "group_fold": 0,
            "sort_rule": 1,
            "size": 100,
            "begin_ts": begin_ts,
            "build": 0,
            "mobi_app": "web",
        }
        if end_ts is not None:
            params["end_ts"] = end_ts
        return self._parse(
            await self._request(
                "GET",
                _IM + "/session_svr/v1/session_svr/get_sessions",
                params=params,
            ),
            SessionPage,
        )

    async def get_messages(
        self,
        talker_id: int,
        *,
        begin_seqno: int,
        end_seqno: int | None = None,
    ) -> MessagePage:
        if (
            talker_id <= 0
            or begin_seqno < 0
            or (end_seqno is not None and end_seqno <= begin_seqno)
        ):
            raise ValueError("Invalid message sequence bounds")
        params: dict[str, str | int] = {
            "talker_id": talker_id,
            "session_type": 1,
            "size": 100,
            "begin_seqno": begin_seqno,
            "sender_device_id": 1,
            "build": 0,
            "mobi_app": "web",
        }
        if end_seqno is not None:
            params["end_seqno"] = end_seqno
        return self._parse(
            await self._request(
                "GET",
                _IM + "/svr_sync/v1/svr_sync/fetch_session_msgs",
                params=params,
            ),
            MessagePage,
        )

    async def send_text(self, receiver_id: int, text: str) -> SendReceipt:
        account = self.account
        csrf = self.cookie_values().get("bili_jct")
        if account is None or not csrf:
            raise ValueError("An authenticated account and CSRF token are required")
        if receiver_id <= 0 or not text.strip():
            raise ValueError("A positive receiver ID and nonempty text are required")
        data: dict[str, str | int] = {
            "msg[sender_uid]": account.mid,
            "msg[receiver_id]": receiver_id,
            "msg[receiver_type]": 1,
            "msg[msg_type]": 1,
            "msg[msg_status]": 0,
            "msg[dev_id]": self.device_id,
            "msg[timestamp]": int(time.time()),
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "msg[new_face_version]": 1,
            "csrf": csrf,
            "csrf_token": csrf,
            "build": 0,
            "mobi_app": "web",
            "from_firework": 0,
        }
        return self._parse(
            await self._request(
                "POST",
                _IM + "/web_im/v1/web_im/send_msg",
                data=data,
            ),
            SendReceipt,
        )

    async def search(
        self,
        keyword: str,
        *,
        kind: SearchKind = "video",
        page: int = 1,
        order: SearchOrder | None = None,
        duration: Literal[0, 1, 2, 3, 4] = 0,
        tids: int = 0,
        user_type: Literal[0, 1, 2, 3] = 0,
        order_sort: Literal[0, 1] = 0,
        category_id: int = 0,
    ) -> SearchPage | LiveSearchPage:
        query = SearchQuery(
            keyword=keyword,
            kind=kind,
            page=page,
            order=order,
            duration=duration,
            tids=tids,
            user_type=user_type,
            order_sort=order_sort,
            category_id=category_id,
        )
        await self._ensure_device()
        params: dict[str, str | int] = {
            "keyword": query.keyword,
            "search_type": query.kind,
            "page": query.page,
        }
        if query.order is not None:
            params["order"] = query.order
        if query.kind == "video":
            params.update(duration=query.duration, tids=query.tids)
        elif query.kind == "bili_user":
            params.update(user_type=query.user_type, order_sort=query.order_sort)
        elif query.kind in ("article", "photo"):
            params["category_id"] = query.category_id
        signed = await self._wbi_params(params)
        data = await self._request(
            "GET",
            _API + "/x/web-interface/wbi/search/type",
            params=signed,
            headers=_AUTH_HEADERS,
        )
        if kind == "live":
            return self._parse(data, LiveSearchPage)
        return self._parse(data, SearchPage)

    async def _ensure_device(self) -> None:
        if self.cookie_values().get("buvid3"):
            return
        cookies = self._parse(
            await self._request(
                "GET", _API + "/x/frontend/finger/spi", headers=_AUTH_HEADERS
            ),
            DeviceCookies,
        )
        self._client.cookies.set(
            "buvid3", cookies.buvid3, domain=".bilibili.com", path="/"
        )
        if cookies.buvid4:
            self._client.cookies.set(
                "buvid4", cookies.buvid4, domain=".bilibili.com", path="/"
            )

    async def get_up_videos(
        self, mid: int, *, page: int = 1, size: int = 30
    ) -> UpVideoPage:
        self._validate_page(mid, page, size)
        params = await self._wbi_params({"mid": mid, "pn": page, "ps": size})
        return self._parse(
            await self._request("GET", _API + "/x/space/wbi/arc/search", params=params),
            UpVideoPage,
        )

    async def _wbi_params(self, params: Mapping[str, str | int]) -> dict[str, str]:
        if self._wbi is None or time.monotonic() - self._wbi_loaded >= 3600:
            data = await self._request("GET", _API + "/x/web-interface/nav")
            images = self._parse(data, WbiConfig).wbi_img
            img_key, sub_key = (
                urlsplit(url).path.rsplit("/", 1)[-1].split(".")[0]
                for url in (images.img_url, images.sub_url)
            )
            if len(img_key) != 32 or len(sub_key) != 32:
                raise BilibiliProtocolError("Invalid WBI keys")
            self._wbi = img_key, sub_key
            self._wbi_loaded = time.monotonic()
        return sign_wbi(params, *self._wbi, int(time.time()))

    async def get_comments(
        self,
        ref: CommentRef,
        *,
        offset: str = "",
        sort: Literal["time", "hot"] = "time",
    ) -> CommentPage:
        if sort not in ("time", "hot"):
            raise ValueError("Unsupported comment sort order")
        params = await self._wbi_params(
            {
                "oid": ref.oid,
                "type": ref.kind,
                "mode": 2 if sort == "time" else 3,
                "pagination_str": json.dumps({"offset": offset}, separators=(",", ":")),
                "plat": 1,
                "web_location": "1315875",
            }
        )
        return self._parse(
            await self._request(
                "GET",
                _API + "/x/v2/reply/wbi/main",
                params=params,
                headers=_AUTH_HEADERS,
            ),
            CommentPage,
        )

    async def iter_comments(
        self, ref: CommentRef, *, sort: Literal["time", "hot"] = "time"
    ) -> AsyncIterator[Comment]:
        offset = ""
        offsets: set[str] = {offset}
        seen: set[int] = set()
        for _ in range(1000):
            page = await self.get_comments(ref, offset=offset, sort=sort)
            for comment in page.replies or []:
                if comment.ref != ref:
                    raise BilibiliProtocolError(
                        "Comment does not match requested subject"
                    )
                if comment.rpid not in seen:
                    seen.add(comment.rpid)
                    yield comment
            if page.cursor.is_end:
                return
            next_offset = page.next_offset
            if not next_offset or next_offset in offsets:
                raise BilibiliProtocolError("Comment pagination did not advance")
            offsets.add(next_offset)
            offset = next_offset
        raise BilibiliProtocolError("Comment pagination limit exceeded")

    async def get_comment_replies(
        self, ref: CommentRef, root: int, *, page: int = 1, size: int = 20
    ) -> CommentReplies:
        self._validate_page(root, page, size)
        if size > 20:
            raise ValueError("Comment reply page size must not exceed 20")
        return self._parse(
            await self._request(
                "GET",
                _API + "/x/v2/reply/reply",
                params={
                    "oid": ref.oid,
                    "type": ref.kind,
                    "root": root,
                    "pn": page,
                    "ps": size,
                },
                headers=_AUTH_HEADERS,
            ),
            CommentReplies,
        )

    async def send_comment(
        self, ref: CommentRef, text: str, *, root: int = 0, parent: int = 0
    ) -> CommentReceipt:
        if not text.strip() or len(text) > 1000:
            raise ValueError("Comment text must contain 1–1000 characters")
        if root < 0 or parent < 0 or (not root and parent):
            raise ValueError("A parent comment requires a root comment")
        data = await self._request(
            "POST",
            _API + "/x/v2/reply/add",
            data={
                "oid": ref.oid,
                "type": ref.kind,
                "root": root,
                "parent": parent or root,
                "message": text,
                "plat": 1,
                "csrf": self._csrf(),
            },
            headers=_AUTH_HEADERS,
        )
        if isinstance(data, dict) and data.get("need_captcha") is True:
            logger.warning("[Bilibili] 评论需要验证码验证")
            raise BilibiliAPIError(status=200, code=12015)
        receipt = self._parse(data, CommentReceipt)
        logger.info(
            "[Bilibili][评论发送] type={}, oid={}, rpid={}",
            ref.kind,
            ref.oid,
            receipt.rpid,
        )
        return receipt

    async def reply_comment(self, comment: Comment, text: str) -> CommentReceipt:
        return await self.send_comment(
            comment.ref, text, root=comment.root or comment.rpid, parent=comment.rpid
        )

    async def like_comment(
        self, ref: CommentRef, rpid: int, *, liked: bool = True
    ) -> None:
        if rpid <= 0:
            raise ValueError("Comment ID must be positive")
        await self._request(
            "POST",
            _API + "/x/v2/reply/action",
            data={
                "oid": ref.oid,
                "type": ref.kind,
                "rpid": rpid,
                "action": int(liked),
                "csrf": self._csrf(),
            },
            headers=_AUTH_HEADERS,
        )
        logger.info(
            "[Bilibili][评论点赞] oid={}, rpid={}, liked={}", ref.oid, rpid, liked
        )

    @staticmethod
    def _validate_page(identifier: int, page: int, size: int) -> None:
        if identifier <= 0 or page <= 0 or not 1 <= size <= 50:
            raise ValueError("Invalid identifier or pagination parameters")

    async def get_user_relation(self, mid: int) -> UserRelation:
        self._validate_page(mid, 1, 1)
        data = await self._request("GET", _API + "/x/relation", params={"fid": mid})
        if (
            isinstance(data, dict)
            and data.get("mid") == 0
            and data.get("attribute") == 0
        ):
            data = {**data, "mid": mid}
        relation = self._parse(data, UserRelation)
        if relation.mid != mid:
            raise BilibiliProtocolError("Relation does not match requested user")
        return relation

    async def follow_user(self, mid: int, *, followed: bool = True) -> None:
        self._validate_page(mid, 1, 1)
        await self._request(
            "POST",
            _API + "/x/relation/modify",
            data={
                "fid": mid,
                "act": 1 if followed else 2,
                "re_src": 11,
                "csrf": self._csrf(),
            },
            headers=_AUTH_HEADERS,
        )
        logger.info("[Bilibili][关注] mid={}, followed={}", mid, followed)

    async def get_user_info(self, mid: int) -> User:
        self._validate_page(mid, 1, 1)
        return self._parse(
            await self._request(
                "GET",
                _API + "/x/web-interface/card",
                params={"mid": mid},
            ),
            UserCard,
        ).card

    async def get_fans(self, mid: int, *, page: int = 1, size: int = 50) -> FansPage:
        self._validate_page(mid, page, size)
        return self._parse(
            await self._request(
                "GET",
                _API + "/x/relation/fans",
                params={"vmid": mid, "pn": page, "ps": size},
            ),
            FansPage,
        )

    async def get_favorite_folders(self, mid: int) -> FavoriteFolders:
        self._validate_page(mid, 1, 1)
        return self._parse(
            await self._request(
                "GET",
                _API + "/x/v3/fav/folder/created/list-all",
                params={"up_mid": mid},
            ),
            FavoriteFolders,
        )

    async def get_favorite_videos(
        self, folder_id: int, *, page: int = 1, size: int = 20
    ) -> FavoritePage:
        self._validate_page(folder_id, page, size)
        return self._parse(
            await self._request(
                "GET",
                _API + "/x/v3/fav/resource/list",
                params={"media_id": folder_id, "pn": page, "ps": size},
            ),
            FavoritePage,
        )

    async def like_video(self, bvid: str, *, liked: bool = True) -> None:
        if not bvid.strip():
            raise ValueError("Video ID is required")
        csrf = self._csrf()
        await self._ensure_device()
        await self._request(
            "POST",
            _API + "/x/web-interface/archive/like",
            data={"bvid": bvid, "like": 1 if liked else 2, "csrf": csrf},
            headers=_AUTH_HEADERS,
        )
        logger.info("[Bilibili][视频点赞] bvid={}, liked={}", bvid, liked)

    async def get_video_like_status(self, bvid: str) -> bool:
        if not bvid.strip():
            raise ValueError("Video ID is required")
        data = await self._request(
            "GET",
            _API + "/x/web-interface/archive/has/like",
            params={"bvid": bvid},
        )
        if type(data) is not int or data not in (0, 1):
            raise BilibiliProtocolError("Invalid video like status")
        return data == 1

    async def favorite_video(
        self, aid: int, folder_ids: Sequence[int], *, favorited: bool = True
    ) -> None:
        if (
            aid <= 0
            or not folder_ids
            or any(folder_id <= 0 for folder_id in folder_ids)
        ):
            raise ValueError("Video and favorite folder IDs must be positive")
        folders = ",".join(str(folder_id) for folder_id in dict.fromkeys(folder_ids))
        await self._request(
            "POST",
            _API + "/x/v3/fav/resource/deal",
            data={
                "rid": aid,
                "type": 2,
                "platform": "web",
                "add_media_ids": folders if favorited else "",
                "del_media_ids": "" if favorited else folders,
                "csrf": self._csrf(),
            },
            headers=_AUTH_HEADERS,
        )
        logger.info(
            "[Bilibili][视频收藏] aid={}, folders={}, favorited={}",
            aid,
            folders,
            favorited,
        )

    async def get_video_favorite_status(self, aid: int) -> FavoriteStatus:
        if aid <= 0:
            raise ValueError("Video ID must be positive")
        return self._parse(
            await self._request(
                "GET", _API + "/x/v2/fav/video/favoured", params={"aid": aid}
            ),
            FavoriteStatus,
        )

    async def get_live_room(self, room_id: int) -> LiveRoom:
        if room_id <= 0:
            raise ValueError("Live room ID must be positive")
        return self._parse(
            await self._request(
                "GET",
                _LIVE + "/room/v1/Room/room_init",
                params={"id": room_id},
                headers=_LIVE_HEADERS,
            ),
            LiveRoom,
        )

    async def get_live_connection(self, room_id: int) -> LiveConnection:
        if room_id <= 0:
            raise ValueError("Live room ID must be positive")
        await self._ensure_device()
        params = await self._wbi_params({"id": room_id, "type": 0})
        return self._parse(
            await self._request(
                "GET",
                _LIVE + "/xlive/web-room/v1/index/getDanmuInfo",
                params=params,
                headers=_LIVE_HEADERS,
            ),
            LiveConnection,
        )

    async def send_live_danmaku(
        self,
        room_id: int,
        text: str,
        *,
        color: int = 0xFFFFFF,
        mode: Literal[1, 4, 5] = 1,
    ) -> LiveSendReceipt:
        if (
            room_id <= 0
            or not text.strip()
            or not 0 <= color <= 0xFFFFFF
            or mode not in (1, 4, 5)
        ):
            raise ValueError("Invalid live danmaku parameters")
        csrf = self._csrf()
        receipt = self._parse(
            await self._request(
                "POST",
                _LIVE + "/msg/send",
                data={
                    "roomid": room_id,
                    "msg": text,
                    "rnd": int(time.time()),
                    "fontsize": 25,
                    "color": color,
                    "mode": mode,
                    "bubble": 0,
                    "csrf": csrf,
                    "csrf_token": csrf,
                },
                headers=_LIVE_HEADERS,
            ),
            LiveSendReceipt,
        )
        logger.info("[Bilibili][发送弹幕] room={}: {}", room_id, text)
        return receipt

    async def get_video_info(self, bvid: str) -> VideoDetails:
        if not bvid.strip():
            raise ValueError("Video ID is required")
        return self._parse(
            await self._request(
                "GET",
                _API + "/x/web-interface/view",
                params={"bvid": bvid},
            ),
            VideoDetails,
        )

    async def get_subtitles(self, bvid: str, cid: int) -> SubtitleList:
        if not bvid.strip() or cid <= 0:
            raise ValueError("Invalid video or part ID")
        params = await self._wbi_params({"bvid": bvid, "cid": cid})
        result = self._parse(
            await self._request(
                "GET", _API + "/x/player/wbi/v2", params=params, headers=_AUTH_HEADERS
            ),
            SubtitleList,
        )
        if result.bvid != bvid or result.cid != cid:
            raise BilibiliProtocolError(
                "Subtitle list does not match requested video part"
            )
        return result

    async def get_subtitle(self, track: SubtitleTrack) -> Subtitle:
        parsed = urlsplit(track.subtitle_url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or not any(
                host == domain or host.endswith("." + domain)
                for domain in ("hdslb.com", "bilibili.com")
            )
        ):
            raise ValueError("Unsupported subtitle host")
        async with self._media_client.stream("GET", track.subtitle_url) as response:
            if response.status_code != 200:
                raise BilibiliAPIError(status=response.status_code)
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > _SUBTITLE_LIMIT:
                    raise BilibiliProtocolError("Subtitle exceeds size limit")
                payload.extend(chunk)
        try:
            return Subtitle.model_validate_json(payload)
        except ValidationError as exc:
            raise BilibiliProtocolError(
                f"Invalid subtitle response: {_validation_details(exc)}"
            ) from None

    async def get_play_urls(
        self,
        bvid: str,
        cid: int,
        *,
        quality: int = 64,
        stream_format: Literal["mp4", "dash"] = "mp4",
    ) -> PlayUrls:
        if (
            not bvid.strip()
            or cid <= 0
            or quality <= 0
            or stream_format not in ("mp4", "dash")
        ):
            raise ValueError("Invalid video or quality parameters")
        params = await self._wbi_params(
            {
                "bvid": bvid,
                "cid": cid,
                "qn": quality,
                "fnval": 16 if stream_format == "dash" else 1,
                "fnver": 0,
                "fourk": 0,
            }
        )
        result = self._parse(
            await self._request(
                "GET",
                _API + "/x/player/wbi/playurl",
                params=params,
                headers=_AUTH_HEADERS,
            ),
            PlayUrls,
        )
        result.headers = dict(_MEDIA_HEADERS)
        return result
