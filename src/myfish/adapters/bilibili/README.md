# Bilibili 适配器

基于 B站网页接口接收私信、账号通知及直播事件，提供文本发送、内容搜索、视频和用户互动、字幕及音视频流查询。实现沿用 Bilink 的平台协议经验，使用 Myfish 的适配器、消息和插件契约。

扫码登录、私信接收、“回复我的”通知、点赞增量及 @ 通知已由用户完成真实账号验收。Cookie 轮换恢复、极端分页边界、陌生人会话覆盖范围和平台限流行为仍需分别验证；评论读取与写入接口目前完成离线测试，尚待真实验收。

## 配置与启动

在项目 `config.toml` 中添加：

```toml
[[adapters]]
id = "bilibili"
[adapters.config]
poll_interval = 20
active_interval = 3
active_duration = 60
reconcile_interval = 300
timeout = 20
notifications = true
live_rooms = []
```

```bash
uv run myfish sync
uv run myfish run
```

首次启动在终端显示登录二维码，使用 B站 App 扫码并确认。已有凭据时先检查账号；只有接口明确返回未登录状态才进入扫码流程。网络错误不会删除已保存的凭据。运行期间登录失效会终止适配器，需要重新启动扫码。

二维码同时保存到 `data/adapters/bilibili/login.png`，登录成功、超时或取消后删除。

扫码返回的刷新令牌随 Cookie 保存。客户端每天检查一次刷新需求，使用 `cryptography` 的 RSA-OAEP/SHA-256 生成刷新路径，解析 HTML 中的刷新口令，不执行 JavaScript。新 Cookie 通过账号校验并原子保存后才确认更新；确认失败时保存待确认令牌，重启后先恢复确认。刷新异常保留本地凭据，延后一小时检查。旧凭据文件缺少刷新令牌时仍可使用，但需要重新扫码才能获得刷新能力。该流程尚待真实账号验收，不能恢复被强制注销或已失效的账号会话。

也可以在 `[adapters.config]` 中通过 `cookies = {SESSDATA = "...", bili_jct = "...", DedeUserID = "..."}` 显式提供登录凭据，该配置优先于已保存凭据。不要提交包含凭据的配置。

本适配器使用 Myfish 已有基础依赖，不需要额外安装 Bilink、Node.js、OpenAI SDK 或 Cron 库。

## 数据目录

所有状态固定保存在 `data/adapters/bilibili/`；修改 Myfish 的 `data_dir` 时，目录随之移动。

| 文件                                               | 用途                                                  |
| -------------------------------------------------- | ----------------------------------------------------- |
| `auth.json`                                      | 登录 Cookie、设备标识、刷新及待确认令牌，原子替换写入 |
| `messages.sqlite3`                               | 按账号隔离的会话游标、同步进度及待分发消息            |
| `messages.sqlite3-wal`、`messages.sqlite3-shm` | SQLite 运行时文件                                     |

目录包含账号凭据和私信内容，备份时应保留其私密性。删除数据库会重建接收基线；删除登录文件不会清除数据库。

## 消息契约

适配器 ID 为 `bilibili`，通过 `setup/run/send` 接入 Myfish。私信转换成标准 `MessageEvent`：

| 字段            | 内容                         |
| --------------- | ---------------------------- |
| `cid`         | `private:<聊天对象 UID>`   |
| `sender_id`   | 发送者 UID 字符串            |
| `sender_name` | 当前使用 UID，不额外请求昵称 |
| `messages`    | 标准消息链                   |
| `raw_payload` | 平台原始消息字段             |

接收支持文本 `Text`、图片 `Image` 和 `bilibili.message.VideoCard`。未知类型或内容解析失败时转换成 `RawMessage`，保留原始内容；调试日志只记录消息类型及字段名。撤回状态消息和自己发送的消息不触发接收事件。

发送目前仅支持文本，可将多个 `Text` 合并为一条私信。不支持的消息段会抛出 `ValueError`；网络和业务失败向调用方抛出异常，不自动重发。`send` 返回 `SendReceipt`，包含服务端 `msg_key`。

