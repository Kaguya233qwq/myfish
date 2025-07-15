import asyncio
import json
from random import random
import re
from time import time
import httpx
import qrcode
import qrcode.constants

from utils.sign_tool import SignTool
from utils import generate_headers


class GetLoginParamsError(Exception):
    """获取登录参数错误"""


class GetLoginQRCodeError(Exception):
    """获取登录二维码失败"""


class NotLoginError(Exception):
    """未登录错误"""


class Login:

    def __init__(self):
        self.params = {}
        self.cookies = {}
        self.headers = generate_headers()
        self.host = "https://passport.goofish.com"
        self.api_mini_login = f"{self.host}/mini_login.htm"
        self.api_generate_qr = f"{self.host}/newlogin/qrcode/generate.do"
        self.api_scan_status = f"{self.host}/newlogin/qrcode/query.do"
        self.api_h5_tk = "https://h5api.m.goofish.com/h5/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/"
        self.api_pc_login_token = (
            "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
        )

    def _cookie_marshal(self, cookies: dict):
        return "; ".join([f"{k}={v}" for k, v in cookies.items()])

    def get_cookie_dict(self) -> dict:
        return self.cookies

    def get_cookie_str(self) -> str:
        return self._cookie_marshal(self.cookies)

    def add_cookie_to_headers(self, data: dict) -> None:
        """通过给定的dict追加headers的值"""
        cookie = self.get_cookie_str()
        added = "; ".join(f"{k}={v}" for k, v in data.items())
        if cookie:
            self.headers.update({"cookie": "; ".join([cookie, added])})
        else:
            self.headers.update({"cookie": added})

    async def get_mh5tk(self) -> dict:
        """获取m_h5_tk和m_h5_tk_enc"""

        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": int(time()),
            "sign": "",
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": 20000,
            "api": "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get",
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.home.0.0",
        }
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(
                self.api_h5_tk, params=params, headers=self.headers
            )
            cookies = {}
            for k, v in resp.cookies.items():
                cookies[k] = v
                self.cookies[k] = v
            return cookies

    async def get_access_token(self) -> dict:
        """获取access_token"""

        cookie = self.get_cookie_dict()
        if not cookie or not cookie.get("unb"):
            raise NotLoginError("请先登录并获取有效的用户信息")
        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": str(int(time()) * 1000),
            "sign": "",
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.taobao.idlemessage.pc.login.token",
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.im.0.0",
        }
        sign_tool = SignTool()
        data_val = (
            '{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"%s"}'
            % sign_tool.generate_device_id(cookie["unb"])
        )
        data = {
            "data": data_val,
        }
        token = cookie["_m_h5_tk"].split("_")[0]
        sign = sign_tool.generate_sign(params["t"], token, data_val)
        params["sign"] = sign
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(
                self.api_pc_login_token,
                params=params,
                headers=self.headers,
                cookies=self.cookies,
                data=data,
            )
            return resp.json()

    async def _get_login_params(self) -> dict:
        """获取二维码登录时需要的表单参数"""

        self.params = {
            "lang": "zh_cn",
            "appName": "xianyu",
            "appEntrance": "web",
            "styleType": "vertical",
            "bizParams": "",
            "notLoadSsoView": False,
            "notKeepLogin": False,
            "isMobile": False,
            "qrCodeFirst": False,
            "stie": 77,
            "rnd": random(),
        }
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                self.api_mini_login,
                params=self.params,
                cookies=self.cookies,
                headers=self.headers,
            )
            pattern = r"window\.viewData\s*=\s*(\{.*?\});"
            # 正则匹配需要的json数据
            match = re.search(pattern, resp.text)
            if match:
                json_string = match.group(1)
                view_data = json.loads(json_string)
                data = view_data.get("loginFormData")
                data["umidTag"] = "SERVER"
                return data
            else:
                raise GetLoginParamsError("获取登录参数失败")

    async def _poll_qrcode_status(self) -> httpx.Response:
        """获取二维码扫描状态"""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(
                self.api_scan_status,
                data=self.params,
                cookies=self.cookies,
                headers=self.headers,
            )
            return resp

    async def generate_login_qrcode(self) -> dict | None:
        """获取登录二维码"""

        async with httpx.AsyncClient(follow_redirects=True) as client:
            params = await self._get_login_params()
            resp = await client.get(
                self.api_generate_qr, params=params, headers=self.headers
            )
            results = resp.json()
            if results.get("content", {}).get("success") == True:
                self.params.update(
                    {
                        "t": results["content"]["data"]["t"],
                        "ck": results["content"]["data"]["ck"],
                    }
                )
                qr_content = results["content"]["data"]["codeContent"]
                qr = qrcode.QRCode(
                    version=5,
                    error_correction=qrcode.constants.ERROR_CORRECT_L,
                    box_size=10,
                    border=2,
                )
                qr.add_data(qr_content)
                qr.make()
                qr_img = qr.make_image()
                qr_img.save("login.png")
                qr.print_ascii(invert=True, tty=True)
                print("请使用手机闲鱼app扫描二维码登录")
                while True:
                    await asyncio.sleep(0.8)
                    resp = await self._poll_qrcode_status()
                    qrcode_status = (
                        resp.json()
                        .get("content", {})
                        .get("data", {})
                        .get("qrCodeStatus")
                    )
                    if qrcode_status == "CONFIRMED":
                        if (
                            resp.json()
                            .get("content", {})
                            .get("data", {})
                            .get("iframeRedirect")
                            is True
                        ):
                            print("您的帐号被风控，需要前往以下网址通过手机验证码登录")
                            print(
                                resp.json()
                                .get("content", {})
                                .get("data", {})
                                .get("iframeRedirectUrl")
                            )
                            return
                        else:
                            print("用户信息已确认，登录成功")
                            cookie = {}
                            # 将cookie保存为字典形式
                            for k, v in resp.cookies.items():
                                cookie[k] = v
                                self.cookies[k] = v
                            return cookie
                    elif qrcode_status == "NEW":
                        continue  # 二维码未被扫描，继续轮询
                    elif qrcode_status == "EXPIRED":
                        print("二维码已过期，请重新获取")
                        break
                    elif qrcode_status == "SCANED":
                        print("二维码已被扫描，请在手机上确认登录")
                    else:
                        print("用户取消确认，登陆失败")
                        break

            else:
                raise GetLoginQRCodeError("获取登录二维码失败")
