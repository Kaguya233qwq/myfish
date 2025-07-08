import asyncio
import json
from random import random
import re
from time import time
import httpx
import qrcode

from utils import generate_headers


class GetLoginParamsError(Exception):
    """获取登录参数错误"""


class GetLoginQRCodeError(Exception):
    """获取登录二维码失败"""


class Login:

    def __init__(self):
        self.params = {}
        self.headers = generate_headers()
        self.host = "https://passport.goofish.com"
        self.api_mini_login = f"{self.host}/mini_login.htm"
        self.api_generate_qr = f"{self.host}/newlogin/qrcode/generate.do"
        self.api_scan_status = f"{self.host}/newlogin/qrcode/query.do"
        self.get_h5_tk = "https://h5api.m.goofish.com/h5/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/"

    def add_to_cookie(self, data: dict):
        """通过给定的dict追加headers的值"""
        cookie = self.headers.get("cookie", "")
        added = "; ".join(f"{k}={v}" for k, v in data.items())
        if cookie:
            self.headers.update({"cookie": "; ".join([cookie, added])})
        else:
            self.headers.update({"cookie": added})

    async def get_mh5tk(self) -> dict:
        """获取m_h5_tk和m_h5_tk_enc"""

        async with httpx.AsyncClient(follow_redirects=True) as client:
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
            resp = await client.post(
                self.get_h5_tk, params=params, headers=self.headers
            )
            cookie = {}
            for k, v in resp.cookies.items():
                cookie[k] = v
            return cookie

    async def get_login_params(self) -> dict:
        """获取二维码登录时需要的表单参数"""

        async with httpx.AsyncClient(follow_redirects=True) as client:
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
            resp = await client.get(
                self.api_mini_login, params=self.params, headers=self.headers
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

    async def poll_qrcode_status(self) -> httpx.Response:
        """获取二维码扫描状态"""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(
                self.api_scan_status,
                data=self.params,
                headers=self.headers,
            )
            return resp

    async def generate_login_qrcode(self):
        """获取登录二维码"""

        async with httpx.AsyncClient(follow_redirects=True) as client:
            params = await self.get_login_params()
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
                    version=1,
                    error_correction=qrcode.ERROR_CORRECT_H,
                    box_size=1,
                    border=2,
                )
                qr.add_data(qr_content)
                qr.make(fit=True)
                qr_img = qr.make_image()
                qr_img.save("login.png")
                qr.print_ascii(invert=True)
                print("请使用手机闲鱼app扫描二维码登录")
                while True:
                    await asyncio.sleep(0.8)
                    resp = await self.poll_qrcode_status()
                    qrcode_status = (
                        resp.json()
                        .get("content", {})
                        .get("data", {})
                        .get("qrCodeStatus")
                    )
                    if qrcode_status == "CONFIRMED":
                        print("用户信息已确认，登录成功")
                        cookie = {}
                        # 将cookie保存为字典形式
                        for k, v in resp.cookies.items():
                            cookie[k] = v
                        self.add_to_cookie(cookie)
                        break
                    elif qrcode_status == "NEW":
                        continue  # 二维码未被扫描，继续轮询
                    elif qrcode_status == "EXPIRED":
                        print("二维码已过期，请重新获取")
                        break
                    elif qrcode_status == "SCANED":
                        print("二维码已被扫描，请在手机上确认登录")
                    else:
                        print("二维码状态未知，请稍后再试")
                        break

            else:
                raise GetLoginQRCodeError("获取登录二维码失败")