```python
from myfish.adapters.bilibili import BilibiliBot
from myfish.core.event import MessageEvent


async def handle(bot: BilibiliBot, event: MessageEvent) -> None:
    await event.reply("收到")
```

将处理函数注册到自己的 Myfish 插件后生效。通用消息插件也可直接接收 B站消息。

## 增量接收

- 首次启动分页建立会话基线，跳过已有历史；正常重启从持久化游标继续。
- 使用 `new_sessions` 发现变化，通过消息序列分页补齐，不依赖未读数或发送时间戳去重。
- 发现接口截断时改用会话列表分页，查询边界保留重叠；分页停滞、空页却声称存在下一页、消息未达到会话声明的序列时终止本次同步。
- 消息和会话游标在同一 SQLite 事务中保存；完整处理发现结果后才推进会话发现时间。失败时保留原进度。
- 定期扫描会话进行校准；收到消息或发送回复后使用活跃间隔，空闲时恢复普通间隔。正常轮询按每轮开始时间计算间隔，扣除处理耗时后再等待；处理超时则直接开始下一轮，不并发补跑。请求异常时指数退避，并附加随机抖动。
- 分发前持久化，回调正常返回后移除收件箱记录。Myfish 回调负责调度插件，回调返回不代表所有插件已执行完毕；不承诺插件操作严格执行一次。

目前私信仅处理用户会话，不接收粉丝团会话。没有接入 WebSocket 或长轮询。重叠查询和定期校准不能恢复平台已删除或不再提供的消息。

## 账号通知

账号通知默认启用，与私信共用 `poll_interval`、`active_interval` 和 `active_duration`，每轮查询评论、回复、点赞和 @ 通知，不需要单独配置通知间隔。设置 `notifications=false` 可整体禁用账号通知。平台的回复、点赞和 @ 通知来自不同接口，内部各自维护分页和进度；单个接口失败不阻止其他通知接口同步，未登录错误除外。

通知使用独立的分页游标、去重记录和持久化收件箱。首次启用时记录已有历史，不把旧记录作为新事件分发；后续查询保留一分钟重叠窗口，分页失败不推进进度。初始化完成后打印“适配器通知事件监听已就绪，正在监听事件..”。去重不依赖网页或 App 的未读计数，也不主动调用标记已读接口。早于重叠窗口才延迟出现的通知可能无法被发现。

| 事件类                   | 事件名               | 来源                                                                |
| ------------------------ | -------------------- | ------------------------------------------------------------------- |
| `BilibiliCommentEvent` | `bilibili.comment` | 视频新增评论，回复通知接口的 `item.type=video`、`business_id=1` |
| `BilibiliReplyEvent`   | `bilibili.reply`   | 评论回复，回复通知接口的 `item.type=reply`                        |
| `BilibiliMentionEvent` | `bilibili.mention` | 收到 @ 通知，包含发送者、提及内容和关联对象                         |
| `BilibiliLikedEvent`   | `bilibili.liked`   | 收到点赞，包含点赞者名单及被赞对象                                  |

评论和回复事件包含账号、通知 ID、毫秒时间戳、回复者 `user`、评论上下文 `target` 及 `raw_payload`。`target` 保留主体、根评论、源评论和被回复评论的标识及文本。通知业务类型不等同于评论 API 的 `type`，调用评论接口前应明确目标类型。

```python
from myfish.adapters.bilibili.events import BilibiliCommentEvent, BilibiliReplyEvent


@plugin.on_event(BilibiliCommentEvent)
@plugin.on_event(BilibiliReplyEvent)
async def on_comment(event: BilibiliCommentEvent) -> None:
    logger.info(
        "收到回复: user={}, subject={}", event.user.mid, event.target.subject_id
    )
```

示例中的 `plugin`、`logger` 复用插件实例及 Myfish 日志。账号通知继承 `BaseEvent`，不会触发通用私信 echo，也不会通过私信回复评论者。未知结构保留为 `BilibiliNotificationEvent`，调试日志只记录字段名。

