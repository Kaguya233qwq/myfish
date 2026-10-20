import re

from .model import FishOrder


def parse_sold_orders(response: dict) -> tuple[list[FishOrder], bool]:
    module = response.get("data", {}).get("module")
    if not isinstance(module, dict) or not isinstance(module.get("items"), list):
        raise ValueError("订单列表响应结构不受支持")  # noqa: TRY004
    next_page = module.get("nextPage", False)
    if next_page not in (True, False, "true", "false"):
        raise ValueError("订单分页标志无效")
    return [FishOrder.from_sold_item(item) for item in module["items"]], str(
        next_page
    ).lower() == "true"


def order_conversation(
    conversations: list,
    account_id: str,
    buyer_id: str,
    item_id: str,
    order_id: str,
) -> str:
    matches = set()
    exact = set()
    for row in conversations:
        if not isinstance(row, dict):
            continue
        user = row.get("singleChatUserConversation")
        if not isinstance(user, dict):
            continue
        conversation = user.get("singleChatConversation")
        if not isinstance(conversation, dict):
            continue
        participants = {
            str(conversation.get(field, "")).removesuffix("@goofish")
            for field in ("pairFirst", "pairSecond")
        }
        if participants != {account_id, buyer_id}:
            continue
        extension = conversation.get("extension")
        if not isinstance(extension, dict) or str(extension.get("itemId")) != item_id:
            continue
        if str(extension.get("itemSellerId", account_id)) != account_id:
            continue
        linked_order = str(extension.get("orderId") or "")
        if linked_order and linked_order != order_id:
            continue
        cid = str(conversation.get("cid") or "").removesuffix("@goofish")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", cid):
            continue
        matches.add(cid)
        if linked_order == order_id:
            exact.add(cid)
    candidates = exact or matches
    if len(candidates) > 1:
        raise ValueError("找到多个买家会话，无法确定订单会话")
    return next(iter(candidates), "")
