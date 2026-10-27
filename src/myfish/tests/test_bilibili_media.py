import unittest
from unittest.mock import patch

import httpx

from myfish.adapters.bilibili.api import (
    BilibiliAPI,
    BilibiliAPIError,
    BilibiliProtocolError,
)
from myfish.adapters.bilibili.model import (
    DashStream,
    PlayUrls,
    Subtitle,
    SubtitleList,
    SubtitleTrack,
)


def track(url: str = "//aisubtitle.hdslb.com/test.json") -> SubtitleTrack:
    return SubtitleTrack(
        id=123, lan="ai-zh", lan_doc="中文", subtitle_url=url, ai_type=1
    )


def stream(identifier=32, *, snake=False):
    fields = {
        "id": identifier,
        "bandwidth": 10000,
        "codecs": "avc1.64001E",
        "width": 640,
        "height": 360,
        "codecid": 7,
    }
    if snake:
        return {
            **fields,
            "base_url": "https://cdn.bilivideo.com/video",
            "backup_url": None,
            "mime_type": "video/mp4",
            "frame_rate": "30",
            "segment_base": {"initialization": "0-100", "index_range": "101-200"},
        }
    return {
        **fields,
        "baseUrl": "https://cdn.bilivideo.com/video",
        "backupUrl": ["https://backup.bilivideo.com/video"],
        "mimeType": "video/mp4",
        "frameRate": "30",
        "SegmentBase": {"Initialization": "0-100", "indexRange": "101-200"},
    }


def dash():
    audio = {
        "id": 30280,
        "base_url": "https://cdn.bilivideo.com/audio",
        "backup_url": [],
        "bandwidth": 1000,
        "mime_type": "audio/mp4",
        "codecs": "mp4a.40.2",
        "segment_base": "",
    }
    return {
        "quality": 32,
        "durl": None,
        "accept_quality": [32, 16],
        "timelength": 10000,
        "dash": {
            "duration": 10,
            "video": [stream(), stream(16, snake=True)],
            "audio": [audio],
        },
    }


class BilibiliMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_subtitles_and_playback_use_wbi_and_share_key_cache(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path.endswith("/nav"):
                data = {
                    "wbi_img": {
                        "img_url": "https://test.invalid/" + "a" * 32 + ".png",
                        "sub_url": "https://test.invalid/" + "b" * 32 + ".png",
                    }
                }
            elif request.url.path.endswith("/v2"):
                data = {
                    "bvid": "BVtest",
                    "cid": 10,
                    "need_login_subtitle": False,
                    "subtitle": {"subtitles": [track().model_dump()]},
                }
            else:
                data = dash()
            return httpx.Response(200, json={"code": 0, "data": data})

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            subtitles = await api.get_subtitles("BVtest", 10)
            playback = await api.get_play_urls(
                "BVtest", 10, quality=32, stream_format="dash"
            )
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[1].url.path, "/x/player/wbi/v2")
        self.assertEqual(requests[2].url.path, "/x/player/wbi/playurl")
        for request in requests[1:]:
            self.assertEqual(request.url.params["bvid"], "BVtest")
            self.assertEqual(request.url.params["cid"], "10")
            self.assertIn("w_rid", request.url.params)
        self.assertEqual(requests[2].url.params["fnval"], "16")
        self.assertEqual(
            subtitles.tracks[0].subtitle_url, "https://aisubtitle.hdslb.com/test.json"
        )
        self.assertFalse(subtitles.need_login)
        self.assertEqual(len(playback.dash.video), 2)
        self.assertEqual(playback.dash.audio[0].id, 30280)
        self.assertEqual(playback.dash.audio[0].segment_base, None)
        self.assertEqual(playback.headers["referer"], "https://www.bilibili.com/")
        self.assertNotIn("cookie", playback.headers)
        self.assertTrue(api._media_client.is_closed)

    async def test_mp4_default_preserves_durl_and_platform_quality_fallback(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "quality": 32,
                        "durl": [
                            {
                                "url": "https://cdn.bilivideo.com/video",
                                "backup_url": ["https://backup.bilivideo.com/video"],
                                "size": 1000,
                                "length": 10000,
                            }
                        ],
                    },
                },
            )

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            with patch.object(
                api, "_wbi_params", return_value={"w_rid": "test", "fnval": 1}
            ):
                result = await api.get_play_urls("BVtest", 10, quality=80)
        self.assertEqual(result.quality, 32)
        self.assertEqual(
            result.durl[0].backup_urls, ["https://backup.bilivideo.com/video"]
        )
        self.assertIsNone(result.dash)

    async def test_subtitle_body_has_timing_and_text_without_auth_cookies(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "body": [
                        {"from": 0, "to": 1.5, "content": "你好", "location": 2},
                        {"from": 1.5, "to": 3, "content": "world"},
                    ]
                },
            )

        async with BilibiliAPI(
            {"SESSDATA": "secret", "bili_jct": "csrf"},
            transport=httpx.MockTransport(handle),
        ) as api:
            subtitle = await api.get_subtitle(
                track("https://subtitle.bilibili.com/test.json")
            )
        self.assertEqual(subtitle.text, "你好\nworld")
        self.assertEqual(subtitle.body[0].start, 0)
        self.assertEqual(subtitle.body[0].end, 1.5)
        self.assertEqual(requests[0].headers["referer"], "https://www.bilibili.com/")
        self.assertNotIn("cookie", requests[0].headers)

    def test_no_subtitles_and_login_required_remain_distinct(self):
        for subtitle in (None, {}, {"subtitles": []}, {"subtitles": None}):
            with self.subTest(subtitle=subtitle):
                result = SubtitleList.model_validate(
                    {
                        "bvid": "BVtest",
                        "cid": 10,
                        "subtitle": subtitle,
                        "need_login_subtitle": True,
                    }
                )
                self.assertEqual(result.tracks, [])
                self.assertTrue(result.need_login)
        self.assertFalse(SubtitleList(bvid="BVtest", cid=10).need_login)
        with self.assertRaises(ValueError):
            SubtitleList.model_validate(
                {"bvid": "BVtest", "cid": 10, "subtitle": "invalid"}
            )

    async def test_subtitle_metadata_for_another_part_is_rejected(self):
        async with BilibiliAPI(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"code": 0, "data": {"bvid": "BVtest", "cid": 20}}
                )
            )
        ) as api:
            with (
                patch.object(api, "_wbi_params", return_value={}),
                self.assertRaises(BilibiliProtocolError),
            ):
                await api.get_subtitles("BVtest", 10)

    def test_dash_supports_both_field_spellings_and_silent_video(self):
        camel = DashStream.model_validate(stream())
        snake = DashStream.model_validate(stream(snake=True))
        self.assertEqual(camel.base_url, snake.base_url)
        self.assertEqual(camel.segment_base, snake.segment_base)
        self.assertEqual(snake.backup_urls, [])
        data = dash()
        data["dash"]["audio"] = None
        self.assertEqual(PlayUrls.model_validate(data).dash.audio, [])

    async def test_missing_media_does_not_become_an_empty_success(self):
        for data in (
            {"quality": 32},
            {"quality": 32, "dash": {"duration": 10, "video": [], "audio": None}},
        ):
            with self.subTest(data=data):
                async with BilibiliAPI(
                    transport=httpx.MockTransport(
                        lambda _, payload=data: httpx.Response(
                            200, json={"code": 0, "data": payload}
                        )
                    )
                ) as api:
                    with (
                        patch.object(api, "_wbi_params", return_value={}),
                        self.assertRaises(BilibiliProtocolError),
                    ):
                        await api.get_play_urls("BVtest", 10, stream_format="dash")

    async def test_invalid_inputs_do_not_issue_requests(self):
        def handle(_):
            self.fail("Invalid input issued a request")

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            for method, args, kwargs in (
                (api.get_subtitles, ("", 10), {}),
                (api.get_subtitles, ("BVtest", 0), {}),
                (api.get_play_urls, ("BVtest", 10), {"stream_format": "invalid"}),
            ):
                with (
                    self.subTest(method=method.__name__),
                    self.assertRaises(ValueError),
                ):
                    await method(*args, **kwargs)
            for url in (
                "https://example.com/test.json",
                "https://hdslb.com.example.com/test.json",
                "https://user@aisubtitle.hdslb.com/test.json",
                "https://aisubtitle.hdslb.com:8443/test.json",
            ):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    await api.get_subtitle(track(url))

    async def test_expired_subtitle_and_redirects_are_not_silently_followed(self):
        for status in (403, 302):
            with self.subTest(status=status):
                requests = []

                def handle(request, captured=requests, response_status=status):
                    captured.append(request)
                    return httpx.Response(
                        response_status,
                        headers={"location": "https://example.com/test.json"},
                    )

                async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
                    with self.assertRaises(BilibiliAPIError) as raised:
                        await api.get_subtitle(track())
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(len(requests), 1)

    async def test_invalid_subtitle_json_and_timestamps_raise_protocol_errors(self):
        for content in (
            b"html",
            b"{}",
            b'{"body":[{"from":2,"to":1,"content":"bad"}]}',
            b'{"body":[{"from":NaN,"to":2,"content":"bad"}]}',
        ):
            with self.subTest(content=content):
                async with BilibiliAPI(
                    transport=httpx.MockTransport(
                        lambda _, payload=content: httpx.Response(200, content=payload)
                    )
                ) as api:
                    with self.assertRaises(BilibiliProtocolError):
                        await api.get_subtitle(track())

    def test_media_models_can_be_serialized_and_restored(self):
        playback = PlayUrls.model_validate(dash())
        self.assertEqual(
            PlayUrls.model_validate_json(playback.model_dump_json()), playback
        )
        subtitles = SubtitleList.model_validate(
            {
                "bvid": "BVtest",
                "cid": 10,
                "subtitle": {"subtitles": [track().model_dump()]},
                "need_login_subtitle": True,
            }
        )
        self.assertEqual(
            SubtitleList.model_validate_json(subtitles.model_dump_json()), subtitles
        )
        text = Subtitle.model_validate(
            {"body": [{"from": 0, "to": 1, "content": "test"}]}
        )
        self.assertEqual(Subtitle.model_validate_json(text.model_dump_json()), text)

    async def test_subtitle_size_limit_is_enforced(self):
        async with BilibiliAPI(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"body": []})
            )
        ) as api:
            with (
                patch("myfish.adapters.bilibili.api._SUBTITLE_LIMIT", 8),
                self.assertRaises(BilibiliProtocolError),
            ):
                await api.get_subtitle(track())
