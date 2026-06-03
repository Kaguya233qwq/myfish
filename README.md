# 🐟 Myfish

> *我的鱼，我做主。*
> **一个Python的现代化、纯异步、跨平台的Bot开发框架。现已内置闲鱼适配器与微信ClawBot适配器支持。**

---

> [!WARNING]
> **关于闲鱼适配器的免责声明与风险提示**
> 咸鱼适配器的特殊性与敏感性，**不保证其代码的长期稳定可用**。本项目仅供学习与安全研究使用，必要时可能会archive或private跑路。请勿用于任何非法或黑产用途，否则后果自负。

> [!IMPORTANT]
> **关于闲鱼适配器的滑块风控**
> 短时间内频繁获取 `access_token` 或高频调用 API 有较大率触发阿里系滑块验证码。若触发风控，需要手动通过浏览器滑块并获取 `x5sec` 的值，将其添加至本地鉴权文件后方可继续运行。
> **收不到消息体**
> 如果是 token 失效后收不到消息，重启框架即可。
> 如果是因为频繁登录、断开重连等测试操作导致的收不到消息则是风控的表现。尝试重启程序直到触发滑块验证码后更新获取到的 `x5sec` 的值后即可解决。

## 🚀 已实现功能/特性

### 🛠️ 框架核心 (core)
- [x] **跨平台微内核架构**：引擎与协议彻底解耦。一套插件代码，无缝多端运行。
- [x] **开发体验 (DX) 优先**：高度标准化的声明式插件路由与泛型依赖注入 (DI)。
- [x] **云原生 DevOps 规范**：开箱即用的 12-Factor 容器化存储与动态配置引擎。
- [x] **全链路纯异步调度**：基于 `asyncio` 的非阻塞事件循环，轻松应对海量并发。

### 🔌 内置适配器支持

**🐟 Fish (闲鱼websocket协议) 适配器**
- [x] 完整的扫码登录与动态鉴权生命周期管理。
- [x] 高性能计算 Sign 签名与解包 MessagePack。
- [x] 核心加密算法编译为二进制模块，提供物理级防逆向保护。
- [x] 深度集成 Web 端常用 API 与实时 WebSocket 长连接通讯。

**🦞 ClawBot (微信ilink协议) 适配器**
- [x] 零配置、开箱即用的扫码登录快速接入。
- [x] 全面覆盖绝大部分图文、媒体等核心消息类型的双向收发。
- [x] 基于纯异步底层构建，提供极高的高并发消息吞吐性能。

## 📦 安装与使用

### 1. 包管理器 (推荐使用 uv)

```bash
uv pip install myfish

# 初始化项目
myfish init my-project
cd my-project

# 启动引擎自动生成config.toml
myfish run

```

*初次启动会自动生成 `config.toml`，按需修改其中的适配器参数后重启即可。*

### 2. 源码构建

```bash
git clone https://github.com/Kaguya233qwq/myfish
cd myfish
uv sync
uv run src/myfish/main.py

```

### 3. Docker 部署

新建 `docker-compose.yml`：

```yml
services:
  myfish:
    image: kaguya233qwq/myfish:latest
    container_name: myfish
    environment:
      - TZ=Asia/Shanghai
      - MYFISH_CONFIG_PATH=/app/data/config.toml
    network_mode: host # 或者指定你的自定义虚拟网络
    volumes:
      - ./plugins:/app/plugins
      - ./data:/app/data
      - ./logs:/app/logs

```

后台运行：`docker-compose up -d`

## 🧩 开发用户插件

采用声明式路由与泛型依赖注入 (DI)。

