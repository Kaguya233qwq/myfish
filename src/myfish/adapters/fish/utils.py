import base64
import copy
import hashlib
import html
import json
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from loguru import logger

from myfish.core.storage import StorageManager

from .model import PublishCategory


def generate_headers() -> dict[str, str]:
    """生成请求头的函数"""
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://www.goofish.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0",
    }


def _get_auth_file() -> Path:
    """动态获取闲鱼适配器的鉴权文件路径"""
    return StorageManager.get_adapter_dir("fish") / "auth.json"


def load_local_auth() -> dict:
    """从本地读取 Cookie"""
    auth_file = _get_auth_file()
    if auth_file.exists():
        try:
            return json.loads(auth_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning(f"本地鉴权文件损坏 [{auth_file.name}]，将重新登录。")
    return {}


def save_local_auth(cookies: dict):
    """持久化 Cookie 到本地"""
    auth_file = _get_auth_file()

    try:
        auth_file.write_text(
            json.dumps(cookies, indent=4, ensure_ascii=False), encoding="utf-8"
        )
        logger.success(f"凭证已成功保存至: {auth_file.resolve()}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"保存凭证失败: {e}")


def rm_local_auth():
    """删除本地 Cookie 文件"""
    auth_file = _get_auth_file()
    if auth_file.exists():
        try:
            auth_file.unlink()
            logger.info(f"已删除本地凭证文件: {auth_file.name}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"删除凭证文件失败: {e}")


def load_message_json(value: Any) -> dict | list | None:
    """解析明文或 Base64 JSON。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, (str, bytes)):
        return None
    try:
        result = json.loads(value)
    except (ValueError, UnicodeError):
        try:
            result = json.loads(base64.b64decode(value, validate=True))
        except (ValueError, UnicodeError):
            return None
    return result if isinstance(result, (dict, list)) else None


def category_options(response: dict) -> list[dict]:
    from pydantic import ValidationError

    options = {}
    for card in response.get("data", {}).get("cardList", []):
        detail = card.get("cardData", {})
        if str(detail.get("propertyId")) != "-10000":
            continue
        for value in detail.get("valuesList", []):
            try:
                category = PublishCategory.model_validate(
                    {key: str(val) for key, val in value.items() if val is not None}
                )
            except ValidationError:
                continue
            options[category.channel_cat_id] = category.platform_data()
    return list(options.values())


class ListingRejected(ValueError):
    """平台明确拒绝，或提交前置条件不满足。"""


def reshelf_payload(
    detail: dict, item_id: str, owner_id: str, quantity: int, *, expected_status="-2"
) -> dict:
    if not owner_id or str(detail.get("userId", "")) != owner_id:
        raise ListingRejected("商品所属账号不匹配")
    if str(detail.get("itemId", "")) != item_id:
        raise ListingRejected("编辑详情中的商品 ID 不匹配")
    if str(detail.get("itemStatus")) != expected_status:
        raise ListingRejected("平台商品状态已改变，请同步状态后重试")
    if (
        isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or not 1 <= quantity <= 9999
    ):
        raise ListingRejected("恢复上架需要 1 至 9999 件可售库存")
    keys = [
        "attribute_biz_line",
        "bizcode",
        "bucketId",
        "canBargain",
        "defaultPrice",
        "errorTipsMsg",
        "freebies",
        "itemStatus",
        "itemTypeStr",
        "quantity",
        "scene",
        "simpleItem",
        "supportBargainPrice",
        "topics",
        "asyncSecurityInfo",
        "baseParams",
        "itemAddrDTO",
        "itemCatDTO",
        "itemGroupDTO",
        "itemPostFeeDTO",
        "itemPriceDTO",
        "itemTextDTO",
        "itemTopicParams",
        "yhbItemInfoDTO",
        "imageInfoDOList",
        "itemLabelExtList",
        "itemProperties",
        "properties",
        "inputProperties",
        "itemSkuList",
        "userRightsProtocols",
    ]
    payload = copy.deepcopy({key: detail[key] for key in keys if key in detail})
    for key in (
        "itemTextDTO",
        "itemPriceDTO",
        "itemCatDTO",
        "itemAddrDTO",
        "imageInfoDOList",
    ):
        if not payload.get(key):
            raise ListingRejected("平台编辑详情不完整，不能恢复上架")
    if payload.get("itemSkuList"):
        raise ListingRejected("暂不支持多规格商品恢复上架")
    payload.update(
        itemId=item_id,
        sourceId=str(detail.get("sourceId") or item_id),
        uniqueCode=uuid.uuid4().hex,
        bizcode="pcMainPublish",
        publishScene="pcMainPublish",
        itemStatus="0",
        quantity=str(quantity),
    )
    for key in ("canFreeShipping", "supportFreight", "onlyTakeSelf"):
        post = payload.get("itemPostFeeDTO", {})
        if key in post:
            post[key] = as_bool(post[key])
    for protocol in payload.get("userRightsProtocols") or []:
        if "enable" in protocol:
            protocol["enable"] = as_bool(protocol["enable"])
    return payload


def edit_fingerprint(detail):
    keys = (
        "itemTextDTO",
        "itemPriceDTO",
        "itemPostFeeDTO",
        "itemCatDTO",
        "imageInfoDOList",
        "quantity",
        "itemProperties",
        "properties",
        "inputProperties",
        "itemSkuList",
    )
    raw = json.dumps(
        {k: detail.get(k) for k in keys}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def edit_values(detail):
    text = detail.get("itemTextDTO") or {}
    desc = str(text.get("desc") or "")
    if text.get("titleDescSeparate") in (True, "true", 1, "1"):
        title = str(text.get("title") or "")
    else:
        title, _, desc = desc.partition("\n")
    return {
        "title": title,
        "description": desc,
        "price": str(
            Decimal(str((detail.get("itemPriceDTO") or {}).get("priceInCent", 0))) / 100
        ),
        "quantity": int(detail.get("quantity") or 1),
        "shipping": "keep",
        "postage": str(
            Decimal(
                str((detail.get("itemPostFeeDTO") or {}).get("postPriceInCent") or 0)
            )
            / 100
        ),
        "images": [
            f"remote:{i}" for i, _ in enumerate(detail.get("imageInfoDOList") or [])
        ],
    }


def published_edit_payload(detail, item_id, owner_id, changes, images):
    payload = reshelf_payload(
        detail, item_id, owner_id, changes["quantity"], expected_status="0"
    )
    text = payload["itemTextDTO"]
    original = edit_values(detail)
    if (original["title"], original["description"]) != (
        changes["title"],
        changes["description"],
    ):
        for key in ("richTextDesc", "descPath", "wlDescription"):
            text.pop(key, None)
    if text.get("titleDescSeparate") in (True, "true", 1, "1"):
        text.update(title=changes["title"], desc=changes["description"])
    else:
        body = changes["title"] + (
            "\n" + changes["description"] if changes["description"] else ""
        )
        text.update(title=body, desc=body, titleDescSeparate=False)
    payload["itemPriceDTO"]["priceInCent"] = str(int(Decimal(changes["price"]) * 100))
    payload["imageInfoDOList"] = copy.deepcopy(images)
    shipping = changes["shipping"]
    if shipping != "keep":
        post = payload.setdefault("itemPostFeeDTO", {})
        post.update(
            canFreeShipping=shipping == "free",
            supportFreight=shipping in {"free", "fixed"},
            templateId="0",
            postPriceInCent=str(int(Decimal(changes["postage"]) * 100))
            if shipping == "fixed"
            else "0",
        )
    return payload


def edit_matches(expected, actual):
    left, right = edit_values(expected), edit_values(actual)
    if any(left[k] != right[k] for k in ("title", "description", "quantity")):
        return False
    if Decimal(left["price"]) != Decimal(right["price"]):
        return False
    if [i.get("url") for i in expected.get("imageInfoDOList", [])] != [
        i.get("url") for i in actual.get("imageInfoDOList", [])
    ]:
        return False
    for key in ("canFreeShipping", "supportFreight", "onlyTakeSelf"):
        if as_bool(expected.get("itemPostFeeDTO", {}).get(key, False)) != as_bool(
            actual.get("itemPostFeeDTO", {}).get(key, False)
        ):
            return False
    return Decimal(
        str(expected.get("itemPostFeeDTO", {}).get("postPriceInCent") or 0)
    ) == Decimal(str(actual.get("itemPostFeeDTO", {}).get("postPriceInCent") or 0))


def as_bool(value):
    if value in (True, "true", "1", 1):
        return True
    if value in (False, "false", "0", 0):
        return False
    raise ListingRejected("平台编辑详情包含无法识别的布尔值")


def passport_url(value: str, base: str) -> str:
    url = urljoin(base, html.unescape(value))
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "passport.goofish.com":
        raise ValueError("不支持的身份验证地址")
    return url


def parse_verification_page(page: str, page_url: str) -> tuple[str, str]:
    """仅识别实际观测到的拍照身份验证页，不执行页面脚本。"""
    qr = re.search(r'new\s+Qrcode\s*\(\s*\{\s*text\s*:\s*("(?:\\.|[^"\\])*")', page)
    poll = re.search(
        r"url\s*:\s*['\"]([^'\"]*/iv/photoVerify/check\.do[^'\"]*)['\"]", page
    )
    if not qr or not poll:
        raise ValueError("验证页未提供受支持的二维码及轮询接口，票据可能已失效")
    content = html.unescape(json.loads(qr.group(1)))
    if not content.startswith("https://"):
        raise ValueError("验证二维码内容无效")
    return content, passport_url(poll.group(1), page_url)
