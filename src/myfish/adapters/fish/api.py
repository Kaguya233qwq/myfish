import json
import os
import random
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger
from myfish.core.adapter import BaseAPI

from .sign import generate_device_id, generate_sign
from .utils import generate_headers, save_local_auth


class FishAPIError(Exception):
    """闲鱼 API 调用异常基类"""


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

    async def close(self):
        """显式关闭客户端"""
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def save_cookies(self, resp: httpx.Response):
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
        self.save_cookies(resp)
        res_json = resp.json()
        if not isinstance(res_json, dict):
            raise FishAPIError("MTOP 返回了非对象响应")
        ret = res_json.get("ret", [])
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
        """卖出订单列表；query_code 支持 ALL、NOT_SHIP。"""
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
        """订单详情，状态节点位于 data.components 的 orderStatusVO 中。"""
        return await self._trade_request(
            "mtop.idle.web.trade.order.detail", {"tid": self._trade_id(order_id)}
        )

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
            self.save_cookies(resp)
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
        return res.get("data", {}).get("accessToken", "")

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

    async def upload_media(self, media_path: str) -> dict:
        """上传图片/媒体"""
        upload_media_url = "https://stream-upload.goofish.com/api/upload.api"
        params = {"floderId": "0", "appkey": "xy_chat", "_input_charset": "utf-8"}
        media_name = os.path.basename(media_path)

        # 使用 httpx 支持的 multipart/form-data 文件上传方式
        with open(media_path, "rb") as f:
            files = {"file": (media_name, f, "image/png")}
            resp = await self.client.post(upload_media_url, params=params, files=files)
            resp.raise_for_status()
            return resp.json()

    async def get_public_channel(self, title: str, images_info: list[dict]) -> dict:
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

    async def get_default_location(
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
    ) -> dict:
        """
        发布商品
        """
        data = {
            "freebies": False,
            "itemTypeStr": "b",
            "quantity": "1",
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
            if "pix" not in image_object:
                raise FishAPIError(f"图片上传失败: {res_json}")

            width, height = map(int, image_object["pix"].split("x"))
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
                    "postPriceInCent": str(int(ds.get("post_price", 0) * 100)),
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
                    int(price["current_price"] * 100)
                )
            if price.get("original_price", 0) > 0:
                data["itemPriceDTO"]["origPriceInCent"] = str(
                    int(price["original_price"] * 100)
                )
        else:
            data["defaultPrice"] = True

        # 处理频道/分类信息
        channel_res = await self.get_public_channel(goods_desc, images_info)
        cat_predict = channel_res.get("data", {}).get("categoryPredictResult", {})

        for card in channel_res.get("data", {}).get("cardList", []):
            card_data = card.get("cardData", {})
            values_list = card_data.get("valuesList", [])
            for card_value in values_list:
                if card_value.get("isClicked"):
                    data["itemLabelExtList"].append(
                        {
                            "channelCateName": card_value["catName"],
                            "channelCateId": card_value["channelCatId"],
                            "tbCatId": card_value["tbCatId"],
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
                    break

        data["itemCatDTO"] = {
            "catId": str(cat_predict.get("catId", "")),
            "catName": str(cat_predict.get("catName", "")),
            "channelCatId": str(cat_predict.get("channelCatId", "")),
            "tbCatId": str(cat_predict.get("tbCatId", "")),
        }

        # 处理地址信息
        location_res = await self.get_default_location()
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

        # 最终发包
        extra_params = {
            "spm_cnt": "a21ybx.publish.0.0",
            "spm_pre": "a21ybx.home.sidebar.1.46413da6EPl7v5",
            "log_id": "46413da6EPl7v5",
        }
        return await self._call_mtop(
            "mtop.idle.pc.idleitem.publish", "1.0", data, extra_params
        )
