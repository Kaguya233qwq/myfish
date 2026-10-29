import time
import unittest

import httpx

from myfish.adapters.bilibili.api import (
    BilibiliAPI,
    BilibiliAPIError,
    BilibiliProtocolError,
)
from myfish.adapters.bilibili.model import (
    LiveSearchPage,
    SearchCourse,
    SearchPage,
    SearchQuery,
    SearchUser,
    SearchVideo,
)


def search_data(result):
    return {
        "page": 2,
        "pagesize": 20,
        "numResults": 25,
        "numPages": 2,
        "result": result,
    }


def video():
    return {
        "type": "video",
        "aid": 123,
        "bvid": "BVtest",
        "title": '<em class="keyword">猫</em> &amp; 狗',
        "mid": 2,
        "author": "test",
    }


class BilibiliSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_signs_keyword_pagination_sort_and_video_filters(self):
        requests = []

        def handle(request):
            requests.append(request)
            data = (
                {
                    "wbi_img": {
                        "img_url": "https://test.invalid/" + "a" * 32 + ".png",
                        "sub_url": "https://test.invalid/" + "b" * 32 + ".png",
                    }
                }
                if request.url.path.endswith("/nav")
                else search_data([video()])
            )
            return httpx.Response(200, json={"code": 0, "data": data})

        async with BilibiliAPI(
            {"buvid3": "device"}, transport=httpx.MockTransport(handle)
        ) as api:
            result = await api.search(
                " 猫 & dog ", page=2, order="pubdate", duration=2, tids=17
            )
        self.assertIsInstance(result, SearchPage)
        self.assertIsInstance(result.items[0], SearchVideo)
        self.assertEqual(result.items[0].title, "猫 & 狗")
        self.assertEqual(result.total, 25)
        self.assertEqual(result.pages, 2)
        params = requests[-1].url.params
        self.assertEqual(requests[-1].url.path, "/x/web-interface/wbi/search/type")
        self.assertEqual(params["keyword"], "猫 & dog")
        self.assertEqual(params["order"], "pubdate")
        self.assertEqual(params["page"], "2")
        self.assertEqual(params["duration"], "2")
        self.assertEqual(params["tids"], "17")
        self.assertIn("w_rid", params)
        self.assertEqual(requests[-1].headers["referer"], "https://www.bilibili.com/")

    async def test_missing_device_cookies_are_fetched_once_and_sent_with_search(self):
        requests = []

        def handle(request):
            requests.append(request)
            data = (
                {"b_3": "device-3", "b_4": "device-4"}
                if request.url.path.endswith("/spi")
                else search_data([])
            )
            return httpx.Response(200, json={"code": 0, "data": data})

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            api._wbi = "a" * 32, "b" * 32
            api._wbi_loaded = time.monotonic()
            await api.search("test")
            await api.search("test", page=2)
        self.assertEqual(len(requests), 3)
        self.assertIn("buvid3=device-3", requests[-1].headers["cookie"])
        self.assertIn("buvid4=device-4", requests[-1].headers["cookie"])

    async def test_user_search_preserves_sort_direction_and_result_type(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": search_data(
                        [
                            {
                                "type": "bili_user",
                                "mid": 2,
                                "uname": '<em class="keyword">test</em>',
                                "fans": 20,
                            }
                        ]
                    ),
                },
            )

        async with BilibiliAPI(
            {"buvid3": "device"}, transport=httpx.MockTransport(handle)
        ) as api:
            api._wbi = "a" * 32, "b" * 32
            api._wbi_loaded = time.monotonic()
            result = await api.search(
                "test", kind="bili_user", order="fans", order_sort=1, user_type=1
            )
        self.assertIsInstance(result.items[0], SearchUser)
        self.assertEqual(result.items[0].uname, "test")
        params = requests[0].url.params
        self.assertEqual(params["search_type"], "bili_user")
        self.assertEqual(params["order_sort"], "1")
        self.assertNotIn("duration", params)

    def test_all_documented_result_categories_are_typed(self):
        records = [
            video(),
            {"type": "bili_user", "mid": 2, "uname": "test"},
            {"type": "article", "id": 1, "mid": 2, "title": "test"},
            {
                "type": "live_room",
                "uid": 2,
                "roomid": 3,
                "uname": "test",
                "title": "test",
            },
            {"type": "live_user", "uid": 2, "roomid": 3, "uname": "test"},
            {"type": "media_bangumi", "media_id": 1, "season_id": 2, "title": "test"},
            {"type": "media_ft", "media_id": 1, "season_id": 2, "title": "test"},
            {"type": "topic", "id": 1, "title": "test"},
            {"type": "photo", "id": 1, "mid": 2, "uname": "test", "title": "test"},
        ]
        result = SearchPage.model_validate(search_data(records))
        self.assertEqual(
            [item.type for item in result.items], [item["type"] for item in records]
        )

    async def test_video_search_preserves_mixed_course_results(self):
        course = {
            "type": "ketang",
            "id": 26368,
            "aid": 26368,
            "bvid": "",
            "mid": 2,
            "author": "teacher",
            "title": '<em class="keyword">Python</em>课程',
        }
        data = search_data([video(), course, video()])
        async with BilibiliAPI(
            {"buvid3": "device"},
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"code": 0, "data": data})
            ),
        ) as api:
            api._wbi = "a" * 32, "b" * 32
            api._wbi_loaded = time.monotonic()
            result = await api.search("Python")
        self.assertEqual(
            [type(item) for item in result.items],
            [SearchVideo, SearchCourse, SearchVideo],
        )
        self.assertEqual(result.items[1].title, "Python课程")
        self.assertEqual(result.items[1].id, 26368)

    async def test_combined_live_search_keeps_rooms_and_streamers_separate(self):
        data = search_data(
            {
                "live_room": [
                    {
                        "type": "live_room",
                        "uid": 2,
                        "roomid": 3,
                        "uname": "test",
                        "title": "test",
                    }
                ],
                "live_user": [
                    {"type": "live_user", "uid": 2, "roomid": 3, "uname": "test"}
                ],
            }
        )
        async with BilibiliAPI(
            {"buvid3": "device"},
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"code": 0, "data": data})
            ),
        ) as api:
            api._wbi = "a" * 32, "b" * 32
            api._wbi_loaded = time.monotonic()
            result = await api.search("test", kind="live", order="online")
        self.assertIsInstance(result, LiveSearchPage)
        self.assertEqual(result.items.live_room[0].roomid, 3)
        self.assertEqual(result.items.live_user[0].uid, 2)

    async def test_invalid_search_combinations_never_issue_requests(self):
        def handle(_):
            self.fail("Invalid query issued a request")

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            for options in (
                {"keyword": " "},
                {"keyword": "test", "page": 0},
                {"keyword": "test", "kind": "unknown"},
                {"keyword": "test", "kind": "video", "order": "fans"},
                {"keyword": "test", "kind": "bili_user", "duration": 1},
                {"keyword": "test", "duration": 5},
                {"keyword": "test", "user_type": 1},
                {"keyword": "test", "kind": "bili_user", "order_sort": 1},
                {"keyword": "test", "category_id": 1},
            ):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    await api.search(**options)

    async def test_category_filters_and_sort_orders(self):
        for kind, order in (
            ("video", "click"),
            ("article", "attention"),
            ("photo", "stow"),
            ("live_room", "live_time"),
            ("bili_user", "level"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(
                    SearchQuery(keyword="test", kind=kind, order=order).order,
                    order,
                )
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": search_data([])})

        async with BilibiliAPI(
            {"buvid3": "device"}, transport=httpx.MockTransport(handle)
        ) as api:
            api._wbi = "a" * 32, "b" * 32
            api._wbi_loaded = time.monotonic()
            await api.search("test", kind="article", category_id=16)
            await api.search("test", kind="media_ft")
        self.assertEqual(requests[0].url.params["category_id"], "16")
        self.assertEqual(requests[1].url.params["search_type"], "media_ft")
        self.assertNotIn("category_id", requests[1].url.params)
        self.assertNotIn("duration", requests[1].url.params)

    def test_empty_search_results_accept_null_or_missing_list(self):
        data = {"page": 1, "numPages": 0, "numResults": 0}
        self.assertEqual(SearchPage.model_validate(data).items, [])
        self.assertEqual(SearchPage.model_validate({**data, "result": None}).items, [])

    async def test_search_block_is_reported_without_retry(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": -412})

        async with BilibiliAPI(
            {"buvid3": "device"}, transport=httpx.MockTransport(handle)
        ) as api:
            api._wbi = "a" * 32, "b" * 32
            api._wbi_loaded = time.monotonic()
            with self.assertRaises(BilibiliAPIError) as raised:
                await api.search("test")
        self.assertEqual(raised.exception.code, -412)
        self.assertEqual(len(requests), 1)

    async def test_unexpected_search_result_is_a_protocol_error(self):
        async with BilibiliAPI(
            {"buvid3": "device"},
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": search_data(
                            [{"type": "video", "title": "missing IDs"}]
                        ),
                    },
                )
            ),
        ) as api:
            api._wbi = "a" * 32, "b" * 32
            api._wbi_loaded = time.monotonic()
            with self.assertRaises(BilibiliProtocolError):
                await api.search("test")
