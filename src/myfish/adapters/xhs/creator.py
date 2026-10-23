from __future__ import annotations

import asyncio
import hashlib
import struct
from collections.abc import AsyncIterator
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Literal, Self
from urllib.parse import urlencode

import httpx
from PIL import Image
from pydantic import JsonValue, TypeAdapter, ValidationError

from myfish.core.adapter import BaseAPI
from myfish.core.logger import logger

from .api import XhsAPI, XhsAPIError, XhsVerificationRequired, parse_data
from .device import generate_telemetry
from .ds import CreatorDS
from .model import (
    CreatorAccount,
    CreatorResponse,
    SigningSession,
    SmsCodeSent,
    SmsReceiver,
)
from .publish import (
    ImageAsset,
    ImagePost,
    MediaSource,
    PublishResult,
    PublishUncertainError,
    TranscodeStatus,
    UploadPermit,
    UploadPermits,
    VideoAsset,
    VideoPost,
    media_id,
    post_payload,
)
from .rap import RapContext, build_payload, creator_fingerprint, sign_rap
from .sign import SigningContext, creator_headers, encode_b1, upload_authorization
from .utils import compact_json

_ORIGIN = "https://creator.xiaohongshu.com"
_PUBLISH_PAGE = _ORIGIN + "/publish/publish?source=official"
_EDITH = "https://edith.xiaohongshu.com"
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_SMS_PATH = "/api/redcaptcha/v2/vc/"
_SMS_ERRORS = {
    40002: "验证码已失效，请勿继续提交该验证码",
    40003: "验证码输入错误，请检查后手动提交",
    40012: "已达到今日短信发送上限，请停止发送并稍后通过官方渠道重试",
}


def _sms_payload(challenge: XhsVerificationRequired) -> dict[str, JsonValue]:
    if (
        challenge.verify_type != "201"
        or challenge.status not in (461, 471)
        or not challenge.verify_uuid
    ):
        raise ValueError("An SMS verification challenge is required")
    return {
        "verifyUuid": challenge.verify_uuid,
        "verifyType": challenge.verify_type,
        "verifyBiz": challenge.status,
        "sourceSite": "",
    }


def _image_info(
    source: MediaSource,
) -> tuple[int, int, Literal["image/jpeg", "image/png", "image/webp"]]:
    with Image.open(BytesIO(source) if isinstance(source, bytes) else source) as image:
        width, height = image.size
        formats: dict[str, Literal["image/jpeg", "image/png", "image/webp"]] = {
            "JPEG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
        }
        if image.format not in formats:
            raise ValueError("Only JPEG, PNG and WebP images are supported")
        mime_type = formats[image.format]
        image.verify()
    return width, height, mime_type


async def _chunks(stream: BinaryIO) -> AsyncIterator[bytes]:
    while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
        yield chunk


