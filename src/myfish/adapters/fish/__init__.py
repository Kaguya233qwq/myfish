import asyncio
import base64
import json
import math
import sys
import time
import urllib.parse

import qrcode
import qrcode.constants
import websockets
from loguru import logger
from myfish.core.adapter import AdapterMetaData, BaseAdapter, OnReceiveCallback
from myfish.core.bot import Bot
from myfish.core.message import (
    MessageChain,
    MessageSegment,
)
from myfish.core.registry import AdapterRegistry

from .api import FishAPI
from .message import FishTradeCard, auto_encode
from .sign import decrypt, generate_mid, generate_uuid
from .tracking import OrderTracker
from .utils import load_local_auth, rm_local_auth
from .verification import verify_identity

FishBot = Bot["FishAdapter"]


async def qrcode_login(client: FishAPI):
    """扫码登录流程"""

    logger.info("请使用闲鱼App扫码登录...")
    await client.get_mh5tk()
    qr_data = await client.qrcode_gen()
    if not qr_data:
        logger.error("二维码生成失败，程序退出。")
        sys.exit(1)
    content = qr_data.get("content")
    if content:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=2,
            border=2,
        )
        qr.add_data(content)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    else:
        logger.error("二维码数据缺失，程序退出。")
        sys.exit(1)

    t = qr_data.get("t", "")
    ck = qr_data.get("ck", "")

    # 开始轮询状态
    is_scanned = False
    verification_attempted = False
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        await asyncio.sleep(1.5)
        status = await client.qrcode_poll(t, ck)
        if status.get("status") == "CONFIRMED":
            logger.info("用户信息已确认，登录成功")
            return client.cookies_dict
        elif status.get("status") == "NEW":
            continue
        elif status.get("status") == "SCANED":
            if not is_scanned:
                is_scanned = True
                logger.info("二维码已被扫描，请在手机上确认登录...")
            continue

        elif status.get("status") == "EXPIRED":
            logger.warning("二维码已过期，请重新获取")
            break

        elif status.get("status") == "CANCELED":
            logger.info("用户在手机端取消了登录")
            break
        elif status.get("status") == "ERROR":
            redirect_url = status.get("redirect_url")
            if isinstance(redirect_url, str) and redirect_url:
                if not verification_attempted:
                    verification_attempted = True
                    if await verify_identity(client, redirect_url):
                        deadline = time.monotonic() + 120
                        if client.cookies_dict.get("unb"):
                            token = await client.get_token()
                            if token.get("data", {}).get("accessToken"):
                                return client.cookies_dict
                            client.client.headers.pop("cookie", None)
                        logger.info(
                            "身份验证已通过，登录会话尚未建立，正在生成新的登录二维码，请再次扫码确认。"
                        )
                        # 沿用验证后的会话及 Cookie，不再轮询已失效的旧二维码。
                        qr_data = await client.qrcode_gen()
                        if not qr_data or not qr_data.get("content"):
                            logger.error("验证后的登录二维码生成失败。")
                            return None
                        qr = qrcode.QRCode(border=2)
                        qr.add_data(qr_data["content"])
                        qr.make(fit=True)
                        qr.print_ascii(invert=True)
                        t = qr_data.get("t", "")
                        ck = qr_data.get("ck", "")
                        is_scanned = False
                        deadline = time.monotonic() + 600
                        continue
                logger.warning(
                    "登录需要额外验证，请在浏览器打开以下网址并按页面提示完成验证："
                    f"{redirect_url}"
                )
                logger.info("完成验证后，请重新启动并扫码登录。")
            else:
                logger.warning(
                    "登录需要额外验证，但服务端未返回有效的验证网址。"
                    "请先在闲鱼 App 或官网完成登录及安全验证，再重新扫码。"
                )
            break
        else:
            print(f"未知的二维码状态: {status}")
            return None


async def ensure_auth(client: FishAPI):
    """
    核心鉴权流：优先读取 -> 验证状态 -> 按需扫码 -> 返回可用凭证
    """
    cookies = client.cookies_dict
    if cookies and "unb" in cookies:
        logger.info("正在验证本地凭证有效性...")

        res = await client.get_token()
        if isinstance(res, dict) and "FAIL_SYS_USER_VALIDATE" in str(res):
            punish_url = res.get("data", {}).get("url", "")
            logger.error("🚨 触发阿里风控滑块验证，请勿再次重启脚本")
            logger.error(
                "请手动完成滑块验证码验证，并将x5sec的值添加到myfish_auth.json"
            )
            logger.error(punish_url)
            await client.close()
            sys.exit(1)
        if isinstance(res, dict) and "FAIL_SYS_SESSION_EXPIRED" in str(res):
            logger.warning("会话已过期，需要重新登陆")
            # 删除过期凭证
            client.cookies_dict = {}
            rm_local_auth()
        else:
            if res.get("data", {}).get("accessToken"):
                unb = cookies["unb"]
                nick = urllib.parse.unquote(cookies["tracknick"])
                logger.success(f"成功登录账号 {nick}({unb})")
                return cookies
            else:
                logger.warning("Token 失效，尝试自动刷新")
                refresh_res = await client.get_token()
                if refresh_res:
                    logger.success("自动刷新 Token 成功")
                    return cookies
                else:
                    logger.warning("自动刷新 Token 失败，可能需要重新登录")

    # 如果没有有效凭证，执行扫码登录流程
    return await qrcode_login(client)


