import unittest

import httpx

from myfish.adapters.xhs.api import XhsAPI, XhsAPIError


def user_note(identifier, *, token="detail-token"):
    return {
        "note_id": identifier,
        "type": "normal",
        "display_title": "作品标题",
        "user": {"user_id": "author", "nick_name": "作者"},
        "interact_info": {"liked_count": "2"},
        "cover": {"url_default": "https://example.com/cover.jpg"},
        "time": 1700000000000,
        "xsec_token": token,
    }


class UserNotesTests(unittest.IsolatedAsyncioTestCase):
    def client(self, pages, requests):
        responses = iter(pages)

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"success": True, "code": 0, "data": next(responses)}
            )

        return XhsAPI({"a1": "a" * 52}, transport=httpx.MockTransport(handle))

    async def test_page_keeps_profile_token_separate_from_note_reference(self):
        requests = []
        async with self.client(
            [{"notes": [user_note("note")], "cursor": "next", "has_more": True}],
            requests,
        ) as api:
            page = await api.get_user_notes(
                "author",
                cursor="previous",
                limit=2,
                xsec_token="profile-token=",
                xsec_source="pc_feed",
            )
        request = requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.path, "/api/sns/web/v1/user_posted")
        self.assertEqual(
            dict(request.url.params),
            {
                "num": "2",
                "cursor": "previous",
                "user_id": "author",
                "image_formats": "jpg,webp,avif",
                "xsec_token": "profile-token=",
                "xsec_source": "pc_feed",
            },
        )
        self.assertEqual(page.cursor, "next")
        note = page.notes[0]
        self.assertEqual(note.title, "作品标题")
        self.assertEqual(note.user.nickname, "作者")
        self.assertEqual(note.time, 1700000000000)
        self.assertEqual(note.ref.xsec_token, "detail-token")
        self.assertEqual(note.ref.xsec_source, "pc_user")

    async def test_pagination_deduplicates_pinned_notes_and_keeps_advancing(self):
        requests = []
        pages = [
            {
                "notes": [user_note("pinned"), user_note("first")],
                "cursor": "a",
                "has_more": True,
            },
            {"notes": [user_note("pinned")], "cursor": "b", "has_more": True},
            {
                "notes": [user_note("second"), user_note("first")],
                "cursor": "c",
                "has_more": False,
            },
        ]
        async with self.client(pages, requests) as api:
            result = [
                note.note_id
                async for note in api.iter_user_notes(
                    "author", pages=5, limit=2, xsec_token="profile"
                )
            ]
        self.assertEqual(result, ["pinned", "first", "second"])
        self.assertEqual(
            [request.url.params["cursor"] for request in requests], ["", "a", "b"]
        )
        self.assertTrue(
            all(request.url.params["xsec_token"] == "profile" for request in requests)
        )

    async def test_cursor_cycle_stops_instead_of_looping(self):
        requests = []
        pages = [
            {"notes": [user_note("one")], "cursor": "a", "has_more": True},
            {"notes": [user_note("two")], "cursor": "b", "has_more": True},
            {"notes": [user_note("three")], "cursor": "a", "has_more": True},
        ]
        async with self.client(pages, requests) as api:
            result = [
                note.note_id async for note in api.iter_user_notes("author", pages=10)
            ]
        self.assertEqual(result, ["one", "two", "three"])
        self.assertEqual(len(requests), 3)

    async def test_empty_page_and_page_limit_stop_requests(self):
        for page in (
            {"notes": [], "has_more": True, "cursor": "next"},
            {"has_more": False},
        ):
            requests = []
            async with self.client([page], requests) as api:
                self.assertEqual(
                    [item async for item in api.iter_user_notes("author", pages=3)], []
                )
            self.assertEqual(len(requests), 1)
        requests = []
        async with self.client(
            [{"notes": [user_note("one")], "has_more": True, "cursor": "next"}],
            requests,
        ) as api:
            self.assertEqual(
                len([item async for item in api.iter_user_notes("author")]), 1
            )
        self.assertEqual(len(requests), 1)

    async def test_invalid_arguments_never_send_a_request(self):
        requests = []
        async with self.client([], requests) as api:
            for user, limit in (("", 1), ("bad/id", 1), ("author", 0), ("author", 31)):
                with self.assertRaises(ValueError):
                    await api.get_user_notes(user, limit=limit)
            with self.assertRaises(ValueError):
                _ = [item async for item in api.iter_user_notes("author", pages=0)]
        self.assertEqual(requests, [])

    async def test_missing_detail_token_does_not_invent_a_reference(self):
        requests = []
        async with self.client(
            [{"notes": [user_note("note", token="")], "has_more": False}], requests
        ) as api:
            page = await api.get_user_notes("author")
        self.assertEqual(page.notes[0].note_id, "note")
        with self.assertRaises(ValueError):
            _ = page.notes[0].ref

    async def test_business_failure_is_not_retried(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"success": False, "code": -1})

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            with self.assertRaises(XhsAPIError) as raised:
                await api.get_user_notes("author")
        self.assertEqual(raised.exception.code, -1)
        self.assertEqual(len(requests), 1)