点赞事件的 `users` 提供本地本次新识别到的点赞者 `mid`、`nickname`、`avatar`；新增名单只有一人时可通过 `event.user` 取得该用户。`reported_users` 保留本次接口返回的用户名单，`target` 保留被赞对象的 `type`、`item_id`、`pid` 等字段。

```python
from myfish.adapters.bilibili.events import BilibiliLikedEvent


@plugin.on_event(BilibiliLikedEvent)
async def on_liked(event: BilibiliLikedEvent) -> None:
    for user in event.users:
        logger.info(
            "收到点赞: user={}({}), target={}",
            user.nickname,
            user.mid,
            event.target.item_id,
        )
```

平台可能合并多人点赞，`count` 是历史区 `total` 的累计计数，`new_count` 只与此前相同口径的本地累计快照比较；缺少可信旧快照时为 `None`，不会把历史总量作为新增量。`latest` 的近期计数不与历史累计计数混用，仅用于补充点赞者信息。接口历史计数尚未更新时，这些信息会暂存，等待累计快照更新后再处理。

`users` 优先使用最新区提供且此前未识别到的用户。历史区只给出不完整名单时，不能仅凭“本地第一次见到这个用户”就认定对方刚刚点赞；无法确定的用户不放进增量名单。未知的历史累计记录先用于校准，旧版本未区分计数来源的缓存也会自动校准。增量计数不能保证还原合并期间每一次点赞、取消点赞的操作，平台用户名单也不保证完整。

相同通知 ID 的快照、已识别用户和待分发增量一同持久化。重复快照、名单排序、仅时间变化、已读标记变化或仅计数减少不会触发新的点赞事件；最新区与历史区的同一条通知合并后比较。计数增加但接口没有返回新的用户名单时，事件 `users` 为空、`user` 为 `None`，不会根据旧名单猜测本次点赞者。

首次启用时会跳过已有记录。测试时请先等待事件监听就绪日志，再通过其他账号新增评论、回复、点赞或 @ 提及。轮询与私信使用相同间隔，例如 `poll_interval=5`、`active_interval=3`；间隔指相邻两轮的开始时间，事件接收延迟还受接口响应和平台通知生成时间影响；单轮处理超过配置间隔时，查询周期也会相应延长。普通框架启动使用配置中的开关，验收命令的 `--notifications` 不会写回配置。

@ 通知使用 `BilibiliMentionEvent`，`user` 为发送者，`target.source_content` 为接口提供的提及文本，`target.title` 为关联内容标题，`target.uri` 为跳转链接。`target.type`、`business_id` 及各 ID 保留平台提供的关联对象信息；部分场景可能不提供文本或完整 ID，可通过 `raw_payload` 查看原始字段。通知来源可以是评论、动态等，不固定为视频评论。

```python
from myfish.adapters.bilibili.events import BilibiliMentionEvent


@plugin.on_event(BilibiliMentionEvent)
async def on_mention(event: BilibiliMentionEvent) -> None:
    logger.info(
        "收到艾特: user={}({}), content={}, url={}",
        event.user.nickname,
        event.user.mid,
        event.target.source_content,
        event.target.uri,
    )
```

可通过 `bot.api.get_mention_notifications(cursor_id=0, at_time=0)` 直接读取 @ 通知分页。此接口不主动标记已读，适配器启动后的首次查询只记录历史，不分发已有通知。分页、持久化、去重和插件分发已通过离线测试，真实接口读取、事件解析及新增 @ 通知接收均已验证。

这些事件来自账号通知列表，不代表订阅了评论区的全部新增评论。系统通知尚未接入；直播事件通过独立的 WebSocket 连接接收，见下文。

## 评论接口（实验性）

评论区使用 `CommentRef(oid=..., kind=...)` 标识，`oid` 是平台的评论主体 ID，`kind` 是评论区类型；视频评论使用 avid 和类型 `1`，不能直接传 BV 号。`get_video_info` 返回的 `aid` 可用于取得视频 avid。动态评论主体和类型应按实际接口数据确定，不能直接使用通知的 `business_id` 作为评论区类型。

