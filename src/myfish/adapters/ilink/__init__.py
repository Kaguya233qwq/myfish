import asyncio
import base64
import time
import qrcode
import httpx
from typing import Union
from myfish.core.bot import Bot
from myfish.core.logger import logger
from myfish.core.adapter import AdapterMetaData, BaseAdapter
from myfish.core.message import MessageChain, MessageSegment
from myfish.core.event import MessageEvent
from myfish.core.registry import AdapterRegistry

from .api import ILinkAPI
from .auth import load_credentials, save_credentials, clear_credentials
from .message import ILinkMessage, Typing, auto_encode_ilink


ClawBot = Bot["ILinkAdapter"]


@AdapterRegistry.register
class ILinkAdapter(BaseAdapter["ILinkAPI"]):
    """微信 ilink协议 适配器"""

    meta_data = AdapterMetaData(
        id="ilink",
        name="ILink适配器",
        description="基于微信ClawBot的iLink协议的适配器",
        author="Kaguya233qwq",
        version="1.0.0",
    )

    def __init__(self, api: ILinkAPI):
        super().__init__(api)
        self.get_updates_buf = ""
        self.bot_id = ""
        self.boot_time_ms = int(time.time() * 1000)

    @classmethod
    def setup(cls, **kwargs) -> "ILinkAdapter":
        api = ILinkAPI()
        return cls(api)

    async def _qrcode_login(self) -> bool:
        """只负责控制登录的状态流转，实际请求交给 api"""
        creds = load_credentials()
        if creds and creds.get("token"):
            self.api.bot_token = creds["token"]
            self.api.base_url = creds.get("base_url", self.api.base_url)
            self.bot_id = creds.get("account_id", "")
            logger.success("[ILink] 成功加载本地鉴权凭证。")
            return True

        max_retries = 3
        retry_count = 0
        fixed_qr_base = self.api.base_url

        while retry_count <= max_retries:
            try:
                res = await self.api.get_bot_qrcode(custom_base=fixed_qr_base)
                qrcode_id = res.get("qrcode", "")
                qr_content = (
                    res.get("qrcode_img_content")
                    or f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={qrcode_id}&bot_type=3"
                )

                qr = qrcode.QRCode(version=1, box_size=2, border=2)
                qr.add_data(qr_content)
                qr.make(fit=True)
                qr.print_ascii(invert=True)

                logger.info("请使用微信扫码授权 ClawBot...")
                logger.info(f"👉 备用链接: {qr_content}")

                current_poll_base = fixed_qr_base
                is_expired = False
                last_status = ""

                while True:
                    await asyncio.sleep(2.0)
                    status_res = await self.api.get_qrcode_status(
                        qrcode_id, custom_base=current_poll_base
                    )
                    status = status_res.get("status")

                    if status != last_status:
                        last_status = status
                        if status == "scaned_but_redirect":
                            if redirect_host := status_res.get("redirect_host"):
                                current_poll_base = f"https://{redirect_host}/ilink/bot"
                        elif status == "confirmed":
                            self.api.bot_token = status_res.get("bot_token", "")
                            self.api.base_url = (
                                status_res.get("baseurl") or current_poll_base
                            )
                            save_credentials(
                                token=self.api.bot_token,
                                base_url=self.api.base_url,
                                account_id=status_res.get("ilink_bot_id", ""),
                                user_id=status_res.get("ilink_user_id", ""),
                            )
                            logger.success("[ILink] 授权成功！")
                            return True
                        elif status == "expired":
                            is_expired = True
                            break

                if is_expired:
                    retry_count += 1
                    continue

            except Exception as e:
                logger.error(f"[ILink] 鉴权流程异常: {e}")
                return False

        return False

    async def send(
        self,
        target_id: str,
        message: Union[MessageSegment, MessageChain],
        cid: str = "",
    ):
        if not self.api.bot_token:
            return

        if isinstance(message, MessageSegment):
            message = MessageChain([message])

        content_chain = MessageChain()
        # 单独处理 Typing 消息段
        for seg in message:
            if isinstance(seg, Typing):
                asyncio.create_task(self.mark_typing(target_id, cid, seg.status))
            else:
                content_chain.append(seg)

        if not content_chain:
            return

        try:
            item_list = await auto_encode_ilink(message, self.api, target_id)

            res = await self.api.send_message(target_id, item_list, cid)

            ret_code = res.get("ret")
            if ret_code is None or ret_code == 0:
                logger.success(f"[发送] -> {target_id}: 成功")
            else:
                logger.error(f"[ILink] 发送失败: {res}")

        except Exception as e:
            logger.error(f"[ILink] 发送异常: {e}")

    async def _handle_raw_message(self, raw_msg: dict):
        """核心职能：数据反序列化 -> 分发事件"""
        from pydantic import ValidationError

        try:
            logger.debug(f"[ILink] 收到原始消息: {raw_msg}")
            ilink_msg = ILinkMessage.model_validate(raw_msg)
        except ValidationError as e:
            logger.debug(f"[ILink] 丢弃非标或无法解析的消息结构: {e}")
            return

        if ilink_msg.message_type != 1:
            return

        # 排除自身
        if not ilink_msg.from_user_id or ilink_msg.from_user_id == self.bot_id:
            return

        if ilink_msg.create_time_ms < self.boot_time_ms:
            return

        msg_chain = ilink_msg.to_message_chain()
        if not msg_chain:
            logger.warning("[ILink] ⚠️ 消息链为空，可能包含当前框架未注册的富媒体类型")
            return

        event = MessageEvent(
            cid=ilink_msg.context_token,
            sender_id=ilink_msg.from_user_id,
            sender_name="微信用户",
            messages=msg_chain,
            raw_payload=raw_msg,
        )

        logger.success(
            f"[接收] <- {event.sender_name}({event.sender_id}): {event.summary}"
        )

        if self._on_receive_callback:
            await self._on_receive_callback(event)

    async def mark_typing(self, target_id: str, cid: str, status: int = 1):
        """
        发送“正在输入”状态
        自动处理 ticket 的获取和下发
        """
        if not self.api.bot_token:
            return

        try:
            config_res = await self.api.get_config(target_id, context_token=cid)
            ticket = config_res.get("typing_ticket")

            if not ticket:
                logger.debug("[ILink] 未获取到 typing_ticket，无法发送输入状态")
                return

            await self.api.send_typing(target_id, ticket, status=status)

        except Exception as e:
            logger.debug(f"[ILink] 发送输入状态失败: {e}")

    async def download_media(self, segment: MessageSegment) -> bytes:
        """
        统一下载接口，支持图片、语音、视频、文件，并自动处理微信的特殊 AES 解密。
        """
        url = (
            getattr(segment, "image_url", None)
            or getattr(segment, "audio_url", None)
            or getattr(segment, "video_url", None)
            or getattr(segment, "file_id", None)
        )

        if not url:
            raise ValueError(f"该消息段 ({segment.type}) 不包含有效的媒体 URL")

        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw_data = resp.content

        aes_key_str = segment.extra.get("aes_key")

        if aes_key_str:
            try:
                decoded_key = base64.b64decode(aes_key_str)

                if len(decoded_key) == 32:
                    try:
                        real_aes_key = bytes.fromhex(decoded_key.decode("ascii"))
                    except ValueError:
                        real_aes_key = decoded_key
                else:
                    real_aes_key = decoded_key

                from .crypto import decrypt_aes_ecb

                return decrypt_aes_ecb(raw_data, real_aes_key)

            except Exception as e:
                logger.error(
                    f"[ILink] 媒体解密失败，返回原始加密数据 (AES_KEY={aes_key_str}): {e}"
                )
                return raw_data

        return raw_data

    async def run(self):
        """生命周期管理主循环"""
        while True:
            if not await self._qrcode_login():
                return

            should_relogin = False
            while not should_relogin:
                try:
                    res = await self.api.get_updates(self.get_updates_buf)
                    if "get_updates_buf" in res:
                        self.get_updates_buf = res["get_updates_buf"]

                    msgs = res.get("msgs", [])
                    for msg in msgs:
                        await self._handle_raw_message(msg)

                except httpx.ReadTimeout:
                    pass
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (401, 403):
                        logger.error("🚨 登录失效 (401/403)！准备重连...")
                        clear_credentials()
                        should_relogin = True
                    else:
                        logger.error(f"[ILink] 接口返回错误: {exc}")
                        await asyncio.sleep(3)
                except Exception as e:
                    logger.error(f"[ILink] 轮询异常: {e}")
                    await asyncio.sleep(3)
