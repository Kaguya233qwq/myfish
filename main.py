import asyncio
from core.login import Login


if __name__ == "__main__":
    login = Login()
    cookie = asyncio.run(login.get_mh5tk())
    login.add_cookie_to_headers(cookie)
    asyncio.run(login.generate_login_qrcode())
    results = asyncio.run(login.get_access_token())
    print(results)
