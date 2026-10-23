import argparse
import asyncio
from pathlib import Path

from myfish.adapters.xhs.api import XhsAPI
from myfish.adapters.xhs.stream import XhsStream


async def check(path: Path, timeout: float) -> None:
    if not 0 < timeout <= 600:
        raise ValueError("Observation timeout must be within 0–600 seconds")
    async with XhsAPI.from_state(path) as api:
        try:
            async with XhsStream(api) as stream:
                print("Push handshake and IM registration accepted", flush=True)
                count = 0
                try:
                    async with asyncio.timeout(timeout):
                        async for message in stream.events(reconnects=0):
                            count += 1
                            print(
                                f"event {count}: {type(message).__name__}", flush=True
                            )
                except TimeoutError:
                    print(f"Observation finished; decoded events: {count}", flush=True)
        finally:
            api.save_state(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    asyncio.run(check(args.state, args.timeout))


if __name__ == "__main__":
    main()
