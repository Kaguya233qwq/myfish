from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from myfish.adapters.fish.api import FishAPIError
from myfish.adapters.fish.listings import FishListingPage

if TYPE_CHECKING:
    from myfish.adapters.fish import FishBot

    from .store import Store


class ListingCatalog:
    def __init__(self, store: Store, bot: Callable[[str], FishBot]) -> None:
        self.store = store
        self.bot = bot
        self._locks: dict[str, asyncio.Lock] = {}

    async def sync(self, account_id: str) -> int:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            api = self.bot(account_id).adapter.api
            previous: FishListingPage | None = None
            cursors: set[str] = set()
            imported = 0
            for page_number in range(1, 101):
                page = await api.get_listing_page(
                    account_id, page_number, previous=previous
                )
                imported += await self.import_page(account_id, page)
                if not page.has_more:
                    return imported
                cursor = repr((page.next_page_model, page.next_page_num))
                if (
                    page.next_page_model is not None or page.next_page_num is not None
                ) and cursor in cursors:
                    raise FishAPIError(
                        "商品列表分页游标重复，已停止同步；已导入的商品已保留"
                    )
                cursors.add(cursor)
                previous = page
                await asyncio.sleep(1)
            raise FishAPIError("商品列表超过本轮同步页数上限，已导入的商品已保留")

    async def import_page(self, account_id: str, page: FishListingPage) -> int:
        now = time.time()

        def insert(db: sqlite3.Connection) -> int:
            before = db.total_changes
            db.executemany(
                "INSERT INTO products(title,description,price,account_id,item_id,mode,content,status,created,updated,product_type) "
                "VALUES(?,'',?,?,?,'disabled','','published',?,?,'unknown') "
                "ON CONFLICT(account_id,item_id) WHERE item_id!='' DO NOTHING",
                [
                    (item.title, str(item.price), account_id, item.item_id, now, now)
                    for item in page.items
                ],
            )
            return db.total_changes - before

        return await self.store.call(insert, write=True)