| 方法                                                | 用途                                          |
| --------------------------------------------------- | --------------------------------------------- |
| `get_comments(ref, offset="", sort="time")`       | WBI 评论列表，支持 `time` 和 `hot` 排序   |
| `iter_comments(ref, sort="time")`                 | 按平台游标逐页读取主评论并按 rpid 去重        |
| `get_comment_replies(ref, root, page=1, size=20)` | 指定根评论的楼中楼回复                        |
| `send_comment(ref, text, root=0, parent=0)`       | 发表评论或指定根评论、父评论的回复            |
| `reply_comment(comment, text)`                    | 根据已有评论对象确定主体及 root/parent 并回复 |
| `like_comment(ref, rpid, liked=True)`             | 点赞评论；`liked=False` 取消点赞            |

```python
from myfish.adapters.bilibili.model import CommentRef

api = bot.adapter.api
ref = CommentRef(oid=123456, kind=1)  # 替换为目标视频 avid
async for comment in api.iter_comments(ref):
    logger.info("评论: rpid={}, user={}", comment.rpid, comment.member.name)
```

`CommentPage` 保留主评论与热评，`next_offset` 是平台返回的不透明分页字符串，下一次请求应原样传入。`iter_comments` 只遍历主评论，楼中楼通过独立接口查询；分页不前进时抛出协议异常，不无限重试。

发送接口返回 `CommentReceipt`，包含 `rpid`、`root`、`parent`。回复根评论时 parent 指向该根评论；回复楼中楼时 root 保持根评论、parent 指向实际被回复评论。所有写操作由调用方显式发起，不随通知自动执行。

评论接口失败通过 `BilibiliAPIError.code` 传递，并对关闭、限流、验证码、内容拒绝和重复评论记录警告。即使外层 `code=0`，响应要求验证码时也不视为发送成功。网络超时会直接向上传递，调用方应先核实是否已发表再决定重试，客户端不会自动重发。

当前仅支持纯文本评论写入，不包含评论图片上传、删除、置顶、验证码处理或评论区事件订阅。

## 平台 API

插件通过 `bot.adapter.api` 访问 `BilibiliAPI`：

| 方法                                                       | 用途                                     |
| ---------------------------------------------------------- | ---------------------------------------- |
| `get_self_info()`                                        | 当前账号                                 |
| `get_reply_notifications(cursor_id=0, reply_time=0)`     | “回复我的”通知分页                     |
| `get_mention_notifications(cursor_id=0, at_time=0)`      | @ 通知分页                              |
| `get_like_notifications(cursor_id=0, like_time=0)`       | 收到点赞的最新记录与历史分页             |
| `get_user_info(mid)`、`get_fans(mid, page=1, size=50)` | 用户与粉丝列表                           |
| `get_favorite_folders(mid)`                              | 收藏夹列表                               |
| `get_favorite_videos(folder_id, page=1, size=20)`        | 收藏夹视频分页                           |
| `get_up_videos(mid, page=1, size=30)`                    | 使用 WBI 签名的投稿列表                  |
| `get_video_info(bvid)`                                   | 视频信息和分 P 标识                      |
| `get_play_urls(bvid, cid, quality=64, stream_format="mp4")` | MP4 或 DASH 音视频流地址及下载请求头 |
| `get_subtitles(bvid, cid)` | 字幕轨列表及登录要求 |
| `get_subtitle(track)` | 获取字幕正文和时间片段 |
| `search(keyword, kind="video", page=1, order=None, ...)` | 分类搜索、排序和筛选 |
| `like_video(bvid, liked=True)` | 视频点赞；`liked=False` 取消点赞 |
| `get_video_like_status(bvid)` | 查询视频近期点赞状态 |
| `favorite_video(aid, folder_ids, favorited=True)` | 加入指定收藏夹；`favorited=False` 从指定收藏夹移除 |
| `get_video_favorite_status(aid)` | 查询视频是否已收藏 |
| `follow_user(mid, followed=True)` | 关注 UP 主；`followed=False` 取消关注 |
| `get_user_relation(mid)` | 查询当前账号与目标用户的关系 |
| `get_live_room(room_id)` | 解析真实房间 ID、主播 UID 和直播状态 |
| `get_live_connection(room_id)` | 获取直播 WebSocket 节点及鉴权令牌，使用真实房间 ID |
| `send_live_danmaku(room_id, text, color=0xFFFFFF, mode=1)` | 向指定直播间发送文本弹幕 |

