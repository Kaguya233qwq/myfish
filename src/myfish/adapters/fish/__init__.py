import asyncio
import base64
import json
import re
import sys
import time
import urllib.parse

import httpx
import qrcode
import qrcode.constants
import websockets
from loguru import logger
from websockets.frames import Close

from myfish.core.adapter import AdapterMetaData, BaseAdapter, OnReceiveCallback
from myfish.core.bot import Bot
from myfish.core.event import MessageEvent
from myfish.core.message import (
    MessageChain,
    MessageSegment,
)
from myfish.core.registry import AdapterRegistry

from .api import (
    FishAPI,
    FishAPIError,
    FishMessageUnconfirmed,
    FishWSRejected,
    token_from_response,
)
from .config import FishConfig
from .message import (
    RecievedMessagePayload,
    auto_encode,
)
from .sign import decrypt, generate_mid, generate_uuid
from .utils import (
    load_local_auth,
    load_message_json,
    parse_verification_page,
    passport_url,
    rm_local_auth,
)

FishBot = Bot["FishAdapter"]

_TOKEN_REFRESH_INTERVAL = 600
_WS_REQUEST_TIMEOUT = 15
_RECONNECT_INTERVAL = 3


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

    def __init__(self, api: FishAPI):
        self.base_url = "wss://wss-goofish.dingtalk.com/"
        super().__init__(api)

        self._active_ws: websockets.ClientConnection | None = None
        self._on_receive_callback: OnReceiveCallback | None = None
        self._pending_requests: dict[str, asyncio.Future[dict]] = {}
        self._ws_token = ""
        self._token_refresh_stage = "waiting"
        self._ws_reconnect_reason = "connection_closed"
        self.api.bind_websocket(self._request_ws)

    @classmethod
    def setup(cls, **kwargs) -> "FishAdapter":
        cookies = FishConfig.model_validate(kwargs).cookies
        if not cookies:
            cookies = load_local_auth()
        api_instance = FishAPI(cookies=cookies)

        return cls(api=api_instance)

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
        if not self._active_ws or "code" in message:
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

    async def _wait_ws_response(self, mid: str) -> bool:
        async with asyncio.timeout(_WS_REQUEST_TIMEOUT):
            while self._active_ws:
                raw = await self._active_ws.recv()
                response = load_message_json(raw)
                if not isinstance(response, dict):
                    continue
                if response.get("headers", {}).get("mid") == mid:
                    code = response.get("code")
                    if code != 200:
                        logger.warning(
                            "[FishWSAdapter] 初始化请求被拒绝: code={}", code
                        )
                    return code == 200
                await self._handle_raw_message(raw)
        return False

    async def _request_ws(
        self, path: str, body: list | None, *, headers: dict | None = None
    ) -> dict:
        ws = self._active_ws
        if not ws:
            raise FishAPIError("WebSocket 未连接")
        mid = await generate_mid()
        future = asyncio.get_running_loop().create_future()
        self._pending_requests[mid] = future
        try:
            request: dict[str, object] = {
                "lwp": path,
                "headers": {**(headers or {}), "mid": mid},
            }
            if body is not None:
                request["body"] = body
            async with asyncio.timeout(_WS_REQUEST_TIMEOUT):
                await ws.send(json.dumps(request))
                response = await future
            if response.get("code") != 200:
                logger.warning(
                    "[FishWSAdapter] WebSocket 请求被拒绝: path={} code={}",
                    path,
                    response.get("code")
                    if isinstance(response.get("code"), int)
                    else "unknown",
                )
                raise FishWSRejected("平台拒绝 WebSocket 请求")
            return response
        finally:
            self._pending_requests.pop(mid, None)
            if not future.done():
                future.cancel()

    def _registration_headers(self, token: str) -> dict[str, str]:
        return {
            "cache-header": "app-key token ua wv",
            "app-key": "444e9908a51d1cb236a27862abc769c9",
            "token": token,
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
            "dt": "j",
            "wv": "im:3,au:3,sy:6",
            "sync": "0,0;0;0;",
            "did": self.api.device_id,
        }

    async def _init_connection(self):
        if not self._active_ws:
            return
        token = await self.api.get_access_token()
        if not token:
            logger.error("[FishWSAdapter] 适配器初始化失败：无法获取 accessToken")
            return False

        self._ws_token = token

        reg_msg = {
            "lwp": "/reg",
            "headers": {
                **self._registration_headers(token),
                "mid": await generate_mid(),
            },
        }
        await self._active_ws.send(json.dumps(reg_msg))
        if not await self._wait_ws_response(reg_msg["headers"]["mid"]):
            return False
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
        if not await self._wait_ws_response(sync_msg["headers"]["mid"]):
            return False
        logger.success("[FishWSAdapter] WebSocket 连接成功~ 正在持续监听闲鱼消息...")
        return True

    async def _heart_beat_loop(self):
        ws = self._active_ws
        if not ws:
            return
        try:
            while self._active_ws is ws:
                await ws.send(
                    json.dumps({"lwp": "/!", "headers": {"mid": await generate_mid()}})
                )
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FishWSAdapter] 心跳发送失败: {}", type(exc).__name__)
            if self._active_ws is ws:
                self._ws_reconnect_reason = "heartbeat_failure"
                await ws.close()

    async def _keep_token_alive_loop(self):
        """刷新登录凭证；WebSocket 在建立连接时注册。"""
        try:
            while True:
                await asyncio.sleep(_TOKEN_REFRESH_INTERVAL)
                ws = self._active_ws
                if ws is None:
                    return
                started = time.monotonic()
                self._token_refresh_stage = "fetch_token"
                logger.info("[FishWSAdapter] 开始刷新登录凭证")
                try:
                    token = await self.api.get_access_token()
                    if not token:
                        raise FishAPIError("Token 刷新未返回有效凭证")
                    if self._active_ws is not ws:
                        return
                    self._token_refresh_stage = "waiting"
                    logger.info(
                        "[FishWSAdapter] 登录凭证刷新成功: elapsed={:.2f}s",
                        time.monotonic() - started,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[FishWSAdapter] 登录凭证刷新失败: stage={} error={} elapsed={:.2f}s，将重新建立连接",
                        self._token_refresh_stage,
                        type(exc).__name__,
                        time.monotonic() - started,
                    )
                    if self._active_ws is ws:
                        self._ws_reconnect_reason = "auth_refresh_failure"
                        await ws.close()
                    return
        except asyncio.CancelledError:
            pass

    async def send(
        self,
        target_id: str,
        message: MessageSegment | MessageChain,
        cid: str = "",
        *,
        redact: bool = False,
    ):
        """等待平台确认发送；明确拒绝时返回 False，结果未知时抛出 FishMessageUnconfirmed。"""
        if not self._active_ws:
            logger.error("[FishWSAdapter] Adapter 未连接，无法发送消息")
            return False

        try:
            payload, custom_type = auto_encode(message)
        except ValueError as e:
            logger.error(f"[FishWSAdapter] 拦截非法发送: {e}")
            return False

        encoded_data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode(
            "utf-8"
        )
        _cid = cid if cid else target_id

        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
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

        log_text = "[敏感交付内容已隐藏]" if redact else message.summary
        try:
            await self._request_ws(msg["lwp"], msg["body"])
        except FishWSRejected:
            logger.warning("[FishWSAdapter] 平台拒绝发送消息")
            return False
        except (FishAPIError, TimeoutError, websockets.ConnectionClosed) as exc:
            raise FishMessageUnconfirmed("消息发送结果未确认，请核对会话记录") from exc
        logger.success(f"[发送] -> {target_id}: {log_text}")
        return True

    async def _handle_raw_message(self, message_str: str | bytes):
        message_dict = load_message_json(message_str)
        if not isinstance(message_dict, dict):
            logger.warning("[FishWSAdapter] 收到无法解析的 WebSocket 帧")
            return
        headers = message_dict.get("headers")
        if "code" in message_dict and isinstance(headers, dict):
            mid = headers.get("mid")
            pending = self._pending_requests.get(mid) if isinstance(mid, str) else None
            if pending and not pending.done():
                pending.set_result(message_dict)
                return
        await self._send_ack(message_dict)
        body = message_dict.get("body")
        if not isinstance(body, dict):
            return
        push_package = body.get("syncPushPackage")
        if not isinstance(push_package, dict):
            return
        data_list = push_package.get("data")
        if not isinstance(data_list, list):
            return
        for item in data_list:
            try:
                if not isinstance(item, dict):
                    continue
                raw_data = item.get("data")
                if not raw_data:
                    continue

                parsed_dict = load_message_json(raw_data)
                if parsed_dict is None and isinstance(raw_data, str):
                    parsed_dict = load_message_json(decrypt(raw_data))
                if not isinstance(parsed_dict, dict) or not isinstance(
                    parsed_dict.get("1"), dict
                ):
                    continue

                from pydantic import ValidationError

                try:
                    incoming_msg = RecievedMessagePayload.model_validate(parsed_dict)
                except ValidationError as exc:
                    fields = [
                        ".".join(map(str, error["loc"])) for error in exc.errors()
                    ]
                    logger.warning(
                        "[FishWSAdapter] 消息字段不兼容: {}", ",".join(fields)
                    )
                    continue

                sender = incoming_msg.data.sender
                if not sender.user_id or str(sender.user_id) == str(self.bot_id):
                    continue

                for event in incoming_msg.data.to_events(self.bot_id, parsed_dict):
                    if isinstance(event, MessageEvent):
                        logger.success(
                            f"[接收] <- {event.sender_name}({event.sender_id}): {event.summary}"
                        )
                    else:
                        logger.info("[接收事件] {}", event.event_type)
                    if self._on_receive_callback:
                        await self._on_receive_callback(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[FishWSAdapter] 单条消息处理失败: {}", type(exc).__name__
                )

    def _safe_close_reason(self, reason: str) -> str:
        if not isinstance(reason, str):
            return ""
        cookies = getattr(self.api, "cookies_dict", {})
        secrets = [self._ws_token]
        if isinstance(cookies, dict):
            secrets.extend(
                value for value in cookies.values() if isinstance(value, str)
            )
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            reason = reason.replace(secret, "[redacted]")
        return reason.replace("\r", " ").replace("\n", " ")[:160]

    def _log_ws_disconnect(
        self,
        ws: websockets.ClientConnection | None,
        started: float,
        exc: websockets.ConnectionClosed | None = None,
    ) -> None:
        protocol = getattr(ws, "protocol", None)
        received = exc.rcvd if exc else getattr(protocol, "close_rcvd", None)
        sent = exc.sent if exc else getattr(protocol, "close_sent", None)
        received = received if isinstance(received, Close) else None
        sent = sent if isinstance(sent, Close) else None
        logger.warning(
            "[FishWSAdapter] WebSocket断开: close_code={} received_code={} sent_code={} reason={} "
            "uptime={:.2f}s refresh_stage={} reconnect_reason={}，3秒后重连",
            getattr(ws, "close_code", None),
            received.code if received else None,
            sent.code if sent else None,
            self._safe_close_reason(
                received.reason
                if received
                else sent.reason
                if sent
                else getattr(ws, "close_reason", "") or ""
            ),
            time.monotonic() - started,
            self._token_refresh_stage,
            self._ws_reconnect_reason,
        )

    async def _connect_ws(self):
        while True:
            heartbeat = None
            token_refresh = None
            ws = None
            started = time.monotonic()
            self._token_refresh_stage = "waiting"
            self._ws_reconnect_reason = "connection_closed"
            try:
                logger.info("[FishWSAdapter] 尝试连接 WebSocket 服务器...")
                async with websockets.connect(
                    self.base_url, additional_headers=self._get_headers()
                ) as ws:
                    self._active_ws = ws
                    started = time.monotonic()
                    if not await self._init_connection():
                        raise ConnectionError("WebSocket 注册或同步失败")
                    heartbeat = asyncio.create_task(self._heart_beat_loop())
                    token_refresh = asyncio.create_task(self._keep_token_alive_loop())

                    async for message_str in ws:
                        await self._handle_raw_message(message_str)
                self._log_ws_disconnect(ws, started)
            except websockets.ConnectionClosed as exc:
                self._log_ws_disconnect(ws, started, exc)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "[FishWSAdapter] 连接异常: error={} reason={}",
                    type(e).__name__,
                    self._safe_close_reason(str(e)),
                )
            finally:
                self._active_ws = None
                for future in self._pending_requests.values():
                    if not future.done():
                        future.set_exception(FishAPIError("WebSocket 连接已断开"))
                self._pending_requests.clear()
                background = [task for task in (heartbeat, token_refresh) if task]
                for task in background:
                    task.cancel()
                if background:
                    await asyncio.gather(*background, return_exceptions=True)
                self._ws_token = ""
            await asyncio.sleep(_RECONNECT_INTERVAL)

    async def run(self):
        """启动 Driver"""
        try:
            valid_cookies = await _ensure_auth(self.api)
            if not valid_cookies or not valid_cookies.get("unb"):
                logger.error("闲鱼登录未完成，停止启动适配器。")
                return
            self.bot_id = valid_cookies.get("unb", "")
            await self._connect_ws()
        finally:
            self._active_ws = None
            await self.api.close()


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
                    if await _verify_identity(client, redirect_url):
                        deadline = time.monotonic() + 120
                        if client.cookies_dict.get("unb"):
                            token = await client.get_token()
                            if token_from_response(token):
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


