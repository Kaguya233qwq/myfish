import json
import mimetypes
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger

from myfish.core.adapter import BaseAPI

from .model import PublishCategory
from .orders import order_conversation
from .sign import generate_device_id, generate_sign
from .utils import (
    ListingRejected,
    category_options,
    generate_headers,
    passport_url,
    reshelf_payload,
    save_local_auth,
)

POLISH_ALREADY_CODES = frozenset(
    {
        "FAIL_BIZ_ITEM_ERROR_POLISH_DUPLICATE",
        "FAIL_BIZ_IDLEITEM_POLISH_AGAIN",
        "IDLEITEM_POLISH_AGAIN",
    }
)


class FishAPIError(Exception):
    """闲鱼 API 调用异常基类"""


def token_from_response(response: dict) -> str:
    codes = response.get("ret")
    data = response.get("data")
    if (
        not isinstance(codes, list)
        or not any(str(code).split("::", 1)[0] == "SUCCESS" for code in codes)
        or not isinstance(data, dict)
    ):
        return ""
    token = data.get("accessToken")
    return token if isinstance(token, str) and token.strip() else ""


class FishWSRejected(FishAPIError):
    """平台明确拒绝 WebSocket 请求。"""


class FishMessageUnconfirmed(FishAPIError):
    """消息发送结果未确认，需要核对会话记录。"""


class FishPublishPreparationError(FishAPIError):
    """发布请求尚未发送，准备阶段已失败。"""


class FishListingUnconfirmed(FishAPIError):
    """商品修改请求已提交，远端状态尚未确认。"""


class FishPolishError(FishAPIError):
    def __init__(self, codes):
        self.codes = tuple(
            code if re.fullmatch(r"[A-Z0-9_]+", code) else "INVALID_RESPONSE"
            for code in codes
        )
        super().__init__("擦亮接口拒绝请求: " + ",".join(self.codes))


