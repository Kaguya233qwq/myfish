from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlencode

import httpx
import qrcode
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import (
    JsonValue,
    TypeAdapter,
    ValidationError,
)

from myfish.core.config import ConfigManager
from myfish.core.logger import init_logger, logger

from .api import SignFormat, XhsAPI, XhsAPIError, XhsVerificationRequired
from .device import (
    DeviceProfile,
)
from .model import (
    ActivateResponse,
    LoginResponse,
    PollResponse,
    QrcodeData,
    QrcodeResponse,
    ScriptResponse,
    VerificationInitResponse,
    VerificationStatusResponse,
)
from .sign import browser_platform
from .utils import (
    get_auth_file,
    write_private_json,
)

Result = TypeVar("Result")
_SSK_SERVER_KEY = bytes.fromhex(
    "2abd22ca0b02bb78a760935708be24e09bb369843669f235c5bc1cec11fab26f"
)


def decrypt_ssk(private_key: X25519PrivateKey, encoded: str) -> bytes:
    encrypted = base64.b64decode(encoded, validate=True)
    if len(encrypted) != 60:
        raise ValueError("Encrypted SSK must contain exactly 60 bytes")
    server_key = X25519PublicKey.from_public_bytes(_SSK_SERVER_KEY)
    shared = private_key.exchange(server_key)
    return AESGCM(shared).decrypt(encrypted[:12], encrypted[12:], None)


