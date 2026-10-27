import unittest

import httpx

from myfish.adapters.bilibili.api import (
    BilibiliAPI,
    BilibiliAPIError,
    BilibiliProtocolError,
)
from myfish.adapters.bilibili.model import UserRelation

_COOKIES = {
    "SESSDATA": "test-session",
    "bili_jct": "test-csrf",
    "buvid3": "test-device",
}


class BilibiliActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_like_and_unlike_send_explicit_actions_and_csrf(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            await api.like_video("BVtest")
            await api.like_video("BVtest", liked=False)
        self.assertEqual(
            [
                httpx.QueryParams(request.content.decode())["like"]
                for request in requests
            ],
            ["1", "2"],
        )
        for request in requests:
            self.assertEqual(request.url.path, "/x/web-interface/archive/like")
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                httpx.QueryParams(request.content.decode())["csrf"], "test-csrf"
            )
            self.assertEqual(
                httpx.QueryParams(request.content.decode())["bvid"], "BVtest"
            )

    async def test_favorite_and_remove_only_touch_selected_folders(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"prompt": True}})

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            await api.favorite_video(123, [11, 12, 11])
            await api.favorite_video(123, [12], favorited=False)
        added, removed = [
            httpx.QueryParams(request.content.decode()) for request in requests
        ]
        self.assertEqual(added["add_media_ids"], "11,12")
        self.assertEqual(added["del_media_ids"], "")
        self.assertEqual(removed["add_media_ids"], "")
        self.assertEqual(removed["del_media_ids"], "12")
        self.assertEqual(added["type"], "2")
        self.assertEqual(added["rid"], "123")
        self.assertEqual(added["csrf"], "test-csrf")
        self.assertTrue(
            all(request.url.path == "/x/v3/fav/resource/deal" for request in requests)
        )

    async def test_follow_and_unfollow_send_explicit_actions(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            await api.follow_user(2)
            await api.follow_user(2, followed=False)
        self.assertEqual(
            [
                httpx.QueryParams(request.content.decode())["act"]
                for request in requests
            ],
            ["1", "2"],
        )
        for request in requests:
            self.assertEqual(request.url.path, "/x/relation/modify")
            form = httpx.QueryParams(request.content.decode())
            self.assertEqual(form["fid"], "2")
            self.assertEqual(form["csrf"], "test-csrf")

    async def test_read_states_preserve_relationship_and_video_state(self):
        requests = []
        responses = [
            {"mid": 2, "attribute": 6, "mtime": 100},
            1,
            {"favoured": True, "count": 1},
        ]

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": responses.pop(0)})

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            relation = await api.get_user_relation(2)
            self.assertTrue(relation.following)
            self.assertTrue(relation.mutual)
            self.assertTrue(await api.get_video_like_status("BVtest"))
            self.assertTrue((await api.get_video_favorite_status(123)).favoured)
        self.assertEqual(requests[0].url.params["fid"], "2")
        self.assertEqual(requests[1].url.params["bvid"], "BVtest")
        self.assertEqual(requests[2].url.params["aid"], "123")

    async def test_unfollowed_relation_with_zero_mid_retains_requested_user(self):
        async with BilibiliAPI(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"code": 0, "data": {"mid": 0, "attribute": 0, "mtime": 0}},
                )
            )
        ) as api:
            relation = await api.get_user_relation(2)
        self.assertEqual(relation.mid, 2)
        self.assertFalse(relation.following)

    async def test_relation_for_another_user_is_rejected(self):
        async with BilibiliAPI(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"code": 0, "data": {"mid": 3, "attribute": 2}}
                )
            )
        ) as api:
            with self.assertRaises(BilibiliProtocolError):
                await api.get_user_relation(2)

    def test_relationship_attributes_do_not_confuse_blocked_with_followed(self):
        for attribute, following, mutual, blocked in (
            (0, False, False, False),
            (2, True, False, False),
            (6, True, True, False),
            (128, False, False, True),
        ):
            with self.subTest(attribute=attribute):
                relation = UserRelation(mid=2, attribute=attribute)
                self.assertEqual(
                    (relation.following, relation.mutual, relation.blocked),
                    (following, mutual, blocked),
                )

    async def test_invalid_ids_and_missing_csrf_never_issue_requests(self):
        def handle(_):
            self.fail("Invalid action issued a request")

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            for method, args, kwargs in (
                (api.like_video, (" ",), {}),
                (api.like_video, ("BVtest",), {}),
                (api.follow_user, (0,), {}),
                (api.follow_user, (2,), {}),
                (api.favorite_video, (0, [1]), {}),
                (api.favorite_video, (1, []), {}),
                (api.favorite_video, (1, [-1]), {}),
                (api.favorite_video, (1, [1]), {}),
                (api.get_user_relation, (0,), {}),
                (api.get_video_favorite_status, (0,), {}),
                (api.get_video_like_status, ("",), {}),
            ):
                with (
                    self.subTest(method=method.__name__, args=args),
                    self.assertRaises(ValueError),
                ):
                    await method(*args, **kwargs)

    async def test_write_failures_never_retry_or_convert_failure_to_success(self):
        for endpoint in ("like_video", "favorite_video", "follow_user"):
            for code in (-101, -111, -352, -412, 22014, 65006):
                with self.subTest(endpoint=endpoint, code=code):
                    await self._assert_failed_write(endpoint, code)

    async def _assert_failed_write(self, endpoint: str, code: int) -> None:
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": code})

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            with self.assertRaises(BilibiliAPIError) as raised:
                if endpoint == "like_video":
                    await api.like_video("BVtest")
                elif endpoint == "favorite_video":
                    await api.favorite_video(1, [2])
                else:
                    await api.follow_user(2)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(len(requests), 1)

    async def test_invalid_like_status_is_not_interpreted_as_false(self):
        async with BilibiliAPI(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"code": 0, "data": {"unexpected": 1}}
                )
            )
        ) as api:
            with self.assertRaises(BilibiliProtocolError):
                await api.get_video_like_status("BVtest")

    async def test_transport_failure_during_write_is_not_retried(self):
        requests = []

        def handle(request):
            requests.append(request)
            raise httpx.ReadTimeout("unknown outcome")

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            with self.assertRaises(httpx.ReadTimeout):
                await api.favorite_video(1, [2])
        self.assertEqual(len(requests), 1)
