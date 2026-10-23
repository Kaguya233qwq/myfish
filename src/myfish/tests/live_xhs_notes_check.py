import argparse
import asyncio
from pathlib import Path

from myfish.adapters.xhs.api import XhsAPI


async def check(path: Path) -> None:
    async with XhsAPI.from_state(path) as api:
        try:
            items = [item async for item in api.iter_recommendations(pages=2, limit=3)]
            print(f"recommendations: {len(items)} unique notes", flush=True)
            if not items:
                raise ValueError("No notes available for read verification")
            ref = items[0].ref
            note = await api.get_note(ref)
            print(f"detail: parsed; images={len(note.card.image_list)}", flush=True)
            comments = [comment async for comment in api.iter_comments(ref, pages=2)]
            print(f"comments: {len(comments)} unique roots", flush=True)
            if comments:
                replies = [
                    comment
                    async for comment in api.iter_comments(
                        ref, root_id=comments[0].id, pages=2
                    )
                ]
                print(f"replies: {len(replies)} unique replies", flush=True)
        finally:
            api.save_state(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(check(args.state))


if __name__ == "__main__":
    main()