```python
from myfish.core.plugin import Plugin, PluginMetadata
from myfish.core.event import MessageEvent
from myfish.adapters.fish import FishBot 

plugin = Plugin(
    metadata=PluginMetadata(
        id="auto_reply",
        name="客服自动回复",
        version="1.0.0",
        author="YourName",
        support_adapters=["fish"] # 声明为仅支持闲鱼适配器的插件，此项设置可以提升性能
    )
)

#  自定义匹配规则
def reject_rule(event: MessageEvent) -> bool:
    return any(word in event.plain_text for word in ["刀", "低价", "便宜"])

@plugin.on_message(rule=reject_rule)
async def handle_bargain(event: MessageEvent):
    await event.reply("抱歉老板，底价出回血了，不议价哦😭")

# 依赖注入：跨平台通用逻辑
@plugin.on_keywords(("ping",))
async def handle_ping(event: MessageEvent):
    await event.reply("pong!")

# 依赖注入：注入特定平台的 Bot，获取调用对应平台api的能力
@plugin.on_keywords(("我的信息", "我是谁"))
async def handle_user_info(event: MessageEvent, bot: FishBot):
    user_info = await bot.adapter.api.get_user_info(event.sender_id)
    await event.reply(f"您的信息：\n{user_info}")

```

## 🔌 开发适配器 (Adapter)

Myfish 核心采用两阶段初始化与注册表装配协议。开发自定义适配器必须继承 `BaseAdapter` 并实现 `setup` 初始化。

```python
import json
from myfish.core.adapter import BaseAdapter, AdapterMetaData
from myfish.core.registry import AdapterRegistry
from myfish.core.event import MessageEvent
from myfish.core.message import MessageChain, Text

@AdapterRegistry.register
class MockAdapter(BaseAdapter[MockAPI]):
    # 框架通过 SSOT 提取 ID 与注册信息
    meta_data = AdapterMetaData(
        id="mock",
        name="Mock适配器",
        description="用于本地测试的开发适配器",
        version="1.0.0",
        author="YourName"
    )

    def __init__(self, api: MockAPI):
        super().__init__(api=api)
        self.bot_id = "unknown"

    @classmethod
    def setup(cls, **kwargs) -> "MockAdapter":
        """同步装配 (由 Config 驱动)"""
        token = kwargs.get("token")
        if not token:
            raise ValueError("MockAdapter 必须在配置中提供 token")
        return cls(api=MockAPI(token=token))

    async def run(self):
        """异步启动与鉴权"""
        self.bot_id = await self.api.get_self_id()
        await self._listen()

    async def _listen(self):
        raw_payload = '{"user": "1001", "text": "你好，Myfish"}'
        data = json.loads(raw_payload)
        
        event = MessageEvent(
            cid=f"group_{data['user']}",
            sender_id=data["user"],
            sender_name="MockUser",
            messages=MessageChain([Text(text=data["text"])]),
            raw_payload=data
        )
        
        if self._on_receive_callback:
            await self._on_receive_callback(event)

    async def send(self, target_id: str, message: MessageChain, cid: str = ""):
        plain_text = "".join([seg.text for seg in message if isinstance(seg, Text)])
        payload = {"to": target_id, "content": plain_text}
        # await self.api.send_msg(payload)

```

## 📝 TODO

* [ ] **适配器扩展**：支持更多平台内置适配器。
* [ ] **适配器API 扩展**：增加适配器中如闲鱼的自动擦亮等移动端实用 API。
* [ ] **闲鱼适配器增强**：尝试实现纯 JS 补环境，在本地全自动计算 `x5sec` 过滑块。
* [ ] **持久化存储**：引入基于 SQLite 的标准插件数据层 (ORM)。

## 🙏 致谢

* [nonebot/nonebot2](https://github.com/nonebot/nonebot2) 为本项目的微内核架构、依赖注入、适配器模式等底层系统工程提供了启蒙与极具价值的参考范式。
* [cv-cat/XianYuApis](https://github.com/cv-cat/XianYuApis) 闲鱼适配器核心 Sign 加密与消息解包算法的 JS 参考，fish适配器 在此基础上完成了纯 Python 算法的移植与升华。
* [corespeed-io/wechatbot](https://github.com/corespeed-io/wechatbot) 微信ClawBot适配器(ilink协议)参考。

本项目欢迎任何有能力的开发者提交 Pull Request，共同建设生态。