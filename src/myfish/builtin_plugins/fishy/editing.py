from __future__ import annotations

import copy
import json
import re
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from myfish.adapters.fish.api import FishListingUnconfirmed, FishPublishPreparationError
from myfish.adapters.fish.utils import (
    ListingRejected,
    edit_fingerprint,
    edit_matches,
    edit_values,
    published_edit_payload,
)

if TYPE_CHECKING:
    from myfish.adapters.fish import FishBot
    from myfish.adapters.fish.api import FishAPI

    from .store import Store


class ListingEditor:
    def __init__(self, store: Store, bot: Callable[[str], FishBot]) -> None:
        self.store, self.bot = store, bot

    def check(self, db, product_id):
        product = db.execute(
            "SELECT * FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if not product or not product["item_id"] or product["status"] != "published":
            raise ListingRejected("仅支持编辑已关联的在售商品")
        active = db.execute(
            "SELECT id FROM jobs WHERE target=? AND kind IN ('publish','delist','reshelf','save_draft','polish','edit') AND status IN ('pending','running','uncertain')",
            (str(product_id),),
        ).fetchone()
        if active:
            raise ListingRejected("商品已有任务或结果待核对，请先处理后再编辑")
        return product

    def view(self, record, product):
        baseline = json.loads(record["baseline"])
        return {
            "revision": record["revision"],
            "state": record["state"],
            "error": record["error"],
            "candidate": json.loads(record["candidate"]),
            "product_type": product["product_type"],
            "mode": product["mode"],
            "remote_images": [
                {"ref": f"remote:{i}", "url": image.get("url", "")}
                for i, image in enumerate(baseline.get("imageInfoDOList", []))
            ],
            "job_id": record["job_id"],
        }

    async def open(self, product_id, *, reload=False):
        products = await self.store.rows(
            "SELECT * FROM products WHERE id=?", (product_id,)
        )
        if not products:
            raise ListingRejected("商品不存在")
        product = products[0]
        records = await self.store.rows(
            "SELECT * FROM product_edits WHERE product_id=?", (product_id,)
        )
        if records and not reload:
            return self.view(records[0], product)
        if records and records[0]["state"] not in {"draft", "failed", "applied"}:
            raise ListingRejected(
                "编辑任务正在执行、审核或待核对，不能重新读取覆盖候选"
            )
        detail = await self.bot(product["account_id"]).adapter.api.get_item_edit_detail(
            product["item_id"], verify_owner=False
        )
        if (
            str(detail.get("itemId")) != product["item_id"]
            or str(detail.get("itemStatus")) != "0"
        ):
            raise ListingRejected("平台商品不处于在售状态，请同步后重试")
        owner = detail.get("userId")
        if owner not in (None, "", 0, "0") and str(owner) != product["account_id"]:
            raise ListingRejected("平台商品所属账号不匹配")
        if detail.get("itemSkuList"):
            raise ListingRejected("暂不支持多规格商品编辑")
        values = edit_values(detail)
        values["quantity"] = max(values["quantity"], 1)
        if not values["images"]:
            raise ListingRejected("平台商品缺少图片，无法准备编辑")

        def insert(db):
            current = self.check(db, product_id)
            if (
                current["item_id"] != product["item_id"]
                or current["account_id"] != product["account_id"]
            ):
                raise ListingRejected("商品关联已改变，请重新打开编辑")
            row = db.execute(
                "SELECT * FROM product_edits WHERE product_id=?", (product_id,)
            ).fetchone()
            if row and row["state"] not in {"draft", "failed", "applied"}:
                raise ListingRejected("已有编辑任务，请等待完成")
            if row and not reload and row["state"] != "applied":
                return dict(row)
            db.execute(
                "INSERT INTO product_edits(product_id,revision,candidate,baseline,updated) VALUES(?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET revision=excluded.revision,candidate=excluded.candidate,baseline=excluded.baseline,expected='{}',state='draft',job_id=NULL,error='',updated=excluded.updated",
                (
                    product_id,
                    uuid.uuid4().hex,
                    json.dumps(values),
                    json.dumps(detail),
                    time.time(),
                ),
            )
            return dict(
                db.execute(
                    "SELECT * FROM product_edits WHERE product_id=?", (product_id,)
                ).fetchone()
            )

        return self.view(await self.store.call(insert, write=True), product)

    async def save(self, product_id, revision, candidate, *, submit=False):
        def save(db):
            product = self.check(db, product_id)
            record = db.execute(
                "SELECT * FROM product_edits WHERE product_id=?", (product_id,)
            ).fetchone()
            if (
                not record
                or record["revision"] != revision
                or record["state"] not in {"draft", "failed"}
            ):
                raise ListingRejected("编辑候选已改变或正在执行，请重新打开")
            baseline = json.loads(record["baseline"])
            for ref in candidate["images"]:
                if ref.startswith("remote:"):
                    index = ref.removeprefix("remote:")
                    if not re.fullmatch(r"0|[1-9][0-9]*", index) or int(index) >= len(
                        baseline.get("imageInfoDOList", [])
                    ):
                        raise ListingRejected("原商品图片引用无效")
                elif (
                    not re.fullmatch(r"[a-f0-9]{32}\.jpg", ref)
                    or not (self.store.directory / "media" / ref).is_file()
                ):
                    raise ListingRejected("新增商品图片无效")
            if product["product_type"] == "virtual" and candidate["shipping"] not in {
                "keep",
                "none",
            }:
                raise ListingRejected("虚拟商品不支持设置快递运费")
            job_id, now = None, time.time()
            if submit:
                job_id = db.execute(
                    "INSERT INTO jobs(kind,target,account_id,run_at,created,updated) VALUES('edit',?,?,?,?,?)",
                    (str(product_id), product["account_id"], now, now, now),
                ).lastrowid
            new_revision = uuid.uuid4().hex
            db.execute(
                "UPDATE product_edits SET revision=?,candidate=?,state=?,job_id=?,expected='{}',error='',updated=? WHERE product_id=?",
                (
                    new_revision,
                    json.dumps(candidate),
                    "queued" if submit else "draft",
                    job_id,
                    now,
                    product_id,
                ),
            )
            return {"revision": new_revision, "job_id": job_id}

        return await self.store.call(save, write=True)

    async def execute(self, job, product, api: FishAPI):
        records = await self.store.rows(
            "SELECT * FROM product_edits WHERE product_id=? AND job_id=?",
            (product["id"], job["id"]),
        )
        if (
            not records
            or records[0]["state"] != "queued"
            or product["status"] != "published"
        ):
            raise ListingRejected("编辑候选或商品状态已改变")
        record = records[0]
        await self.store.execute(
            "UPDATE product_edits SET state='running',updated=? WHERE product_id=? AND job_id=?",
            (time.time(), product["id"], job["id"]),
        )
        detail = await api.get_item_edit_detail(product["item_id"])
        if edit_fingerprint(detail) != edit_fingerprint(json.loads(record["baseline"])):
            raise ListingRejected("平台商品已被修改，请重新读取详情后编辑")
        changes = json.loads(record["candidate"])
        if product["product_type"] == "virtual":
            changes["quantity"] = 1
        if changes["quantity"] < 1:
            raise ListingRejected("发布数量必须大于零")
        # 在上传前校验商品归属和状态。
        published_edit_payload(
            detail,
            product["item_id"],
            product["account_id"],
            changes,
            detail["imageInfoDOList"],
        )
        images = []
        for ref in changes["images"]:
            if ref.startswith("remote:"):
                images.append(
                    copy.deepcopy(detail["imageInfoDOList"][int(ref.split(":")[1])])
                )
            else:
                try:
                    response = await api.upload_media(
                        str(self.store.directory / "media" / ref)
                    )
                    obj = response["object"]
                    width, height = map(int, obj["pix"].split("x"))
                    if (
                        min(width, height) <= 0
                        or not isinstance(obj["url"], str)
                        or not obj["url"].startswith("https://")
                    ):
                        raise ValueError("invalid upload")
                    images.append(
                        {
                            "url": obj["url"],
                            "widthSize": width,
                            "heightSize": height,
                            "type": 0,
                            "major": not images,
                            "status": "done",
                            "isQrCode": False,
                        }
                    )
                except Exception as exc:
                    raise FishPublishPreparationError(
                        "编辑图片上传失败，尚未提交商品修改"
                    ) from exc
        for index, image in enumerate(images):
            image["major"] = index == 0
        payload = published_edit_payload(
            detail, product["item_id"], product["account_id"], changes, images
        )
        await self.store.execute(
            "UPDATE product_edits SET candidate=?,expected=?,updated=? WHERE product_id=? AND job_id=?",
            (
                json.dumps(changes),
                json.dumps(payload),
                time.time(),
                product["id"],
                job["id"],
            ),
        )
        after = await api.submit_item_edit(product["item_id"], payload)
        if str(after.get("itemStatus")) == "-9":

            def review(db):
                db.execute(
                    "UPDATE product_edits SET state='reviewing',updated=? WHERE product_id=? AND job_id=?",
                    (time.time(), product["id"], job["id"]),
                )
                db.execute(
                    "UPDATE products SET status='reviewing',updated=? WHERE id=?",
                    (time.time(), product["id"]),
                )

            await self.store.call(review, write=True)
        else:
            try:
                matched = await self.reconcile(product["id"], after, running=True)
            except ListingRejected as exc:
                raise FishListingUnconfirmed(
                    "商品编辑已提交，但回查字段无法确认，请核对平台内容"
                ) from exc
            if not matched:
                raise FishListingUnconfirmed(
                    "商品编辑已提交，但回查内容尚未与编辑候选一致"
                )

    async def failure(self, job, safe, error):
        await self.store.execute(
            "UPDATE product_edits SET state=?,error=?,updated=? WHERE product_id=? AND job_id=? AND state IN ('queued','running')",
            (
                "failed" if safe else "uncertain",
                error,
                time.time(),
                job["target"],
                job["id"],
            ),
        )

    async def resolve(self, job_id, decision):
        rows = await self.store.rows(
            "SELECT e.*,j.status AS job_state,p.item_id,p.account_id FROM product_edits e JOIN jobs j ON j.id=e.job_id JOIN products p ON p.id=e.product_id WHERE j.id=?",
            (job_id,),
        )
        if not rows or (
            rows[0]["job_state"] not in {"failed", "uncertain"}
            and not (rows[0]["state"] == "reviewing" and decision == "not_executed")
        ):
            raise ListingRejected("只能核对当前失败或结果不明确的编辑任务")
        record = rows[0]
        expected = json.loads(record["expected"])
        if expected:
            detail = await self.bot(
                record["account_id"]
            ).adapter.api.get_item_edit_detail(record["item_id"], verify_owner=False)
            owner = detail.get("userId")
            if str(detail.get("itemId")) != record["item_id"] or (
                owner not in (None, "", 0, "0") and str(owner) != record["account_id"]
            ):
                raise ListingRejected("平台商品或账号不匹配")
            if decision == "executed":
                if not await self.reconcile(record["product_id"], detail):
                    raise ListingRejected(
                        "平台内容尚未确认与编辑候选一致，请等待审核或继续核对"
                    )
                return
            if str(detail.get("itemStatus")) != "0" or edit_matches(expected, detail):
                raise ListingRejected("平台尚在审核或内容已符合候选，不能确认未执行")
        elif decision == "executed":
            raise ListingRejected("该任务没有已提交的编辑内容，不能确认已执行")

        def reset(db):
            current = db.execute(
                "SELECT revision FROM product_edits WHERE product_id=? AND job_id=?",
                (record["product_id"], job_id),
            ).fetchone()
            if not current or current["revision"] != record["revision"]:
                raise ListingRejected("已有更新的编辑候选，请刷新")
            db.execute(
                "UPDATE product_edits SET state='draft',expected='{}',error='',revision=?,updated=? WHERE product_id=?",
                (uuid.uuid4().hex, time.time(), record["product_id"]),
            )
            db.execute(
                "UPDATE jobs SET status='cancelled',error='管理员已核对编辑未执行',updated=? WHERE id=?",
                (time.time(), job_id),
            )
            if expected:
                db.execute(
                    "UPDATE products SET status='published',updated=? WHERE id=?",
                    (time.time(), record["product_id"]),
                )

        await self.store.call(reset, write=True)

    async def reconcile(self, product_id, detail, *, running=False):
        rows = await self.store.rows(
            "SELECT * FROM product_edits WHERE product_id=?", (product_id,)
        )
        if not rows or rows[0]["state"] not in (
            {"running"} if running else {"reviewing", "uncertain"}
        ):
            return False
        record = rows[0]
        expected = json.loads(record["expected"])
        if (
            not expected
            or str(detail.get("itemId")) != str(expected.get("itemId"))
            or str(detail.get("itemStatus")) != "0"
            or not edit_matches(expected, detail)
        ):
            return False
        changes = json.loads(record["candidate"])

        def finish(db):
            current = db.execute(
                "SELECT revision,state FROM product_edits WHERE product_id=?",
                (product_id,),
            ).fetchone()
            if (
                not current
                or current["revision"] != record["revision"]
                or current["state"] != record["state"]
            ):
                raise ListingRejected("编辑任务已改变，请刷新")
            db.execute(
                "UPDATE products SET title=?,description=?,price=?,quantity=?,images=?,shipping=CASE WHEN ?='keep' THEN shipping ELSE ? END,postage=CASE WHEN ?='keep' THEN postage ELSE ? END,status='published',updated=? WHERE id=?",
                (
                    changes["title"],
                    changes["description"],
                    changes["price"],
                    changes["quantity"],
                    json.dumps(
                        [
                            ref
                            for ref in changes["images"]
                            if not ref.startswith("remote:")
                        ]
                    ),
                    changes["shipping"],
                    changes["shipping"],
                    changes["shipping"],
                    changes["postage"],
                    time.time(),
                    product_id,
                ),
            )
            db.execute(
                "UPDATE product_edits SET state='applied',error='',updated=? WHERE product_id=?",
                (time.time(), product_id),
            )
            db.execute(
                "UPDATE jobs SET status='succeeded',error='',updated=? WHERE id=? AND status='uncertain'",
                (time.time(), record["job_id"]),
            )

        await self.store.call(finish, write=True)
        return True
