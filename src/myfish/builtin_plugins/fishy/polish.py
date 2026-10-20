"""商品擦亮调度。"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from myfish.adapters.fish.api import POLISH_ALREADY_CODES, FishPolishError

INELIGIBLE_ERRORS = (
    "平台提示当前商品状态不支持擦亮，已暂停自动擦亮",
    "商品不在售，已跳过擦亮",
)

ALREADY_POLISHED_MESSAGE = "平台确认该商品已擦亮，本次未新增擦亮"


@dataclass(frozen=True)
class PolishResult:
    status: Literal["succeeded", "skipped"]
    message: str = ""


if TYPE_CHECKING:
    from myfish.adapters.fish import FishBot
    from myfish.adapters.fish.api import FishAPI

    from .store import Store


class Polisher:
    def __init__(self, store: Store, bots: Sequence[FishBot]) -> None:
        self.store = store
        self.bots = bots

    @staticmethod
    def cooldown_message(product) -> str:
        next_at = (
            datetime.fromtimestamp(product["polish_next_at"])
            .astimezone()
            .isoformat(timespec="seconds")
        )
        if product["polish_status"] in {"polished", "already_polished"}:
            return f"平台已确认该商品已擦亮，下次可擦亮时间：{next_at}"
        return f"尚未到下次擦亮时间：{next_at}"

    async def configure(self, product_id, enabled, hours):
        from .service import Preconditions

        def update(db):
            product = db.execute(
                "SELECT * FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if not product:
                raise Preconditions("商品不存在")
            db.execute(
                "UPDATE products SET auto_polish=?,polish_interval_hours=?,polish_next_at=MAX(polish_next_at,CASE WHEN polish_last_at>0 THEN polish_last_at+? ELSE 0 END) WHERE id=?",
                (enabled, hours, hours * 3600, product_id),
            )
            if not enabled:
                db.execute(
                    "UPDATE jobs SET status='cancelled',updated=? WHERE kind='polish' AND target=? AND status='pending'",
                    (time.time(), str(product_id)),
                )
                db.execute(
                    "UPDATE products SET polish_status='paused',polish_error='' WHERE id=? AND "
                    "(polish_status IN ('queued','ineligible') OR "
                    "(polish_status IN ('never','waiting','paused') AND polish_error IN (?,?)))",
                    (product_id, *INELIGIBLE_ERRORS),
                )
            elif product["polish_status"] == "ineligible" or (
                product["polish_status"] in {"never", "waiting", "paused"}
                and product["polish_error"] in INELIGIBLE_ERRORS
            ):
                db.execute(
                    "UPDATE products SET polish_status='waiting',polish_error='',polish_next_at=CASE WHEN polish_last_at>0 THEN polish_last_at+MAX(24,polish_interval_hours)*3600 ELSE 0 END WHERE id=?",
                    (product_id,),
                )

        await self.store.call(update, write=True)

    async def resume_if_published(self, product_id: int) -> None:
        await self.store.execute(
            "UPDATE products SET polish_status=CASE WHEN auto_polish=1 THEN 'waiting' ELSE 'paused' END,"
            "polish_error='',polish_next_at=CASE WHEN polish_last_at>0 "
            "THEN polish_last_at+MAX(24,polish_interval_hours)*3600 ELSE 0 END "
            "WHERE id=? AND status='published' AND item_id!='' AND "
            "(polish_status='ineligible' OR (polish_status IN ('never','waiting','paused') "
            "AND polish_error IN (?,?)))",
            (product_id, *INELIGIBLE_ERRORS),
        )

    async def queue_due(self):
        ready = {
            bot.bot_id for bot in self.bots if bot.bot_id and bot.adapter._active_ws
        }
        if not ready:
            return
        now = time.time()

        def queue(db):
            placeholders = ",".join("?" for _ in ready)
            rows = db.execute(
                f"SELECT * FROM products p WHERE account_id IN ({placeholders}) AND auto_polish=1 AND status='published' AND item_id!='' AND polish_next_at<=? AND polish_status NOT IN ('queued','running','uncertain','ineligible') AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.target=CAST(p.id AS TEXT) AND j.kind IN ('publish','delist','reshelf','edit','save_draft','polish') AND j.status IN ('pending','running')) ORDER BY polish_next_at,id LIMIT 50",
                (*ready, now),
            ).fetchall()
            for product in rows:
                if db.execute(
                    "SELECT 1 FROM product_edits WHERE product_id=? AND state IN ('queued','running','reviewing','uncertain')",
                    (product["id"],),
                ).fetchone():
                    continue
                db.execute(
                    "INSERT INTO jobs(kind,target,account_id,run_at,created,updated) VALUES('polish',?,?,?,?,?)",
                    (str(product["id"]), product["account_id"], now, now, now),
                )
                db.execute(
                    "UPDATE products SET polish_status='queued',polish_error='' WHERE id=?",
                    (product["id"],),
                )

        await self.store.call(queue, write=True)

    async def execute(self, product, api: FishAPI) -> PolishResult:
        from .service import Preconditions

        now = time.time()
        product_id = product["id"]
        if product["status"] != "published" or not product["item_id"]:
            await self.store.execute(
                "UPDATE products SET polish_status='ineligible',polish_error=? WHERE id=?",
                (INELIGIBLE_ERRORS[1], product_id),
            )
            return PolishResult("skipped", INELIGIBLE_ERRORS[1])
        if product["polish_next_at"] > now:
            await self.store.execute(
                "UPDATE products SET polish_status=CASE WHEN polish_status IN ('polished','already_polished') THEN polish_status ELSE 'waiting' END WHERE id=?",
                (product_id,),
            )
            return PolishResult("skipped", self.cooldown_message(product))
        await self.store.execute(
            "UPDATE products SET polish_status='running',polish_error='' WHERE id=?",
            (product_id,),
        )
        try:
            response = await api.polish_item(product["item_id"])
        except FishPolishError as exc:
            codes = set(exc.codes)
            auth_error = any(
                "SESSION_EXPIRED" in c or "TOKEN" in c or "USER_VALIDATE" in c
                for c in codes
            )
            unsupported = any("UNSUPPORTED_ITEM_STATUS" in c for c in codes)
            delay = 3600 if auth_error else 21600
            message = (
                "登录或安全验证未完成，请先恢复账号登录"
                if auth_error
                else "平台拒绝擦亮: " + ",".join(exc.codes)
            )
            if unsupported:
                message = INELIGIBLE_ERRORS[0]
            await self.store.execute(
                "UPDATE products SET polish_status=?,polish_error=?,polish_next_at=? WHERE id=?",
                (
                    "ineligible" if unsupported else "error",
                    message,
                    product["polish_next_at"] if unsupported else now + delay,
                    product_id,
                ),
            )
            if auth_error or any("LIMIT" in code for code in codes):
                await self.store.execute(
                    "INSERT INTO polish_accounts(account_id,next_attempt) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET next_attempt=MAX(next_attempt,excluded.next_attempt)",
                    (product["account_id"], now + delay),
                )
            raise Preconditions(message) from exc
        codes = {str(code).split("::", 1)[0] for code in response.get("ret", [])}
        duplicate = "SUCCESS" not in codes and bool(codes & POLISH_ALREADY_CODES)
        if not duplicate and "SUCCESS" not in codes:
            raise RuntimeError("擦亮响应未确认成功")
        next_at = now + max(24, product["polish_interval_hours"]) * 3600
        await self.store.execute(
            "UPDATE products SET polish_status=?,polish_error='',polish_next_at=?,polish_last_at=CASE WHEN ? THEN polish_last_at ELSE ? END WHERE id=?",
            (
                "already_polished" if duplicate else "polished",
                next_at,
                duplicate,
                now,
                product_id,
            ),
        )
        if duplicate:
            return PolishResult("skipped", ALREADY_POLISHED_MESSAGE)
        return PolishResult("succeeded")