@AdapterRegistry.register
class FishAdapter(BaseAdapter["FishAPI"]):
    """闲鱼适配器"""

    meta_data = AdapterMetaData(
        id="fish",
        name="闲鱼适配器",
        description="闲鱼Web端的WebSocket连接实现",
        version="1.0.0",
        author="Kaguya233qwq",
    )

    def __init__(
        self,
        api: FishAPI,
        *,
        track_orders: bool = False,
        order_poll_interval: float = 60,
        order_max_pages: int = 5,
    ):
        if not isinstance(track_orders, bool):
            raise TypeError("track_orders 必须是布尔值")
        if not math.isfinite(order_poll_interval) or order_poll_interval < 10:
            raise ValueError("order_poll_interval 必须是至少 10 秒的有限数值")
        if (
            isinstance(order_max_pages, bool)
            or not isinstance(order_max_pages, int)
            or not 1 <= order_max_pages <= 100
        ):
            raise ValueError("order_max_pages 必须在 1 到 100 之间")
        self.base_url = "wss://wss-goofish.dingtalk.com/"
        super().__init__(api)

        self._active_ws: websockets.ClientConnection | None = None
        self._bg_tasks = []
        self._on_receive_callback: OnReceiveCallback | None = None
        self.track_orders = track_orders
        self.order_poll_interval = order_poll_interval
        self.order_max_pages = order_max_pages
        self.order_tracker: OrderTracker | None = None

    @classmethod
    def setup(cls, **kwargs) -> "FishAdapter":
        cookies = kwargs.get("cookies")
        if not cookies:
            cookies = load_local_auth()
        api_instance = FishAPI(cookies=cookies)

        return cls(
            api=api_instance,
            track_orders=kwargs.get("track_orders", False),
            order_poll_interval=kwargs.get("order_poll_interval", 60),
            order_max_pages=kwargs.get("order_max_pages", 5),
        )

    def _get_headers(self) -> dict:
        cookie_str = "; ".join([f"{k}={v}" for k, v in self.api.cookies_dict.items()])

        return {
            "Cookie": cookie_str,
            "Host": "wss-goofish.dingtalk.com",
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/133.0.0.0 Safari/537.36",
            "Origin": "https://www.goofish.com",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    async def _send_ack(self, message: dict):
        if not self._active_ws:
            return
        req_headers = message.get("headers", {})
        ack = {
            "code": 200,
            "headers": {
                "mid": req_headers.get("mid", await generate_mid()),
                "sid": req_headers.get("sid", ""),
            },
        }
        for key in ["app-key", "ua", "dt"]:
            if key in req_headers:
                ack["headers"][key] = req_headers[key]
        try:
            await self._active_ws.send(json.dumps(ack))
        except Exception as e:  # noqa: BLE001
            logger.error(f"ACK 发送失败: {e}")

    async def _init_connection(self):
        if not self._active_ws:
            return
        token = await self.api.get_access_token()
        if not token:
            logger.error("[FishWSAdapter] 适配器初始化失败：无法获取 accessToken")
            return False

        reg_msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.api.device_id,
                "mid": await generate_mid(),
            },
        }
        await self._active_ws.send(json.dumps(reg_msg))
        current_time = int(time.time() * 1000)

        sync_msg = {
            "lwp": "/r/SyncStatus/ackDiff",
            "headers": {"mid": await generate_mid()},
            "body": [
                {
                    "pipeline": "sync",
                    "tooLong2Tag": "PNM,1",
                    "channel": "sync",
                    "topic": "sync",
                    "highPts": 0,
                    "pts": current_time * 1000,
                    "seq": 0,
                    "timestamp": current_time,
                }
            ],
        }
        await self._active_ws.send(json.dumps(sync_msg))
        logger.success("[FishWSAdapter] WebSocket 连接成功~ 正在持续监听闲鱼消息...")
        return True

    async def _heart_beat_loop(self):
        if not self._active_ws:
            return
        try:
            while True:
                await self._active_ws.send(
                    json.dumps({"lwp": "/!", "headers": {"mid": await generate_mid()}})
                )
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            pass

    async def _keep_token_alive_loop(self):
        try:
            while True:
                await asyncio.sleep(600)
                try:
                    await self.api.get_access_token()
                except Exception:  # noqa: BLE001, S110
                    pass
        except asyncio.CancelledError:
            pass

    async def send(
        self,
        target_id: str,
        message: MessageSegment | MessageChain,
        cid: str = "",
    ):
        """将Message对象打包成底层WebSocket消息格式并发送"""
        if not self._active_ws:
            logger.error("[FishWSAdapter] Adapter 未连接，无法发送消息")
            return

        try:
            payload, custom_type = auto_encode(message)
        except ValueError as e:
            logger.error(f"[FishWSAdapter] 拦截非法发送: {e}")
            return

        encoded_data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode(
            "utf-8"
        )
        _cid = cid if cid else target_id

        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {"mid": await generate_mid()},
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{_cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {"type": custom_type, "data": encoded_data},
                    },
                    "redPointPolicy": 0,
                    "extension": {"extJson": "{}"},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                    "mtags": {},
                    "msgReadStatusSetting": 1,
                },
                {"actualReceivers": [f"{target_id}@goofish", f"{self.bot_id}@goofish"]},
            ],
        }

        log_text = message.summary
        logger.success(f"[发送] -> {target_id}: {log_text}")

        await self._active_ws.send(json.dumps(msg))

    async def _handle_raw_message(self, message_str: str):
        message_dict = json.loads(message_str)
        await self._send_ack(message_dict)
        if "syncPushPackage" not in message_str:
            return

        try:
            body = message_dict.get("body", {})
            push_package = body.get("syncPushPackage", {})
            data_list = push_package.get("data", [])

            for item in data_list:
                raw_data = item.get("data")
                if not raw_data:
                    continue

                parsed_dict = None
                try:
                    parsed_dict = json.loads(raw_data)
                except json.JSONDecodeError:
                    try:
                        decrypted_str = decrypt(raw_data)
                        if isinstance(decrypted_str, dict):
                            parsed_dict = decrypted_str
                        else:
                            parsed_dict = json.loads(decrypted_str)
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"[FishWSAdapter] 无法按正常流程解密: {e}")
                        base64_decripted = base64.b64decode(raw_data).decode()
                        logger.debug(
                            f"[FishWSAdapter] Base64解密结果: {base64_decripted}"
                        )
                        continue

                if not parsed_dict:
                    continue

                logger.debug(f"[FishWSAdapter] Received raw message: {parsed_dict}")

                from pydantic import ValidationError

                from .message import RecievedMessagePayload

                try:
                    incoming_msg = RecievedMessagePayload.model_validate(parsed_dict)
                except ValidationError:
                    continue

                sender = incoming_msg.data.sender
                if not sender.user_id or str(sender.user_id) == str(self.bot_id):
                    continue

                msg_chain = incoming_msg.data.content.to_message_chain()
                if self.order_tracker and any(
                    isinstance(segment, FishTradeCard) for segment in msg_chain
                ):
                    self.order_tracker.request_sync()
                if not msg_chain:
                    return

                from myfish.core.event import MessageEvent

                event = MessageEvent(
                    cid=incoming_msg.data.cid,
                    sender_id=str(sender.user_id),
                    sender_name=sender.name,
                    messages=msg_chain,
                    raw_payload=parsed_dict,
                )

                logger.success(
                    f"[接收] <- {event.sender_name}({event.sender_id}): {event.summary}"
                )

                if self._on_receive_callback:
                    await self._on_receive_callback(event)

        except Exception as e:  # noqa: BLE001
            logger.exception(f"[FishWSAdapter] Driver 清洗数据时发生异常: {e}")

    async def _connect_ws(self):
        while True:
            try:
                logger.info("[FishWSAdapter] 尝试连接 WebSocket 服务器...")
                async with websockets.connect(
                    self.base_url, additional_headers=self._get_headers()
                ) as ws:
                    self._active_ws = ws
                    if not await self._init_connection():
                        return
                    self._bg_tasks.append(asyncio.create_task(self._heart_beat_loop()))

                    async for message_str in ws:
                        await self._handle_raw_message(message_str)  # pyright: ignore[reportArgumentType]

            except websockets.ConnectionClosed:
                self._active_ws = None
                logger.warning("[FishWSAdapter] WebSocket断开，3秒后重连...")
                await asyncio.sleep(3)
            except Exception as e:  # noqa: BLE001
                self._active_ws = None
                logger.error(f"[FishWSAdapter] 发生致命错误: {e}")
                await asyncio.sleep(5)

    async def run(self):
        """启动 Driver"""
        try:
            valid_cookies = await ensure_auth(self.api)
            if not valid_cookies or not valid_cookies.get("unb"):
                logger.error("闲鱼登录未完成，停止启动适配器。")
                return
            self.bot_id = valid_cookies.get("unb", "")
            if self.track_orders:
                from myfish.core.storage import StorageManager

                account_id = self.api._trade_id(self.bot_id)
                self.order_tracker = OrderTracker(
                    self.api,
                    account_id,
                    self._emit_order_event,
                    state_path=StorageManager.get_adapter_dir("fish")
                    / f"orders-{account_id}.json",
                    interval=self.order_poll_interval,
                    max_pages=self.order_max_pages,
                )
                self._bg_tasks.append(asyncio.create_task(self.order_tracker.run()))
            self._bg_tasks.append(asyncio.create_task(self._keep_token_alive_loop()))
            await self._connect_ws()
        finally:
            for task in self._bg_tasks:
                task.cancel()
            if self._bg_tasks:
                await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()
            self.order_tracker = None
            await self.api.close()

    async def _emit_order_event(self, event):
        if self._on_receive_callback:
            await self._on_receive_callback(event)
