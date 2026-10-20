from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import traceback
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from myfish.adapters.fish.api import (
    FishAPIError,
    FishListingUnconfirmed,
    FishPublishPreparationError,
)
from myfish.adapters.fish.events import FishOrderEvent
from myfish.adapters.fish.model import FishOrder, OrderStatus
from myfish.adapters.fish.utils import ListingRejected
from myfish.core.message import Text

from .editing import ListingEditor
from .polish import Polisher, PolishResult
from .tracking import OrderTracker

if TYPE_CHECKING:
    from myfish.adapters.fish import FishBot

    from .store import Store


class Preconditions(ValueError):
    pass


class Service:
    def __init__(
        self,
        store: Store,
        bots: Sequence[FishBot],
        *,
        emit: Callable[[FishBot, FishOrderEvent], Awaitable[None]] | None = None,
        legacy_order_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.bots = bots
        self.wake = asyncio.Event()
        self.polisher = Polisher(store, bots)
        self.editor = ListingEditor(store, self.bot)
        self.trackers: dict[str, OrderTracker] = {}
        self._emit = emit
        self._legacy_order_dir = legacy_order_dir

    def bot(self, account_id: str) -> FishBot:
        for bot in self.bots:
            if bot.bot_id == account_id:
                return bot
        raise Preconditions("闲鱼账号未登录或当前未连接此账号")

    async def audit(self, actor, action, target):
        await self.store.execute(
            "INSERT INTO audit(actor,action,target,created) VALUES(?,?,?,?)",
            (actor, action, str(target), time.time()),
        )

    async def products(self):
        rows = await self.store.rows(
            "SELECT p.id,p.title,p.description,p.price,p.account_id,p.item_id,p.mode,p.images,p.auto_delivery,p.status,p.updated,p.product_type,p.category,p.quantity,p.shipping,p.postage,p.auto_polish,p.polish_interval_hours,p.polish_next_at,p.polish_last_at,p.polish_status,p.polish_error,p.draft_id,p.draft_state,p.draft_synced_at,(SELECT j.error FROM jobs j WHERE j.kind='save_draft' AND j.target=CAST(p.id AS TEXT) ORDER BY j.id DESC LIMIT 1) AS draft_error FROM products p ORDER BY p.id DESC"
        )

        edits = {
            row["product_id"]: row["state"]
            for row in await self.store.rows(
                "SELECT product_id,state FROM product_edits"
            )
        }
        return [{**row, "edit_state": edits.get(row["id"], "")} for row in rows]

    async def add_product(self, data, *, sync_draft=False):
        if data.mode not in {"reusable", "physical"}:
            raise Preconditions("不支持的交付方式")
        now = time.time()
        sql = "INSERT INTO products(title,description,price,account_id,item_id,mode,content,images,auto_delivery,status,created,updated,product_type,category,quantity,shipping,postage,auto_polish,polish_interval_hours) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        params = (
            data.title,
            data.description,
            str(data.price),
            data.account_id,
            data.item_id,
            data.mode,
            self.store.encrypt(data.content) if data.content else "",
            json.dumps(data.images),
            data.auto_delivery,
            "published" if data.item_id else "draft",
            now,
            now,
            data.product_type,
            json.dumps(data.category.platform_data() if data.category else {}),
            data.quantity,
            data.shipping,
            str(data.postage),
            data.auto_polish,
            data.polish_interval_hours,
        )

        def insert(db):
            product_id = db.execute(sql, params).lastrowid
            if sync_draft and not data.item_id:
                db.execute(
                    "INSERT INTO jobs(kind,target,account_id,run_at,created,updated) VALUES('save_draft',?,?,?,?,?)",
                    (str(product_id), data.account_id, now, now, now),
                )
                db.execute(
                    "UPDATE products SET draft_state='queued' WHERE id=?", (product_id,)
                )
            return product_id

        product_id = await self.store.call(insert, write=True)
        if sync_draft:
            self.wake.set()
        return product_id

    async def delivery_content(self, product_id):
        rows = await self.store.rows(
            "SELECT product_type,mode,content FROM products WHERE id=?", (product_id,)
        )
        if (
            not rows
            or rows[0]["product_type"] != "virtual"
            or rows[0]["mode"] not in {"reusable", "disabled"}
        ):
            raise Preconditions("只有固定内容虚拟商品支持修改交付内容")
        return (
            self.store.decrypt(rows[0]["content"])
            if rows[0]["mode"] == "reusable"
            else ""
        )

    async def update_delivery_content(self, product_id, content):
        if not content.strip():
            raise Preconditions("交付内容不能为空")
        encrypted = self.store.encrypt(content)

        def update(db):
            product = db.execute(
                "SELECT product_type,mode FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if (
                not product
                or product["product_type"] != "virtual"
                or product["mode"] not in {"reusable", "disabled"}
            ):
                raise Preconditions("只有固定内容虚拟商品支持修改交付内容")
            now = time.time()
            db.execute(
                "UPDATE products SET mode='reusable',content=?,updated=? WHERE id=?",
                (encrypted, now, product_id),
            )
            return db.execute(
                "UPDATE orders SET content=?,updated=? WHERE product_id=? AND delivery_state='reserved'",
                (encrypted, now, product_id),
            ).rowcount

        return await self.store.call(update, write=True)

    async def enqueue(self, kind, target, account_id, run_at=None, *, sale_order=None):
        now = time.time()

        def insert(db):
            if kind in {"publish", "delist", "save_draft", "polish", "reshelf"}:
                product = db.execute(
                    "SELECT * FROM products WHERE id=?", (target,)
                ).fetchone()
                if (
                    product
                    and product["mode"] == "disabled"
                    and kind in {"publish", "save_draft", "reshelf"}
                ):
                    raise Preconditions("商品交付方式已停用，请先设置固定交付内容")
                if not product or product["account_id"] != account_id:
                    raise Preconditions("商品不存在或所属账号已改变")
                if kind == "reshelf" and (
                    not product["item_id"] or product["status"] != "unlisted"
                ):
                    raise Preconditions("只有已下架的关联商品支持恢复上架")
                uncertain = db.execute(
                    "SELECT id FROM jobs WHERE target=? AND kind='reshelf' AND status='uncertain'",
                    (str(target),),
                ).fetchone()
                edit = db.execute(
                    "SELECT state FROM product_edits WHERE product_id=? AND state IN ('queued','running','reviewing','uncertain')",
                    (target,),
                ).fetchone()
                if edit:
                    raise Preconditions(
                        "商品编辑正在执行、审核或待核对，请先处理编辑任务"
                    )
                if uncertain:
                    raise Preconditions("恢复上架结果待核对，请先处理不确定任务")
                if kind in {"publish", "save_draft"} and (
                    product["item_id"]
                    or product["status"] not in {"draft", "failed"}
                    or product["draft_state"] in {"uncertain", "syncing"}
                ):
                    raise Preconditions("商品正在同步、已发布或草稿结果待核对")
                if kind == "polish":
                    if not product["item_id"] or product["status"] != "published":
                        raise Preconditions("只有在售商品支持擦亮")
                    if product["polish_status"] == "uncertain":
                        raise Preconditions("上次擦亮结果待核对，不能重复提交")
                    pending = db.execute(
                        "SELECT id FROM jobs WHERE kind='polish' AND target=? AND status IN ('pending','running')",
                        (str(target),),
                    ).fetchone()
                    if pending:
                        return pending["id"]
                    if (run_at or now) < product["polish_next_at"]:
                        raise Preconditions(self.polisher.cooldown_message(product))
                if kind == "delist":
                    db.execute(
                        "UPDATE jobs SET status='cancelled',updated=? WHERE kind='polish' AND target=? AND status='pending'",
                        (now, str(target)),
                    )
                    db.execute(
                        "UPDATE products SET polish_status='paused' WHERE id=? AND polish_status='queued'",
                        (target,),
                    )
                active = db.execute(
                    "SELECT kind FROM jobs WHERE target=? AND kind IN ('publish','delist','save_draft','polish','reshelf','edit') AND status IN ('pending','running')",
                    (str(target),),
                ).fetchone()
                if active:
                    if active["kind"] == kind:
                        return None
                    if not (kind == "delist" and active["kind"] == "polish"):
                        raise Preconditions(
                            "商品已有发布、下架、草稿或擦亮任务，请等待完成"
                        )
            job_id = db.execute(
                "INSERT INTO jobs(kind,target,account_id,run_at,created,updated) VALUES(?,?,?,?,?,?)",
                (kind, str(target), account_id, run_at or now, now, now),
            ).lastrowid
            if sale_order is not None:
                linked = db.execute(
                    "UPDATE sale_reshelves SET job_id=? WHERE order_key=? "
                    "AND job_id IN (SELECT id FROM jobs WHERE status='cancelled')",
                    (job_id, sale_order),
                ).rowcount
                if linked != 1:
                    raise Preconditions("售后上架任务尚未完成核对")
            if kind == "save_draft":
                db.execute(
                    "UPDATE products SET draft_state='queued' WHERE id=?", (target,)
                )
            if kind == "polish":
                db.execute(
                    "UPDATE products SET polish_status='queued',polish_error='' WHERE id=?",
                    (target,),
                )
            return job_id

        try:
            job = await self.store.call(insert, write=True)
        except sqlite3.IntegrityError:
            return None
        self.wake.set()
        return job

    async def schedule_product(self, product_id, kind, run_at=None):
        rows = await self.store.rows("SELECT * FROM products WHERE id=?", (product_id,))
        if not rows:
            raise Preconditions("商品不存在")
        product = rows[0]
        if kind == "publish" and (
            product["item_id"] or product["status"] not in {"draft", "failed"}
        ):
            raise Preconditions("商品已有平台 ID 或正在发布，请先核对平台商品")
        if kind == "delist" and not product["item_id"]:
            raise Preconditions("商品尚未关联平台商品 ID")
        sale_order = None
        if kind == "reshelf" and product["product_type"] == "virtual":
            previous = await self.store.rows(
                "SELECT s.order_key FROM sale_reshelves s JOIN orders o ON o.key=s.order_key "
                "JOIN jobs j ON j.id=s.job_id WHERE o.product_id=? AND o.item_id=? "
                "AND o.account_id=? AND j.status='cancelled' ORDER BY s.created DESC LIMIT 1",
                (product_id, product["item_id"], product["account_id"]),
            )
            if previous:
                sale_order = previous[0]["order_key"]
        return await self.enqueue(
            kind, product_id, product["account_id"], run_at, sale_order=sale_order
        )

    async def listing_state(self, product_id):
        rows = await self.store.rows("SELECT * FROM products WHERE id=?", (product_id,))
        if not rows or not rows[0]["item_id"]:
            raise Preconditions("商品尚未关联平台 ID")
        product = rows[0]
        api = self.bot(product["account_id"]).adapter.api
        detail = await api.get_item_edit_detail(product["item_id"], verify_owner=False)
        owner = detail.get("userId")
        if str(detail.get("itemId")) != product["item_id"] or (
            owner not in (None, "", 0, "0") and str(owner) != product["account_id"]
        ):
            raise Preconditions("平台商品或所属账号不匹配")
        states = {"0": "published", "-2": "unlisted", "-9": "reviewing"}
        if (
            product["product_type"] == "virtual"
            and str(detail.get("itemStatus")) == "1"
        ):
            known_sale = await self.store.rows(
                "SELECT 1 FROM sale_reshelves s JOIN orders o ON o.key=s.order_key "
                "WHERE o.product_id=? AND o.item_id=? AND o.account_id=? LIMIT 1",
                (product_id, product["item_id"], product["account_id"]),
            )
            if known_sale:
                states["1"] = "unlisted"
        state = states.get(str(detail.get("itemStatus")))
        if not state:
            raise Preconditions("平台商品处于已售或限制状态，保留本地记录")
        await self.editor.reconcile(product_id, detail)
        return product, state

    async def refresh_listing(self, product_id):
        product, state = await self.listing_state(product_id)

        def update(db):
            active = db.execute(
                "SELECT id FROM jobs WHERE target=? AND kind IN ('publish','delist','reshelf','edit') AND status IN ('pending','running')",
                (str(product_id),),
            ).fetchone()
            if active:
                raise Preconditions("商品有待执行的上下架任务，请完成后同步")
            db.execute(
                "UPDATE products SET status=?,updated=? WHERE id=? AND item_id=? AND account_id=?",
                (
                    state,
                    time.time(),
                    product_id,
                    product["item_id"],
                    product["account_id"],
                ),
            )

        await self.store.call(update, write=True)
        if state == "published":
            await self.polisher.resume_if_published(product_id)
        return state

    async def sync_listings(
        self, account_id: str, *, after_id: int = 0, limit: int = 20
    ) -> int:
        query = (
            "SELECT p.id FROM products p WHERE p.account_id=? AND p.item_id!='' AND p.id>? "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.target=CAST(p.id AS TEXT) "
            "AND j.kind IN ('publish','delist','reshelf','edit') AND j.status IN ('pending','running')) "
            "ORDER BY p.id LIMIT ?"
        )
        rows = await self.store.rows(query, (account_id, after_id, limit))
        if not rows and after_id:
            rows = await self.store.rows(query, (account_id, 0, limit))
        cursor = 0
        for row in rows:
            cursor = row["id"]
            try:
                await self.refresh_listing(cursor)
            except Preconditions:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "商品状态查询受阻，本轮停止该账号请求: {}", type(exc).__name__
                )
                break
            await asyncio.sleep(1)
        return cursor

    async def observe(
        self, account_id: str, order: FishOrder, chat_id: str = ""
    ) -> None:
        key = f"{account_id}:{order.order_id}"

        def record(db):
            product = db.execute(
                "SELECT id,auto_delivery,product_type,mode FROM products WHERE account_id=? AND item_id=?",
                (account_id, order.item_id),
            ).fetchone()
            if not product:
                return False, False
            db.execute(
                "INSERT INTO orders(key,account_id,order_id,product_id,item_id,buyer_id,buyer_name,amount,quantity,chat_id,platform_status,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET platform_status=excluded.platform_status,buyer_name=CASE WHEN excluded.buyer_name!='' THEN excluded.buyer_name ELSE orders.buyer_name END,amount=CASE WHEN excluded.amount!='' THEN excluded.amount ELSE orders.amount END,chat_id=CASE WHEN excluded.chat_id!='' THEN excluded.chat_id ELSE orders.chat_id END,updated=excluded.updated",
                (
                    key,
                    account_id,
                    order.order_id,
                    product["id"],
                    order.item_id,
                    order.buyer_id,
                    order.buyer_name,
                    order.amount,
                    order.quantity,
                    chat_id,
                    order.status.value,
                    time.time(),
                ),
            )
            row = db.execute("SELECT * FROM orders WHERE key=?", (key,)).fetchone()
            if row["delivery_state"] == "reserved" and (
                order.in_refund
                or order.status
                in {
                    OrderStatus.PENDING_PAYMENT,
                    OrderStatus.CLOSED,
                    OrderStatus.REFUNDING,
                    OrderStatus.REFUNDED,
                }
            ):
                db.execute(
                    "UPDATE orders SET delivery_state='pending',content='' WHERE key=?",
                    (key,),
                )
                db.execute(
                    "UPDATE jobs SET status='cancelled',updated=? WHERE status='pending' "
                    "AND id IN (SELECT job_id FROM sale_reshelves WHERE order_key=?)",
                    (time.time(), key),
                )
            auto = bool(
                product["auto_delivery"]
                and product["product_type"] == "virtual"
                and product["mode"] == "reusable"
                and order.can_ship
                and row["chat_id"]
                and row["delivery_state"] in {"pending", "reserved", "sent"}
            )
            return auto, product["product_type"] == "virtual" and product[
                "mode"
            ] == "reusable" and order.can_ship

        auto, sold = await self.store.call(record, write=True)
        if sold:
            try:
                await self.reserve(key)
            except Preconditions as exc:
                logger.warning("售出商品交付准备失败: {}", exc)
            else:
                await self._queue_sale_reshelf(key)
        if auto:
            await self.enqueue("deliver", key, account_id)

    async def _queue_sale_reshelf(self, key: str) -> None:
        def queue(db):
            product = db.execute(
                "SELECT p.*,o.delivery_state FROM products p JOIN orders o ON o.product_id=p.id "
                "WHERE o.key=? AND o.account_id=p.account_id AND o.item_id=p.item_id",
                (key,),
            ).fetchone()
            if (
                not product
                or product["product_type"] != "virtual"
                or product["mode"] != "reusable"
                or product["status"] != "published"
                or product["delivery_state"] == "pending"
                or db.execute(
                    "SELECT 1 FROM sale_reshelves WHERE order_key=?", (key,)
                ).fetchone()
            ):
                return False
            now = time.time()
            active = db.execute(
                "SELECT 1 FROM jobs WHERE target=? AND kind IN ('publish','delist','save_draft','polish','reshelf','edit') "
                "AND (status IN ('pending','running') OR (kind IN ('reshelf','edit') AND status='uncertain'))",
                (str(product["id"]),),
            ).fetchone()
            if active:
                return False
            job_id = db.execute(
                "INSERT INTO jobs(kind,target,account_id,run_at,created,updated) VALUES('reshelf',?,?,?,?,?)",
                (str(product["id"]), product["account_id"], now, now, now),
            ).lastrowid
            db.execute(
                "INSERT INTO sale_reshelves(order_key,job_id,created) VALUES(?,?,?)",
                (key, job_id, now),
            )
            db.execute(
                "UPDATE products SET status='unlisted',updated=? WHERE id=?",
                (now, product["id"]),
            )
            return True

        if await self.store.call(queue, write=True):
            self.wake.set()

    async def sync(
        self, account_id: str, *, order_id: str = "", chat_id: str = ""
    ) -> FishOrder | None:
        bot = self.bot(account_id)
        tracker = self.trackers.get(account_id)
        if tracker is None:
            state_path = self.store.directory / "orders" / f"{account_id}.json"
            if not state_path.exists():
                legacy_paths = [self.store.directory / f"orders-{account_id}.json"]
                if self._legacy_order_dir is not None:
                    legacy_paths.append(
                        self._legacy_order_dir / f"orders-{account_id}.json"
                    )
                for legacy_path in legacy_paths:
                    if legacy_path.is_file():
                        OrderTracker(
                            bot.adapter.api, account_id, state_path=legacy_path
                        )
                        state_path.parent.mkdir(parents=True, exist_ok=True)
                        legacy_path.rename(state_path)
                        break
            tracker = OrderTracker(
                bot.adapter.api,
                account_id,
                state_path=state_path,
                max_pages=10,
            )
            self.trackers[account_id] = tracker
        events = await tracker.sync_once()
        found = None
        for order in tracker.current_orders.values():
            await self.observe(
                account_id, order, chat_id if order.order_id == order_id else ""
            )
            if order.order_id == order_id:
                found = order
        if self._emit:
            for event in events:
                await self._emit(bot, event)
        return found

    async def reserve(self, key):
        def reserve(db):
            order = db.execute("SELECT * FROM orders WHERE key=?", (key,)).fetchone()
            if not order:
                raise Preconditions("订单不存在")
            product = db.execute(
                "SELECT * FROM products WHERE id=?", (order["product_id"],)
            ).fetchone()
            if product["product_type"] == "physical":
                raise Preconditions(
                    "实物订单请通过快递发货，禁止发送虚拟内容或标记无需物流"
                )
            if product["mode"] != "reusable":
                raise Preconditions("商品交付方式已停用，请先设置固定交付内容")
            if order["delivery_state"] != "pending":
                return dict(order)
            content = self.store.decrypt(product["content"])
            if not content:
                raise Preconditions("固定交付内容为空")
            db.execute(
                "UPDATE orders SET delivery_state='reserved',content=?,error='' WHERE key=?",
                (self.store.encrypt(content), key),
            )
            return dict(
                db.execute("SELECT * FROM orders WHERE key=?", (key,)).fetchone()
            )

        return await self.store.call(reserve, write=True)

    async def deliver(self, key: str) -> None:
        rows = await self.store.rows("SELECT * FROM orders WHERE key=?", (key,))
        if not rows:
            raise Preconditions("订单不存在")
        order = rows[0]
        products = await self.store.rows(
            "SELECT product_type,mode FROM products WHERE id=?", (order["product_id"],)
        )
        if products and products[0]["product_type"] == "physical":
            raise Preconditions("实物订单请通过快递发货，不能执行虚拟交付")
        if not products or products[0]["mode"] != "reusable":
            raise Preconditions("商品交付方式已停用，不能执行自动交付")
        if order["delivery_state"] == "completed":
            return
        bot = self.bot(order["account_id"])
        fresh = await self.sync(order["account_id"], order_id=order["order_id"])
        order = (await self.store.rows("SELECT * FROM orders WHERE key=?", (key,)))[0]
        if (
            fresh
            and fresh.status in {OrderStatus.SHIPPED, OrderStatus.COMPLETED}
            and order["delivery_state"] == "sent"
        ):
            await self.finish(key)
            return
        if (
            not fresh
            or not fresh.can_ship
            or fresh.item_id != order["item_id"]
            or fresh.buyer_id != order["buyer_id"]
            or fresh.quantity != order["quantity"]
        ):
            raise Preconditions("无法确认订单处于可发货状态，或订单信息已改变")
        if order["delivery_state"] == "sending":
            raise Preconditions("上次消息发送结果不明确，请管理员查看任务错误日志")
        if order["delivery_state"] != "sent":
            if not bot.adapter._active_ws:
                raise Preconditions("WebSocket 未连接，尚未发送")
            if not order["chat_id"]:
                try:
                    chat_id = await bot.adapter.api.get_order_conversation(
                        order["order_id"], order["buyer_id"], order["item_id"]
                    )
                except FishAPIError as exc:
                    raise Preconditions(f"读取买家会话失败，尚未发送：{exc}") from exc
                except TimeoutError as exc:
                    raise Preconditions("读取买家会话超时，尚未发送") from exc
                if not chat_id:
                    raise Preconditions("订单已付款，但未找到对应的买家会话")
                await self.store.execute(
                    "UPDATE orders SET chat_id=?,updated=? WHERE key=? AND chat_id=''",
                    (chat_id, time.time(), key),
                )
        order = await self.reserve(key)
        if order["delivery_state"] == "completed":
            return
        if order["delivery_state"] == "sending":
            raise Preconditions("上次消息发送结果不明确，禁止自动重复发送，请人工核对")
        if order["delivery_state"] == "reserved":

            def claim(db):
                if (
                    db.execute(
                        "UPDATE orders SET delivery_state='sending' WHERE key=? AND delivery_state='reserved'",
                        (key,),
                    ).rowcount
                    != 1
                ):
                    return None
                return dict(
                    db.execute("SELECT * FROM orders WHERE key=?", (key,)).fetchone()
                )

            claimed = await self.store.call(claim, write=True)
            if not claimed:
                raise Preconditions("订单正在发送或结果待核对，未重复发送")
            order = claimed
            sent = await bot.adapter.send(
                order["buyer_id"],
                Text(text=self.store.decrypt(order["content"])),
                cid=order["chat_id"],
                redact=True,
            )
            if sent is not True:
                raise Preconditions("消息没有确认写入连接，请人工核对后处理")
            await self.store.execute(
                "UPDATE orders SET delivery_state='sent' WHERE key=?", (key,)
            )
        # sent 阶段重试不重发消息。
        await bot.adapter.api.ship_order(order["order_id"])
        await self.finish(key)

    async def finish(self, key):
        await self.store.execute(
            "UPDATE orders SET delivery_state='completed',error='',updated=? WHERE key=?",
            (time.time(), key),
        )

    async def process(self, job):
        if job["kind"] == "deliver":
            await self.deliver(job["target"])
            return
        rows = await self.store.rows(
            "SELECT * FROM products WHERE id=?", (job["target"],)
        )
        if not rows:
            raise Preconditions("商品不存在")
        product = rows[0]
        if product["mode"] == "disabled" and job["kind"] in {
            "publish",
            "save_draft",
            "reshelf",
        }:
            raise Preconditions("商品交付方式已停用，请先设置固定交付内容")
        api = self.bot(job["account_id"]).adapter.api
        if job["kind"] == "edit":
            result = await self.editor.execute(job, product, api)
            await self.polisher.resume_if_published(product["id"])
            return result
        if job["kind"] == "polish":
            return await self.polisher.execute(product, api)
        if job["kind"] == "delist":
            await api.downshelf_item(product["item_id"])
            await self.store.execute(
                "UPDATE products SET status='unlisted',updated=? WHERE id=?",
                (time.time(), product["id"]),
            )
        elif job["kind"] == "reshelf":
            sales = await self.store.rows(
                "SELECT o.order_id,o.platform_status,o.product_id,o.item_id,o.account_id FROM sale_reshelves s "
                "JOIN orders o ON o.key=s.order_key WHERE s.job_id=?",
                (job.get("id"),),
            )
            if sales and (
                product["product_type"] != "virtual"
                or sales[0]["product_id"] != product["id"]
                or sales[0]["item_id"] != product["item_id"]
                or sales[0]["account_id"] != job["account_id"]
                or sales[0]["platform_status"]
                not in {
                    OrderStatus.PENDING_SHIP.value,
                    OrderStatus.SHIPPED.value,
                    OrderStatus.COMPLETED.value,
                }
            ):
                return "skipped"
            if sales:
                fresh = await self.sync(
                    job["account_id"], order_id=sales[0]["order_id"]
                )
                if not fresh:
                    raise Preconditions("未能回查售出订单，尚未提交恢复上架")
                if fresh.in_refund or fresh.status not in {
                    OrderStatus.PENDING_SHIP,
                    OrderStatus.SHIPPED,
                    OrderStatus.COMPLETED,
                }:
                    return "skipped"
                if fresh.item_id != product["item_id"]:
                    return "skipped"
            if not product["item_id"] or product["status"] != "unlisted":
                raise Preconditions("商品不处于已下架状态")
            quantity = (
                product["quantity"] if product["product_type"] == "physical" else 1
            )
            if quantity < 1:
                raise Preconditions("发布数量必须大于零")
            result = await api.reshelf_item(
                product["item_id"],
                quantity=quantity,
                **({"after_sale": True} if sales else {}),
            )
            state = (
                "reviewing" if str(result.get("itemStatus")) == "-9" else "published"
            )
            await self.store.execute(
                "UPDATE products SET status=?,updated=? WHERE id=?",
                (state, time.time(), product["id"]),
            )
            if state == "published":
                await self.polisher.resume_if_published(product["id"])
        elif job["kind"] in {"publish", "save_draft"}:
            is_draft = job["kind"] == "save_draft"
            if product["item_id"]:
                raise Preconditions("商品已关联平台商品，禁止重复发布")
            images = json.loads(product["images"])
            if not images and not is_draft:
                raise Preconditions("发布需要至少一张商品图片")
            quantity = (
                product["quantity"] if product["product_type"] == "physical" else 1
            )
            category = json.loads(product["category"])
            if not category:
                raise Preconditions("请编辑草稿并手动选择商品品类后发布")
            shipping = {"choice": "无需邮寄"}
            if product["product_type"] == "physical":
                shipping = (
                    {"choice": "包邮"}
                    if product["shipping"] == "free"
                    else {"choice": "一口价", "post_price": product["postage"]}
                )
            await self.store.execute(
                "UPDATE products SET draft_state='syncing' WHERE id=?"
                if is_draft
                else "UPDATE products SET status='publishing' WHERE id=?",
                (product["id"],),
            )
            call = api.save_item_draft if is_draft else api.publish_item
            response = await call(
                [str(self.store.directory / "media" / name) for name in images],
                product["title"] + "\n" + product["description"],
                {"current_price": float(product["price"])},
                shipping,
                quantity=max(quantity, 1),
                category=category,
                **({"draft_id": product["draft_id"]} if is_draft else {}),
            )
            ret = response.get("ret", [])
            if not any(str(code).split("::", 1)[0] == "SUCCESS" for code in ret):
                if is_draft:
                    await self.store.execute(
                        "UPDATE products SET draft_state=? WHERE id=?",
                        ("dirty" if product["draft_id"] else "local", product["id"]),
                    )
                    raise Preconditions("平台拒绝保存草稿，请检查登录和商品类目要求")
                await self.store.execute(
                    "UPDATE products SET status='draft' WHERE id=? AND item_id=''",
                    (product["id"],),
                )
                codes = {str(code).split("::", 1)[0] for code in ret}
                if "FAIL_BIZ_CHANNEL_CAT_ID_PATH_QUERY_ERROR" in codes:
                    raise Preconditions(
                        "平台拒绝发布：频道类目路径无效，请检查类目推荐与发布类目是否一致"
                    )
                raise Preconditions("平台拒绝发布，请检查账号登录和商品类目要求")
            if is_draft:
                draft_id = str(
                    response.get("data", {}).get("draftId") or product["draft_id"] or ""
                )
                if (
                    not draft_id
                    or len(draft_id) > 256
                    or any(ord(c) <= 32 or ord(c) == 127 for c in draft_id)
                ):
                    raise RuntimeError(
                        "保存草稿响应缺少草稿 ID，请核对闲鱼客户端草稿箱"
                    )
                if product["draft_id"] and draft_id != product["draft_id"]:
                    raise RuntimeError("更新草稿返回了不同的草稿 ID，请核对平台结果")
                await self.store.execute(
                    "UPDATE products SET draft_id=?,draft_state='synced',draft_synced_at=? WHERE id=?",
                    (draft_id, time.time(), product["id"]),
                )
                return
            item_id = str(response.get("data", {}).get("itemId") or "")
            if not item_id.isascii() or not item_id.isdigit():
                raise RuntimeError("发布响应缺少商品 ID，请人工核对")
            await self.store.execute(
                "UPDATE products SET item_id=?,status='published',updated=? WHERE id=?",
                (item_id, time.time(), product["id"]),
            )
        else:
            raise Preconditions("未知任务类型")

    async def work_once(self):
        now = time.time()

        def claim(db):
            row = db.execute(
                "SELECT * FROM jobs j WHERE status='pending' AND run_at<=? AND (kind!='polish' OR NOT EXISTS (SELECT 1 FROM polish_accounts a WHERE a.account_id=j.account_id AND a.next_attempt>?)) ORDER BY id LIMIT 1",
                (now, now),
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE jobs SET status='running',updated=? WHERE id=?",
                    (now, row["id"]),
                )
                if row["kind"] == "polish":
                    db.execute(
                        "INSERT INTO polish_accounts(account_id,next_attempt) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET next_attempt=excluded.next_attempt",
                        (row["account_id"], now + 30),
                    )
            return dict(row) if row else None

        job = await self.store.call(claim, write=True)
        if not job:
            return False
        status, error = "succeeded", ""
        failure_log = None
        try:
            outcome = await self.process(job)
            if isinstance(outcome, PolishResult):
                status, error = outcome.status, outcome.message
            elif outcome == "skipped":
                status = "skipped"
        except Exception as exc:  # noqa: BLE001
            safe_failure = isinstance(
                exc, (Preconditions, FishPublishPreparationError, ListingRejected)
            )
            status = "failed" if safe_failure else "uncertain"
            error = (
                str(exc)
                if safe_failure or isinstance(exc, FishListingUnconfirmed)
                else f"远端结果待核对（{type(exc).__name__}）"
            )
            location = "\n".join(
                f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(exc.__traceback__)
            )
            failure_log = (
                f"{type(exc).__name__}: {str(exc) if isinstance(exc, FishAPIError) else error}",
                location,
            )
            if job["kind"] == "edit":
                await self.editor.failure(job, safe_failure, error)
            if job["kind"] == "polish" and not safe_failure:
                await self.store.execute(
                    "UPDATE products SET polish_status='uncertain',polish_error='擦亮结果未确认，请先核对平台结果' WHERE id=?",
                    (job["target"],),
                )
                await self.store.execute(
                    "INSERT INTO polish_accounts(account_id,next_attempt) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET next_attempt=MAX(next_attempt,excluded.next_attempt)",
                    (job["account_id"], time.time() + 3600),
                )
            if job["kind"] == "save_draft":
                await self.store.execute(
                    "UPDATE products SET draft_state=CASE WHEN ? THEN CASE WHEN draft_id='' THEN 'local' ELSE 'dirty' END ELSE 'uncertain' END WHERE id=?",
                    (safe_failure, job["target"]),
                )
            if job["kind"] == "publish" and isinstance(
                exc, FishPublishPreparationError
            ):
                await self.store.execute(
                    "UPDATE products SET status='draft' WHERE id=? AND item_id=''",
                    (job["target"],),
                )
            logger.warning(
                "虚拟商品任务失败: job={} kind={} error={}",
                job["id"],
                job["kind"],
                error,
            )
            if not safe_failure:
                logger.warning(
                    "任务异常位置: {}",
                    " -> ".join(
                        f"{frame.name}:{frame.lineno}"
                        for frame in traceback.extract_tb(exc.__traceback__)
                    ),
                )
            if job["kind"] == "deliver":
                await self.store.execute(
                    "UPDATE orders SET error=? WHERE key=?", (error, job["target"])
                )

        def finish_job(db):
            finished = time.time()
            db.execute(
                "UPDATE jobs SET status=?,error=?,updated=? WHERE id=?",
                (status, error, finished, job["id"]),
            )
            if failure_log:
                db.execute(
                    "INSERT INTO job_logs(job_id,message,location,created) VALUES(?,?,?,?)",
                    (job["id"], *failure_log, finished),
                )

        await self.store.call(finish_job, write=True)
        await self.audit("system", f"{job['kind']}:{status}", job["target"])
        return True

    async def worker(self):
        while True:
            try:
                await self.polisher.queue_due()
                if await self.work_once():
                    continue
                self.wake.clear()
                await asyncio.wait_for(self.wake.wait(), timeout=2)
            except TimeoutError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.error("虚拟商品任务处理异常: {}", type(exc).__name__)
                await asyncio.sleep(5)
