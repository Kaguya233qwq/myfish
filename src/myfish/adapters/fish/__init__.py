import asyncio
import base64
import json
import time
import urllib.parse
from typing import Optional, Union

import qrcode
import qrcode.constants
import websockets
from loguru import logger

from .api import FishAPI
from .sign import decrypt, generate_mid, generate_uuid
from .utils import rm_local_auth, load_local_auth
from .message import auto_encode

from myfish.core.bot import Bot
from myfish.core.message import (
    MessageChain,
    MessageSegment,
)
from myfish.core.adapter import BaseAdapter, OnReceiveCallback

FishBot = Bot["FishWebSocketAdapter"]


async def qrcode_login(client: FishAPI):
    """扫码登录流程"""

    logger.info("请使用闲鱼App扫码登录...")
    await client.get_mh5tk()
    qr_data = await client.qrcode_gen()
    if not qr_data:
        logger.error("二维码生成失败，程序退出。")
        exit(1)
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
        exit(1)

    t = qr_data.get("t", "")
    ck = qr_data.get("ck", "")

    # 开始轮询状态
    is_scanned = False
    while True:
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
            logger.warning(
                "您的帐号被风控，需要前往以下网址通过手机验证码登录："
                f"{status.get('redirect_url', '')}"
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
            exit(1)
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


class FishWebSocketAdapter(BaseAdapter):
    """闲鱼WebSocket适配器"""

    def __init__(self):
        self.base_url = "wss://wss-goofish.dingtalk.com/"
        cookies = load_local_auth()
        self.api = FishAPI(cookies=cookies)
        self.myid = self.api.cookies_dict.get("unb", "")

        self._active_ws: Optional[websockets.ClientConnection] = None
        self._bg_tasks = []
        self._on_receive_callback: Optional[OnReceiveCallback] = None

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
        except Exception as e:
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
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def send(
        self,
        target_id: str,
        message: Union[MessageSegment, MessageChain],
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
                {"actualReceivers": [f"{target_id}@goofish", f"{self.myid}@goofish"]},
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
                    json.loads(raw_data)
                except json.JSONDecodeError:
                    try:
                        decrypted_str = decrypt(raw_data)
                        if isinstance(decrypted_str, dict):
                            parsed_dict = decrypted_str
                        else:
                            parsed_dict = json.loads(decrypted_str)
                    except Exception as e:
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
                if not sender.user_id or str(sender.user_id) == str(self.myid):
                    continue

                msg_chain = incoming_msg.data.content.to_message_chain()
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

        except Exception as e:
            logger.exception(f"[FishWSAdapter] Driver 清洗数据时发生异常: {e}")

    async def run(self):
        """启动 Driver"""
        await ensure_auth(self.api)

        headers = self._get_headers()
        self._bg_tasks.append(asyncio.create_task(self._keep_token_alive_loop()))

        while True:
            try:
                logger.info("[FishWSAdapter] 尝试连接 WebSocket 服务器...")
                async with websockets.connect(
                    self.base_url, additional_headers=headers
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
            except Exception as e:
                self._active_ws = None
                logger.error(f"[FishWSAdapter] 发生致命错误: {e}")
                await asyncio.sleep(5)
