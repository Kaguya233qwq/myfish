import asyncio
from core.login import Login


if __name__ == "__main__":
    login = Login()
    cookie = asyncio.run(login.get_mh5tk())
    login.add_to_cookie(cookie)
    asyncio.run(login.generate_login_qrcode())
    print(login.headers.get("cookie"))
