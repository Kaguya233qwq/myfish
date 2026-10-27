import asyncio
import base64
import hmac
import io
import json
import re
import secrets
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self, TypeAlias
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from myfish.adapters.fish.api import FishAPIError, FishPublishPreparationError
from myfish.adapters.fish.model import PublishCategory

from .auth import Auth, AuthError, SessionUser
from .config import Settings
from .service import Preconditions, Service


async def current_user(request: Request) -> SessionUser:
    auth: Auth = request.app.state.auth
    user = await auth.session(request.cookies.get("vg_session", ""))
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
        request.headers.get("x-csrf-token", "").encode(), user["csrf"].encode()
    ):
        raise HTTPException(403, "请求令牌无效，请刷新页面")
    return user


CurrentUser: TypeAlias = Annotated[SessionUser, Depends(current_user)]


async def require_admin(user: CurrentUser) -> SessionUser:
    if user["role"] != "admin":
        raise HTTPException(403, "仅管理员可执行此操作")
    return user


AdminUser: TypeAlias = Annotated[SessionUser, Depends(require_admin)]


class Login(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class Setup(Login):
    token: str = Field(min_length=20, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")


class NewUser(Login):
    role: str = "operator"


class PasswordChange(BaseModel):
    old_password: str = Field(max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class UserUpdate(BaseModel):
    role: str
    active: bool


class PasswordReset(BaseModel):
    password: str = Field(min_length=12, max_length=128)


class ProductInput(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=5000)
    price: Decimal = Field(gt=0, le=1000000, decimal_places=2)
    account_id: str = Field(pattern=r"^[0-9]{1,30}$")
    item_id: str = Field(default="", pattern=r"^[0-9]{0,30}$")
    mode: str = Field(pattern=r"^(reusable|physical)$")
    product_type: str = Field(default="virtual", pattern=r"^(virtual|physical)$")
    category: PublishCategory | None = None
    quantity: int = Field(default=1, ge=1, le=9999)
    shipping: str = Field(default="free", pattern=r"^(free|fixed)$")
    postage: Decimal = Field(default=Decimal(0), ge=0, le=100000, decimal_places=2)
    content: str = Field(default="", max_length=10000)
    images: list[str] = Field(default_factory=list, max_length=9)
    auto_delivery: bool = False
    auto_polish: bool = False
    polish_interval_hours: int = Field(default=24, ge=24, le=168)

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        if (self.product_type == "physical") != (self.mode == "physical"):
            raise ValueError("实物商品必须选择快递发货，虚拟商品必须选择固定内容")
        if self.product_type == "physical" and (self.auto_delivery or self.content):
            raise ValueError("实物商品不能开启虚拟内容自动交付")
        if (
            self.product_type == "physical"
            and self.shipping == "fixed"
            and self.postage <= 0
        ):
            raise ValueError("固定运费必须大于零")
        return self


class ImportedProductType(BaseModel):
    product_type: Literal["virtual", "physical"]


class CategoryQuery(BaseModel):
    account_id: str = Field(pattern=r"^[0-9]{1,30}$")
    product_type: str = Field(pattern=r"^(virtual|physical)$")
    keyword: str = Field(default="", max_length=200)


class ListingChanges(BaseModel):
    model_config = {"extra": "forbid"}
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=5000)
    price: Decimal = Field(gt=0, le=1000000, decimal_places=2)
    quantity: int = Field(ge=1, le=9999)
    shipping: str = Field(pattern=r"^(keep|free|fixed|none)$")
    postage: Decimal = Field(ge=0, le=100000, decimal_places=2)
    images: list[str] = Field(min_length=1, max_length=9)

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        if len(set(self.images)) != len(self.images):
            raise ValueError("商品图片不能重复")
        if self.shipping == "fixed" and self.postage <= 0:
            raise ValueError("固定运费必须大于零")
        return self


class ListingEditRequest(BaseModel):
    revision: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate: ListingChanges
    submit: bool = False


class Automation(BaseModel):
    auto_delivery: bool


class PolishSettings(BaseModel):
    enabled: bool
    interval_hours: int = Field(default=24, ge=24, le=168)


class DeliveryContent(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class Schedule(BaseModel):
    run_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ImageInput(BaseModel):
    data: str = Field(max_length=15000000)


class Bind(BaseModel):
    item_id: str = Field(pattern=r"^[0-9]{1,30}$")


class Chat(BaseModel):
    chat_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")


class Resolution(BaseModel):
    decision: str = Field(pattern=r"^(executed|not_executed)$")
    draft_id: str = Field(default="", max_length=256, pattern=r"^[^\x00-\x20\x7f]*$")


class BodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 16 * 1024 * 1024:
                return await JSONResponse(
                    {"detail": "请求超过 16 MiB"}, status_code=413
                )(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {
                "type": "http.request",
                "body": b"".join(chunks),
                "more_body": False,
            }

        await self.app(scope, replay, send)


def create_app(service: Service, settings: Settings) -> FastAPI:
    store = service.store
    auth = Auth(store, settings.session_hours)
    app = FastAPI(title="渔渔", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.add_middleware(BodyLimit)
    hosts = ["localhost", "127.0.0.1", settings.host]
    if settings.public_origin:
        hostname = urlsplit(settings.public_origin).hostname
        if hostname:
            hosts.append(hostname)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[host for host in hosts if host]
    )

    @app.middleware("http")
    async def security(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = settings.public_origin.rstrip("/") or str(
                request.base_url
            ).rstrip("/")
            if origin and origin != expected:
                return JSONResponse({"detail": "来源校验失败"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.alicdn.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(FishPublishPreparationError)
    async def publish_preparation_error(
        request: Request, exc: FishPublishPreparationError
    ) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        return JSONResponse({"detail": "记录已存在或关联数据无效"}, status_code=409)

    @app.get("/api/auth/status")
    async def setup_status():
        rows = await store.rows("SELECT COUNT(*) AS n FROM users")
        return {"needs_setup": rows[0]["n"] == 0}

    @app.post("/api/auth/setup")
    async def setup(body: Setup):
        await auth.create_user(
            body.username, body.password, "admin", setup_token=body.token
        )
        await service.audit(body.username, "setup", "admin")
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login(body: Login, request: Request, response: Response):
        token, csrf, expires = await auth.login(
            body.username,
            body.password,
            request.client.host if request.client else "unknown",
        )
        response.set_cookie(
            "vg_session",
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        await service.audit(body.username, "login", "")
        return {"csrf": csrf, "expires": expires}

    @app.get("/api/auth/me")
    async def me(user: CurrentUser):
        return user

    @app.post("/api/auth/logout")
    async def logout(response: Response, user: CurrentUser):
        await store.execute("DELETE FROM sessions WHERE id=?", (user["session_id"],))
        response.delete_cookie("vg_session", path="/")
        return {"ok": True}

    @app.post("/api/auth/password")
    async def change_password(body: PasswordChange, user: CurrentUser):
        await auth.change_password(user["id"], body.old_password, body.new_password)
        await service.audit(user["username"], "password_changed", user["id"])
        return {"ok": True}

    @app.get("/api/auth/sessions")
    async def sessions(user: CurrentUser):
        return await store.rows(
            "SELECT id,created,expires FROM sessions WHERE user_id=? AND expires>?",
            (user["id"], time.time()),
        )

    @app.delete("/api/auth/sessions/{session_id}")
    async def revoke_session(session_id: int, user: CurrentUser):
        await store.execute(
            "DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, user["id"])
        )
        return {"ok": True}

    @app.get("/api/users")
    async def users(user: AdminUser):
        return await store.rows(
            "SELECT id,username,role,active,created FROM users ORDER BY id"
        )

    @app.post("/api/users")
    async def add_user(body: NewUser, user: AdminUser):
        user_id = await auth.create_user(body.username, body.password, body.role)
        await service.audit(user["username"], "user_created", user_id)
        return {"id": user_id}

    @app.put("/api/users/{user_id}")
    async def update_user(user_id: int, body: UserUpdate, user: AdminUser):
        if body.role not in {"admin", "operator"} or user_id == user["id"]:
            raise Preconditions("不能在这里修改自己的权限，或角色无效")

        def update(db: sqlite3.Connection) -> None:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise Preconditions("账号不存在")
            if (
                row["active"]
                and row["role"] == "admin"
                and (not body.active or body.role != "admin")
                and (
                    db.execute(
                        "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
                    ).fetchone()[0]
                    <= 1
                )
            ):
                raise Preconditions("必须保留至少一个有效管理员")
            db.execute(
                "UPDATE users SET active=?,role=? WHERE id=?",
                (body.active, body.role, user_id),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

        await store.call(update, write=True)
        await service.audit(user["username"], "user_updated", user_id)
        return {"ok": True}

    @app.post("/api/users/{user_id}/password")
    async def reset_password(user_id: int, body: PasswordReset, user: AdminUser):
        await auth.reset_password(user_id, body.password)
        await service.audit(user["username"], "password_reset", user_id)
        return {"ok": True}

    @app.get("/api/dashboard")
    async def dashboard(user: CurrentUser):
        return await store.call(
            lambda db: {
                "products": db.execute("SELECT COUNT(*) FROM products").fetchone()[0],
                "pending_jobs": db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')"
                ).fetchone()[0],
                "orders": db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                "attention": db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('failed','uncertain')"
                ).fetchone()[0],
            }
        )

    @app.get("/api/accounts")
    async def accounts(user: CurrentUser):
        return [
            {"id": bot.bot_id, "connected": bool(bot.adapter._active_ws)}
            for bot in service.bots
            if bot.bot_id
        ]

    @app.get("/api/products")
    async def products(user: CurrentUser):
        return await service.products()

    @app.post("/api/catalog/categories")
    async def categories(body: CategoryQuery, user: CurrentUser):
        api = service.bot(body.account_id).adapter.api
        hint = (
            "虚拟商品 电子资料 卡券" if body.product_type == "virtual" else "实物商品"
        )
        return await api.get_publish_categories(f"{hint} {body.keyword}".strip())

    @app.post("/api/products")
    async def add_product(body: ProductInput, user: CurrentUser):
        service.bot(body.account_id)
        if not body.item_id and body.category is None:
            raise Preconditions("请选择商品品类后保存草稿")
        if body.mode == "reusable" and not body.content.strip():
            raise Preconditions("可重复交付商品必须填写交付内容")
        for name in body.images:
            if (
                not re.fullmatch(r"[a-f0-9]{32}\.jpg", name)
                or not (store.directory / "media" / name).is_file()
            ):
                raise Preconditions("商品图片无效，请重新上传")
        product_id = await service.add_product(body, sync_draft=True)
        await service.audit(user["username"], "product_created", product_id)
        return {"id": product_id}

    @app.put("/api/products/{product_id}/product-type")
    async def imported_product_type(
        product_id: int, body: ImportedProductType, user: CurrentUser
    ) -> dict[str, bool]:
        await service.configure_imported_type(product_id, body.product_type)
        await service.audit(user["username"], "product_type_configured", product_id)
        return {"ok": True}

    @app.put("/api/products/{product_id}/automation")
    async def automation(product_id: int, body: Automation, user: CurrentUser):
        rows = await store.rows(
            "SELECT product_type,mode,content FROM products WHERE id=?", (product_id,)
        )
        if not rows:
            raise Preconditions("商品不存在")
        if rows[0]["product_type"] == "physical" and body.auto_delivery:
            raise Preconditions("实物商品不能开启虚拟内容自动交付")
        if body.auto_delivery and (
            rows[0]["mode"] != "reusable" or not rows[0]["content"]
        ):
            raise Preconditions("请先设置固定交付内容")
        await store.execute(
            "UPDATE products SET auto_delivery=?,updated=? WHERE id=?",
            (body.auto_delivery, time.time(), product_id),
        )
        await service.audit(user["username"], "automation_updated", product_id)
        return {"ok": True}

    @app.put("/api/products/{product_id}/polish-settings")
    async def polish_settings(product_id: int, body: PolishSettings, user: CurrentUser):
        await service.polisher.configure(product_id, body.enabled, body.interval_hours)
        service.wake.set()
        await service.audit(user["username"], "polish_settings_updated", product_id)
        return {"ok": True}

    @app.put("/api/products/{product_id}")
    async def update_product(product_id: int, body: ProductInput, user: CurrentUser):
        service.bot(body.account_id)
        if not body.item_id and body.category is None:
            raise Preconditions("请选择商品品类后保存草稿")
        for name in body.images:
            if (
                not re.fullmatch(r"[a-f0-9]{32}\.jpg", name)
                or not (store.directory / "media" / name).is_file()
            ):
                raise Preconditions("商品图片无效")

        def update(db: sqlite3.Connection) -> None:
            old = db.execute(
                "SELECT * FROM products WHERE id=?", (product_id,)
            ).fetchone()
            active = db.execute(
                "SELECT id FROM jobs WHERE target=? AND kind IN ('publish','delist','save_draft') AND status IN ('pending','running')",
                (str(product_id),),
            ).fetchone()
            if not old or old["item_id"] or old["status"] != "draft" or active:
                raise Preconditions("仅可编辑没有待执行任务的未发布草稿")
            if old["draft_state"] in {"syncing", "uncertain"}:
                raise Preconditions("草稿同步正在执行或结果待核对，请先处理同步任务")
            if old["draft_id"] and old["account_id"] != body.account_id:
                raise Preconditions("已关联闲鱼草稿的商品不能更换所属账号")
            content = store.encrypt(body.content) if body.content else old["content"]
            if body.mode == "reusable" and not content:
                raise Preconditions("请填写固定交付内容")
            db.execute(
                "UPDATE products SET title=?,description=?,price=?,account_id=?,item_id=?,mode=?,content=?,images=?,auto_delivery=?,status=?,updated=?,product_type=?,category=?,quantity=?,shipping=?,postage=? WHERE id=?",
                (
                    body.title,
                    body.description,
                    str(body.price),
                    body.account_id,
                    body.item_id,
                    body.mode,
                    content,
                    json.dumps(body.images),
                    body.auto_delivery,
                    "published" if body.item_id else "draft",
                    time.time(),
                    body.product_type,
                    json.dumps(body.category.platform_data() if body.category else {}),
                    body.quantity,
                    body.shipping,
                    str(body.postage),
                    product_id,
                ),
            )
            db.execute(
                "UPDATE products SET draft_state=CASE WHEN draft_id='' THEN 'local' ELSE 'dirty' END WHERE id=?",
                (product_id,),
            )
            db.execute(
                "UPDATE products SET auto_polish=?,polish_interval_hours=? WHERE id=?",
                (body.auto_polish, body.polish_interval_hours, product_id),
            )
            if not body.item_id:
                now = time.time()
                db.execute(
                    "INSERT INTO jobs(kind,target,account_id,run_at,created,updated) VALUES('save_draft',?,?,?,?,?)",
                    (str(product_id), body.account_id, now, now, now),
                )
                db.execute(
                    "UPDATE products SET draft_state='queued' WHERE id=?", (product_id,)
                )

        await store.call(update, write=True)
        service.wake.set()
        await service.audit(user["username"], "product_updated", product_id)
        return {"ok": True}

    @app.get("/api/products/{product_id}/delivery-content")
    async def delivery_content(product_id: int, user: CurrentUser):
        return {"content": await service.delivery_content(product_id)}

    @app.put("/api/products/{product_id}/delivery-content")
    async def update_delivery_content(
        product_id: int, body: DeliveryContent, user: CurrentUser
    ):
        updated = await service.update_delivery_content(product_id, body.content)
        await service.audit(user["username"], "delivery_content_updated", product_id)
        return {"ok": True, "reserved_updated": updated}

    @app.post("/api/products/{product_id}/bind")
    async def bind(product_id: int, body: Bind, user: CurrentUser):
        def bind(db: sqlite3.Connection) -> None:
            product = db.execute(
                "SELECT * FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if not product:
                raise Preconditions("商品不存在")
            if product["item_id"]:
                raise Preconditions("已有平台 ID 的商品不能覆盖绑定")
            active = db.execute(
                "SELECT id FROM jobs WHERE target=? AND kind IN ('publish','save_draft','delist') AND status IN ('pending','running')",
                (str(product_id),),
            ).fetchone()
            if active:
                raise Preconditions("商品任务正在执行，请等待完成再关联平台商品")
            db.execute(
                "UPDATE products SET item_id=?,status='published',updated=? WHERE id=?",
                (body.item_id, time.time(), product_id),
            )

        await store.call(bind, write=True)
        await service.audit(user["username"], "product_bound", product_id)
        return {"ok": True}

    @app.post("/api/products/{product_id}/listing-edit/open")
    async def open_listing_edit(
        product_id: int, user: CurrentUser, reload: bool = False
    ):
        return await service.editor.open(product_id, reload=reload)

    @app.post("/api/products/{product_id}/listing-edit/resolve")
    async def resolve_listing_edit(product_id: int, body: Resolution, user: AdminUser):
        records = await store.rows(
            "SELECT job_id FROM product_edits WHERE product_id=?", (product_id,)
        )
        if not records or not records[0]["job_id"]:
            raise Preconditions("没有可核对的编辑任务")
        await service.editor.resolve(records[0]["job_id"], body.decision)
        await service.audit(
            user["username"], "listing_edit_resolved_" + body.decision, product_id
        )
        return {"ok": True}

    @app.put("/api/products/{product_id}/listing-edit")
    async def save_listing_edit(
        product_id: int, body: ListingEditRequest, user: CurrentUser
    ):
        result = await service.editor.save(
            product_id,
            body.revision,
            body.candidate.model_dump(mode="json"),
            submit=body.submit,
        )
        await service.audit(
            user["username"],
            "listing_edit_submit" if body.submit else "listing_edit_saved",
            product_id,
        )
        service.wake.set()
        return result

    @app.post("/api/products/{product_id}/{action}")
    async def product_action(
        product_id: int, action: str, body: Schedule, user: CurrentUser
    ):
        if action == "refresh_listing":
            state = await service.refresh_listing(product_id)
            await service.audit(user["username"], action, product_id)
            return {"status": state}
        if action not in {"publish", "delist", "save_draft", "polish", "reshelf"}:
            raise HTTPException(404, "操作不存在")
        job = await service.schedule_product(product_id, action, body.run_at)
        await service.audit(user["username"], action, product_id)
        return {"job_id": job}

    @app.get("/api/orders")
    async def orders(user: CurrentUser):
        return await store.rows(
            "SELECT o.key,o.account_id,o.order_id,o.product_id,o.item_id,o.buyer_id,o.buyer_name,o.amount,o.quantity,o.chat_id,o.platform_status,o.delivery_state,o.error,o.updated,p.product_type,p.mode AS delivery_mode FROM orders o LEFT JOIN products p ON p.id=o.product_id ORDER BY o.updated DESC LIMIT 500"
        )

    @app.post("/api/accounts/{account_id}/products/sync")
    async def sync_products(account_id: str, user: CurrentUser) -> dict[str, int]:
        try:
            imported = await service.import_listings(account_id)
        except FishAPIError as exc:
            raise HTTPException(502, str(exc)) from exc
        await service.audit(user["username"], "sync_products", account_id)
        return {"imported": imported}

    @app.post("/api/accounts/{account_id}/sync")
    async def sync(account_id: str, user: CurrentUser):
        await service.sync(account_id)
        return {"ok": True}

    @app.put("/api/orders/{key}/chat")
    async def chat(key: str, body: Chat, user: CurrentUser):
        await store.execute(
            "UPDATE orders SET chat_id=? WHERE key=?", (body.chat_id, key)
        )
        return {"ok": True}

    @app.post("/api/orders/{key}/deliver")
    async def deliver(key: str, user: CurrentUser):
        rows = await store.rows("SELECT * FROM orders WHERE key=?", (key,))
        if not rows:
            raise Preconditions("订单不存在")
        product = await store.rows(
            "SELECT product_type,mode FROM products WHERE id=?",
            (rows[0]["product_id"],),
        )
        if product and product[0]["product_type"] == "physical":
            raise Preconditions("实物订单请在闲鱼 App 通过快递发货")
        if not product or product[0]["mode"] != "reusable":
            raise Preconditions("请先设置固定交付内容")
        if rows[0]["delivery_state"] == "sending":
            raise Preconditions("消息发送结果待核对，禁止直接重发")
        job = await service.enqueue("deliver", key, rows[0]["account_id"])
        await service.audit(user["username"], "delivery_requested", key)
        return {"job_id": job}

    @app.get("/api/jobs")
    async def jobs(user: CurrentUser):
        return await store.rows("SELECT * FROM jobs ORDER BY id DESC LIMIT 500")

    @app.get("/api/jobs/{job_id}/logs")
    async def job_logs(job_id: int, user: AdminUser):
        jobs = await store.rows("SELECT error,updated FROM jobs WHERE id=?", (job_id,))
        if not jobs:
            raise HTTPException(404, "任务不存在")
        logs = await store.rows(
            "SELECT message,location,created FROM job_logs WHERE job_id=? ORDER BY id",
            (job_id,),
        )
        if not logs and jobs[0]["error"]:
            logs = [
                {
                    "message": jobs[0]["error"],
                    "location": "",
                    "created": jobs[0]["updated"],
                }
            ]
        return logs

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: int, user: CurrentUser):
        def cancel(db: sqlite3.Connection) -> None:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] != "pending":
                raise Preconditions("仅可取消等待执行的任务")
            db.execute(
                "UPDATE jobs SET status='cancelled',updated=? WHERE id=?",
                (time.time(), job_id),
            )
            if job["kind"] == "save_draft":
                db.execute(
                    "UPDATE products SET draft_state=CASE WHEN draft_id='' THEN 'local' ELSE 'dirty' END WHERE id=?",
                    (job["target"],),
                )
            if job["kind"] == "edit":
                db.execute(
                    "UPDATE product_edits SET state='draft',expected='{}',revision=?,updated=? WHERE product_id=? AND job_id=?",
                    (secrets.token_hex(16), time.time(), job["target"], job_id),
                )
            if job["kind"] == "polish":
                db.execute(
                    "UPDATE products SET polish_status='paused',polish_next_at=MAX(polish_next_at,?+polish_interval_hours*3600) WHERE id=?",
                    (time.time(), job["target"]),
                )

        await store.call(cancel, write=True)
        await service.audit(user["username"], "job_cancelled", job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/resolve")
    async def resolve(job_id: int, body: Resolution, user: AdminUser):
        checked_state = None
        records = await store.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if records and records[0]["kind"] == "edit":
            await service.editor.resolve(job_id, body.decision)
            await service.audit(
                user["username"], "listing_edit_resolved_" + body.decision, job_id
            )
            return {"ok": True}
        if records and records[0]["kind"] == "reshelf":
            _, checked_state = await service.listing_state(records[0]["target"])
            expected = (
                {"published", "reviewing"}
                if body.decision == "executed"
                else {"unlisted"}
            )
            if checked_state not in expected:
                raise Preconditions("核对结论与平台当前状态不符")

        def resolve(db: sqlite3.Connection) -> None:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] not in {"uncertain", "failed"}:
                raise Preconditions("仅可核对失败或结果不明确的任务")
            if job["kind"] == "polish":
                active = db.execute(
                    "SELECT id FROM jobs WHERE kind='polish' AND target=? AND id>?",
                    (job["target"], job_id),
                ).fetchone()
                if active:
                    raise Preconditions("请处理最新的擦亮任务")
                now = time.time()
                product = db.execute(
                    "SELECT * FROM products WHERE id=?", (job["target"],)
                ).fetchone()
                next_at = (
                    now + product["polish_interval_hours"] * 3600
                    if body.decision == "executed"
                    else max(
                        now,
                        product["polish_last_at"]
                        + product["polish_interval_hours"] * 3600,
                    )
                )
                db.execute(
                    "UPDATE products SET polish_status=?,polish_error='',polish_next_at=?,polish_last_at=CASE WHEN ? THEN ? ELSE polish_last_at END WHERE id=?",
                    (
                        "polished" if body.decision == "executed" else "never",
                        next_at,
                        body.decision == "executed",
                        now,
                        job["target"],
                    ),
                )
            elif job["kind"] == "save_draft":
                product = db.execute(
                    "SELECT * FROM products WHERE id=?", (job["target"],)
                ).fetchone()
                newer = db.execute(
                    "SELECT id FROM jobs WHERE kind='save_draft' AND target=? AND id>?",
                    (job["target"], job_id),
                ).fetchone()
                if newer:
                    raise Preconditions("已有更新的草稿同步任务，请核对最新任务")
                draft_id = body.draft_id or product["draft_id"]
                if product["draft_id"] and draft_id != product["draft_id"]:
                    raise Preconditions("不能用不同的草稿 ID 覆盖已有平台草稿关联")
                if body.decision == "executed" and not draft_id:
                    raise Preconditions("确认已保存时请填写平台返回的草稿 ID")
                db.execute(
                    "UPDATE products SET draft_id=?,draft_state=?,draft_synced_at=? WHERE id=?",
                    (
                        draft_id
                        if body.decision == "executed"
                        else product["draft_id"],
                        "synced"
                        if body.decision == "executed"
                        else "dirty"
                        if product["draft_id"]
                        else "local",
                        time.time()
                        if body.decision == "executed"
                        else product["draft_synced_at"],
                        product["id"],
                    ),
                )
            elif job["kind"] == "publish":
                if body.decision == "executed":
                    raise Preconditions("已发布的商品请先在商品列表中关联平台商品 ID")
                db.execute(
                    "UPDATE products SET status='draft' WHERE id=? AND item_id=''",
                    (job["target"],),
                )
            elif job["kind"] == "deliver":
                row = db.execute(
                    "SELECT delivery_state FROM orders WHERE key=?", (job["target"],)
                ).fetchone()
                if not row or row["delivery_state"] != "sending":
                    raise Preconditions(
                        "该订单可使用继续确认发货操作，或尚未到消息发送阶段"
                    )
                db.execute(
                    "UPDATE orders SET delivery_state=?,error='' WHERE key=?",
                    (
                        "sent" if body.decision == "executed" else "reserved",
                        job["target"],
                    ),
                )
            elif job["kind"] == "reshelf":
                newer = db.execute(
                    "SELECT id FROM jobs WHERE target=? AND kind IN ('reshelf','delist','publish') AND id>?",
                    (job["target"], job_id),
                ).fetchone()
                if newer:
                    raise Preconditions("已有更新的上下架任务，请核对最新任务")
                db.execute(
                    "UPDATE products SET status=?,updated=? WHERE id=?",
                    (checked_state, time.time(), job["target"]),
                )
            elif job["kind"] == "delist" and body.decision == "executed":
                db.execute(
                    "UPDATE products SET status='unlisted' WHERE id=?", (job["target"],)
                )
            db.execute(
                "UPDATE jobs SET status='cancelled',error='管理员已核对远端结果',updated=? WHERE id=?",
                (time.time(), job_id),
            )

        await store.call(resolve, write=True)
        await service.audit(user["username"], "remote_result_" + body.decision, job_id)
        return {"ok": True}

    @app.get("/api/audit")
    async def audit(user: AdminUser):
        return await store.rows("SELECT * FROM audit ORDER BY id DESC LIMIT 500")

    @app.post("/api/media")
    async def upload(body: ImageInput, user: CurrentUser):
        def save_image() -> str:
            decoded = base64.b64decode(body.data, validate=True)
            if len(decoded) > 10 * 1024 * 1024:
                raise ValueError("图片不能超过 10 MiB")
            image = Image.open(io.BytesIO(decoded))
            if image.width * image.height > 20000000:
                raise ValueError("图片尺寸过大")
            image = image.convert("RGB")
            image.thumbnail((2400, 2400))
            directory = store.directory / "media"
            directory.mkdir(exist_ok=True)
            name = secrets.token_hex(16) + ".jpg"
            image.save(directory / name, "JPEG", quality=90)
            return name

        try:
            name = await asyncio.to_thread(save_image)
        except (ValueError, OSError) as exc:
            raise Preconditions("无效或过大的图片") from exc
        return {"name": name}

    @app.get("/api/media/{name}")
    async def media(name: str, user: CurrentUser):
        if (
            not re.fullmatch(r"[a-f0-9]{32}\.jpg", name)
            or not (store.directory / "media" / name).is_file()
        ):
            raise HTTPException(404, "图片不存在")
        return FileResponse(store.directory / "media" / name)

    dist = Path(__file__).parent / "frontend" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    async def index():
        if not (dist / "index.html").is_file():
            return JSONResponse(
                {
                    "detail": "前端尚未构建，请在插件 frontend 目录执行 npm ci && npm run build"
                },
                status_code=503,
            )
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app
