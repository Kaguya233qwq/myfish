# 小红书适配器

支持终端扫码登录、私信实时接收和文本发送。运行无需 Node.js、浏览器或 JavaScript 引擎。

## 框架配置

在 `config.toml` 中添加：

```toml
[[adapters]]
id = "xhs"

[adapters.config]
receive_offline = false
reconnects = 5
```

执行 `uv run myfish sync` 同步依赖，再执行 `uv run myfish run`。首次运行在终端扫码登录；后续复用 `data/adapters/xhs/auth.state.json`。凭据、设备状态和二维码统一保存在 `data/adapters/xhs/`，无需配置文件路径。状态文件包含登录凭据，应保密保存。

在线私信由独立的 WebSocket 接收任务处理，不定期轮询历史接口。文本发送使用 WebSocket 并等待业务确认；超时或断线时不会自动重发或切换 HTTP，以免重复发送。历史消息 API 仍可显式调用。断线补拉尚未实现，不保证断线期间消息完整。

接收支持文本、图片、大表情、视频和帖子卡片；发送目前只支持文本。群聊、撤回通知、未支持的消息类型以及自身发送的同步消息不会触发插件。`receive_offline = false` 时跳过启动前的推送消息；该选项不会主动获取历史。

## 已验证能力

2026-10-22，使用本人测试账号完成以下真实接口验证：

| 能力 | 结果 |
| --- | --- |
| 匿名导航、DS 参数和安全脚本获取 | 通过 |
| 旧版 Python `websectiga` 解码 | 通过 |
| X25519 / AES-GCM 会话密钥协商 | 通过 |
| 新版 Python 签名及指纹上报 | 参考样本及本机环境生成模式均通过匿名验证 |
| 二维码创建、手机确认和正式会话校验 | 通过 |
| 旧版签名读取账号信息和私信会话列表 | 通过 |
| 会话时间游标、历史 store_id 游标及未读信息 | 通过 |
| HTTP 文本私信发送 | 服务端接受，测试接收方确认收到 |
| WebSocket 握手、IM 注册及实时文本推送 | 通过，实际解码收到回复 |
| 互动通知推送 | `push/message_center` 已实测收到帖子点赞、帖子评论和新增关注的分类计数变化 |

单次成功不代表所有账号、网络或长期运行均有效。新版指纹支持完整参考样本和本机环境生成两种模式。`x84` 可独立生成，`x83` 尚未复现；环境模式暂将其留空。

## 互动事件

适配器监听 `push/message_center` 的分类变化，按需查询对应通知列表，将通知内容转换为框架事件。运行期间不定期轮询通知接口，重复推送会按通知 ID 和时间去重。启动时建立最近通知基线，不分发已有通知；当前不保证补齐断线期间的互动通知。

| 事件 | `event_type` | 内容 |
| --- | --- | --- |
| `XhsFollowEvent` | `xhs.follow` | 新关注者 |
| `XhsLikeEvent` | `xhs.like` | 被点赞的帖子；评论被点赞时另含 `comment` |
| `XhsCommentEvent` | `xhs.comment` | 帖子和评论；回复评论时包含 `comment.target_comment` |

三个事件均包含 `account_id`、`notification_id`、`timestamp_ms`、`user` 和 `raw_payload`。`user` 提供操作者的 ID、昵称和头像。`note.content` 是通知接口返回的帖子摘要，不保证是完整正文；`comment.content` 是接口返回的评论内容。需要完整帖子时可显式调用帖子详情 API。

在插件中注册监听：

```python
from myfish.adapters.xhs.events import (
    XhsCommentEvent,
    XhsFollowEvent,
    XhsLikeEvent,
)


@plugin.on_event(XhsFollowEvent)
async def on_follow(event: XhsFollowEvent) -> None:
    print(event.user.nickname)


@plugin.on_event(XhsLikeEvent)
async def on_like(event: XhsLikeEvent) -> None:
    print(event.user.nickname, event.note.id)


@plugin.on_event(XhsCommentEvent)
async def on_comment(event: XhsCommentEvent) -> None:
    print(event.user.nickname, event.note.id, event.comment.content)
```

帖子点赞、帖子评论和重新关注已使用真实通知验证。收藏、@ 提醒等尚未映射为事件；未知业务通道、主题、通知类型和解析失败会记录 `DEBUG` 日志。将框架 `logger_level` 设置为 `"DEBUG"` 可查看类型和字段结构，日志不包含完整消息内容或登录凭据。单条通知解析失败不会阻止同批其他通知分发。

