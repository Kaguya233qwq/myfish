import asyncio
import json
from random import random
import re
import httpx


class GetLoginParamsError(Exception):
    """获取登录参数错误"""


class GetLoginQRCodeError(Exception):
    """获取登录二维码失败"""


class Login:

    def __init__(self):
        self.params = {}
        self.host = "https://passport.goofish.com"
        self.api_mini_login = f"{self.host}/mini_login.htm"
        self.api_generate_qr = f"{self.host}/newlogin/qrcode/generate.do"
        self.api_scan_status = f"{self.host}/newlogin/qrcode/query.do"

    def generate_headers(self):
        """生成请求头参数"""
        return {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "referer": "https://www.goofish.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0",
        }

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
                self.api_mini_login, params=self.params, headers=self.generate_headers()
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

    async def poll_qrcode_status(self) -> dict:
        """获取二维码扫描状态"""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(
                self.api_scan_status,
                data=self.params,
                headers=self.generate_headers(),
            )
            print(resp.json())
            return resp.json()

    async def generate_login_qrcode(self):
        """获取登录二维码"""

        async with httpx.AsyncClient(follow_redirects=True) as client:
            params = await self.get_login_params()
            resp = await client.get(
                self.api_generate_qr, params=params, headers=self.generate_headers()
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

                while True:
                    await asyncio.sleep(0.8)
                    resp = await self.poll_qrcode_status()
                    if resp.get("content", {}).get("data", {}).get("resultCode") == 100:
                        continue
                    else:
                        break

            else:
                raise GetLoginQRCodeError("获取登录二维码失败")


if __name__ == "__main__":
    login = Login()
    asyncio.run(login.generate_login_qrcode())