async def _ensure_auth(client: FishAPI):
    """
    核心鉴权流：优先读取 -> 验证状态 -> 按需扫码 -> 返回可用凭证
    """
    cookies = client.cookies_dict
    if cookies and "unb" in cookies:
        logger.info("正在验证本地凭证有效性...")

        for _ in range(2):
            res = await client.get_token()
            ret = res.get("ret", [])
            codes = (
                {str(code).split("::", 1)[0] for code in ret}
                if isinstance(ret, list)
                else set()
            )
            if "FAIL_SYS_USER_VALIDATE" in codes:
                logger.error(
                    "登录触发安全验证，请在浏览器完成验证并更新闲鱼适配器 auth.json 中的 x5sec"
                )
                return None
            if "FAIL_SYS_SESSION_EXPIRED" in codes:
                logger.warning("会话已过期，需要重新登录")
                client.cookies_dict = {}
                client.client.cookies.clear()
                rm_local_auth()
                break
            if token_from_response(res):
                cookies = client.cookies_dict
                nick = urllib.parse.unquote(cookies.get("tracknick", ""))
                logger.success("成功登录账号 {}({})", nick, cookies["unb"])
                return cookies
            logger.warning("未获得有效 Token")
        else:
            logger.error("鉴权失败，停止启动闲鱼适配器，请检查登录状态")
            return None

    # 如果没有有效凭证，执行扫码登录流程
    return await qrcode_login(client)