适配器和独立认证命令均使用框架日志配置。初始化细节、HTTP 方法与接口路径、响应状态和推送注册信息记录为 `DEBUG`；扫码提示、收发消息、事件及连接恢复使用常规日志；连接中断和重试耗尽分别使用 `WARNING` 和 `ERROR`。HTTP 调试日志不包含查询参数、请求体、Cookie 或密钥。终端二维码保留直接渲染。

## 终端扫码登录

安装可选依赖后，使用完整的新版指纹字段 JSON 对象执行：

```bash
uv run --extra xhs python -m myfish.adapters.xhs.auth \
  --current-sign --profile-fields data/adapters/xhs/profile.json
```

该文件应包含同一设备的完整字段集合，不能混合不同设备的字段。仓库不附带第三方项目的指纹样本。

程序完成导航、安全参数初始化、访客激活及指纹上报后，显示终端二维码，并生成 `data/adapters/xhs/login.png`。手机确认后，程序换取正式会话并验证账号不是访客，成功后将 Cookie 保存至 `data/adapters/xhs/auth.json`。

正式扫码登录会提交 X25519 公钥，并使用返回的账号 SSK 替换访客 SSK。新版签名模式下，账号密钥缺失或认证失败时终止登录，不保存不完整状态。

### 风控扫码验证

登录收尾及适配器启动时的账号读取遇到 HTTP 461/471、`verifytype=124` 时，程序显示 App 验证二维码，图片保存在 `data/adapters/xhs/verify.png`。验证会在独立客户端中重新初始化安全参数，不覆盖账号会话；手机确认通过后，原读取请求最多重试一次。取消、失败、过期或超时均终止本次验证。

手动检查已有会话并处理账号读取接口要求的验证：

```bash
uv run --extra xhs python -m myfish.adapters.xhs.auth --verify
```

该命令复用 `auth.state.json`。它不会主动制造验证挑战；接口未要求验证时只校验账号状态。其他接口收到验证要求时抛出 `XhsVerificationRequired`，保留 `status`、`verify_uuid` 和 `verify_type`，可交给 `auth.verify_risk` 处理。评论、关注和私信发送不会自动验证或重发；其他验证类型需通过官方渠道处理。

扫码验证协议及 `mns0201` 已完成模拟测试和固定向量对照，尚待真实挑战验证。

只验证匿名初始化和指纹上报，无需扫码：

```bash
uv run --extra xhs python -m myfish.adapters.xhs.auth \
  --current-sign --profile-fields data/adapters/xhs/profile.json --probe-device
```

根据当前运行环境生成设备字段并验证：

```bash
uv run --extra xhs python -m myfish.adapters.xhs.auth \
  --current-sign --host-device --probe-device
```

环境模式支持 Linux、Windows 和 macOS，按以下规则构建设备档案：

| 信息 | 生成方式 | 回退值 |
| --- | --- | --- |
| 系统与架构 | 标准库读取；UA 和平台字段使用对应浏览器约定 | 未知系统使用 Windows 档案，未知 Linux 架构使用 x86_64 |
| CPU | 逻辑核心数 | 8 |
| 内存 | `psutil` 读取物理内存，四舍五入到邻近的 2 次幂档位，限制为 0.25–8 GiB | 8 GiB |
| 时区 | `tzlocal` 获取 IANA 名称，按该时区计算当前偏移 | UTC，偏移 0 |
| 屏幕 | Linux 读取 DRM 已连接显示器的首个模式；Windows/macOS 使用可选 Tk 桌面查询 | 1920×1080 |
| 浏览器版本、GPU、色深和渲染摘要 | 固定浏览器与软件渲染器配置；摘要为合成值 | 保持固定配置 |

macOS 的浏览器 UA 和平台遵循桌面 Chrome 的 Intel 标识约定，不用于表达实际芯片架构。
屏幕探测值不保证等于浏览器 CSS 像素尺寸；可用区域暂取完整屏幕，不推测任务栏或桌面保留区域。
`x84` 使用本次加载时间生成；已验证的环境模式默认将 `x83` 留空，无需参考文件；这不代表已复现 `x83` 算法。
可额外传入 `--profile-fields` 对照，此模式仅取其中的 `x83`。
Windows/macOS 分支已有模拟测试，尚未在对应系统实测；接口接受上报并不能证明合成字段等同真实浏览器。
2026-10-22，两轮匿名初始化、密钥协商和指纹上报均通过：一轮保留参考 `x83`，一轮留空。
随后使用 `--current-sign --host-device` 完成手机扫码确认、正式会话签发及非访客账号校验，`x83` 全程留空。
另起客户端使用保存的 Cookie 和旧版签名读取账号信息、会话列表，均返回 HTTP 200、`success=true`、`code=0`。
后续加入状态持久化后，重新完成登录，并连续启动两个独立进程恢复同一状态文件；新版签名读取账号信息与会话列表均返回 HTTP 200、`success=true`、`code=0`。
这验证了短时间内的跨进程恢复和计数续用，尚未覆盖长期运行或服务端状态过期。

