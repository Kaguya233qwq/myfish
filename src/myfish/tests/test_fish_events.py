import base64
import json
import unittest
from dataclasses import dataclass
from typing import ClassVar
from unittest.mock import patch

from myfish.adapters.fish.events import FishTradeEvent
from myfish.adapters.fish.message import (
    Content,
    FishEventSegment,
    FishPayloadNode,
    MessageBody,
    TradeCard,
)
from myfish.core.event import BaseEvent, MessageEvent
from myfish.core.message import MessageSegment


class FishEventTests(unittest.TestCase):
    def test_new_event_segment_uses_generic_conversion_in_mixed_container(self):
        @dataclass
        class NoticeEvent(BaseEvent):
            event_type: ClassVar[str] = "fish.test.notice"
            account_id: str
            cid: str
            sender_id: str
            text: str

        class NoticeSegment(FishEventSegment):
            text: str

            def to_event(self, account_id: str, body: MessageBody) -> NoticeEvent:
                return NoticeEvent(account_id, body.cid, body.sender.user_id, self.text)

        with patch.dict(FishPayloadNode._registry):

            class NoticeNode(FishPayloadNode):
                content_type = 9001

                @classmethod
                def decode(cls, payload: dict) -> list[MessageSegment]:
                    return [NoticeSegment(text=payload["notice"])]

            events = self.parse(
                {
                    "contentType": 101,
                    "custom": {
                        "data": [
                            {"contentType": 1, "text": {"text": "hello"}},
                            {"contentType": 26, "order_id": "123"},
                            {"contentType": 9001, "notice": "new event"},
                        ]
                    },
                }
            )

        self.assertEqual(len(events), 3)
        self.assertIsInstance(events[0], MessageEvent)
        self.assertEqual(events[0].plain_text, "hello")
        self.assertEqual(events[0].raw_payload, {"source": "test"})
        self.assertIsInstance(events[1], FishTradeEvent)
        self.assertEqual(events[1].order_id, "123")
        self.assertEqual(events[2], NoticeEvent("10", "chat", "20", "new event"))
        self.assertNotIn(9001, FishPayloadNode._registry)

    def test_event_segment_requires_conversion_implementation(self):
        class IncompleteSegment(FishEventSegment):
            pass

        with self.assertRaises(TypeError):
            IncompleteSegment()

    def parse(self, content):
        body = MessageBody.model_validate(
            {
                "2": "chat@goofish",
                "10": {"senderUserId": "20", "reminderTitle": "buyer"},
                "6": content,
            }
        )
        return list(body.to_events("10", {"source": "test"}))

    def test_trade_notification_uses_message_node_decoder(self):
        payload = {"contentType": 26, "order_id": "100", "title": "已付款"}
        segments = Content.model_validate(payload).to_message_chain()
        self.assertEqual(segments, [TradeCard(order_id="100", title="已付款")])
        events = self.parse(payload)
        self.assertEqual(
            events, [FishTradeEvent("10", "100", "chat", "20", "buyer", "已付款")]
        )

    def test_dx_card_fields_are_parsed_from_platform_payload(self):
        events = self.parse(
            {
                "contentType": 26,
                "dxCard": {
                    "item": {
                        "main": {
                            "exContent": {
                                "title": "已付款",
                                "desc": "待发货",
                                "button": {
                                    "text": "发货",
                                    "targetUrl": "fleamarket://order?bizOrderId=123",
                                },
                            },
                            "clickParam": {"args": {"task_id": "task"}},
                        }
                    }
                },
            }
        )
        self.assertEqual(
            events,
            [
                FishTradeEvent(
                    "10",
                    "123",
                    "chat",
                    "20",
                    "buyer",
                    "已付款",
                    "待发货",
                    "发货",
                    "task",
                )
            ],
        )

    def test_mixed_container_separates_messages_and_trade_notifications(self):
        content = [
            {"type": "text", "text": "hello"},
            {"contentType": 26, "order_id": "100"},
            {"contentType": 26, "order_id": "101"},
        ]
        for encoded in (
            content,
            base64.b64encode(json.dumps(content).encode()).decode(),
        ):
            with self.subTest(encoded=type(encoded).__name__):
                events = self.parse({"contentType": 101, "custom": {"data": encoded}})
                self.assertEqual(len(events), 3)
                self.assertIsInstance(events[0], MessageEvent)
                self.assertEqual(events[0].plain_text, "hello")
                self.assertEqual(events[0].raw_payload, {"source": "test"})
                self.assertEqual(
                    [event.order_id for event in events[1:]], ["100", "101"]
                )

    def test_bad_notification_does_not_drop_other_container_items(self):
        events = self.parse(
            {
                "contentType": 101,
                "custom": {
                    "data": [
                        {"contentType": 26, "order_id": []},
                        {"contentType": 26, "order_id": "100"},
                        {"contentType": 1, "text": {"text": "hi"}},
                    ]
                },
            }
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].plain_text, "hi")
        self.assertEqual(events[1].order_id, "100")

    def test_unknown_content_is_ignored(self):
        self.assertEqual(self.parse({"contentType": 999}), [])

    def test_order_id_is_read_from_exact_url_parameters(self):
        for target in (
            "fleamarket://order?item_id=999&bizOrderId=123",
            "https://example.com/order?id=123&sid=999",
            "fleamarket://order?bizOrderId=%31%32%33",
        ):
            with self.subTest(target=target):
                content = {
                    "contentType": 26,
                    "dxCard": {"item": {"main": {"targetUrl": target}}},
                }
                self.assertEqual(self.parse(content)[0].order_id, "123")

    def test_missing_invalid_and_conflicting_order_ids_are_ignored(self):
        for target in (
            "fleamarket://order?item_id=123",
            "fleamarket://order?id=",
            "fleamarket://order?id=abc",
            "fleamarket://order?id=１２３",
            "fleamarket://order?id=123&id=456",
            "fleamarket://order?id=123&bizOrderId=456",
        ):
            with self.subTest(target=target):
                content = {
                    "contentType": 26,
                    "dxCard": {"item": {"main": {"targetUrl": target}}},
                }
                self.assertEqual(self.parse(content), [])
        for order_id in ("", "invalid", [], None):
            with self.subTest(order_id=order_id):
                self.assertEqual(
                    self.parse({"contentType": 26, "order_id": order_id}), []
                )

    def test_button_order_url_is_used_when_main_url_has_no_order_id(self):
        content = {
            "contentType": 26,
            "dxCard": {
                "item": {
                    "main": {
                        "targetUrl": "fleamarket://home",
                        "exContent": {
                            "button": {"targetUrl": "fleamarket://order?bizOrderId=123"}
                        },
                    }
                }
            },
        }
        self.assertEqual(self.parse(content)[0].order_id, "123")

    def test_nested_container_decodes_messages_and_trade_cards_once(self):
        content = {
            "contentType": 101,
            "custom": {
                "data": [
                    {"contentType": 26, "dxCard": None},
                    {
                        "contentType": 101,
                        "custom": {
                            "data": base64.b64encode(
                                json.dumps(
                                    [
                                        {"type": "text", "text": "hello"},
                                        {"contentType": 26, "order_id": "123"},
                                        {
                                            "contentType": 2,
                                            "image": {"pics": [{"url": "image"}]},
                                        },
                                    ]
                                ).encode()
                            ).decode()
                        },
                    },
                ]
            },
        }
        segments = Content.model_validate(content).to_message_chain()
        self.assertEqual(
            [seg.type for seg in segments], ["text", "trade_card", "image"]
        )
        events = self.parse(content)
        self.assertEqual(len(events), 2)
        self.assertEqual([seg.type for seg in events[0].messages], ["text", "image"])
        self.assertIsInstance(events[1], FishTradeEvent)
        self.assertEqual(events[1].order_id, "123")


if __name__ == "__main__":
    unittest.main()
