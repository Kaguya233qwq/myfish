import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from myfish.adapters.xhs.api import XhsAPI, XhsAPIError
from myfish.adapters.xhs.creator import CreatorAPI
from myfish.adapters.xhs.ds import CreatorDS
from myfish.adapters.xhs.publish import (
    ImageAsset,
    ImagePost,
    PublishUncertainError,
    VideoAsset,
    VideoMetadata,
    VideoPost,
    VideoTrack,
    Visibility,
    post_payload,
)
from myfish.adapters.xhs.rap import RapContext, sign_rap
from myfish.adapters.xhs.sign import creator_headers, upload_authorization
from myfish.tests.test_xhs_creator import script

_RAP = json.loads((Path(__file__).parent / "fixtures/xhs_rap.json").read_text())
_FINGERPRINT = bytes.fromhex(_RAP["fingerprints"][0])
_NOW = 1792713600000
_ASSET = ImageAsset(
    file_id="image-01", size=512, width=32, height=24, mime_type="image/png"
)


def image_bytes(format: str = "PNG") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (32, 24), "blue").save(buffer, format=format)
    return buffer.getvalue()


def video_post() -> VideoPost:
    return VideoPost(
        video=VideoAsset(file_id="video-01", size=2048, video_id="transcode-01"),
        cover=_ASSET,
        metadata=VideoMetadata(
            video=VideoTrack(
                width=1920, height=1080, duration_ms=1500, frame_rate=30, format="AVC"
            )
        ),
    )


class PublishPayloadTests(unittest.TestCase):
    def test_upload_signatures_match_reference(self):
        cases = [
            (
                1709905283,
                1709912483,
                "synthetic_image-01",
                13975,
                "ros-upload.xiaohongshu.com",
                "9661e603dce9defb88955ae354b0a96f56cb4933",
            ),
            (
                1792713600,
                1792717200,
                "synthetic_video-02",
                10485761,
                "ros-upload-d4.xhscdn.com",
                "c1474f36373c972860c9f9450f33d0a70748599f",
            ),
            (
                1792713601,
                1792713602,
                "tiny",
                1,
                "bucket.myqcloud.com",
                "a9b2ae91b25a6b11b9a6a69da7ea41b42f0be53d",
            ),
        ]
        for start, end, identifier, size, host, signature in cases:
            with self.subTest(host=host):
                auth = upload_authorization(
                    "/spectrum/" + identifier, host, size, start, end
                )
                self.assertTrue(auth.endswith("q-signature=" + signature))
                self.assertIn(f"q-sign-time={start};{end}&", auth)

    def test_images_preserve_metadata_and_visibility_without_location(self):
        for visibility in Visibility:
            payload = post_payload(ImagePost(images=[_ASSET], visibility=visibility))
            common = payload["common"]
            self.assertEqual(common["privacy_info"]["type"], visibility.value)
            self.assertNotIn("post_loc", common)
            binds = json.loads(common["business_binds"])
            self.assertNotIn("coProduceBind", binds)
            self.assertFalse(binds["noteCopyBind"]["copyable"])
            source = json.loads(common["source"])
            self.assertEqual(json.loads(source["extraInfo"])["systemId"], "web")
            image = payload["image_info"]["images"][0]
            self.assertEqual((image["width"], image["height"]), (32, 24))
            self.assertEqual(
                json.loads(image["extra_info_json"])["mimeType"], "image/png"
            )
        self.assertEqual(ImagePost(images=[_ASSET]).visibility, Visibility.PRIVATE)

    def test_video_preserves_actual_metadata_and_explicit_cover(self):
        payload = post_payload(video_post())
        video = payload["video_info"]
        self.assertIsNone(payload["image_info"])
        self.assertEqual(video["composite_metadata"]["video"]["duration"], 1500)
        self.assertEqual(video["composite_metadata"]["video"]["format"], "AVC")
        self.assertEqual(video["composite_metadata"]["audio"], {})
        self.assertEqual(video["segments"]["items"][0]["duration"], 1.5)
        self.assertEqual(video["cover"]["file_id"], "spectrum/image-01")
        self.assertTrue(video["cover"]["frame"]["is_upload"])

    def test_schedule_uses_timing_bind(self):
        post = ImagePost(images=[_ASSET], scheduled_at_ms=_NOW + 3600000)
        binds = json.loads(post_payload(post)["common"]["business_binds"])
        self.assertEqual(binds["bizType"], 13)
        self.assertEqual(binds["notePostTiming"]["postTime"], str(post.scheduled_at_ms))


class PublishAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests: list[httpx.Request] = []
        self.account = XhsAPI({"a1": "a" * 52, "web_session": "private-cookie"})
        self.handler = self.respond

        async def handle(request: httpx.Request) -> httpx.Response:
            await request.aread()
            self.requests.append(request)
            return self.handler(request)

        self.creator = CreatorAPI(self.account, transport=httpx.MockTransport(handle))
        self.creator.session.clock = lambda: _NOW
        self.creator.ds = CreatorDS.from_script(script())
        self.creator._ds_loaded_at = _NOW

    async def asyncTearDown(self):
        await self.creator.close()
        await self.account.close()

    def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/permit"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "code": 0,
                    "data": {
                        "uploadTempPermits": [
                            {
                                "fileIds": ["spectrum/upload-01"],
                                "uploadAddr": "ros-upload-d4.xhscdn.com",
                                "token": "upload-token",
                                "expireTime": _NOW + 3600000,
                            }
                        ]
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200, headers={"X-Ros-Video-Id": "transcode-01"})
        if request.url.path.endswith("query_transcode"):
            return httpx.Response(
                200, json={"success": True, "data": {"hasFirstFrame": True}}
            )
        return httpx.Response(
            200, json={"success": True, "result": 0, "data": {"noteId": "note-01"}}
        )

    async def test_image_upload_streams_exact_bytes_without_account_headers(self):
        for format in ("PNG", "JPEG", "WEBP"):
            with self.subTest(format=format):
                content = image_bytes(format)
                asset = await self.creator.upload_image(content)
                self.assertEqual(
                    asset.mime_type,
                    "image/" + ("jpeg" if format == "JPEG" else format.lower()),
                )
                self.assertEqual(
                    (asset.width, asset.height, asset.size), (32, 24, len(content))
                )
                permit, upload = self.requests[-2:]
                self.assertEqual(permit.url.params["scene"], "image")
                self.assertIn("x-s", permit.headers)
                self.assertEqual(upload.content, content)
                self.assertEqual(
                    str(upload.url),
                    "https://ros-upload-d4.xhscdn.com/spectrum/upload-01",
                )
                self.assertEqual(upload.headers["content-length"], str(len(content)))
                self.assertEqual(upload.headers["x-cos-security-token"], "upload-token")
                for name in ("cookie", "x-s", "x-s-common"):
                    self.assertNotIn(name, upload.headers)
                self.assertEqual(
                    upload.headers["authorization"],
                    upload_authorization(
                        upload.url.path,
                        upload.url.host,
                        len(content),
                        _NOW // 1000,
                        _NOW // 1000 + 3600,
                    ),
                )

    async def test_path_upload_and_video_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.jpg"
            path.write_bytes(image_bytes("JPEG"))
            asset = await self.creator.upload_image(path)
            self.assertEqual(asset.mime_type, "image/jpeg")
        content = b"synthetic-video" * 100000
        video = await self.creator.upload_video(content)
        self.assertEqual(video.video_id, "transcode-01")
        self.assertEqual(video.size, len(content))
        self.assertEqual(self.requests[-1].content, content)
        self.assertEqual(self.requests[-2].url.params["scene"], "video")

    async def test_invalid_local_media_never_requests_permit(self):
        for source in (b"", b"invalid", Path("/nonexistent/myfish-image.png")):
            with self.subTest(source=source), self.assertRaises((OSError, ValueError)):
                await self.creator.upload_image(source)
        with self.assertRaises(ValueError):
            await self.creator.upload_video(b"")
        self.assertEqual(self.requests, [])

    async def test_bad_upload_permits_never_send_file(self):
        for key, value in (
            ("uploadAddr", "http://ros-upload.xiaohongshu.com"),
            ("uploadAddr", "ros-upload.example.com"),
            ("fileIds", ["../escape"]),
            ("expireTime", _NOW - 1),
        ):

            def respond(request, key=key, value=value):
                response = self.respond(request)
                body = response.json()
                body["data"]["uploadTempPermits"][0][key] = value
                return httpx.Response(200, json=body)

            self.handler = respond
            before = len(self.requests)
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                await self.creator.upload_image(image_bytes())
            self.assertEqual(len(self.requests) - before, 1)

    async def test_missing_video_receipt_is_an_error(self):
        self.handler = lambda request: (
            httpx.Response(200) if request.method == "PUT" else self.respond(request)
        )
        with self.assertRaises(XhsAPIError):
            await self.creator.upload_video(b"synthetic-video")
        self.assertEqual(len(self.requests), 2)

    async def test_publish_binds_rap_and_accepts_result_status(self):
        post = ImagePost(images=[_ASSET], title="测试标题", description="中文正文")
        with patch(
            "myfish.adapters.xhs.creator.creator_headers", wraps=creator_headers
        ) as signer:
            result = await self.creator.publish(post, rap_fingerprint=_FINGERPRINT)
        self.assertEqual(signer.call_args.args[3].environment_size, 1351)
        self.assertTrue(result.accepted)
        self.assertEqual(result.note_id, "note-01")
        request = self.requests[0]
        self.assertEqual(
            str(request.url), "https://edith.xiaohongshu.com/web_api/sns/v2/note"
        )
        self.assertIn("x-rap-param", request.headers)
        self.assertIn("x-s", request.headers)
        self.assertRegex(request.headers["x-b3-traceid"], r"^[0-9a-f]{16}$")
        self.assertRegex(request.headers["x-xray-traceid"], r"^[0-9a-f]{32}$")
        self.assertEqual(request.headers["priority"], "u=1, i")
        self.assertEqual(json.loads(request.content), post_payload(post))
        self.assertEqual(len(self.requests), 1)

    async def test_publish_generates_rap_from_existing_device_snapshot(self):
        parameters = RapContext(_NOW)
        original = dict(self.account.fingerprint.fields)
        for dimensions in ("1920;1080", "2560;1392", "1440;900"):
            self.creator.fingerprint.fields["x10"] = dimensions
            fingerprint = self.creator.get_rap_fingerprint()
            with patch(
                "myfish.adapters.xhs.creator.RapContext", return_value=parameters
            ):
                result = await self.creator.publish(ImagePost(images=[_ASSET]))
            request = self.requests[-1]
            self.assertTrue(result.accepted)
            self.assertEqual(
                request.headers["x-rap-param"],
                sign_rap(str(request.url), request.content, fingerprint, parameters),
            )
        self.assertEqual(self.account.fingerprint.fields, original)
        self.assertEqual(len(self.requests), 3)

    async def test_missing_device_dimensions_prevent_automatic_publication(self):
        for value in (None, "1920", "1920;bad", "0;1080", "١٩٢٠;1080"):
            self.creator.fingerprint.fields["x10"] = value
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                await self.creator.publish(ImagePost(images=[_ASSET]))
        self.assertEqual(self.requests, [])

    async def test_missing_note_id_does_not_turn_success_into_failure(self):
        self.handler = lambda _: httpx.Response(
            200, json={"success": True, "result": 0}
        )
        result = await self.creator.publish(
            ImagePost(images=[_ASSET]), rap_fingerprint=_FINGERPRINT
        )
        self.assertTrue(result.accepted)
        self.assertIsNone(result.note_id)

    async def test_rejected_and_uncertain_posts_are_not_retried(self):
        for failure in (False, True):

            def respond(request, failure=failure):
                if failure:
                    raise httpx.ReadTimeout("private response", request=request)
                return httpx.Response(200, json={"success": False, "result": -9059})

            self.handler = respond
            before = len(self.requests)
            with (
                self.subTest(timeout=failure),
                self.assertRaises(
                    PublishUncertainError if failure else XhsAPIError
                ) as error,
            ):
                await self.creator.publish(
                    ImagePost(images=[_ASSET]), rap_fingerprint=_FINGERPRINT
                )
            self.assertNotIn("private", str(error.exception))
            self.assertEqual(len(self.requests) - before, 1)
            if not failure:
                self.assertEqual(error.exception.code, -9059)

    async def test_invalid_fingerprint_and_past_schedule_fail_before_network(self):
        with self.assertRaises(ValueError):
            await self.creator.publish(ImagePost(images=[_ASSET]), rap_fingerprint=b"")
        with self.assertRaises(ValueError):
            await self.creator.publish(
                ImagePost(images=[_ASSET], scheduled_at_ms=_NOW),
                rap_fingerprint=_FINGERPRINT,
            )
        self.assertEqual(self.requests, [])

    async def test_video_waits_for_transcode_before_publication(self):
        await self.creator.publish(video_post(), rap_fingerprint=_FINGERPRINT)
        self.assertEqual([request.method for request in self.requests], ["GET", "POST"])
        self.assertEqual(self.requests[0].url.params["video_id"], "transcode-01")

    async def test_transcode_rejection_and_timeout_never_publish(self):
        self.handler = lambda _: httpx.Response(
            200, json={"success": False, "code": -1}
        )
        with self.assertRaises(XhsAPIError):
            await self.creator.publish(video_post(), rap_fingerprint=_FINGERPRINT)
        self.handler = lambda _: httpx.Response(
            200, json={"success": True, "data": {"status": 0}}
        )
        with self.assertRaises(TimeoutError):
            await self.creator.wait_for_transcode(
                "transcode-01", timeout=0.02, interval=1
            )
        self.assertTrue(all(request.method == "GET" for request in self.requests))