省略 `--current-sign` 可进行旧签名对照实验。旧签名完成过二维码创建和手机确认，但未通过本次完整登录验证。

扫码确认后的中间会话保存在权限为 `0600` 的 `data/adapters/xhs/auth.pending.json` 中。旧签名实验可在五分钟内使用 `--resume` 继续最终请求；`--resume` 仍仅支持旧签名实验。新版成功登录后会额外保存同名 `.state.json` 文件，包含设备字段、域名及路径范围内的 Cookie、协商密钥、时间锚点和请求计数；其权限同样为 `0600`，应作为登录凭证保存。

## x83 验证结论

当前默认保留 `x83` 字段并设为空字符串，不省略字段，也不生成替代内容。
2026-10-22 的验证结果如下：

| 验证 | 结果 |
| --- | --- |
| 固定其他签名输入，仅改变 `x83` | `b1` 和普通只读请求的签名保持一致 |
| 空值、参考值、参考值、空值四轮独立匿名初始化与指纹上报 | 全部接受 |
| 空值设备的二维码确认及正式登录 | 通过 |
| 空值登录状态的跨进程恢复及后续账号、会话列表读取 | 新版签名通过 |

`x83` 会改变加密后的 `profileData`，因此也会改变该上报请求的请求体及其签名。
本次未观察到它影响上述流程的成功与否；未测量服务端内部评分，也未验证长期风控或其他接口。
四轮匿名验证分别创建新身份，属于功能对照，不代表所有请求的其他输入逐字节相同。

可使用自己的参考字段文件复现匿名对照：

```bash
uv run --extra xhs python src/myfish/tests/live_xhs_x83_check.py \
  --reference data/adapters/xhs/profile.json
```

脚本只取参考文件中的 `x83`，其余设备字段使用本机环境生成，不保存登录凭证。

## 只读接口验证

使用登录命令保存的凭证执行：

```bash
uv run --extra xhs python src/myfish/tests/live_xhs_check.py \
  --cookies data/adapters/xhs/auth.json
```

也可以提供本人账号的完整网页版 Cookie，文件格式为 Cookie 名称到字符串值的 JSON 对象，至少包含 `a1` 和 `web_session`。

新版登录状态可跨进程恢复，登录后执行：

```bash
uv run --extra xhs python src/myfish/tests/live_xhs_check.py \
  --state data/adapters/xhs/auth.state.json
```

`--state` 使用新版签名，不重新生成设备字段、不重新激活访客，也不自动回退到旧签名。
检查结束后保存最新 Cookie 和计数，供下一次恢复使用。同一状态文件应由单个客户端顺序使用。
状态文件没有人为设置有效期，服务端是否接受仍由实测判定，失效时需要重新登录。

使用 `--sign-format xyw` 验证另一种旧签名格式；使用 `--peer 用户ID` 额外验证指定会话的历史记录。

脚本只执行读取操作，输出接口状态及业务错误码，不输出账号资料或消息正文。凭证、二维码和指纹文件保存在 Git 已忽略的 `local/` 中。

## 私信 API

账号、会话和消息接口返回 Pydantic 模型，不再返回原始 `httpx.Response`：

```python
from pathlib import Path

from myfish.adapters.xhs.api import XhsAPI

async with XhsAPI.from_state(Path("data/adapters/xhs/auth.state.json")) as api:
    account = await api.get_self_info()
    unread = await api.get_unread()
    async for chat in api.iter_chats(limit=20):
        pass
    async for message in api.iter_message_history("接收方用户ID", limit=20):
        text = message.text
    api.save_state(Path("data/adapters/xhs/auth.state.json"))
```

`get_chats` 返回单批 `ChatPage`；`next_ts` 是下一批游标，`-1` 表示结束。
实测 `total` 为当前批次数量，单独递增 `page` 不推进数据，遍历应使用 `iter_chats`。
`get_message_history` 返回 `MessagePage`，遍历根据最小正 `store_id` 推进 `last_id`，去除重复消息并在游标停滞时结束。
消息保留原始 `content`，仅已识别的文本消息通过 `text` 属性读取；未知类型不转换成文本。
未读查询不会调用已读标记接口。

文本发送使用独立的 HTTP 短连接接口，传入的是主页 `/user/profile/` 后的用户 ID，而非公开小红书号：

```python
receipt = await api.send_text(user_id, "测试消息")
```

