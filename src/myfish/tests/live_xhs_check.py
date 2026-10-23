import argparse
import asyncio
import json
from pathlib import Path

import httpx

from myfish.adapters.xhs.api import SignFormat, XhsAPI, XhsAPIError


async def check(
    path: Path, sign_format: SignFormat, peer: str | None, *, state: bool = False
) -> None:
    if state:
        client = XhsAPI.from_state(path)
    else:
        cookies = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cookies, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in cookies.items()
        ):
            raise ValueError("Cookie file must contain a JSON object of strings")
        client = XhsAPI(cookies, sign_format=sign_format)
    failed = False
    async with client as api:
        operations = [("self", api.get_self_info), ("chats", api.get_chats)]
        for name, operation in operations:
            try:
                await operation()
            except XhsAPIError as exc:
                failed = True
                print(f"{name}: {exc}")
            except httpx.TransportError as exc:
                failed = True
                print(f"{name}: transport error ({type(exc).__name__})")
            else:
                print(f"{name}: HTTP 200, success=true, code=0")
        if peer:
            try:
                await api.get_message_history(peer)
            except XhsAPIError as exc:
                failed = True
                print(f"history: {exc}")
            except httpx.TransportError as exc:
                failed = True
                print(f"history: transport error ({type(exc).__name__})")
            else:
                print("history: HTTP 200, success=true, code=0")
    if state:
        client.save_state(path)
    if failed:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--cookies", type=Path)
    credentials.add_argument("--state", type=Path)
    parser.add_argument("--sign-format", choices=("xys", "xyw"), default="xys")
    parser.add_argument("--peer")
    args = parser.parse_args()
    asyncio.run(
        check(
            args.state or args.cookies,
            args.sign_format,
            args.peer,
            state=args.state is not None,
        )
    )


if __name__ == "__main__":
    main()
