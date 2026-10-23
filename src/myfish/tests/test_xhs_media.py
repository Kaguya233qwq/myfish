import json
import unittest

from myfish.adapters.xhs.message import Sticker, decode_content
from myfish.core.message import Image, Video


def decode(content_type: int, content: dict):
    return decode_content(
        json.dumps({"content_type": content_type, "content": json.dumps(content)})
    )[0]


class MediaTests(unittest.TestCase):
    def test_image_prefers_original_and_keeps_geometry(self):
        image = decode(
            2,
            {
                "link": "https://example.com/fallback.jpg",
                "localPath": "/private/local.jpg",
                "size": {"width": 1440, "height": 1912},
                "imageFileId": "image-id",
                "isSendOriginal": True,
                "imageDataMap": {
                    "IM_PRV": {"url": "https://example.com/preview.jpg"},
                    "IM_ORG": {"url": "https://example.com/original.jpg"},
                },
            },
        )
        self.assertIsInstance(image, Image)
        self.assertEqual(image.image_url, "https://example.com/original.jpg")
        self.assertEqual((image.width, image.height), (1440, 1912))
        self.assertEqual(image.extra["file_id"], "image-id")
        with self.assertRaisesRegex(ValueError, "no HTTP resource"):
            decode(2, {"link": "file:///private/local.jpg"})

    def test_sticker_keeps_semantics_instead_of_becoming_plain_image(self):
        sticker = decode(
            13,
            {
                "image": "https://example.com/sticker.webp",
                "emojiKey": "emoji-id",
                "subType": "greeting",
                "fireworkId": "animation-id",
                "playMode": 1,
                "interactAnimType": "greeting",
                "operationInfo": "opaque-metadata",
            },
        )
        self.assertIsInstance(sticker, Sticker)
        self.assertEqual(sticker.type, "sticker")
        self.assertEqual(sticker.emoji_key, "emoji-id")
        self.assertEqual(sticker.firework_id, "animation-id")
        self.assertEqual(sticker.play_mode, 1)

    def test_video_uses_video_link_and_millisecond_duration(self):
        video = decode(
            11,
            {
                "link": "https://example.com/cover.jpg",
                "videoLink": "https://example.com/video.mp4",
                "duration": 8011,
                "size": {"width": 720, "height": 1208},
                "videoData": {
                    "duration": 0,
                    "videoFileId": "video-file",
                    "videoId": "video-id",
                },
                "imageDataMap": {"IM_PRV": {"url": "https://example.com/thumb.jpg"}},
            },
        )
        self.assertIsInstance(video, Video)
        self.assertEqual(video.duration_ms, 8011)
        self.assertEqual(video.video_url, "https://example.com/video.mp4")
        self.assertEqual(video.thumb_url, "https://example.com/thumb.jpg")
        self.assertEqual(video.extra["video_id"], "video-id")
        with self.assertRaisesRegex(ValueError, "no HTTP resource"):
            decode(11, {"duration": 1, "link": "https://example.com/cover.jpg"})
