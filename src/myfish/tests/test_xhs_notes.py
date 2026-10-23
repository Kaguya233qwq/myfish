import json
import unittest

import httpx
from pydantic import ValidationError

from myfish.adapters.xhs.api import XhsAPI, XhsAPIError
from myfish.adapters.xhs.notes import NoteRef

REF = NoteRef(note_id="note1", xsec_token="token=", xsec_source="pc_feed")


def item(identifier: str) -> dict:
    return {
        "id": identifier,
        "model_type": "note",
        "xsec_token": "token=",
        "note_card": {
            "type": "normal",
            "display_title": "Title",
            "user": {"user_id": "author"},
        },
    }


def comment(identifier: str) -> dict:
    return {
        "id": identifier,
        "note_id": "note1",
        "content": "text",
        "create_time": 0,
        "user_info": {"user_id": "author"},
    }


class NoteTests(unittest.IsolatedAsyncioTestCase):
    def test_note_links_require_explicit_security_token(self):
        for path in ("explore/note1", "discovery/item/note1"):
            ref = NoteRef.from_url(
                f"https://www.xiaohongshu.com/{path}?xsec_token=token%3D&xsec_source=pc_feed"
            )
            self.assertEqual(ref, REF)
        with self.assertRaises(ValueError):
            NoteRef.from_url("https://example.com/explore/note1?xsec_token=x")
        with self.assertRaises(ValidationError):
            NoteRef(note_id="note1", xsec_token="")

    async def test_recommendations_carry_context_and_skip_non_notes(self):
        requests = []
        pages = iter(
            [
                {
                    "cursor_score": "a",
                    "items": [item("note1"), {"id": "ad", "model_type": "ad"}],
                },
                {"cursor_score": "b", "items": [item("note1"), item("note2")]},
            ]
        )

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"success": True, "code": 0, "data": next(pages)}
            )

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            notes = [note async for note in api.iter_recommendations(pages=2, limit=2)]
        self.assertEqual([note.id for note in notes], ["note1", "note2"])
        self.assertEqual(notes[0].ref, REF)
        second = json.loads(requests[1].content)
        self.assertEqual(
            (second["cursor_score"], second["refresh_type"], second["note_index"]),
            ("a", 3, 2),
        )

    async def test_comment_cursor_and_sub_comment_context(self):
        requests = []
        pages = iter(
            [
                {"comments": [comment("c1")], "cursor": "a", "has_more": True},
                {
                    "comments": [comment("c1"), comment("c2")],
                    "cursor": "b",
                    "has_more": False,
                },
            ]
        )

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"success": True, "code": 0, "data": next(pages)}
            )

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            comments = [
                c async for c in api.iter_comments(REF, root_id="root", pages=2)
            ]
        self.assertEqual([c.id for c in comments], ["c1", "c2"])
        self.assertTrue(requests[0].url.path.endswith("/comment/sub/page"))
        self.assertEqual(requests[1].url.params["cursor"], "a")
        self.assertEqual(requests[0].url.params["root_comment_id"], "root")
        self.assertEqual(requests[0].url.params["xsec_token"], "token=")

    async def test_interactions_set_explicit_state_and_replies_target_comment(self):
        requests = []

        def handle(request):
            requests.append(request)
            data = (
                {"comment": comment("created")}
                if request.url.path.endswith("/comment/post")
                else {}
            )
            return httpx.Response(200, json={"success": True, "code": 0, "data": data})

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            await api.set_note_like("note1", True)
            await api.set_note_like("note1", False)
            await api.set_note_collect("note1", True)
            await api.set_note_collect("note1", False)
            result = await api.post_comment("note1", "text", reply_to="root")
            self.assertEqual(result.id, "created")
            await api.delete_comment("note1", result.id)
        self.assertEqual(
            [r.url.path.rsplit("/", 1)[-1] for r in requests],
            ["like", "dislike", "collect", "uncollect", "post", "delete"],
        )
        self.assertEqual(json.loads(requests[3].content), {"note_ids": "note1"})
        self.assertEqual(json.loads(requests[4].content)["target_comment_id"], "root")
        self.assertEqual(
            json.loads(requests[5].content),
            {"note_id": "note1", "comment_id": "created"},
        )

    async def test_interaction_rejection_is_not_retried(self):
        count = 0

        def handle(request):
            nonlocal count
            count += 1
            return httpx.Response(200, json={"success": False, "code": 42})

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            with self.assertRaises(XhsAPIError):
                await api.set_note_collect("note1", True)
        self.assertEqual(count, 1)