返回 `SendReceipt`，包含客户端消息 ID、服务端消息 ID 及不在对象表示中展示的原始回执。
发送采用 Python Protobuf 编码及 RSA 加密，不自动重试；超时代表结果未确认，应先检查消息历史，避免重复发送。
目前仅实现文本发送，图片、群聊、撤回和已读标记尚未实现。

## 消息节点清洗

`message.py` 使用与其他适配器一致的消息段和节点注册方式：

| 协议内容类型 | 节点 | 消息段 |
| --- | --- | --- |
| `1` | `XhsTextNode` | 框架 `Text` |
| `2` | `XhsImageNode` | 框架 `Image` |
| `3` 中的 `type=note` | `XhsNoteNode` | `NoteCard` |
| `11` | `XhsVideoNode` | 框架 `Video` |
| `13` | `XhsStickerNode` | `Sticker` |

`decode_content` 接收历史消息中的 `content` JSON 字符串，返回框架 `MessageChain`。
未知类型返回空消息链，格式错误或卡片 ID 与链接不匹配时明确报错。

```python
from myfish.adapters.xhs.message import NoteCard, decode_content

chain = decode_content(message.content)
for segment in chain:
    if isinstance(segment, NoteCard):
        note = await api.get_note(segment.ref)
```

笔记卡片保留标题、封面、作者、笔记类型及 `NoteRef`。
分享链接支持 `xhsdiscover://item/<note_id>` 和实际观察到的 `app_share` 来源，不执行链接或跟随重定向。
2026-10-22，已用真实会话中分享的笔记卡片完成节点解码和详情读取验证。
卡片当前仅支持接收和清洗，未实现卡片发送。

图片节点优先保留原图地址，回退至详情图、预览图或远端链接，并保留宽高及资源标识。
视频节点区分视频与封面地址，使用协议中的毫秒时长；不会把封面误当作视频资源。
大表情定义为 `Sticker` 消息段，继承框架 `Image`，额外保留表情键、子类型、动画标识和播放参数。
这些参数仅作为数据保留，不执行动画脚本或操作指令。

2026-10-22，已从真实会话历史中分别解析图片、大表情和视频，并通过上述节点清洗。
目前验证了资源地址和元数据解析，未下载媒体文件，也未验证资源的长期有效性。
图片、大表情和视频尚不支持发送；发送仍需实现上传及对应消息编码。


## 实时接收

```python
from myfish.adapters.xhs.stream import XhsStream

async with XhsStream(api) as stream:
    async for event in stream.events():
        pass
```

连接时获取新的推送令牌，等待鉴权成功后注册 IM，保留初始化期间到达的消息。
客户端使用应用层心跳、接收超时、有限次数的指数退避重连，以及最多 4096 条消息 ID 的内存去重。
推送返回 `PushMessage` 或 `MessageAck`；`PushMessage.payload` 保留服务端 JSON 字符串，尚未转换为框架 `MessageEvent`。
仅解码已观察到的私信 Protobuf 分支，其他业务帧不生成消息事件。
标签页设备标识及推送指纹随状态文件保存，旧状态文件可自动补齐这些字段。

只读监听验证，不输出消息正文：

```bash
uv run --extra xhs python src/myfish/tests/live_xhs_push_check.py \
  --state data/adapters/xhs/auth.state.json --timeout 60
```

2026-10-22，真实环境已通过握手、注册和空闲心跳观察，并捕获两条实际文本推送。
重连、初始化期间消息保留和重复消息过滤已有模拟测试；网络断线后的真实重连尚未专门验证。

## 关注关系

```python
await api.follow_user(user_id)
await api.unfollow_user(user_id)
```

目标参数为用户内部 ID，不是公开小红书号。接口成功后返回 `None`；HTTP 或业务失败继续抛出 `XhsAPIError`，不自动重试。
业务拒绝会记录框架警告，提示检查目标用户及账号限制状态，不把所有失败都判定为风控。
2026-10-22，用户反馈账号处于限制状态后，对已互关的测试小号提交一次关注请求，返回 HTTP 200、`success=false`、`code=-10000`，框架警告正常触发，没有自动重试。
关注接口返回 `-10000` 时，根据本次实测及用户反馈记录“您被系统限制关注他人”的专用警告；该映射仅适用于关注接口，不扩展到其他接口。随后经用户明确授权，取消关注成功，关系由 `BOTH` 变为 `FANS`；再关注仍返回 `-10000`，最终未恢复互关。该次观察显示两种操作的限制表现不同，不能假定账号受限时所有写入均失败。

## 作者作品列表

`get_user_notes` 读取指定作者的一页作品，返回 `UserNotePage`，包含 `notes`、`cursor` 和 `has_more`。作者 ID 使用个人主页 `/user/profile/` 后的 ID，不是公开小红书号。