API 返回类型化模型；非成功业务码抛出 `BilibiliAPIError`，响应结构不符合约定时抛出 `BilibiliProtocolError`。WBI 签名使用 Python 计算，密钥缓存一小时，不修改调用方参数。

收藏夹备份、投稿订阅和视频下载属于插件业务，本适配器不会自动执行这些任务。播放地址不保证请求画质一定可用，取决于接口返回及账号权限。

## 搜索

`search` 使用 WBI 分类搜索接口。`keyword` 为关键词，`kind` 为分类，`page` 从 1 开始；每页数量及可访问的总页数由平台决定。`order=None` 使用平台默认排序。

| `kind` | 分类 | 可显式指定的 `order` |
| --- | --- | --- |
| `video` | 视频 | `totalrank` 综合、`click` 播放、`pubdate` 发布时间、`dm` 弹幕、`stow` 收藏、`scores` 评论 |
| `bili_user` | 用户 | `0` 默认、`fans` 粉丝数、`level` 等级 |
| `article` | 专栏 | 视频分类的排序项，以及 `attention` 喜欢数 |
| `live`、`live_room` | 直播综合、直播间 | `online` 人气、`live_time` 开播时间 |
| `live_user` | 主播 | 平台默认 |
| `media_bangumi`、`media_ft` | 番剧、影视 | 平台默认 |
| `photo` | 相簿，历史分类 | 与视频分类相同 |
| `topic` | 话题，历史分类 | 平台默认 |

历史分类保留文档所列协议，当前可用性未验收；平台停用或过滤时会返回业务错误。

| 筛选参数 | 用途 |
| --- | --- |
| `duration` | 视频时长：`0` 全部、`1` 10 分钟以下、`2` 10～30 分钟、`3` 30～60 分钟、`4` 60 分钟以上 |
| `tids` | 视频分区 ID，`0` 为全部 |
| `user_type` | 用户分类：`0` 全部、`1` UP 主、`2` 普通用户、`3` 认证用户 |
| `order_sort` | 用户按粉丝数或等级排序时：`0` 降序、`1` 升序 |
| `category_id` | 专栏或相簿分区 ID，`0` 为全部 |

不适用于当前分类的非默认筛选值、无效分类及无效排序会在请求前抛出 `ValueError`。

普通分类返回 `SearchPage`，包含 `items`、`page`、`page_size`、`total` 和 `pages`。`items` 使用 `SearchVideo`、`SearchUser` 等类型化模型，保留额外平台字段，标题和用户名移除关键词高亮标签。视频搜索可能混入 `ketang` 课堂条目，使用 `SearchCourse` 单独表示；其 ID 不作为视频 AV 号使用。`kind="live"` 返回 `LiveSearchPage`，通过 `items.live_room` 和 `items.live_user` 分别获取直播间与主播。

```python
from myfish.adapters.bilibili.model import SearchPage, SearchVideo

result = await bot.adapter.api.search(
    "Python", kind="video", order="pubdate", duration=1, page=1
)
if isinstance(result, SearchPage):
    for item in result.items:
        if isinstance(item, SearchVideo):
            logger.info("搜索结果: bvid={}, title={}", item.bvid, item.title)
```

搜索和视频点赞缺少 `buvid3` 时，客户端先从平台接口获取设备 Cookie，并在客户端生命周期内复用。设备 Cookie 不替代登录凭据，也不保证请求不会被平台限制。

