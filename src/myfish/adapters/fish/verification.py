"""闲鱼身份验证页的二维码及状态轮询协议。"""

import asyncio
import html
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import qrcode
from loguru import logger


def passport_url(value: str, base: str) -> str:
    url = urljoin(base, html.unescape(value))
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "passport.goofish.com":
        raise ValueError("不支持的身份验证地址")
    return url


def parse_verification_page(page: str, page_url: str) -> tuple[str, str]:
    """仅识别实际观测到的拍照身份验证页，不执行页面脚本。"""
    qr = re.search(r'new\s+Qrcode\s*\(\s*\{\s*text\s*:\s*("(?:\\.|[^"\\])*")', page)
    poll = re.search(r"url\s*:\s*['\"]([^'\"]*/iv/photoVerify/check\.do[^'\"]*)['\"]", page)
    if not qr or not poll:
        raise ValueError("验证页未提供受支持的二维码及轮询接口，票据可能已失效")
    content = html.unescape(json.loads(qr.group(1)))
    if not content.startswith("https://"):
        raise ValueError("验证二维码内容无效")
    return content, passport_url(poll.group(1), page_url)


async def verify_identity(api, page_url: str, *, timeout: float = 480) -> bool:
    """由本人扫码完成验证；沿用登录客户端并接收成功回跳的 Cookie。"""
    stage = "验证入口"
    try:
        page_url = passport_url(page_url, page_url)
        api.client.headers.pop("cookie", None)
        for _ in range(4):
            response = await api.client.get(page_url)
            response.raise_for_status()
            for item in (*response.history, response):
                api.save_cookies(item)
            page_url = passport_url(str(response.url), page_url)
            if urlsplit(page_url).path == "/iv/static/mini_expired.htm":
                logger.warning("身份验证入口票据已过期，请重新扫码登录以获取新的验证二维码。")
                return False
            if urlsplit(page_url).path != "/iv/mini/normal_validate.htm":
                break
            stage = "验证方式跳转"
            # 使用脚本跳转
            target = re.search(
                r'''window\.location\.href\s*=\s*['"]([^'"]*/iv/mini/verify_modes\.htm[^'"]*)['"]''',
                response.text,
            )
            if not target:
                raise ValueError("验证入口未提供验证方式地址")
            next_url = urlsplit(passport_url(target.group(1), page_url))
            query = [(k, v) for k, v in parse_qsl(next_url.query, keep_blank_values=True) if k != "_umidfg"]
            page_url = urlunsplit(next_url._replace(query=urlencode(query)))
            logger.info("已进入登录安全验证，正在获取身份验证二维码…")
        else:
            raise ValueError("验证入口跳转次数过多")
        stage = "二维码页面解析"
        content, poll_url = parse_verification_page(response.text, page_url)
        qr = qrcode.QRCode(border=2)
        qr.add_data(content)
        qr.make(fit=True)
        logger.warning("请使用闲鱼 App 扫描下方身份验证二维码，按手机提示完成本人验证。")
        qr.print_ascii(invert=True)
        deadline = time.monotonic() + timeout
        previous_code = None
        stage = "验证状态轮询"
        while time.monotonic() < deadline:
            await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            response = await api.client.get(
                poll_url,
                headers={"Referer": page_url, "X-Requested-With": "XMLHttpRequest"},
            )
            response.raise_for_status()
            for item in (*response.history, response):
                api.save_cookies(item)
            result = response.json()
            if result.get("hasError"):
                raise ValueError("身份验证接口返回错误，票据可能已过期")
            data = result.get("content", {})
            code = str(data.get("code", ""))
            if code != previous_code:
                messages = {"0": "等待扫码验证…", "1": "请在手机上完成验证…", "2": "身份验证审核中…"}
                if code in messages:
                    logger.info(messages[code])
                previous_code = code
            if code == "3":
                callback = data.get("url")
                if not isinstance(callback, str) or not callback:
                    raise ValueError("验证成功但未返回登录回跳地址")
                callback = passport_url(callback, page_url)
                stage = "验证成功回跳"

                response = await api.client.get(callback)
                response.raise_for_status()
                for item in (*response.history, response):
                    api.save_cookies(item)
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
        logger.warning("终端身份验证未完成，阶段={}，错误类型={}。请在浏览器中完成验证。", stage, type(exc).__name__)
    return False
