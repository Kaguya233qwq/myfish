import asyncio
from core.login import Login


async def async_login():
    login = Login()
    await login.get_mh5tk()
    await login.generate_login_qrcode()
    access_token = await login.get_access_token()
    print(access_token)
    print(login.get_cookie_str())


if __name__ == "__main__":
    asyncio.run(async_login())