## 直播间

在 `[adapters.config]` 中设置 `live_rooms = [12345]` 订阅房间，将示例 ID 替换为需要监听的房间 ID。默认空列表不连接直播间；配置中的相同 ID 自动去重，短号会先解析为真实房间 ID。

每个房间使用独立 WebSocket 连接，直播接收不受私信轮询间隔影响。连接使用当前账号获取 WBI 鉴权令牌和设备 Cookie，协商协议 2，通过标准库 `zlib` 解压批量消息。客户端每 30 秒发送应用层心跳，60 秒未收到心跳回复时重新连接；网络异常按 1～60 秒退避，重连时重新获取令牌并轮换节点。框架退出时取消所有直播任务并关闭连接。

| 事件类 | 事件名 | 内容 |
| --- | --- | --- |
| `BilibiliDanmakuEvent` | `bilibili.live.danmaku` | `danmaku` 提供 `uid`、`uname`、`text` 和平台时间戳 |
| `BilibiliGiftEvent` | `bilibili.live.gift` | `gift` 提供赠送者、礼物 ID、名称、数量及平台币种和计数 |
| `BilibiliLiveInteractionEvent` | `bilibili.live.interaction` | `interaction` 提供用户信息和 `kind`：`enter`、`follow`、`share` 或 `unknown` |
| `BilibiliLiveStatusEvent` | `bilibili.live.status` | `live=True` 开播，`live=False` 主播准备中或下播 |

事件均继承 `BilibiliLiveEvent`，包含 `account_id`、真实 `room_id`、`command`、`timestamp_ms` 和 `raw_payload`。旧版 `INTERACT_WORD` 和新版 Protobuf `INTERACT_WORD_V2` 均转换为互动事件。平台隐藏用户身份时，保留 `uid=0` 及平台提供的昵称；不猜测真实用户。

```python
from myfish.adapters.bilibili.events import BilibiliDanmakuEvent


@plugin.on_event(BilibiliDanmakuEvent)
async def on_danmaku(event: BilibiliDanmakuEvent) -> None:
    logger.info(
        "直播弹幕: room={}, user={}, text={}",
        event.room_id,
        event.danmaku.uname,
        event.danmaku.text,
    )
```

直播事件属于平台业务事件。发送弹幕通过 `bot.adapter.api.send_live_danmaku(room_id, text)` 显式调用，使用 HTTP 接口；返回 `LiveSendReceipt`，保留服务端的 `mode_info`。`mode` 可为 `1` 滚动、`4` 底部、`5` 顶部，实际权限及文字长度由平台决定。失败会抛出异常，不自动重发。

未知命令或无法解析的已知命令保留为 `BilibiliLiveEvent`，输出命令和字段名的调试日志。直播推送不写入私信数据库，不补发断线期间的历史事件；关注互动仅代表该直播流上报的行为，不等同于全账号粉丝通知。

鉴权、心跳及真实弹幕接收已通过只读验证。礼物、互动、开播／下播转换及发送弹幕完成离线测试，仍待真实验收。参考：[直播信息流](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/live/message_stream.md)、[直播弹幕](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/live/danmaku.md)。

## 字幕与音视频流

`get_video_info` 的 `pages` 提供各分 P 的 `cid`。字幕和播放地址均按 `bvid` 与 `cid` 查询，使用 WBI 签名。

`get_subtitles` 返回 `SubtitleList`，`tracks` 为可用字幕轨，`need_login` 保留播放器返回的登录要求。每条 `SubtitleTrack` 包含 `lan`、`lan_doc`、`ai_type`、`ai_status` 和 `subtitle_url`，插件可自行选择语言。无可用字幕时 `tracks` 为空；这不代表视频没有语音或画面文字。

```python
api = bot.adapter.api
info = await api.get_video_info(bvid)
subtitles = await api.get_subtitles(info.bvid, info.cid)
if subtitles.tracks:
    subtitle = await api.get_subtitle(subtitles.tracks[0])
    text = subtitle.text
    for segment in subtitle.body:
        logger.debug("字幕: {}-{} {}", segment.start, segment.end, segment.content)
```