class FishAPI(BaseAPI):
    def __init__(self, cookies: dict):
        self.device_id = generate_device_id(cookies.get("unb", ""))
        headers = generate_headers()
        headers.update(
            {
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://www.goofish.com",
                "referer": "https://www.goofish.com/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
            }
        )
        self.cookies_dict = cookies
        self.client = httpx.AsyncClient(
            headers=headers, follow_redirects=True, timeout=20.0
        )
        self._poll_params = {}
        self._ws_request: Callable[[str, list], Awaitable[dict]] | None = None

    def bind_websocket(self, request: Callable[[str, list], Awaitable[dict]]) -> None:
        self._ws_request = request

    async def get_order_conversation(
        self, order_id: str, buyer_id: str, item_id: str
    ) -> str:
        order_id = self._trade_id(order_id)
        buyer_id = self._trade_id(buyer_id)
        item_id = self._trade_id(item_id)
        if self._ws_request is None:
            raise FishAPIError("WebSocket 未连接")
        response = await self._ws_request("/r/Conversation/listNewest", [0, 1000])
        conversations = response.get("body")
        if not isinstance(conversations, list):
            raise FishAPIError("平台会话列表格式无效")
        try:
            return order_conversation(
                conversations,
                str(self.cookies_dict.get("unb", "")),
                buyer_id,
                item_id,
                order_id,
            )
        except ValueError as exc:
            raise FishAPIError(str(exc)) from exc

    async def close(self):
        """显式关闭客户端"""
        await self.client.aclose()

    async def _identity_response(
        self, url: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        url = passport_url(url, url)
        self.client.headers.pop("cookie", None)
        response = await self.client.get(url, headers=headers)
        response.raise_for_status()
        for item in (*response.history, response):
            self._save_cookies(item)
        return response

    async def get_identity_page(self, url: str) -> httpx.Response:
        return await self._identity_response(url)

    async def get_identity_status(self, url: str, page_url: str) -> dict:
        response = await self._identity_response(
            url, {"Referer": page_url, "X-Requested-With": "XMLHttpRequest"}
        )
        return response.json()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _save_cookies(self, resp: httpx.Response):
        cookie_updated = False
        for cookie_string in resp.headers.get_list("set-cookie"):
            pure_cookie = cookie_string.split(";")[0].strip()
            if "=" in pure_cookie:
                k, v = pure_cookie.split("=", 1)
                if self.cookies_dict.get(k) != v:
                    self.cookies_dict[k.strip()] = v.strip()
                    cookie_updated = True
        if cookie_updated:
            try:
                save_local_auth(self.cookies_dict)
            except Exception as e:  # noqa: BLE001
                logger.error(f"持久化 Cookie 失败: {e}")

    async def _call_mtop(
        self,
        api_name: str,
        version: str,
        data_dict: dict,
        extra_params: dict | None = None,
        spm_cnt: str | None = "a21ybx.im.0.0",
        headers: dict[str, str] | None = None,
        _token_retry: bool = True,
    ) -> dict:
        """
        核心封装：统一处理阿里 MTOP 协议的签名和请求构建
        """
        sign = ""
        t_str = str(int(time.time()) * 1000)
        data_val = json.dumps(data_dict, separators=(",", ":")) if data_dict else "{}"

        tk = self.cookies_dict.get("_m_h5_tk", "")
        token = tk.split("_")[0] if tk else ""
        if token and data_val:
            sign = generate_sign(t_str, token, data_val)

        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": t_str,
            "sign": sign,
            "v": version,
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api_name,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": spm_cnt,
        }

        if extra_params:
            params.update(extra_params)

        payload: dict[str, str] = {"data": data_val}
        url = f"https://h5api.m.goofish.com/h5/{api_name.lower()}/{version}/"
        latest_cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies_dict.items())
        request_headers = {"cookie": latest_cookie_str, **(headers or {})}
        resp = await self.client.post(
            url, params=params, data=payload, headers=request_headers
        )
        resp.raise_for_status()

        # 更新Cookie
        self._save_cookies(resp)
        try:
            res_json = resp.json()
        except ValueError as exc:
            raise FishAPIError(
                f"{api_name} 返回非 JSON 响应（HTTP {resp.status_code}）"
            ) from exc
        if not isinstance(res_json, dict):
            raise FishAPIError("MTOP 返回了非对象响应")
        ret = res_json.get("ret", [])
        codes = (
            {str(item).split("::", 1)[0] for item in ret}
            if isinstance(ret, list)
            else set()
        )
        if (
            _token_retry
            and codes
            & {
                "FAIL_SYS_TOKEN_EXOIRED",
                "FAIL_SYS_TOKEN_EXPIRED",
                "FAIL_SYS_TOKEN_EMPTY",
            }
            and self.cookies_dict.get("_m_h5_tk", "") != tk
        ):
            # 仅在平台明确拒绝旧 Token 且已返回新 Token 时重新签名一次。
            return await self._call_mtop(
                api_name,
                version,
                data_dict,
                extra_params,
                spm_cnt,
                headers,
                _token_retry=False,
            )
        if not isinstance(ret, list) or not any(
            str(item).split("::", 1)[0] == "SUCCESS" for item in ret
        ):
            logger.warning(
                "API 调用返回异常: api={}，ret={}",
                api_name,
                [str(item).split("::", 1)[0] for item in ret]
                if isinstance(ret, list)
                else [],
            )

        return res_json

    async def _trade_request(
        self, api: str, data: dict, version: str = "1.0", *, seller: bool = False
    ) -> dict:
        """交易请求不自动重试；失败不能当作空列表或动作成功。"""
        headers = None
        if seller:
            headers = {
                "origin": "https://seller.goofish.com",
                "referer": "https://seller.goofish.com/",
            }
        result = await self._call_mtop(
            api,
            version,
            data,
            extra_params={"valueType": "string"} if seller else None,
            spm_cnt="a21107h.42826273.0.0" if seller else "a21ybx.im.0.0",
            headers=headers,
        )
        ret = result.get("ret", [])
        if not isinstance(ret, list) or not any(
            str(item).split("::", 1)[0] == "SUCCESS" for item in ret
        ):
            # 不把完整响应（可能有验证码 URL、订单信息）写入异常。
            codes = (
                [str(item).split("::", 1)[0] for item in ret]
                if isinstance(ret, list)
                else []
            )
            raise FishAPIError(
                f"{api} 调用失败: {','.join(codes) or 'INVALID_RESPONSE'}"
            )
        return result

    @staticmethod
    def _trade_id(value: str) -> str:
        value = str(value).strip()
        if not value or not value.isascii() or not value.isdigit():
            raise ValueError("订单、商品及用户 ID 必须为数字字符串")
        return value

    @staticmethod
    def _trade_page(page: int, page_size: int):
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page 必须为正整数")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ValueError("page_size 必须在 1 到 100 之间")

    async def get_sold_orders(
        self, page: int = 1, page_size: int = 20, query_code: str = "ALL"
    ) -> dict:
        """卖出订单列表"""
        self._trade_page(page, page_size)
        if query_code not in {"ALL", "NOT_SHIP"}:
            raise ValueError("query_code 仅支持 ALL、NOT_SHIP")
        return await self._trade_request(
            "mtop.taobao.idle.trade.merchant.sold.get",
            {
                "pageNumber": page,
                "rowsPerPage": page_size,
                "orderIds": "",
                "queryCode": query_code,
                "orderSearchParam": "{}",
            },
            seller=True,
        )

    async def get_order_detail(self, order_id: str) -> dict:
        """订单详情"""
        return await self._trade_request(
            "mtop.idle.web.trade.order.detail", {"tid": self._trade_id(order_id)}
        )

    async def downshelf_item(self, item_id: str) -> dict:
        """将在售商品下架"""
        result = await self._trade_request(
            "mtop.taobao.idle.item.downshelf",
            {"itemId": self._trade_id(item_id)},
            version="2.0",
        )
        if result.get("data", {}).get("success") is not True:
            raise FishAPIError("下架接口未确认操作成功")
        return result

    async def polish_item(self, item_id: str) -> dict:
        """擦亮一件在售商品"""
        result = await self._call_mtop(
            "mtop.taobao.idle.item.polish",
            "1.0",
            {"itemId": self._trade_id(item_id)},
            spm_cnt="a21ybx.item.0.0",
        )
        ret = result.get("ret", [])
        codes = (
            [str(code).split("::", 1)[0] for code in ret]
            if isinstance(ret, list)
            else []
        )
        if "SUCCESS" in codes and result.get("data", {}).get("success") is not False:
            return result
        if set(codes) & POLISH_ALREADY_CODES:
            return result
        raise FishPolishError(codes or ["INVALID_RESPONSE"])

    async def ship_order(
        self, order_id: str, trade_text: str = "", pic_list: list[str] | None = None
    ) -> dict:
        """标记无需物流发货，不负责发送卡密，也不适用于填写快递单号。"""
        return await self._trade_request(
            "mtop.taobao.idle.logistic.consign.dummy",
            {
                "orderId": self._trade_id(order_id),
                "tradeText": trade_text.strip(),
                "picList": pic_list or [],
                "newUnconsign": True,
            },
        )

    async def request_red_flower(self, order_id: str) -> dict:
        """调用平台求小红花能力"""
        return await self._trade_request(
            "mtop.taobao.idlemessage.red.flower",
            {
                "orderId": self._trade_id(order_id),
                "channel": "list",
            },
        )

    async def rate_buyer(self, order_id: str, feedback: str = "不错的买家") -> dict:
        """卖家提交首次好评"""
        if not feedback.strip():
            raise ValueError("评价内容不能为空")
        return await self._trade_request(
            "mtop.taobao.idle.rate.create",
            {
                "tradeId": self._trade_id(order_id),
                "rate": 1,
                "feedback": feedback.strip(),
                "createOrAppend": 0,
            },
            version="4.0",
        )

    async def get_pending_ratings(self, page: int = 1, page_size: int = 20) -> dict:
        """卖家端待评价列表。"""
        self._trade_page(page, page_size)
        return await self._trade_request(
            "mtop.taobao.idle.merchant.rate.list",
            {
                "pageNumber": page,
                "rowsPerPage": page_size,
                "queryType": "ORDER",
                "rateSearchParam": {"sellerRateStatus": "5"},
            },
            seller=True,
        )

    async def get_refund_orders(
        self, dispute_status: str = "1", page: int = 1, page_size: int = 20
    ) -> dict:
        """退款列表；1/2/3 为退款中，5 为退款成功"""
        self._trade_page(page, page_size)
        if dispute_status not in {"1", "2", "3", "5"}:
            raise ValueError("dispute_status 仅支持 1、2、3、5")
        return await self._trade_request(
            "mtop.taobao.idle.merchant.refund.list",
            {
                "pageNumber": page,
                "rowsPerPage": page_size,
                "queryType": "refund",
                "refundSearchParam": {
                    "disputeStatus": dispute_status,
                    "queryCode": "ALL",
                },
            },
            seller=True,
        )

    async def skip_bargain_and_ship(
        self, order_id: str, item_id: str, buyer_id: str
    ) -> dict:
        """小刀/拼团订单免拼发货"""
        return await self._trade_request(
            "mtop.idle.groupon.activity.seller.freeshipping",
            {
                "bizOrderId": self._trade_id(order_id),
                "itemId": int(self._trade_id(item_id)),
                "buyerId": int(self._trade_id(buyer_id)),
            },
        )

    async def _get_login_params(self) -> dict[str, Any]:
        """获取二维码登录时需要的表单参数"""

        self.client.headers.pop("cookie", None)
        url = "https://passport.goofish.com/mini_login.htm"
        self._poll_params = {
            "lang": "zh_cn",
            "appName": "xianyu",
            "appEntrance": "web",
            "styleType": "vertical",
            "bizParams": "",
            "notLoadSsoView": False,
            "notKeepLogin": False,
            "isMobile": False,
            "qrCodeFirst": False,
            "site": 77,
            "rnd": random.random(),
        }
        resp = await self.client.get(url, params=self._poll_params)
        resp.raise_for_status()

        pattern = r"window\.viewData\s*=\s*(\{.*?\});"
        match = re.search(pattern, resp.text)
        if not match:
            return {}

        view_data = json.loads(match.group(1))
        data = view_data.get("loginFormData", {})
        data["umidTag"] = "SERVER"
        return data

    async def qrcode_gen(self) -> dict | None:
        """生成登录二维码的数据"""

        params = await self._get_login_params()
        if not params:
            logger.error("无法获取登录参数，无法生成二维码")
            return None
        url = "https://passport.goofish.com/newlogin/qrcode/generate.do"
        resp = await self.client.get(url, params=params)
        resp.raise_for_status()

        results = resp.json()
        if not results.get("content", {}).get("success"):
            logger.error(f"获取登录二维码失败: {results}")
            return None

        return {
            "t": results["content"]["data"]["t"],
            "ck": results["content"]["data"]["ck"],
            "content": results["content"]["data"]["codeContent"],
        }

    async def qrcode_poll(self, t: str, ck: str) -> dict:
        """轮询二维码扫描状态"""
        url = "https://passport.goofish.com/newlogin/qrcode/query.do"
        self._poll_params.update(
            {
                "t": t,
                "ck": ck,
            }
        )
        resp = await self.client.post(url, data=self._poll_params)
        resp.raise_for_status()
        data = resp.json().get("content", {}).get("data", {})
        if data.get("iframeRedirect"):
            logger.debug(
                "扫码登录要求额外验证：qrCodeStatus={}，响应字段={}，"
                "HTTP状态={}（不记录 Cookie 和登录票据）",
                data.get("qrCodeStatus", "UNKNOWN"),
                sorted(data),
                resp.status_code,
            )
            # iframeRedirect 是跳转标志，验证地址通常位于独立字段中。
            redirect_url = ""
            for candidate in (
                data.get("iframeRedirectUrl"),
                data.get("url"),
                data.get("redirectUrl"),
                data.get("iframeRedirect"),
            ):
                if not isinstance(candidate, str):
                    continue
                candidate = candidate.strip()
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                elif candidate.startswith("/"):
                    candidate = urljoin(url, candidate)
                parsed = urlsplit(candidate)
                if parsed.scheme in ("http", "https") and parsed.netloc:
                    redirect_url = candidate
                    break
            return {
                "status": "ERROR",
                "redirect_url": redirect_url,
            }
        status = data.get("qrCodeStatus", "UNKNOWN")
        if status == "CONFIRMED":
            self._save_cookies(resp)
        return {
            "status": status,
        }

    async def get_token(self) -> dict:
        data = {
            "appKey": "444e9908a51d1cb236a27862abc769c9",
            "deviceId": self.device_id,
        }
        extra_params = {
            "spm_pre": "a21ybx.item.want.1.14ad3da6ALVq3n",
            "log_id": "14ad3da6ALVq3n",
        }
        return await self._call_mtop(
            "mtop.taobao.idlemessage.pc.login.token", "1.0", data, extra_params
        )

    async def get_access_token(self) -> str:
        """获取 access token"""
        res = await self.get_token()
        return token_from_response(res)

    async def get_mh5tk(self):
        """获取 m_h5_tk 和 m_h5_tk_enc"""
        return await self._call_mtop(
            "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get",
            "1.0",
            {},
            spm_cnt="a21ybx.home.0.0",
        )

    async def get_user_info(self, user_id: str, is_self: bool = False) -> dict:
        """获取用户信息"""
        extra_params = {
            "spm_pre": "a21ybx.home.nav.1.62953da6OYFsax",
            "log_id": "62953da6OYFsax",
        }
        data: dict[str, bool | str] = {"self": is_self}
        if user_id:
            data["userId"] = user_id
        return await self._call_mtop(
            "mtop.idle.web.user.page.head", "1.0", data, extra_params
        )

    async def get_self_info(self) -> dict:
        """获取自己的用户信息"""
        return await self.get_user_info(user_id="", is_self=True)

    async def get_item_list(
        self, user_id: str, page: int = 1, page_size: int = 20
    ) -> dict:
        """获取卖家商品列表"""
        data = {
            "userId": user_id,
            "pageNumber": page,
            "pageSize": page_size,
        }
        extra_params = {
            "spm_pre": "a21ybx.home.nav.1.62953da6OYFsax",
            "log_id": "62953da6OYFsax",
        }
        return await self._call_mtop(
            "mtop.idle.web.xyh.item.list", "1.0", data, extra_params
        )

    async def get_item_info(self, item_id: str) -> dict:
        """获取闲鱼商品详情"""

        data = {"itemId": str(item_id)}
        extra_params = {
            "spm_pre": "a21ybx.item.want.1.12523da6waCtUp",
            "log_id": "12523da6waCtUp",
        }
        return await self._call_mtop(
            "mtop.taobao.idle.pc.detail", "1.0", data, extra_params
        )

    async def get_item_edit_detail(
        self, item_id: str, *, verify_owner: bool = True
    ) -> dict:
        result = await self._call_mtop(
            "mtop.idle.pc.idleitem.editDetail",
            "1.0",
            {"itemId": self._trade_id(item_id)},
        )
        if not any(
            str(code).split("::", 1)[0] == "SUCCESS" for code in result.get("ret", [])
        ):
            raise ListingRejected("平台拒绝读取商品编辑详情，请检查登录或平台编辑限制")
        data = result.get("data")
        if not isinstance(data, dict):
            raise FishAPIError("商品编辑详情结构异常")
        if str(data.get("itemId", "")) != str(item_id):
            raise ListingRejected("平台返回的商品 ID 与请求不匹配")
        # 编辑详情可能用 0 隐去卖家 ID，改由商品详情核验归属。
        if verify_owner and data.get("userId") in (None, "", 0, "0"):
            response = await self.get_item_info(item_id)
            if not any(
                str(code).split("::", 1)[0] == "SUCCESS"
                for code in response.get("ret", [])
            ):
                codes = {
                    str(code).split("::", 1)[0] for code in response.get("ret", [])
                }
                if codes & {"FAIL_SYS_USER_VALIDATE", "RGV587_ERROR"}:
                    raise ListingRejected(
                        "商品归属校验触发安全验证，请先完成浏览器验证并更新凭证后重试"
                    )
                raise ListingRejected(
                    "平台未提供有效卖家 ID，且商品详情读取失败，无法核验归属"
                )
            public = response.get("data", {})
            public_item = public.get("itemDO", {})
            seller = str(public.get("sellerDO", {}).get("sellerId") or "")
            if str(public_item.get("itemId", "")) != str(item_id):
                raise ListingRejected("商品详情 ID 与请求不匹配，无法核验归属")
            if not seller or seller == "0":
                raise ListingRejected("平台未提供有效卖家 ID，无法核验商品归属")
            data = {**data, "userId": seller}
        return data

    async def reshelf_item(
        self, item_id: str, *, quantity: int, after_sale: bool = False
    ) -> dict:
        """通过原商品编辑恢复上架，并回查在售状态。"""
        item_id = self._trade_id(item_id)
        detail = await self.get_item_edit_detail(item_id)
        status = str(detail.get("itemStatus"))
        owner_id = str(self.cookies_dict.get("unb", ""))
        if after_sale and status == "0":
            if str(detail.get("userId", "")) != owner_id or not owner_id:
                raise ListingRejected("商品所属账号不匹配")
            return detail
        expected_status = "1" if after_sale and status == "1" else "-2"
        payload = reshelf_payload(
            detail, item_id, owner_id, quantity, expected_status=expected_status
        )
        result = await self._call_mtop(
            "mtop.idle.pc.idleitem.edit",
            "1.0",
            payload,
            spm_cnt="a21ybx.publish.0.0",
            _token_retry=False,
        )
        if not isinstance(result.get("ret"), list) or not result["ret"]:
            raise FishListingUnconfirmed("恢复上架响应缺少结果码，请核对平台状态")
        if not any(
            str(code).split("::", 1)[0] == "SUCCESS" for code in result.get("ret", [])
        ):
            raise ListingRejected(
                "平台拒绝恢复上架，请检查商品是否支持 PC 编辑及发布要求"
            )
        returned_id = result.get("data", {}).get("itemId")
        if returned_id is not None and str(returned_id) != item_id:
            raise FishListingUnconfirmed("恢复上架返回不同商品 ID，请核对平台结果")
        try:
            after = await self.get_item_edit_detail(item_id, verify_owner=False)
            if str(after.get("itemStatus")) == "-9":
                owner = after.get("userId")
                if owner not in (None, "", 0, "0") and str(owner) != str(
                    self.cookies_dict.get("unb", "")
                ):
                    raise FishListingUnconfirmed(
                        "审核商品的所属账号不匹配，请核对平台状态"
                    )
                return after
            if after.get("userId") in (None, "", 0, "0"):
                after = await self.get_item_edit_detail(item_id)
        except (ListingRejected, FishAPIError, httpx.HTTPError) as exc:
            raise FishListingUnconfirmed("恢复上架已提交，但无法回查平台状态") from exc
        if (
            str(after.get("itemId")) != item_id
            or str(after.get("userId")) != str(self.cookies_dict.get("unb", ""))
            or str(after.get("itemStatus")) != "0"
        ):
            state = {"-2": "仍为主动下架", "-9": "处于审核中"}.get(
                str(after.get("itemStatus")), "未确认原商品在售"
            )
            raise FishListingUnconfirmed(
                "恢复请求已提交，回查" + state + "，请核对平台状态"
            )
        return after

    async def submit_item_edit(self, item_id: str, payload: dict) -> dict:
        item_id = self._trade_id(item_id)
        if str(payload.get("itemId")) != item_id:
            raise ListingRejected("编辑提交的商品 ID 不匹配")
        result = await self._call_mtop(
            "mtop.idle.pc.idleitem.edit",
            "1.0",
            payload,
            spm_cnt="a21ybx.publish.0.0",
            _token_retry=False,
        )
        codes = result.get("ret")
        if not isinstance(codes, list) or not codes:
            raise FishListingUnconfirmed("商品编辑响应缺少结果码，请核对平台结果")
        if not any(str(code).split("::", 1)[0] == "SUCCESS" for code in codes):
            raise ListingRejected(
                "平台拒绝商品编辑，请检查登录、安全验证和 PC 编辑限制"
            )
        returned_id = result.get("data", {}).get("itemId")
        if returned_id is not None and str(returned_id) != item_id:
            raise FishListingUnconfirmed("编辑响应返回不同商品 ID，请核对平台结果")
        try:
            after = await self.get_item_edit_detail(item_id, verify_owner=False)
        except (ListingRejected, FishAPIError, httpx.HTTPError) as exc:
            raise FishListingUnconfirmed("商品编辑已提交，但无法回查平台状态") from exc
        owner = after.get("userId")
        if str(after.get("itemId")) != item_id or (
            owner not in (None, "", 0, "0")
            and str(owner) != str(self.cookies_dict.get("unb", ""))
        ):
            raise FishListingUnconfirmed("编辑回查的商品或所属账号不匹配")
        if str(after.get("itemStatus")) not in {"0", "-9"}:
            raise FishListingUnconfirmed("编辑已提交，但商品尚未处于在售或审核状态")
        return after

    async def upload_media(self, media_path: str) -> dict:
        """上传图片/媒体"""
        upload_media_url = "https://stream-upload.goofish.com/api/upload.api"
        params = {"floderId": "0", "appkey": "xy_chat", "_input_charset": "utf-8"}
        media_name = os.path.basename(media_path)

        # 使用 httpx 支持的 multipart/form-data 文件上传方式
        with open(media_path, "rb") as f:  # noqa: ASYNC230
            files = {
                "file": (
                    media_name,
                    f,
                    mimetypes.guess_type(media_name)[0] or "application/octet-stream",
                )
            }
            headers = {
                k: v
                for k, v in self.client.headers.items()
                if k.lower() != "content-type"
            }
            headers["cookie"] = "; ".join(
                f"{k}={v}" for k, v in self.cookies_dict.items()
            )
            request = httpx.Request(
                "POST", upload_media_url, params=params, files=files, headers=headers
            )
            resp = await self.client.send(request)
            resp.raise_for_status()
            self._save_cookies(resp)
            try:
                result = resp.json()
            except ValueError as exc:
                raise FishPublishPreparationError(
                    f"图片上传返回非 JSON 响应（HTTP {resp.status_code}），请检查登录状态和上传接口"
                ) from exc
            if not isinstance(result, dict):
                raise FishPublishPreparationError("图片上传返回格式错误")
            return result

    async def get_publish_categories(self, description: str) -> list[dict]:
        result = await self._get_public_channel(description, [])
        ret = result.get("ret", [])
        if not any(str(code).split("::", 1)[0] == "SUCCESS" for code in ret):
            raise FishPublishPreparationError("类目查询失败，请检查账号登录状态")
        return category_options(result)

    async def _get_public_channel(
        self, title: str, images_info: list[dict], *, category: dict | None = None
    ) -> dict:
        """获取商品分类等推荐频道信息"""
        data = {
            "title": title,
            "lockCpv": False,
            "multiSKU": False,
            "publishScene": "mainPublish",
            "scene": "newPublishChoice",
            "description": title,
            "imageInfos": [],
            "uniqueCode": "1775905618164677",
        }
        if category:
            data.update(
                {
                    key: category[key]
                    for key in ("catId", "catName", "channelCatId")
                    if category.get(key)
                }
            )
            data["selectedList"] = [
                {
                    "propertyId": "-10000",
                    "propertyName": "分类",
                    "channelCateId": category["channelCatId"],
                    "channelCateName": category["catName"],
                    "isUserClick": "1",
                    "from": "newPublishChoice",
                    "labelFrom": "newPublish",
                }
            ]
        for info in images_info:
            data["imageInfos"].append(
                {
                    "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
                    "isQrCode": False,
                    "url": info["url"],
                    "heightSize": info["height"],
                    "widthSize": info["width"],
                    "major": True,
                    "type": 0,
                    "status": "done",
                }
            )

        extra_params = {
            "spm_cnt": "a21ybx.publish.0.0",
            "spm_pre": "a21ybx.item.sidebar.1.67321598K9Vgx8",
            "log_id": "67321598K9Vgx8",
        }
        return await self._call_mtop(
            "mtop.taobao.idle.kgraph.property.recommend", "2.0", data, extra_params
        )

    async def _get_default_location(
        self, longitude: float = 118.78248347393424, latitude: float = 31.91629189813543
    ) -> dict:
        """获取默认发货地址信息"""
        data = {"longitude": longitude, "latitude": latitude}
        extra_params = {
            "spm_cnt": "a21ybx.publish.0.0",
            "spm_pre": "a21ybx.item.sidebar.1.38262218ame5nr",
            "log_id": "38262218ame5nr",
        }
        return await self._call_mtop(
            "mtop.taobao.idle.local.poi.get", "1.0", data, extra_params
        )

    async def publish_item(
        self,
        images_path: list[str],
        goods_desc: str,
        price: dict[str, float] | None,
        ds: dict[str, Any],
        quantity: int = 1,
        category: dict | None = None,
    ) -> dict:
        data = await self._prepare_item_payload(
            images_path, goods_desc, price, ds, quantity, category
        )
        return await self._call_mtop(
            "mtop.idle.pc.idleitem.publish",
            "1.0",
            data,
            {"spm_cnt": "a21ybx.publish.0.0"},
        )

    async def save_item_draft(
        self,
        images_path: list[str],
        goods_desc: str,
        price: dict[str, float] | None,
        ds: dict[str, Any],
        quantity: int = 1,
        category: dict | None = None,
        *,
        draft_id: str = "",
    ) -> dict:
        """保存到闲鱼客户端草稿箱"""
        if (
            not isinstance(draft_id, str)
            or len(draft_id) > 256
            or any(ord(c) <= 32 or ord(c) == 127 for c in draft_id)
        ):
            raise ValueError("无效的平台草稿 ID")
        data = await self._prepare_item_payload(
            images_path, goods_desc, price, ds, quantity, category
        )
        if draft_id:
            data["draftId"] = draft_id
        return await self._call_mtop(
            "mtop.idle.idleitem.draft.edit"
            if draft_id
            else "mtop.idle.idleitem.draft.publish",
            "1.0",
            data,
            {"spm_cnt": "a21ybx.publish.0.0"},
        )

    async def _prepare_item_payload(
        self,
        images_path: list[str],
        goods_desc: str,
        price: dict[str, float] | None,
        ds: dict[str, Any],
        quantity: int = 1,
        category: dict | None = None,
    ) -> dict:
        """
        发布商品
        """
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or not 1 <= quantity <= 9999
        ):
            raise ValueError("发布数量必须为 1-9999 的整数")
        data = {
            "freebies": False,
            "itemTypeStr": "b",
            "quantity": str(quantity),
            "simpleItem": "true",
            "imageInfoDOList": [],
            "itemTextDTO": {
                "desc": goods_desc,
                "title": goods_desc,
                "titleDescSeparate": False,
            },
            "itemLabelExtList": [],
            "itemPriceDTO": {},
            "userRightsProtocols": [
                {"enable": False, "serviceCode": "SKILL_PLAY_NO_MIND"}
            ],
            "itemPostFeeDTO": {
                "canFreeShipping": False,
                "supportFreight": False,
                "onlyTakeSelf": False,
            },
            "itemAddrDTO": {},
            "defaultPrice": False,
            "itemCatDTO": {},
            "uniqueCode": str(int(time.time() * 1000000)),
            "sourceId": "pcMainPublish",
            "bizcode": "pcMainPublish",
            "publishScene": "pcMainPublish",
        }

        images_info = []
        for image_path in images_path:
            res_json = await self.upload_media(image_path)
            image_object = res_json.get("object", {})
            if not isinstance(image_object, dict) or not all(
                image_object.get(k) for k in ("pix", "url")
            ):
                raise FishPublishPreparationError(
                    "图片上传未返回图片信息，请检查登录状态或平台上传限制"
                )

            try:
                width, height = map(int, image_object["pix"].split("x"))
                if min(width, height) <= 0:
                    raise ValueError("invalid image dimensions")
            except (ValueError, TypeError, AttributeError) as exc:
                raise FishPublishPreparationError(
                    "图片上传响应中的尺寸格式错误"
                ) from exc
            info = {"url": image_object["url"], "height": height, "width": width}
            images_info.append(info)
            data["imageInfoDOList"].append(
                {
                    "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
                    "isQrCode": False,
                    "url": info["url"],
                    "heightSize": info["height"],
                    "widthSize": info["width"],
                    "major": True,
                    "type": 0,
                    "status": "done",
                }
            )

        # 处理物流设置
        choice = ds.get("choice")
        if choice == "包邮":
            data["itemPostFeeDTO"].update(
                {"canFreeShipping": True, "supportFreight": True}
            )
        elif choice == "按距离计费":
            data["itemPostFeeDTO"].update(
                {"supportFreight": True, "templateId": "-100"}
            )
        elif choice == "一口价":
            data["itemPostFeeDTO"].update(
                {
                    "supportFreight": True,
                    "templateId": "0",
                    "postPriceInCent": str(
                        int(Decimal(str(ds.get("post_price", 0))) * 100)
                    ),
                }
            )
        elif choice == "无需邮寄":
            data["itemPostFeeDTO"]["templateId"] = "0"
        else:
            raise ValueError(f"无效的物流选项: {choice}")

        if ds.get("can_self_pickup"):
            data["onlyTakeSelf"] = True

        # 处理价格
        if price:
            if price.get("current_price", 0) > 0:
                data["itemPriceDTO"]["priceInCent"] = str(
                    int(Decimal(str(price["current_price"])) * 100)
                )
            if price.get("original_price", 0) > 0:
                data["itemPriceDTO"]["origPriceInCent"] = str(
                    int(Decimal(str(price["original_price"])) * 100)
                )
        else:
            data["defaultPrice"] = True

        # 处理频道/分类信息
        explicit_category = (
            PublishCategory.model_validate(category).platform_data()
            if category is not None
            else None
        )
        channel_res = await self._get_public_channel(
            goods_desc,
            images_info,
            **({"category": explicit_category} if explicit_category else {}),
        )
        if "ret" in channel_res and not any(
            str(code).split("::", 1)[0] == "SUCCESS" for code in channel_res["ret"]
        ):
            raise FishPublishPreparationError(
                "发布前类目属性查询失败，请检查登录或重新选择品类"
            )
        cat_predict = channel_res.get("data", {}).get("categoryPredictResult", {})
        selected_category = None

        for card in channel_res.get("data", {}).get("cardList", []):
            card_data = card.get("cardData", {})
            values_list = card_data.get("valuesList", [])
            for card_value in values_list:
                is_category = str(card_data.get("propertyId")) == "-10000"
                chosen = (
                    (
                        str(card_value.get("channelCatId"))
                        == explicit_category["channelCatId"]
                    )
                    if explicit_category and is_category
                    else card_value.get("isClicked") in (True, 1, "1", "true")
                )
                if chosen:
                    if not is_category:
                        transport = card_value.get("transportData")
                        if not isinstance(transport, dict):
                            transport = {
                                key: card_value[key]
                                for key in (
                                    "valueId",
                                    "valueName",
                                    "tbCatId",
                                    "labelType",
                                )
                                if key in card_value
                            }
                        label = dict(transport)
                        label.update(
                            {
                                "propertyId": card_data.get("propertyId"),
                                "propertyName": card_data.get("propertyName"),
                                "isUserClick": "1",
                                "from": "newPublishChoice",
                                "labelFrom": "newPublish",
                            }
                        )
                        data["itemLabelExtList"].append(label)
                        break
                    if str(card_data.get("propertyId")) == "-10000":
                        selected_category = card_value
                    missing = [
                        key
                        for key in ("catName", "channelCatId")
                        if key not in card_value
                    ] + [
                        key
                        for key in ("propertyName", "propertyId")
                        if key not in card_data
                    ]
                    if missing:
                        raise FishPublishPreparationError(
                            "类目推荐选中标签缺少字段: " + ",".join(missing)
                        )
                    data["itemLabelExtList"].append(
                        {
                            "channelCateName": card_value["catName"],
                            "channelCateId": card_value["channelCatId"],
                            "labelType": "common",
                            "propertyName": card_data["propertyName"],
                            "isUserClick": "1",
                            "from": "newPublishChoice",
                            "propertyId": card_data["propertyId"],
                            "labelFrom": "newPublish",
                            "text": card_value["catName"],
                            "properties": f"{card_data['propertyId']}##{card_data['propertyName']}:{card_value['channelCatId']}##{card_value['catName']}",
                        }
                    )
                    # 平台的推荐标签可不包含淘宝类目 ID，不能当作必填字段。
                    if card_value.get("tbCatId") is not None:
                        data["itemLabelExtList"][-1]["tbCatId"] = card_value["tbCatId"]
                    break

        # 新版响应的 categoryPredictResult 可能只有 sugShow，实际类目在分类卡片中。
        category = explicit_category or selected_category or cat_predict
        if not category.get("channelCatId"):
            raise FishPublishPreparationError(
                "类目推荐未返回有效频道类目，请选择商品类目后再发布"
            )
        data["itemCatDTO"] = {
            key: str(category[key])
            for key in (
                "catId",
                "catName",
                "channelCatId",
                "tbCatId",
                "leafId",
                "channelCat1Id",
                "channelCat2Id",
                "channelCat3Id",
            )
            if category.get(key) is not None and category[key] != ""
        }
        if explicit_category and selected_category is None:
            data["itemLabelExtList"].insert(
                0,
                {
                    "propertyId": "-10000",
                    "propertyName": "分类",
                    "channelCateId": explicit_category["channelCatId"],
                    "channelCateName": explicit_category["catName"],
                    "labelType": "common",
                    "isUserClick": "1",
                    "from": "newPublishChoice",
                    "labelFrom": "newPublish",
                    "properties": f"-10000##分类:{explicit_category['channelCatId']}##{explicit_category['catName']}",
                },
            )

        # 处理地址信息
        location_res = await self._get_default_location()
        common_addrs = location_res.get("data", {}).get("commonAddresses", [])
        if common_addrs:
            loc = common_addrs[0]
            data["itemAddrDTO"] = {
                "area": loc.get("area"),
                "city": loc.get("city"),
                "divisionId": loc.get("divisionId"),
                "gps": f"{loc.get('longitude')},{loc.get('latitude')}",
                "poiId": loc.get("poiId"),
                "poiName": loc.get("poi"),
                "prov": loc.get("prov"),
            }

        return data
