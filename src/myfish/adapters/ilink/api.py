import base64
import os
import struct
import urllib.parse
from uuid import uuid4
from typing import Any

import httpx

from myfish.core.adapter import BaseAPI
from myfish.core.logger import logger

from .crypto import decrypt_aes_ecb, generate_aes_key, get_md5, encrypt_aes_ecb

CHANNEL_VERSION = "1.0.2"
ILINK_APP_ID = "bot"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"


def _build_client_version() -> str:
    parts = CHANNEL_VERSION.split(".")
    major = int(parts[0]) & 0xFF if len(parts) > 0 else 0
    minor = int(parts[1]) & 0xFF if len(parts) > 1 else 0
    patch = int(parts[2]) & 0xFF if len(parts) > 2 else 0
    return str((major << 16) | (minor << 8) | patch)


ILINK_APP_CLIENT_VERSION = _build_client_version()


class ILinkAPI(BaseAPI):
    """
    提供微信 iLink 协议的原生业务逻辑与 API 调用
    """

    def __init__(self):
        self.base_url = "https://ilinkai.weixin.qq.com"
        self.bot_token: str = ""
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0))
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _get_headers(self) -> dict[str, str]:
        val = struct.unpack(">I", os.urandom(4))[0]
        uin_b64 = base64.b64encode(str(val).encode("utf-8")).decode("ascii")

        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": uin_b64,
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
        }
        if self.bot_token:
            headers["Authorization"] = f"Bearer {self.bot_token}"
        return headers

    def _inject_base_info(self, json_data: dict[str, Any] | None) -> dict[str, Any]:
        payload = json_data or {}
        payload["base_info"] = {"channel_version": CHANNEL_VERSION}
        return payload

    async def _post(
        self,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
        custom_base: str | None = None,
    ) -> dict:
        base = custom_base if custom_base else self.base_url
        url = f"{base.rstrip('/')}/ilink/bot/{endpoint.lstrip('/')}"

        payload = self._inject_base_info(json_data)
        response = await self.client.post(
            url, json=payload, headers=self._get_headers()
        )
        response.raise_for_status()
        return response.json()

    async def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        custom_base: str | None = None,
    ) -> dict:
        base = custom_base if custom_base else self.base_url
        url = f"{base.rstrip('/')}/ilink/bot/{endpoint.lstrip('/')}"
        response = await self.client.get(
            url, params=params, headers=self._get_headers()
        )
        response.raise_for_status()
        return response.json()

    async def get_bot_qrcode(self, custom_base: str | None = None) -> dict:
        return await self._get("get_bot_qrcode", {"bot_type": "3"}, custom_base)

    async def get_qrcode_status(
        self, qrcode_id: str, custom_base: str | None = None
    ) -> dict:
        return await self._get("get_qrcode_status", {"qrcode": qrcode_id}, custom_base)

    async def get_updates(self, cursor: str) -> dict:
        return await self._post("getupdates", {"get_updates_buf": cursor})

    async def send_message(
        self, target_id: str, item_list: list[dict], context_token: str
    ) -> dict:
        """原生发送消息接口"""
        payload = {
            "msg": {
                "to_user_id": target_id,
                "client_id": str(uuid4()),
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": item_list,
            }
        }
        return await self._post("sendmessage", payload)

    async def get_config(self, target_id: str, context_token: str) -> dict:
        """
        获取当前会话的配置信息 (包含用于发送正在输入的 typing_ticket)
        """
        payload = {
            "ilink_user_id": target_id,
            "context_token": context_token,
        }
        return await self._post("getconfig", payload)

    async def send_typing(self, target_id: str, ticket: str, status: int = 1) -> dict:
        """发送 '对方正在输入...' 状态 (供业务插件调用)"""
        payload = {
            "ilink_user_id": target_id,
            "typing_ticket": ticket,
            "status": status,
        }
        return await self._post("sendtyping", payload)

    async def upload_media(
        self, file_url_or_path: str, to_user_id: str, media_type: int = 2
    ) -> dict:
        """
        上传媒体文件到微信 CDN，并返回必要的参数供发消息接口使用
        """
        if file_url_or_path.startswith(("http://", "https://")):
            resp = await self.client.get(file_url_or_path)
            resp.raise_for_status()
            raw_data = resp.content
        else:
            with open(file_url_or_path, "rb") as f:
                raw_data = f.read()

        raw_size = len(raw_data)
        raw_md5 = get_md5(raw_data)
        aes_key = generate_aes_key()
        aes_key_b64 = base64.b64encode(aes_key).decode("utf-8")

        ciphertext = encrypt_aes_ecb(raw_data, aes_key)
        file_size = len(ciphertext)
        file_key = str(uuid4())

        upload_req = await self._post(
            "getuploadurl",
            {
                "filekey": file_key,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": raw_size,
                "rawfilemd5": raw_md5,
                "filesize": file_size,
                "no_need_thumb": True,
                "aeskey": aes_key_b64,
            },
        )
        upload_param = (
            upload_req.get("encrypted_query_param")
            or upload_req.get("upload_url", "").split("=")[-1]
        )

        final_cdn_url = f"{CDN_BASE_URL}/upload?encrypted_query_param={urllib.parse.quote(upload_param, safe='')}&filekey={urllib.parse.quote(file_key, safe='')}"
        cdn_resp = await self.client.post(
            final_cdn_url,
            content=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
        )
        cdn_resp.raise_for_status()

        return {
            "cdn_url": final_cdn_url.split("?")[0],
            "aes_key": aes_key_b64,
            "cdn_param": cdn_resp.headers.get("x-encrypted-param", ""),
        }

    async def download_media(self, url: str, aes_key_str: str = "") -> bytes:
        """
        下载并解密 CDN 媒体资源。
        :param url: 文件的直接或加密下载地址
        :param aes_key_str: 微信下发的密钥字符串 (可能是普通 Base64，也可能是 Hex-String 的 Base64)
        """
        resp = await self.client.get(url)
        resp.raise_for_status()
        raw_data = resp.content

        if not aes_key_str:
            return raw_data

        try:
            decoded_key = base64.b64decode(aes_key_str)

            if len(decoded_key) == 32:
                try:
                    real_aes_key = bytes.fromhex(decoded_key.decode("ascii"))
                except ValueError:
                    real_aes_key = decoded_key
            else:
                real_aes_key = decoded_key

            return decrypt_aes_ecb(raw_data, real_aes_key)

        except Exception as e:
            logger.error(f"[ILink] 媒体解密失败 (AES_KEY={aes_key_str}): {e}")
            return raw_data