```python
page = await api.get_user_notes(user_id, limit=20)
if page.has_more:
    next_page = await api.get_user_notes(user_id, cursor=page.cursor, limit=20)

async for note in api.iter_user_notes(user_id, pages=2, limit=20):
    print(note.note_id, note.title, note.time)
    detail = await api.get_note(note.ref)
```

`UserNote` 复用帖子卡片字段，提供标题、作者、封面、类型、互动计数和毫秒级发布时间。`note.ref` 使用该作品自己的令牌，便于继续读取详情；列表不保证提供完整正文。

两个方法均可传入 `xsec_token`、`xsec_source`。这里的令牌来自作者主页，不能与作品的详情令牌混用。分页迭代默认读取一页，可通过 `pages` 限定最多读取的页数；按帖子 ID 去重，在末页、空页或游标循环时停止。置顶作品可能打乱时间顺序，不应只用首条作品或时间戳判断是否有新内容。

已用测试账号验证分页读取和作品详情衔接。该接口不自动启动轮询，也不产生新帖事件。

## 推荐、笔记和评论

这组接口可供 agent 的工具函数调用，返回类型化模型；本模块不包含 agent 调度或自动决策逻辑。
推荐项通过 `FeedItem.ref` 保留笔记 ID、访问令牌及来源，供后续详情和评论请求复用：

```python
async for item in api.iter_recommendations(pages=2, limit=5):
    note = await api.get_note(item.ref)
    # note.card.title / desc / user / image_list / video / interact_info
    comments = await api.get_comments(item.ref)
    if comments.comments:
        replies = await api.get_comments(item.ref, root_id=comments.comments[0].id)
```

也可以使用 `NoteRef.from_url(url)` 解析带 `xsec_token` 的正式笔记链接，支持 `/explore/`、`/discovery/item/` 路径和 `xhsdiscover://item/` 分享链接。
不自动跟随短链接，也不从没有令牌的笔记 ID 猜测访问参数。
推荐和评论遍历分别通过 `pages` 限制最大请求页数，过滤重复项；需要继续浏览时，可通过单页接口获取并传递游标。
一级评论中的 `sub_comments` 为内嵌回复预览，全部回复应通过 `root_id` 单独翻页。
`iter_comments` 支持 `root_id`，并在服务端宣称还有数据但游标不推进时抛出异常。

点赞、收藏及评论写入供调用方显式执行，不会在读取推荐或详情时触发：

```python
await api.set_note_like(note.ref.note_id, True)
await api.set_note_collect(note.ref.note_id, True)

await api.set_note_like(note.ref.note_id, False)
await api.set_note_collect(note.ref.note_id, False)

created = await api.post_comment(note.ref.note_id, "评论内容")
reply = await api.post_comment(note.ref.note_id, "回复内容", reply_to=comment.id)
await api.delete_comment(note.ref.note_id, created.id)
```

两个状态设置接口返回 `NoteAction`，表示服务端接受的操作；没有自动重试，也不根据本地旧状态切换。
推荐摘要中的收藏状态可能缺失，此时 `collected` 为 `None`；需要当前状态时应读取详情，不能把未知状态视为未收藏。
图片、视频等媒体字段保留访问地址及数据，不自动下载、播放或识别媒体内容。

2026-10-22，纯 Python 签名实测读取两页推荐共 6 篇笔记，并完成详情、一级评论及楼中楼读取。
该次调用未附加 `x-rap-param`，结果成功；不代表所有接口、账号或未来协议都无需 RAP。
2026-10-22，在用户指定的测试笔记上，点赞、取消点赞、收藏和取消收藏均通过重新读取详情核实，原状态已恢复。
普通评论发布后可从评论列表读回，真实响应结构与模型一致；测试评论已删除。
回复自己刚创建的评论返回业务错误码 `-9043`，原因尚未确定；回复他人评论的成功场景仍待验证。

只读复现验证：

```bash
uv run --extra xhs python src/myfish/tests/live_xhs_notes_check.py \
  --state data/adapters/xhs/auth.state.json
```

## 创作者平台签名

`CreatorAPI` 复用 PC 登录凭据，通过创作者发布页导航获取平台 Cookie，并维护独立的 Cookie、设备快照和请求计数，不修改 PC 客户端状态。

```python
from myfish.adapters.xhs.creator import CreatorAPI

async with CreatorAPI(api) as creator:
    await creator.initialize()
    account = await creator.get_self_info()
```

`api` 为已登录的 `XhsAPI`。返回的 `CreatorAccount` 提供 `user_id`、`nickname` 和 `avatar`，不暴露账号响应中的手机号等额外信息。

