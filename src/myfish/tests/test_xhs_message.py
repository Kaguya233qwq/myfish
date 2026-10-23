import json
import unittest
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from myfish.adapters.xhs.api import XhsAPI
from myfish.adapters.xhs.message import (
    NoteCard,
    decode_content,
    decode_message,
    encode_text,
)
from myfish.adapters.xhs.model import PushMessage
from myfish.adapters.xhs.sign import encrypt_message


class MessageTests(unittest.IsolatedAsyncioTestCase):
    def test_text_wire_format_and_inbound_message(self):
        self.assertEqual(
            encode_text("a", "b", "hi", "id", 1).hex(),
            "08014a1c0a02696410011a002201612a0162320268693801420048005a006001",
        )
        result = decode_message(bytes.fromhex("22100a02696412036d736718012a03616263"))
        self.assertIsInstance(result, PushMessage)
        self.assertEqual(result.payload, "abc")
        for value in (b"\x22\xff", b"\x00", b"\x22\x05x"):
            with self.assertRaises(ValueError):
                decode_message(value)

    def test_rsa_chunking_preserves_binary_payload(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        with patch("myfish.adapters.xhs.sign.rsa.RSAPublicNumbers") as numbers:
            numbers.return_value.public_key.return_value = key.public_key()
            for size in (0, 1, 117, 118, 234, 256):
                with self.subTest(size=size):
                    payload = bytes(index % 256 for index in range(size))
                    encrypted = encrypt_message(payload).encode("latin1")
                    self.assertEqual(len(encrypted), ((size + 116) // 117) * 128)
                    plain = b"".join(
                        key.decrypt(encrypted[i : i + 128], padding.PKCS1v15())
                        for i in range(0, len(encrypted), 128)
                    )
                    self.assertEqual(plain, payload)

    async def test_sending_uses_explicit_receiver_and_does_not_retry(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "code": 0,
                        "data": {"guest": False, "user_id": "sender"},
                    },
                )
            raise httpx.ReadTimeout("uncertain delivery")

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            with self.assertRaises(httpx.ReadTimeout):
                await api.send_text("receiver", "test", message_id="fixed-id")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1].url.path, "/api/im/web/short_link/send_message")

    async def test_send_receipt_matches_observed_http_schema(self):
        def handle(request):
            data = (
                {"guest": False, "user_id": "sender"}
                if request.method == "GET"
                else {"message_id": "server-id", "chat_ack": "opaque-ack"}
            )
            return httpx.Response(200, json={"success": True, "code": 0, "data": data})

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            receipt = await api.send_text("receiver", "test", message_id="client-id")
        self.assertEqual(receipt.client_id, "client-id")
        self.assertEqual(receipt.message_id, "server-id")
        self.assertNotIn("opaque-ack", repr(receipt))

    def test_shared_note_node_cleans_app_link_into_message_segment(self):
        card = {
            "type": "note",
            "id": "note1",
            "title": "Shared title",
            "noteType": "normal",
            "link": "xhsdiscover://item/note1?xsec_token=token%3D&xsec_source=app_share",
            "image": "https://example.com/cover.jpg",
            "user": {"id": "author", "nickname": "Author", "image": "avatar"},
        }
        chain = decode_content(
            json.dumps({"content_type": 3, "content": json.dumps(card)})
        )
        self.assertEqual(len(chain), 1)
        segment = chain[0]
        self.assertIsInstance(segment, NoteCard)
        self.assertEqual(segment.ref.note_id, "note1")
        self.assertEqual(segment.ref.xsec_source, "app_share")
        self.assertEqual(segment.ref.xsec_token, "token=")
        self.assertEqual(segment.author.user_id, "author")
        self.assertNotIn("token=", repr(segment))
        card["id"] = "different"
        with self.assertRaisesRegex(ValueError, "Invalid shared note reference"):
            decode_content(json.dumps({"content_type": 3, "content": json.dumps(card)}))

    def test_message_nodes_preserve_text_and_do_not_guess_unknown_types(self):
        text = decode_content('{"content_type":1,"content":"hello"}')
        self.assertEqual(text[0].text, "hello")
        self.assertEqual(decode_content('{"content_type":999,"content":"unknown"}'), [])
        self.assertEqual(
            decode_content(
                json.dumps({"content_type": 3, "content": json.dumps({"type": "user"})})
            ),
            [],
        )
