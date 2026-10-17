# 闲鱼交易能力

交易能力复用 `FishAPI` 的异步 HTTP 客户端、MTOP 签名和 Cookie 持久化。
不依赖上游项目的 Web 管理后台、数据库或调度框架，没有新增依赖。

## 接口

所有接口返回平台原始响应字典。平台未返回 `SUCCESS` 时抛出 `FishAPIError`，
网络及 HTTP 错误保留 httpx 异常。写入请求没有自动重试：超时不代表平台未执行，
应先查询订单再决定是否重试。

| FishAPI 方法 | 平台接口 | 版本 | 作用 |
| --- | --- | --- | --- |
| `get_sold_orders(page=1, page_size=20, query_code="ALL")` | `mtop.taobao.idle.trade.merchant.sold.get` | 1.0 | 卖出订单；也支持 NOT_SHIP |
| `get_order_detail(order_id)` | `mtop.idle.web.trade.order.detail` | 1.0 | 订单详情及状态节点 |
| `ship_order(order_id, trade_text="", pic_list=None)` | `mtop.taobao.idle.logistic.consign.dummy` | 1.0 | 无需物流发货，不负责发送卡密 |
| `request_red_flower(order_id)` | `mtop.taobao.idlemessage.red.flower` | 1.0 | 平台求小红花，不是普通消息 |
| `rate_buyer(order_id, feedback="不错的买家")` | `mtop.taobao.idle.rate.create` | 4.0 | 卖家首次好评 |
| `get_pending_ratings(page=1, page_size=20)` | `mtop.taobao.idle.merchant.rate.list` | 1.0 | 待评价订单 |
| `get_refund_orders(dispute_status="1", page=1, page_size=20)` | `mtop.taobao.idle.merchant.refund.list` | 1.0 | 退款订单；状态支持 1、2、3、5 |
| `skip_bargain_and_ship(order_id, item_id, buyer_id)` | `mtop.idle.groupon.activity.seller.freeshipping` | 1.0 | 特定小刀/拼团订单免拼发货 |

订单列表、待评价和退款接口使用卖家端 Origin/Referer，且只作用于单次请求。
发货、求小红花、评价都必须由插件显式调用，服务端校验订单归属与状态。
`ship_order` 不支持实物快递单号；小刀免拼发货也不会被自动当作普通发货替代。

```python
api = bot.adapter.api
response = await api.get_sold_orders(query_code="NOT_SHIP")
detail = await api.get_order_detail(order_id)
# 在业务确认已交付、订单允许发货后：
await api.ship_order(order_id, trade_text="已通过会话交付")
# 交易完成、允许评价后：
await api.rate_buyer(order_id, feedback="沟通顺利，谢谢支持")
await api.request_red_flower(order_id)
```

## 订单状态跟踪

在现有 config.toml 的闲鱼适配器配置中开启：

```toml
[[adapters]]
id = "fish"
[adapters.config]
track_orders = true
order_poll_interval = 60
order_max_pages = 5
```

默认关闭。开启后登录成功便同步订单，每次最多拉取指定页数，每页 20 条；
收到交易卡片会唤醒检查，密集消息合并，同账号检查间隔至少 10 秒。
未覆盖到的旧订单不推断为关闭；需要全量历史时显式分页拉取。

账号快照保存于 `data/adapters/fish/orders-<账号ID>.json`，最多保留 5000 笔。
多页请求全部成功后才提交本次快照；失败保留已有状态，重启读取快照避免重复发现。
持久化只包含订单 ID、商品/买家 ID、状态和评价标志，不含地址、电话、完整响应。
缓存文件损坏或账号不匹配会显式报错，不会静默清空。

插件事件：

- `fish.order.discovered`：首次观测，可能是历史订单，不能当成“刚支付”。
- `fish.order.updated`：状态、评价标志等已改变，含 `previous` 和最新 `order`。

```python
from myfish.adapters.fish import FishBot
from myfish.adapters.fish.orders import FishOrderUpdatedEvent
from myfish.core.plugin import Plugin, PluginMetadata

plugin = Plugin(PluginMetadata(
    id="trade_monitor", name="订单状态", support_adapters=["fish"],
))

@plugin.on_event("fish.order.updated")
async def on_order_updated(event: FishOrderUpdatedEvent, bot: FishBot):
    order = event.order
    print(event.previous.status, "->", order.status)
    # 事件供业务判断；不要无条件在这里发货、评价或求小红花。
```

事件不提供消息回复上下文；需要通知时通过 `bot.send_msg` 明确指定会话。
框架提供异步事件分发，不提供事务性任务队列或业务动作的恰好执行一次保证。
发货/评价等业务需持久化自己的执行记录，动作前再查询当前状态。

`FishOrder` 保留 `raw_status`，未知状态不会猜测为可发货；退款中会覆盖交易状态。
`can_ship`、`can_rate` 仅是快照提示，不能替代实时平台校验。
退款列表作为独立查询接口提供，跟踪器当前比较卖出列表中可见的退款状态，
不会承诺捕获每个退款流程的中间节点。

## 验证

当前目录运行本地测试：

```powershell
python -m unittest discover -s tests -v
```

真实只读检查（不会自动执行发货/评价/小红花，也不覆盖凭证文件）：

```powershell
python tests/live_trade_check.py --auth ../../data/adapters/fish/auth.json
# 有指定订单时追加 --order-id <订单ID> 以验证详情查询
```

2026-10-17 实际只读检查：卖出订单、待评价、退款列表返回 SUCCESS；
卖出列表为空，无法据此验证真实订单详情。写入接口仅验证协议和失败语义，
真实发货/评价/求小红花仍需指定测试订单及允许动作。

## 调研依据

分析固定版本 `c9efea9b96330211849ea70c62b85980a31e0f22` 的实际请求构造，
按本项目接口封装重新实现，不引入对方的服务及调度实现。

- [发货请求](https://github.com/zhinianboke/xianyu-auto-reply/blob/c9efea9b96330211849ea70c62b85980a31e0f22/websocket/app/services/shipping/confirm_service.py)
- [订单列表、详情、退款查询](https://github.com/zhinianboke/xianyu-auto-reply/blob/c9efea9b96330211849ea70c62b85980a31e0f22/common/services/order_service.py)
- [评价与待评价列表](https://github.com/zhinianboke/xianyu-auto-reply/blob/c9efea9b96330211849ea70c62b85980a31e0f22/common/services/rate_service.py)
- [小红花请求](https://github.com/zhinianboke/xianyu-auto-reply/blob/c9efea9b96330211849ea70c62b85980a31e0f22/scheduler/app/services/scheduler/red_flower_task.py)
- [免拼发货](https://github.com/zhinianboke/xianyu-auto-reply/blob/c9efea9b96330211849ea70c62b85980a31e0f22/websocket/app/services/shipping/freeshipping_service.py)