字幕正文返回 `Subtitle`，`body` 中的 `start`、`end` 单位为秒，`content` 为文本；`text` 按片段顺序用换行拼接。下载字幕使用独立客户端，不携带登录 Cookie，仅接受平台字幕域名的 HTTPS 地址，不自动跟随重定向，正文大小上限为 10 MiB。链接失效时可重新获取字幕列表。

`get_play_urls` 默认保留 MP4 调用方式，地址在 `durl`。使用 `stream_format="dash"` 获取 DASH 音视频轨：

```python
playback = await api.get_play_urls(info.bvid, info.cid, stream_format="dash")
headers = playback.headers
if playback.dash is not None:
    video_tracks = playback.dash.video
    audio_tracks = playback.dash.audio
```

`DashStream` 提供 `base_url`、`backup_urls`、`mime_type`、`codecs`、`bandwidth` 和 `codecid`，视频轨另有 `width`、`height`、`frame_rate`；`segment_base` 保留可用的初始化段及索引范围。没有音轨时 `audio` 为空。DASH 可能返回多个清晰度和编码，插件应自行选择轨道，不能把传入的 `quality` 当作已经完成轨道筛选。

下载媒体时使用返回的 `headers`，其中包含 `Referer` 和 `User-Agent`，不包含登录 Cookie。流地址有时效，应在需要时获取；实际可用清晰度以平台响应和账号权限为准。视频下载、音视频合并、抽帧、OCR、语音识别和内容理解由插件处理。

字幕列表、字幕正文、MP4 与 DASH 均已通过真实只读验证，媒体验证只读取小范围数据。参考：[播放器及字幕](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/player.md)、[视频流](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/videostream_url.md)。

## 视频与用户互动

视频点赞使用 BV 号；收藏使用 avid 和收藏夹 ID。`get_video_info`、视频搜索结果均可提供 avid，当前账号的收藏夹可通过 `get_favorite_folders` 获取。`folder_ids` 支持多个收藏夹，取消收藏只移除指定收藏夹中的记录，不影响其他收藏夹；不会自动关注作者。

`get_user_relation` 返回 `UserRelation`，其中 `following` 表示已关注，`mutual` 表示互相关注，`blocked` 表示已拉黑，`attribute` 保留平台关系属性。关注、取消关注、点赞和收藏均由插件显式调用，适配器不自动执行。

`get_video_like_status` 只能反映平台提供的近期点赞状态，返回 `False` 不保证从未点赞。`get_video_favorite_status` 查询是否存在收藏，不提供具体收藏夹归属。业务错误与网络错误会向调用方抛出，不自动重试写入；网络超时后不能直接判断操作是否成功。

分类搜索及关系、点赞、收藏状态已通过真实只读查询。点赞、收藏、关注及取消操作已完成离线测试，仍待指定目标后进行真实账号验收。

接口参考：[分类搜索](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/search/search_request.md)、[视频互动](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/action.md)、[用户关系](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/user/relation.md)。

## 测试

```bash
uv run python -m unittest discover -s src/myfish/tests -p 'test_bilibili*.py'
```

测试使用模拟 HTTP 响应和临时数据库，不访问真实账号，不发送私信。覆盖适配器注册、消息映射、发送失败传播、扫码状态、WBI 对照、分页补齐、去重、账号隔离和重启恢复。

真实登录及接收验收使用独立入口，不加载插件、不自动发送消息：

```bash
uv run python -m myfish.tests.live_bilibili_check --duration 180 --notifications
```

订阅直播间进行只读验收，可重复传入 `--live-room`，此参数不写回配置：

```bash
uv run python -m myfish.tests.live_bilibili_check --duration 180 --live-room 12345
```

先完成扫码；首次建立基线后，再由另一个账号发送私信或回复评论，检查接收日志。计时包含扫码时间。该入口仍会保存凭据和接收进度，正常登录态维护可能执行 Cookie 刷新请求。