async def _verify_identity(
    api: FishAPI, page_url: str, *, timeout: float = 480
) -> bool:
    """由本人扫码完成验证；沿用登录客户端并接收成功回跳的 Cookie。"""
    stage = "验证入口"
    try:
        page_url = passport_url(page_url, page_url)
        for _ in range(4):
            response = await api.get_identity_page(page_url)
            page_url = passport_url(str(response.url), page_url)
            if urllib.parse.urlsplit(page_url).path == "/iv/static/mini_expired.htm":
                logger.warning(
                    "身份验证入口票据已过期，请重新扫码登录以获取新的验证二维码。"
                )
                return False
            if urllib.parse.urlsplit(page_url).path != "/iv/mini/normal_validate.htm":
                break
            stage = "验证方式跳转"
            # 使用脚本跳转
            target = re.search(
                r"""window\.location\.href\s*=\s*['"]([^'"]*/iv/mini/verify_modes\.htm[^'"]*)['"]""",
                response.text,
            )
            if not target:
                raise ValueError("验证入口未提供验证方式地址")
            next_url = urllib.parse.urlsplit(passport_url(target.group(1), page_url))
            query = [
                (k, v)
                for k, v in urllib.parse.parse_qsl(
                    next_url.query, keep_blank_values=True
                )
                if k != "_umidfg"
            ]
            page_url = urllib.parse.urlunsplit(
                next_url._replace(query=urllib.parse.urlencode(query))
            )
            logger.info("已进入登录安全验证，正在获取身份验证二维码…")
        else:
            raise ValueError("验证入口跳转次数过多")
        stage = "二维码页面解析"
        content, poll_url = parse_verification_page(response.text, page_url)
        qr = qrcode.QRCode(border=2)
        qr.add_data(content)
        qr.make(fit=True)
        logger.warning(
            "请使用闲鱼 App 扫描下方身份验证二维码，按手机提示完成本人验证。"
        )
        qr.print_ascii(invert=True)
        deadline = time.monotonic() + timeout
        previous_code = None
        stage = "验证状态轮询"
        while time.monotonic() < deadline:
            await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            result = await api.get_identity_status(poll_url, page_url)
            if result.get("hasError"):
                raise ValueError("身份验证接口返回错误，票据可能已过期")
            data = result.get("content", {})
            code = str(data.get("code", ""))
            if code != previous_code:
                messages = {
                    "0": "等待扫码验证…",
                    "1": "请在手机上完成验证…",
                    "2": "身份验证审核中…",
                }
                if code in messages:
                    logger.info(messages[code])
                previous_code = code
            if code == "3":
                callback = data.get("url")
                if not isinstance(callback, str) or not callback:
                    raise ValueError("验证成功但未返回登录回跳地址")
                callback = passport_url(callback, page_url)
                stage = "验证成功回跳"

                await api.get_identity_page(callback)
                logger.success("身份验证通过，继续确认登录状态。")
                return True
            if code == "-1":
                logger.error("身份验证未通过，请重新登录后重试。")
                return False
            if code not in {"0", "1", "2"}:
                raise ValueError("身份验证返回未知状态，请在浏览器中完成验证")
        logger.warning("身份验证二维码已超时，请重新登录。")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        # HTTP 异常字符串包含 htoken，不能直接输出异常或响应正文
        logger.warning(
            "终端身份验证未完成，阶段={}，错误类型={}。请在浏览器中完成验证。",
            stage,
            type(exc).__name__,
        )
    return False