当前支持 Creator 4.3.6 的 `mns0101`、`mns0201`、`X-s` 和 `X-S-Common`。DS 从服务器获取，以 Python 解析已识别的指令结构和初始参数，再直接计算摘要；不执行服务器 JavaScript，不引入解释器或 Node.js。DS 缓存五分钟，过期后在签名前刷新，未知指令版本会终止请求。

2026-10-23，两个独立客户端使用已有 PC 登录态完成发布页导航、DS 初始化及 `/api/galaxy/user/info` 读取，均返回 HTTP 200、`success=true`、`code=0`。签名测试包含 128 组 DS 输入、12 组变更初始值的 DS 输入，以及 19 组完整请求头对照，覆盖两种签名档位、中文正文、随机种子边界和发布请求的 `envConst=1351`。

可使用已有状态文件执行只读验证：

```bash
uv run --extra xhs python -m myfish.tests.live_xhs_creator_check
```

### Creator RAP

`rap.sign_rap(url, body, fingerprint, context)` 生成 `x-rap-param`。`url` 必须是实际请求的完整 HTTPS URL，`body` 为实际发送的 UTF-8 字节；两者共同参与请求摘要。`RapContext(timestamp_ms)` 生成本次请求的随机材料，也可显式传入固定值复现结果。

指纹参数为 Creator 的二进制字段序列。编码器检查字段边界、重复字段及必需字段，并更新本次请求的随机标识和请求摘要；其他字段保持输入值。

`rap.creator_fingerprint` 构造无交互状态的 Creator 指纹，`CreatorAPI.get_rap_fingerprint()` 从现有设备快照和创作者会话生成该输入。发布时默认使用生成结果，也可通过 `rap_fingerprint` 显式覆盖。

| 字段 | 默认来源 |
| --- | --- |
| `0x3EA`、`0x3EB` | 签名时写入新的随机标识及完整 URL、正文摘要 |
| `0x444`、`0x446` | 现有设备档案 `x10` 的宽高，作为页面尺寸近似值 |
| `0x447` | 当前 Creator 会话初始化时间 |
| `0x433`—`0x436`、`0x43B`、`0x43C` | 坐标、交互时间及记录清零，表示未采集鼠标和键盘输入 |
| 其余字段 | 保留已观察到的 Creator 默认标志及特征位 |

尺寸、初始化时间和交互字段的语义来自捕获样本布局推断，尚未通过原始采集器逐项核实。无浏览器时无法直接获取页面视口，因此尺寸使用设备可用区域近似；未知特征位仍使用固定值。不复制捕获样本中的历史输入、轨迹或时间戳，也不生成模拟的人类交互。该实现不等同于真实浏览器采集结果，不能保证减少风控或通过服务端检查。

RAP 使用自定义 S-box 的分组密码变体、随机 ASCII 填充和 xxHash32 校验，压缩使用标准库 zlib。该压缩路径与参考项目支持的 zlib 回退分支完成完整字节对照，不承诺与其另一套压缩器生成相同的压缩字节。

51 组摘要、64 个加密分组和 12 组完整 RAP 封装已通过离线参考对照。另有 3 组生成指纹的字段布局及完整封装对照，覆盖不同尺寸与会话时间。RAP 与 MNS 使用同一份正文，并绑定实际目标 URL。离线对照验证编码和加密一致性，不代表服务端接受无交互指纹。

### 上传与发布

`CreatorAPI` 提供上传许可、图片上传、视频上传、转码状态查询及发布接口。请求签名和上传授权均由 Python 计算，无需 Node.js。模拟响应测试及真实图片上传已通过，视频上传和发布尚待验收。

真实验收进度：图片上传已通过，发布返回 HTTP 461、`verifytype=201`，尚未取得发布成功回执；同期 PC 和 Creator 账号查询正常。`201` 的验证初始化已返回成功，服务端没有说明触发该校验的具体原因。

在保持图文正文、已上传资源及私密范围相同的后续对照中，改用参考项目原始 436 字节 Creator RAP 指纹仍返回 HTTP 461、`verifytype=201`。该结果不能将拦截单独归因于本项目的无交互 RAP 指纹；测试存在会话时间差及既有验证状态，尚不能排除其他设备字段或历史风险状态的影响。

使用参考项目固定版本的 PC 登录态衔接、Creator 初始化、设备模板、JavaScript 签名及 `curl_cffi` 客户端进行对照，复用同一已上传图片，发布正文逐字节相同，仍返回 HTTP 461、`verifytype=201`。参考项目会因响应正文为 `success=true` 而返回成功；调用方必须同时检查 HTTP 状态与验证头，不能将该返回值作为发布成功回执。对照环境与本项目运行依赖隔离，未引入生产依赖。

### 发布验证