def show_qrcode(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".xhs-qr-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            qr.make_image().save(stream, kind="PNG")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    logger.info("[XHS] 二维码图片已保存：{}", path)


async def verify_risk(
    api: XhsAPI,
    challenge: XhsVerificationRequired,
    output: Path,
    *,
    timeout: float = 180,
) -> None:
    if challenge.verification_method == "sms":
        raise RuntimeError(
            "type=201 要求短信验证码验证，当前终端仅支持扫码验证；请通过官方网页完成验证"
        )
    if challenge.verify_type != "124":
        raise RuntimeError(
            f"暂不支持 type={challenge.verify_type or 'unknown'} 的验证，需先确认服务端要求的验证方式"
        )
    if timeout <= 0:
        raise ValueError("Verification timeout must be positive")
    deadline = time.monotonic() + timeout
    async with asyncio.timeout(timeout), XhsAPI(api.cookie_values()) as verifier:
        verifier._client.cookies = httpx.Cookies(api._client.cookies)
        if api.use_current_signing:
            verifier.set_profile_fields(deepcopy(api.fingerprint.fields))
        await initialize_device(verifier)
        await activate(verifier)
        verifier.use_current_signing = api.use_current_signing
        await report_profile(verifier)
        verifier.set_cookie("xsecappid", "fe-login")
        payload: dict[str, JsonValue] = {
            "verifyType": challenge.verify_type,
            "verifyUuid": challenge.verify_uuid,
            "verifyBiz": str(challenge.status),
            "sourceSite": "/",
        }
        response = await verifier.post(
            "/api/redcaptcha/v2/qr/init", payload, security=True, tier="0201"
        )
        rid = VerificationInitResponse.model_validate_json(response.content).data.rid
        url = "https://www.xiaohongshu.com/web-login/qrcode-transfer?" + urlencode(
            {
                "rid": rid,
                "verifyUuid": challenge.verify_uuid,
                "verifyType": challenge.verify_type,
            }
        )
        logger.info("[XHS] 请用小红书 App 扫描验证二维码并确认")
        show_qrcode(url, output.parent / "verify.png")
        payload["rid"] = rid
        previous = None
        while time.monotonic() < deadline:
            response = await verifier.post(
                "/api/redcaptcha/v2/qr/status/query",
                payload,
                security=True,
                tier="0201",
            )
            status = VerificationStatusResponse.model_validate_json(
                response.content
            ).data.status
            if status != previous:
                if status == 2:
                    logger.info("[XHS] 验证二维码已扫码，请在手机确认")
                elif status == 4:
                    logger.success("[XHS] 扫码验证通过")
                previous = status
            if status == 4:
                return
            if status in (3, 5, 6):
                raise RuntimeError(
                    {3: "扫码验证已取消", 5: "验证二维码已过期", 6: "扫码验证失败"}[
                        status
                    ]
                )
            await asyncio.sleep(min(1.5, max(0, deadline - time.monotonic())))
    raise TimeoutError("等待风控扫码验证超时")


async def read_with_verification(
    api: XhsAPI, output: Path, read: Callable[[], Awaitable[Result]]
) -> Result:
    try:
        return await read()
    except XhsVerificationRequired as challenge:
        await verify_risk(api, challenge, output)
    return await read()


def _integer_at(values: list[JsonValue], index: int) -> int:
    if not 0 <= index < len(values):
        raise ValueError("Scripting index is out of range")
    value = values[index]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("Scripting index must be a nonnegative integer")
    return value


def decode_legacy_websectiga(script: str) -> str:
    """Decode the legacy scripting format documented by Cialle/RedCrack."""
    encoded = re.search(r'"b"\s*:\s*("(?:[^"\\]|\\.)*")', script)
    constants = re.search(r'"d"\s*:\s*', script)
    if encoded is None or constants is None:
        raise ValueError("Unsupported scripting format")
    try:
        packed = TypeAdapter(str).validate_json(encoded[1], strict=True)
        raw_table, _ = json.JSONDecoder().raw_decode(script[constants.end() :])
        table = TypeAdapter(list[JsonValue]).validate_python(raw_table, strict=True)
        text = base64.b64decode(packed + "=" * (-len(packed) % 4)).decode("utf-8")
        instructions = [
            [ord(character) - 1 for character in text[index : index + 5]]
            for index in range(0, len(text), 5)
        ]
        first, last = _integer_at(table, 92), _integer_at(table, 93)
        if not first <= last < len(instructions):
            raise ValueError("Scripting instruction range is invalid")
        selected = instructions[first : last + 1]
        values = [
            _integer_at(table, selected[675 + index][2]) for index in range(0, 128, 2)
        ]
        result = "".join(
            chr(values[index + offset])
            for index in range(56, -1, -8)
            for offset in range(8)
        )
    except (ValueError, TypeError, IndexError, KeyError) as exc:
        raise ValueError("Legacy scripting layout is incompatible") from exc
    if re.fullmatch(r"[0-9a-fA-F]{64}", result) is None:
        raise ValueError("Legacy scripting did not produce a valid websectiga")
    return result


async def initialize_device(api: XhsAPI) -> None:
    logger.debug("[XHS] 初始化设备导航及安全参数")
    await api.initialize_navigation()
    await api.initialize_ds()
    response = await api.post(
        "/api/sec/v1/scripting",
        {"callFrom": "web", "callback": "seccallback"},
        security=True,
    )
    data = ScriptResponse.model_validate_json(response.content).data
    api.set_cookie("sec_poison_id", data.secPoisonId)
    api.set_cookie("websectiga", decode_legacy_websectiga(data.data))
    api.session.tiga_timestamp = int(time.time() * 1000)


async def report_profile(api: XhsAPI) -> None:
    api.fingerprint.update(api.cookie_values())
    logger.debug("[XHS] 上报设备指纹")
    await api.post(
        "/api/sec/v1/shield/webprofile",
        {
            "platform": browser_platform(api.fingerprint.fields.get("x19")),
            "sdkVersion": api.profile_sdk_version,
            "svn": "2",
            "profileData": api.fingerprint.profile_data(),
        },
        security=True,
    )
    if not api.cookie_values().get("gid"):
        raise RuntimeError("webprofile did not issue gid")


def save_cookies(path: Path, cookies: dict[str, str]) -> None:
    write_private_json(path, json.dumps(cookies, ensure_ascii=False))


async def login(
    api: XhsAPI, output: Path, *, timeout: float = 180, current_sign: bool = False
) -> None:
    await activate(api)
    api.use_current_signing = current_sign
    logger.debug("[XHS] 获取登录二维码")
    response = await api.post("/api/sns/web/v1/login/qrcode/create", {"qr_type": 1})
    qr_data = QrcodeResponse.model_validate_json(response.content).data
    response = await api.post(
        "/api/qrcode/userinfo", {"qrId": qr_data.qr_id, "code": qr_data.code}
    )
    if PollResponse.model_validate_json(response.content).data.codeStatus != 0:
        raise RuntimeError("二维码初始状态异常")
    await report_profile(api)
    await scan_qrcode(api, output, qr_data, timeout=timeout)


async def activate(api: XhsAPI) -> None:
    private_key = X25519PrivateKey.generate()
    public_key = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
    response = await api.post(
        "/api/sns/web/v1/login/activate", {"client_public_key_base64": public_key}
    )
    data = ActivateResponse.model_validate_json(response.content).data
    session = data.session
    if session:
        api.set_cookie("web_session", session)
    if not api.cookie_values().get("web_session"):
        raise RuntimeError("匿名激活未签发访客会话")
    if data.ssk:
        api.session_secret = decrypt_ssk(private_key, data.ssk)


async def scan_qrcode(
    api: XhsAPI, output: Path, qr_data: QrcodeData, *, timeout: float
) -> None:
    logger.info("[XHS] 请用小红书 App 扫码并确认登录")
    show_qrcode(qr_data.url, output.parent / "login.png")
    deadline = time.monotonic() + timeout
    previous = -1
    while time.monotonic() < deadline:
        response = await api.post(
            "/api/qrcode/userinfo", {"qrId": qr_data.qr_id, "code": qr_data.code}
        )
        state = PollResponse.model_validate_json(response.content).data.codeStatus
        if state != previous:
            if state == 1:
                logger.info("[XHS] 登录二维码已扫码，请在手机确认")
            elif state == 2:
                logger.info("[XHS] 扫码已确认，正在校验账号会话")
            previous = state
        if state == 3:
            raise RuntimeError("二维码已过期，请重新运行登录命令")
        if state == 2:
            break
        await asyncio.sleep(2)
    else:
        raise TimeoutError("等待扫码超时")
    pending = output.with_suffix(".pending.json")
    save_cookies(
        pending,
        {
            **api.cookie_values(),
            "_qr_id": qr_data.qr_id,
            "_qr_code": qr_data.code,
            "_signature_version": "current" if api.use_current_signing else "legacy",
        },
    )
    await finish_login(api, output, qr_data.qr_id, qr_data.code)
    pending.unlink(missing_ok=True)


async def finish_login(api: XhsAPI, output: Path, qr_id: str, code: str) -> None:
    visitor_session = api.cookie_values().get("web_session", "")
    private_key = X25519PrivateKey.generate()
    public_key = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
    path = "/api/sns/web/v1/login/qrcode/status?" + urlencode(
        {"qr_id": qr_id, "code": code, "client_public_key_base64": public_key}
    )
    response = await read_with_verification(
        api,
        output,
        lambda: api._get(
            path,
            headers={"x-login-mode": ""},
        ),
    )
    info = LoginResponse.model_validate_json(response.content).data.login_info
    secret = decrypt_ssk(private_key, info.ssk) if info.ssk else None
    if api.use_current_signing and secret is None:
        raise RuntimeError("正式登录响应缺少账号 SSK，拒绝保存匿名密钥")
    session = info.session
    if session:
        api.set_cookie("web_session", session)
    current_session = api.cookie_values().get("web_session", "")
    if not current_session or current_session == visitor_session:
        raise RuntimeError("服务端未签发正式登录会话")
    if secret is not None:
        api.session_secret = secret
    account = await read_with_verification(api, output, api.get_self_info)
    if account.guest:
        raise RuntimeError("当前会话仍为访客，拒绝保存")
    save_cookies(output, api.cookie_values())
    if api.use_current_signing:
        api.save_state(output.with_suffix(".state.json"))
    logger.success("[XHS] 登录成功，凭据和设备状态已保存")


async def run(
    output: Path,
    sign_format: SignFormat,
    *,
    resume: bool = False,
    probe_device: bool = False,
    current_sign: bool = False,
    profile_fields: Path | None = None,
    host_device: bool = False,
    verify: bool = False,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if verify:
        state_file = output.with_suffix(".state.json")
        if not state_file.exists():
            raise RuntimeError("缺少已登录状态，请先启动适配器完成登录")
        async with XhsAPI.from_state(state_file) as api:
            account = await read_with_verification(api, output, api.get_self_info)
            if account.guest:
                raise RuntimeError("会话已失效，请重新登录")
            api.save_state(state_file)
            save_cookies(output, api.cookie_values())
            logger.success("[XHS] 账号会话校验通过")
        return
    if resume:
        pending = output.with_suffix(".pending.json")
        if time.time() - pending.stat().st_mtime > 300:
            raise RuntimeError("中间会话已过期，请重新扫码")
        cookies = TypeAdapter(dict[str, str]).validate_json(
            pending.read_bytes(), strict=True
        )
        qr_id = cookies.pop("_qr_id")
        code = cookies.pop("_qr_code")
        if cookies.pop("_signature_version", "legacy") != "legacy":
            raise RuntimeError("新版签名恢复尚未持久化完整设备状态，请重新登录")
        async with XhsAPI(cookies, sign_format=sign_format) as api:
            await finish_login(api, output, qr_id, code)
        pending.unlink(missing_ok=True)
        return
    fields = (
        TypeAdapter(dict[str, JsonValue]).validate_json(profile_fields.read_bytes())
        if profile_fields is not None
        else None
    )
    profile = DeviceProfile.from_environment() if host_device else None
    async with XhsAPI.create(
        profile=profile, fields=fields, sign_format=sign_format
    ) as api:
        if profile is not None:
            logger.debug(
                "[XHS] 运行环境：platform={}, cores={}, memory={} GiB, screen={}x{}, timezone={}",
                profile.platform,
                profile.cpu_cores,
                profile.memory_gb,
                profile.screen_width,
                profile.screen_height,
                profile.timezone,
            )
        await initialize_device(api)
        if probe_device:
            await activate(api)
            api.use_current_signing = current_sign
            await report_profile(api)
            logger.success("[XHS] 匿名设备初始化与指纹上报成功")
            return
        await login(api, output, current_sign=current_sign)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sign-format", choices=("xys", "xyw"), default="xys")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--probe-device", action="store_true")
    parser.add_argument("--current-sign", action="store_true")
    parser.add_argument("--profile-fields", type=Path)
    parser.add_argument("--host-device", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify and (
        args.resume
        or args.probe_device
        or args.current_sign
        or args.profile_fields
        or args.host_device
    ):
        parser.error("--verify cannot be combined with login or device options")
    if args.host_device and not args.current_sign:
        parser.error("--host-device requires --current-sign")
    if args.current_sign and args.profile_fields is None and not args.host_device:
        parser.error("--current-sign requires --profile-fields or --host-device")
    if args.resume and (args.current_sign or args.profile_fields or args.probe_device):
        parser.error("--resume currently supports only legacy signing")
    ConfigManager.load_config()
    init_logger(ConfigManager.get_config().logger_level)
    try:
        asyncio.run(
            run(
                get_auth_file(),
                args.sign_format,
                resume=args.resume,
                probe_device=args.probe_device,
                current_sign=args.current_sign,
                profile_fields=args.profile_fields,
                host_device=args.host_device,
                verify=args.verify,
            )
        )
    except XhsAPIError as exc:
        logger.error("[XHS] 账号请求被拒绝: HTTP {}, code={}", exc.status, exc.code)
    except httpx.TransportError as exc:
        logger.error("[XHS] 网络连接失败: error={}", type(exc).__name__)
    except ValidationError:
        logger.error("[XHS] 服务端响应或中间会话格式与当前协议不一致")
    except InvalidTag:
        logger.error("[XHS] 服务器 SSK 认证失败，登录状态未保存")
    except (RuntimeError, TimeoutError) as exc:
        logger.error("[XHS] 账号认证未完成: {}", exc)
    except ValueError:
        logger.error("[XHS] 设备、密钥或会话数据与当前协议不一致")
    else:
        return
    parser.exit(1)


if __name__ == "__main__":
    main()