class CreatorAPI(BaseAPI):
    def __init__(
        self, account: XhsAPI, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not account.cookie_values().get("web_session"):
            raise ValueError("Creator requires an authenticated PC session")
        self.session = SigningSession()
        self.ds: CreatorDS | None = None
        self._ds_loaded_at = 0
        self.fingerprint = deepcopy(account.fingerprint)
        self.fingerprint.fields.update(
            {
                "x42": "3.5.6",
                "x44": str(self.session.loaded_at_ms),
                "x66": {"referer": "", "location": _PUBLISH_PAGE, "frame": 0},
                "x84": generate_telemetry(self.session.loaded_at_ms),
            }
        )
        user_agent = self.fingerprint.fields["x1"]
        if not isinstance(user_agent, str):
            raise TypeError("Creator user agent is missing")
        self._client = httpx.AsyncClient(
            cookies=httpx.Cookies(account._client.cookies),
            transport=transport,
            timeout=20,
            follow_redirects=False,
            headers={
                "user-agent": user_agent,
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "referer": _ORIGIN + "/",
            },
        )
        for name, value in {
            "webBuild": "1.26.0",
            "xsecappid": "ugc",
            "loadts": str(self.session.loaded_at_ms),
        }.items():
            self._client.cookies.delete(name)
            self._client.cookies.set(name, value, domain=".xiaohongshu.com", path="/")
        self._b1 = encode_b1(self.fingerprint.fields, current=True)
        self._upload_client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(120, connect=20),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            await self._client.aclose()
        finally:
            await self._upload_client.aclose()

    async def initialize(self) -> None:
        response = await self._client.get(
            _PUBLISH_PAGE,
            follow_redirects=True,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-site",
            },
        )
        if response.status_code != 200:
            raise XhsAPIError(status=response.status_code)
        await self.refresh_ds()

    async def refresh_ds(self) -> None:
        response = await self._client.get(
            "https://as.xiaohongshu.com/api/sec/v1/ds?appId=ugc"
        )
        if response.status_code != 200:
            raise XhsAPIError(status=response.status_code)
        ds = CreatorDS.from_script(response.text)
        self.ds = ds
        self._ds_loaded_at = self.session.clock()
        logger.debug("[XHS][Creator] DS 参数已更新")

    async def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        payload: dict[str, JsonValue] | None = None,
        origin: str = _ORIGIN,
        tier: Literal["0101", "0201"] = "0101",
        rap_fingerprint: bytes | None = None,
        referer: str | None = None,
        environment_size: int = 1337,
    ) -> httpx.Response:
        if tier == "0101" and (
            self.ds is None or self.session.clock() - self._ds_loaded_at >= 300_000
        ):
            await self.refresh_ds()
        self.session.sequence += 1
        now = self.session.clock()
        context = SigningContext(
            timestamp_ms=now,
            loaded_at_ms=self.session.loaded_at_ms,
            sequence=self.session.sequence,
            environment_size=1306 if tier == "0201" else environment_size,
            seed=self.session.random_bits(32),
            environment=bytes.fromhex(
                "f8536767c9b581635e0744fa8415"
                if tier == "0201"
                else "f9536767c9b581635e0744fa8415"
            ),
        )
        ds = self.ds
        ds_digest = (
            ds.digest(struct.pack("<Q", now) + hashlib.md5(path.encode()).digest())
            if ds is not None and tier == "0101"
            else None
        )
        body = compact_json(payload or {}) if method == "POST" else ""
        a1 = next(
            (
                cookie.value
                for cookie in self._client.cookies.jar
                if cookie.name == "a1"
            ),
            None,
        )
        if not a1:
            raise ValueError("Creator a1 cookie is missing")
        headers = creator_headers(
            path,
            body,
            self.fingerprint.fields,
            context,
            a1=a1,
            b1=self._b1,
            ds_anchor=f"{self._ds_loaded_at or self.session.loaded_at_ms};{ds.anchor if ds is not None else 'undefined'}",
            ds_digest=ds_digest,
            tier=tier,
            has_body=method == "POST",
        )
        headers.update(
            {
                "authorization": "",
                "priority": "u=1, i",
                "x-b3-traceid": f"{self.session.random_bits(64):016x}",
                "x-xray-traceid": f"{(now << 23) | self.session.random_bits(23):016x}{self.session.random_bits(64):016x}",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin" if origin == _ORIGIN else "same-site",
            }
        )
        if method == "POST":
            headers.update({"content-type": "application/json", "origin": _ORIGIN})
        if referer is not None:
            headers["referer"] = referer
        if rap_fingerprint is not None:
            headers["x-rap-param"] = sign_rap(
                origin + path, body.encode("utf-8"), rap_fingerprint, RapContext(now)
            )
        request = self._client.build_request(
            method,
            origin + path,
            headers=headers,
            content=body.encode() if method == "POST" else None,
        )
        if request.url.raw_path != path.encode():
            raise ValueError("HTTP URL normalization changed the Creator signature")
        response = await self._client.send(request)
        logger.debug(
            "[XHS][Creator] {} {} -> {}",
            method,
            path.split("?", 1)[0],
            response.status_code,
        )
        if response.status_code in (461, 471) and response.headers.get("verifyuuid"):
            challenge = XhsVerificationRequired(
                status=response.status_code,
                verify_uuid=response.headers["verifyuuid"],
                verify_type=response.headers.get("verifytype", ""),
            )
            logger.warning(
                "[XHS][Creator] 请求需要安全验证: path={}, HTTP={}, type={}, method={}",
                path.split("?", 1)[0],
                response.status_code,
                challenge.verify_type or "unknown",
                challenge.verification_method,
            )
            raise challenge
        if response.status_code != 200:
            raise XhsAPIError(status=response.status_code)
        try:
            result = CreatorResponse.model_validate_json(response.content)
        except ValidationError:
            raise XhsAPIError(status=response.status_code) from None
        is_sms = path.startswith(_SMS_PATH)
        if (
            not result.success
            or result.code not in (None, 0)
            or (is_sms and result.code != 0)
        ):
            if is_sms and result.code in _SMS_ERRORS:
                logger.warning(
                    "[XHS][Creator] 短信验证失败: code={}, {}",
                    result.code,
                    _SMS_ERRORS[result.code],
                )
            logger.debug(
                "[XHS][Creator] 业务请求被拒绝: path={}, code={}",
                path.split("?", 1)[0],
                result.code,
            )
            raise XhsAPIError(status=response.status_code, code=result.code)
        return response

    async def init_sms_verification(
        self, challenge: XhsVerificationRequired
    ) -> SmsReceiver:
        return parse_data(
            await self._request(
                "POST",
                _SMS_PATH + "init",
                payload=_sms_payload(challenge),
                origin=_EDITH,
            ),
            SmsReceiver,
        )

    async def send_sms_code(self, challenge: XhsVerificationRequired) -> SmsCodeSent:
        return parse_data(
            await self._request(
                "POST",
                _SMS_PATH + "send",
                payload=_sms_payload(challenge),
                origin=_EDITH,
            ),
            SmsCodeSent,
        )

    async def check_sms_code(
        self,
        challenge: XhsVerificationRequired,
        *,
        rid: str,
        code: str,
        check_count: int = 0,
    ) -> None:
        payload = _sms_payload(challenge)
        if not rid:
            raise ValueError("SMS request ID is required")
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError("SMS code must contain six ASCII digits")
        if isinstance(check_count, bool) or check_count < 0:
            raise ValueError("SMS check count must be a non-negative integer")
        payload.update({"rid": rid, "code": code, "checkCount": check_count})
        await self._request("POST", _SMS_PATH + "check", payload=payload, origin=_EDITH)

    async def get_self_info(self) -> CreatorAccount:
        return parse_data(
            await self._request("GET", "/api/galaxy/user/info"), CreatorAccount
        )

    async def get_upload_permit(self, scene: Literal["image", "video"]) -> UploadPermit:
        if scene not in ("image", "video"):
            raise ValueError("Unsupported upload scene")
        query = urlencode(
            {
                "biz_name": "spectrum",
                "scene": scene,
                "file_count": 1,
                "version": 1,
                "source": "web",
            }
        )
        response = await self._request(
            "GET",
            "/api/media/v1/upload/creator/permit?" + query,
            referer=_PUBLISH_PAGE,
            environment_size=1349,
        )
        permit = parse_data(response, UploadPermits).permits[0]
        return permit.model_copy(
            update={"signed_at_ms": int(response.request.headers["x-t"])}
        )

    async def _upload(
        self, source: MediaSource, scene: Literal["image", "video"]
    ) -> tuple[str, int, str]:
        stream: BinaryIO = (
            await asyncio.to_thread(source.open, "rb")
            if isinstance(source, Path)
            else BytesIO(source)
        )
        try:
            size = await asyncio.to_thread(stream.seek, 0, 2)
            if size <= 0:
                raise ValueError("Media file must not be empty")
            await asyncio.to_thread(stream.seek, 0)
            permit = await self.get_upload_permit(scene)
            address = permit.upload_addr
            url = httpx.URL(address if "://" in address else "https://" + address)
            if (
                url.scheme != "https"
                or url.username
                or url.password
                or url.path not in ("", "/")
                or url.query
                or url.fragment
                or not any(
                    url.host.endswith(suffix)
                    for suffix in (".xiaohongshu.com", ".xhscdn.com", ".myqcloud.com")
                )
            ):
                raise ValueError("Unsupported media upload address")
            identifier = media_id(permit.file_ids[0])
            target = url.copy_with(path="/" + identifier)
            if permit.expires_at <= self.session.clock() // 1000:
                raise ValueError("Media upload permit has expired")
            headers = {
                "authorization": upload_authorization(
                    target.path,
                    target.netloc.decode(),
                    size,
                    permit.signed_at_ms // 1000,
                    permit.expires_at,
                ),
                "content-length": str(size),
                "content-type": "",
                "x-cos-security-token": permit.token.get_secret_value(),
                "origin": _ORIGIN,
                "referer": _ORIGIN + "/",
                "user-agent": self._client.headers["user-agent"],
            }
            response = await self._upload_client.put(
                target, headers=headers, content=_chunks(stream)
            )
            if not 200 <= response.status_code < 300:
                raise XhsAPIError(status=response.status_code)
            video_id = response.headers.get("x-ros-video-id", "")
            if scene == "video" and not video_id:
                raise XhsAPIError(status=response.status_code)
            logger.info("[XHS][上传] {} 上传完成: {} bytes", scene, size)
            return identifier, size, video_id
        finally:
            await asyncio.to_thread(stream.close)

    async def upload_image(self, source: MediaSource) -> ImageAsset:
        width, height, mime_type = await asyncio.to_thread(_image_info, source)
        identifier, size, _ = await self._upload(source, "image")
        return ImageAsset(
            file_id=identifier,
            size=size,
            width=width,
            height=height,
            mime_type=mime_type,
        )

    async def upload_video(self, source: MediaSource) -> VideoAsset:
        identifier, size, video_id = await self._upload(source, "video")
        return VideoAsset(file_id=identifier, size=size, video_id=video_id)

    async def get_transcode(self, video_id: str) -> TranscodeStatus:
        if not video_id or len(video_id) > 256:
            raise ValueError("Invalid video identifier")
        query = urlencode(
            {"video_id": video_id, "need_transcode": "false", "resource_type": 0}
        )
        response = await self._request(
            "GET",
            "/web_api/sns/capa/postgw/query_transcode?" + query,
            origin=_EDITH,
            environment_size=1349,
        )
        return parse_data(response, TranscodeStatus)

    async def wait_for_transcode(
        self, video_id: str, *, timeout: float = 120, interval: float = 2
    ) -> TranscodeStatus:
        if timeout <= 0 or interval <= 0:
            raise ValueError("Transcode timeout and interval must be positive")
        async with asyncio.timeout(timeout):
            while True:
                status = await self.get_transcode(video_id)
                if status.ready:
                    return status
                await asyncio.sleep(interval)

    def get_rap_fingerprint(self) -> bytes:
        dimensions = self.fingerprint.fields.get("x10")
        if not isinstance(dimensions, str):
            raise TypeError("Creator device dimensions must be a string")
        parts = dimensions.split(";")
        if len(parts) != 2 or not all(
            part.isascii() and part.isdecimal() for part in parts
        ):
            raise ValueError("Creator device dimensions are invalid")
        return creator_fingerprint(
            loaded_at_ms=self.session.loaded_at_ms,
            viewport_width=int(parts[0]),
            viewport_height=int(parts[1]),
        )

    async def publish(
        self,
        post: ImagePost | VideoPost,
        *,
        rap_fingerprint: bytes | None = None,
        wait_for_video: bool = True,
        transcode_timeout: float = 120,
    ) -> PublishResult:
        payload = post_payload(post)
        path = "/web_api/sns/v2/note"
        now = self.session.clock()
        if rap_fingerprint is None:
            rap_fingerprint = self.get_rap_fingerprint()
        build_payload(
            _EDITH + path,
            compact_json(payload).encode(),
            rap_fingerprint,
            RapContext(now),
        )
        if post.scheduled_at_ms is not None and post.scheduled_at_ms <= now:
            raise ValueError("Scheduled publication must be in the future")
        if isinstance(post, VideoPost) and wait_for_video:
            await self.wait_for_transcode(
                post.video.video_id, timeout=transcode_timeout
            )
        try:
            response = await self._request(
                "POST",
                path,
                payload=payload,
                origin=_EDITH,
                rap_fingerprint=rap_fingerprint,
                environment_size=1351,
            )
        except httpx.TransportError as exc:
            logger.warning("[XHS][发布] 请求结果未知，请检查作品列表后再决定是否重试")
            raise PublishUncertainError from exc
        raw = _JSON_OBJECT.validate_json(response.content)
        data = raw.get("data")
        note_id = None
        if isinstance(data, dict):
            for key in ("noteId", "note_id", "id"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    note_id = value
                    break
        logger.info(
            "[XHS][发布] 请求已接受: type={}, note_id={}",
            "video" if isinstance(post, VideoPost) else "normal",
            note_id or "unknown",
        )
        return PublishResult(note_id=note_id, raw=raw)