HTTP 461/471 的验证异常保留 `verify_type` 和 `verify_uuid`，`verification_method` 区分已识别的短信、扫码和未知验证方式。日志不输出验证标识或登录凭据。

| `verifytype` | 验证方式 | 当前支持 |
| --- | --- | --- |
| `124` | 扫码验证 | 终端二维码及状态查询 |
| `201` | 短信验证（`smsCaptchaLite`） | 实验性 Creator 接口：初始化、发送和校验；尚待完整发布验证 |

官方 [验证码 SDK](https://fe-static.xhscdn.com/formula-static/redcaptcha/captcha/eeddec12375cb942e53c6cf32e8a2258.js) 将 `201` 交给短信组件，流程为 `/api/redcaptcha/v2/vc/init` 初始化、`/vc/send` 发送验证码、`/vc/check` 校验。网页组件初始化成功后会自动请求发送验证码；单独调用初始化接口不会执行这段前端逻辑。短信验证使用绑定手机号，不会产生 App 确认通知。

#### 实验性短信接口

短信接口依据网页抓包实现，尚未完成真实验证码校验与发布的完整流程验证，接口可能随协议验证结果调整。终端登录命令暂未集成该流程。

`CreatorAPI.init_sms_verification(challenge)` 返回接收号码信息；`send_sms_code(challenge)` 显式发送短信，返回 `rid` 和服务端 `expireSec`；`check_sms_code(challenge, rid=..., code=..., check_count=0)` 校验验证码。三个操作复用同一 Creator 客户端，保留服务端 Cookie。验证码使用字符串，以保留前导零；`check_count` 由调用方按实际提交次数维护。发送和校验均不自动重试，校验通过后也不会自动重新发布。

短信接口必须同时满足 HTTP 200、`success=true`、`code=0` 才视为成功。已观察到以下失败响应仍携带 `success=true`，调用方可通过 `XhsAPIError.code` 判断：

| `code` | 含义 | 处理方式 |
| --- | --- | --- |
| `40002` | 验证码已失效 | 停止提交该验证码 |
| `40003` | 验证码输入错误 | 检查输入，手动决定是否再次提交 |
| `40012` | 已超过当天短信发送限制 | 停止发送；不能将响应中的 `expireSec=0` 视为允许立即重发 |

发送、校验的请求结构及上述失败码已根据网页抓包覆盖离线测试。校验成功后的发布请求、验证有效范围及持续时间尚未完成真实验证；目前不能认定每次发布都需要短信，也不能保证一次验证可以持续复用。

当前不会自动发送短信、替换成扫码验证或重发帖子。验证码类型说明所需的验证方式，不能据此推断触发原因，或认定 RAP 指纹已通过校验。

### 媒体上传与发布示例

图片支持 JPEG、PNG 和 WebP，输入为 `pathlib.Path` 或原始 `bytes`。客户端在申请许可前检查图片格式、尺寸及完整性；文件通过独立 HTTP 客户端分块上传，不携带账号 Cookie。

```python
from pathlib import Path

from myfish.adapters.xhs.creator import CreatorAPI
from myfish.adapters.xhs.publish import ImagePost, Visibility

async with CreatorAPI(api) as creator:
    await creator.initialize()
    image = await creator.upload_image(Path("photo.jpg"))
    result = await creator.publish(
        ImagePost(
            title="标题",
            description="正文",
            images=[image],
            visibility=Visibility.PRIVATE,
        ),
    )
```

发布默认生成无交互 Creator 指纹，无需手动传入指纹模板。需要使用自行提供的指纹时，传入 `rap_fingerprint=...`，其类型必须为 Creator 二进制字段序列，不能使用 PC 指纹 JSON。当前自动生成路径尚待真实私密发帖验收。

`ImagePost` 和 `VideoPost` 默认仅自己可见，可显式设置 `Visibility.PUBLIC`。`scheduled_at_ms` 接受未来的毫秒时间戳；不传则立即发布。当前不支持位置、话题、提及用户或互关可见范围。

视频通过 `upload_video` 上传，再与已上传的封面及实际媒体元数据组合：

```python
from myfish.adapters.xhs.publish import VideoMetadata, VideoPost, VideoTrack

video = await creator.upload_video(Path("clip.mp4"))
cover = await creator.upload_image(Path("cover.jpg"))
post = VideoPost(
    title="视频标题",
    video=video,
    cover=cover,
    metadata=VideoMetadata(
        video=VideoTrack(
            width=1920,
            height=1080,
            duration_ms=3000,
            frame_rate=30,
            format="AVC",
        ),
    ),
)
result = await creator.publish(post)
```

示例中的媒体参数应替换为实际值；含音轨时通过 `VideoMetadata.audio` 提供 `AudioTrack`。客户端不自动提取封面、探测视频编码或引入 FFmpeg。视频发布前默认等待转码就绪，最长等待 120 秒，可通过 `transcode_timeout` 调整；已确认就绪的调用方可设置 `wait_for_video=False`。

`PublishResult.accepted` 表示服务端接受请求，不表示内容已完成审核或公开可见；`note_id` 在响应缺少作品 ID 时为 `None`。业务拒绝抛出 `XhsAPIError`。发布期间发生网络错误时抛出 `PublishUncertainError`，提示先检查作品列表；客户端不会自动重发，避免重复发布。

## 实现与测试

密码原语统一使用框架基础依赖 `cryptography`；小红书可选依赖仅包括 `psutil`（系统内存）和 `tzlocal`（跨平台时区），不依赖 `xhshow` 或 `pycryptodome`。RC4 和 DES 通过 `cryptography` 的 `decrepit` 模块兼容既有协议，签名输出保持不变。

| 模块 | 职责 |
| --- | --- |
| `api.py` | 类型化 HTTP 接口、分页、Cookie 范围及状态恢复 |
| `auth.py` | 安全参数初始化、指纹上报及扫码登录流程 |
| `creator.py` | 创作者平台会话、媒体上传、转码查询及发布 |
| `publish.py` | 媒体、发布参数及请求体构建 |
| `ds.py` | Creator DS 参数解析及摘要计算 |
| `rap.py` | RAP 请求绑定、指纹编码及加密封装 |
| `device.py` | 环境探测、设备档案及指纹字段生成 |
| `sign.py` | MNS、XYS、XYW、SSK 绑定及指纹加密 |
| `model.py` | 账号、会话、消息、回执及持久化数据结构 |
| `message.py` | 私信 Protobuf 编解码 |
| `stream.py` | 推送鉴权、心跳、重连及事件去重 |
| `notes.py` | 推荐、笔记、评论及互动的数据结构 |
| `utils.py` | 编码、设备标识、时间及私有文件写入 |


签名函数不读取 Cookie 文件、不发送请求，也不在内部生成时间和随机输入。会话对象负责提供这些输入，便于复现结果与切换协议版本。

旧版 `DeviceProfile` 支持配置屏幕、内存、CPU、时区和图形信息，相同设备标识及配置会生成稳定字段。其中 Canvas/WebGL 标识是为协议验证生成的合成值，不是浏览器渲染结果。新版 `x84` 根据加载时间生成，`x83` 尚未复现，可传入参考值或留空进行实验。

迁移采用固定输入对照后再替换运行实现的顺序，依次完成编码、MNS、签名封装、会话状态和旧版设备档案。`tests/fixtures/xhs_signing.json` 保存合成输入的参考结果，不包含真实账号凭证。24 组 MNS 明文及签名对照覆盖新旧环境参数、随机种子边界和中文请求体。

迁移后已使用环境生成的设备档案完成匿名激活、密钥协商、新版指纹上报、二维码创建、手机确认和正式账号校验。保存的凭证另起客户端通过旧版签名读取账号信息与会话列表。

`mns0201` 使用协议约定的 XXTEA 变体，24 组固定向量与参考实现逐字节一致。`mns0101` 使用独立密钥流和自定义 Base58 编码，Creator 请求使用 `ugc` 应用标识及专用 DS 摘要。RAP 算法保持独立，不读取凭据、不发送请求。

[Spider_XHS PR #192](https://github.com/cv-cat/Spider_XHS/pull/192) 还更新了 Chrome 155 的设备参考样本。该样本包含特定浏览器的页面状态和采集结果，本项目暂保留现有已验证档案，未直接复制其 `x83`、遥测计数或渲染摘要。

协议参考：

- [Spider_XHS](https://github.com/cv-cat/Spider_XHS)：登录顺序、新版签名字段、会话密钥协商、私信 Protobuf 字段布局及 RWP 传输协议。
- [ReaJason/xhs](https://github.com/ReaJason/xhs/blob/master/xhs/core.py)：点赞、收藏及取消操作的请求字段。
- [RedCrack](https://github.com/Cialle/RedCrack)：旧版 `websectiga` 数据结构。
- [xhshow](https://github.com/Cloxl/xhshow)：旧版签名与指纹协议参考。

未知脚本格式会终止初始化，不执行返回的 JavaScript。

```bash
uv run --extra xhs python -m unittest discover -s src/myfish/tests -p 'test_xhs*.py' -v
```

自动测试覆盖请求编码、错误处理、登录状态、凭证保存、指纹复用、零填充及签名的请求和密钥绑定。测试使用模拟响应，不代替真实接口验证。
