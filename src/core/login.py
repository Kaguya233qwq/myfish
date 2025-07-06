import asyncio
import json
from random import random
import re
import httpx
import qrcode


class GetLoginParamsError(Exception):
    """获取登录参数错误"""


class GetLoginQRCodeError(Exception):
    """获取登录二维码失败"""


class Login:

    def __init__(self):
        self.params = {}
        self.headers = self.generate_headers()
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
            "cookie": "cookie2=119612b35dcb0c8820dda0bc373796c4; mtop_partitioned_detect=1; _m_h5_tk=22ad4fad451cbf4a6406764014b2a643_1751818329466; _m_h5_tk_enc=39206459409cb7e4995aaf2437e8f628; cna=yWnxIDTIKw8CAbfxyYh/l881; xlly_s=1; _samesite_flag_=true; t=59433eaeaa0bd131784313c85e3fb30e; _tb_token_=7be858e6807d1; tfstk=gTkvzHtmQUY0QzkYqmRlS2jKNaKkKQmVmqoCIP4c143-AD701x2mW13mbVV6u-Dty-koiO4DuRns-RLH-pvn0mPUCeYhlVPR8RZ6IPOuGz1JkMTH-pvo0myaCe0isNVawlz7clabC3e7blE_CtZ62zZTY1_j5VtJ2lz75Rws58t8bzGoUsUN_P6ttCXrphu8yO6seoTaRmU84uk8DSUBLzBtgYEYMyijpEf9duMoeWVljUeSYb0b2JpA8ohSv-ZtQH1TW5H4elhyks0Kuli7wqxG_uhtfvy4pG9j2-UYOoFDBh3I2cG3wYxBL-eblf2qjMLm2x3m0YnGf_wYnbFKHJ9FlPcovAEtQFJ-JciE17HOkglKKv3S-w4LjstJ215aGulWf42u25XzquUHq0fN_7AQ2yxJ215aGur8-34l_1PkO",
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
                headers=self.generate_headers(),
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
                        set_cookie = resp.headers.get("set-cookie")
                        # 将新设置的cookie解析为headers格式
                        if set_cookie:
                            cookie_dict = {}
                            for cookie in set_cookie.split(";"):
                                if "=" in cookie:
                                    key, value = cookie.split("=", 1)
                                    cookie_dict[key.strip()] = value.strip()
                            self.headers["cookie"] += "; " + "; ".join(
                                f"{k}={v}" for k, v in cookie_dict.items()
                            )
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


if __name__ == "__main__":
    login = Login()
    asyncio.run(login.generate_login_qrcode())
