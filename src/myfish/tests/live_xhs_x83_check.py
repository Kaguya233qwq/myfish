import argparse
import asyncio
import tempfile
from pathlib import Path

import httpx
from pydantic import JsonValue, TypeAdapter

from myfish.adapters.xhs.api import XhsAPIError
from myfish.adapters.xhs.auth import run


async def check(reference: Path) -> None:
    fields = TypeAdapter(dict[str, JsonValue]).validate_json(reference.read_bytes())
    value = fields.get("x83")
    if not isinstance(value, str) or not value:
        raise ValueError("Reference x83 must be a nonempty string")
    failed = False
    with tempfile.TemporaryDirectory(prefix="myfish-x83-") as directory:
        root = Path(directory)
        empty = root / "empty.json"
        empty.write_text('{"x83":""}', encoding="utf-8")
        for index, (name, profile) in enumerate(
            (
                ("empty", empty),
                ("reference", reference),
                ("reference", reference),
                ("empty", empty),
            ),
            start=1,
        ):
            print(f"[{index}/4] x83={name}", flush=True)
            try:
                await run(
                    root / "cookies.json",
                    "xys",
                    probe_device=True,
                    current_sign=True,
                    host_device=True,
                    profile_fields=profile,
                )
            except XhsAPIError as exc:
                failed = True
                print(f"result: {exc}", flush=True)
            except httpx.TransportError as exc:
                failed = True
                print(f"result: transport error ({type(exc).__name__})", flush=True)
            else:
                print("result: accepted", flush=True)
    if failed:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(check(args.reference))


if __name__ == "__main__":
    main()
