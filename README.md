# 🐟 Myfish

> *我的鱼，我做主。*
> 
> **一个现代化、纯异步、基于纯Python实现的闲鱼API协议库与Bot开发框架。**

---

> [!WARNING]
> **免责声明与风险提示**
> 由于本项目的特殊性与敏感性，**不保证代码的长期稳定可用**。本项目仅供学习与安全研究使用，随时可能会删库跑路。请勿用于任何非法或黑产用途，否则后果自负。

> [!IMPORTANT]
> **关于滑块风控**
> 短时间内频繁获取 `access_token` 或高频调用 API 有较大率触发阿里系滑块验证码。若触发风控，需要手动通过浏览器滑块并获取 `x5sec` 的值，将其添加至本地鉴权文件后方可继续运行。
> **收不到消息体**
> 如果是token失效后收不到消息，重启框架即可。
> 如果是因为频繁登录、断开重连等测试操作导致的收不到消息则是风控的表现。尝试重启程序直到触发滑块验证码后更新获取到的 `x5sec` 的值后即可解决。

## ✨ 核心特性

与常见的基于 JS 引擎缝合的闲鱼脚本不同，Myfish 进行了深度的底层重构与优化，并将Sign加密解密相关逻辑用编译型语言重写后封装为二进制模块供框架适配器调用(Fish适配器)，提供高内聚、高并发、高扩展性、开箱即用的开发体验。

* **100% 纯Python：**
  无需安装node.js环境，纯python实现Api签名、消息体解密，无跨进程调用开销负担。

* **全链路异步架构：**
  基于 `asyncio` 和 `httpx` 构建，网络 I/O 零阻塞。单机即可轻松承载海量消息与多账号管理，为企业级客服机器人和业务监控提供坚实底座。

* **平台无关的适配器模式：**
  平台与框架完全解耦。只需实现新的适配器，即可将你的核心业务逻辑无缝横向扩展至 QQ、微信、钉钉等任意第三方通讯生态，真正做到“一套代码，多端运行”。

* **插件开发简单快捷易上手：**
  借助优雅的装饰器路由与依赖注入机制，一行代码即可完成事件监听与指令注册。彻底告别臃肿的面条代码，像搭积木一样极速构建你的自定义业务功能。

## 已实现功能

- [x] **完整的扫码登录流程** 
- [x] **完善的Auth凭证管理流程**
- [x] **高性能计算sign与解包messagepack**
- [x] **集成web端大部分API与WS通讯协议**
- [x] **核心算法编译成二进制保护处理**
- [x] **类似成熟bot框架如Nonebot的插件式开发体验**
- [x] **可扩展的平台适配器模式, 你的业务不止于闲鱼**

## 安装与使用

以下方式任选一种：

- ### 通过包管理器(pip或uv):

使用包管理器安装myfish:

`pip install myfish` 或 `uv pip install myfish`

然后使用框架自带的脚手架初始化项目并运行：

```bash
myfish init my-project

myfish run
```

- ### 从源代码构建:

确保uv已安装至你的设备。

1. 克隆项目: `git clone https://github.com/Kaguya233qwq/myfish`

2. 同步依赖: `uv sync`

3. 运行: `uv run src/myfish/main.py`

构建wheel(可选)：

`uv build`

测试cli(可选):

`uv run myfish -h`

- ### Docker镜像:

新建docker-compose.yml

```yml
version: '3.8'

services:
  myfish:
    image: kaguya233qwq/myfish:latest
    container_name: myfish
    restart: always
    environment:
      - TZ=Asia/Shanghai
      - LOG_LEVEL=INFO
    volumes:
      # 挂载外部插件目录
      - ./plugins:/app/plugins
      # 挂载数据目录：用于持久化保存auth凭证和配置
      - ./data:/app/data
      # 挂载日志目录
      - ./logs:/app/logs
```

后台运行：`docker-compose up -d`

手动构建镜像(开发者):

```bash
docker build -t myfish:local .

docker run -it --rm \
  -v $(pwd)/plugins:/app/plugins \
  -v $(pwd)/data:/app/data \
  myfish:local
```

### 开发用户插件

Myfish 采用了极其优雅的声明式路由与依赖注入 (DI) 机制。开发插件就像搭积木一样简单，你无需关心底层的协议流转，只需专注于你的业务逻辑。

创建一个 hello.py 文件放入插件目录即可：

```python
from myfish.core.plugin import Plugin, PluginMetadata
from myfish.core.event import MessageEvent
from myfish.adapters.fish import FishBot # 引入具体平台的 Bot 类型

# 声明插件元数据
plugin = Plugin(
    metadata=PluginMetadata(
        id="auto_reply"
        name="客服自动回复",
        description="极其优雅的开箱即用客服插件",
        version="1.0.0",
        author="YourName",
    )
)

# 自定义拦截规则
def reject_rule(event: MessageEvent) -> bool:
    # 仅当消息包含特定词汇时放行
    return any(word in event.plain_text for word in ["刀", "低价", "便宜"])

# 注册消息处理器
@plugin.on_message(rule=reject_rule)
async def handle_bargain(event: MessageEvent):
    await event.reply("抱歉老板，底价出回血了，不议价哦😭")

# 注册快捷指令与依赖注入：获取用户信息
# 平台的api挂载在适配器上（详见‘适配器开发’）
# 所以注入对应平台的Bot实例即可获得平台api的调用能力
@plugin.on_keywords(("我的信息", "我是谁"))
async def handle_user_info(event: MessageEvent, bot: FishBot):
    user_info = await bot.adapter.api.get_user_info(event.sender_id)
    await event.reply(f"获取到的您的信息：\n{user_info}")
```

核心概念：

**event (事件上下文)**：封装了当前消息的所有属性和信息（发送者、消息链等），并提供了极简的 .reply() 方法。

**bot (引擎实例)**：当你的业务需要主动调用平台 API（如改价、拉取详情、自动发货）时，可通过依赖注入方式使用。

**rule (规则校验器)**：执行核心业务前的前置拦截器，保障业务函数的纯粹性。

**handler (处理器)**：使用装饰器的方式注册处理器，监听指定规则的事件并触发回调。

### 开发适配器

Myfish的野心绝不仅限于闲鱼。框架的核心 (core) 是一套完全平台无关的统一标准。通过编写 适配器 (Adapter)，你可以将这套强大的框架接入 QQ、微信、钉钉等任何第三方通讯平台，并无缝衔接地使用你在插件中编写的业务逻辑。

适配器本质上是一个防腐层，它只负责干三件事：

1. 连接平台：接管平台的WebSocket或HTTP轮询。

2. 解码 (Decode)：将平台杂乱的Raw JSON解析为干净的MessageEvent并上报给引擎。

3. 编码 (Encode)：将引擎下发的标准MessageChain打包成平台专用的底层格式发出去。

开发示例：如何编写一个 MockAdapter

```python
import json
from myfish.core.adapter import BaseAdapter
from myfish.core.event import MessageEvent
from myfish.core.message import MessageChain, Text

class MockAdapter(BaseAdapter):
    def __init__(self):
        super().__init__()
        # 平台的API客户端可以在这里初始化
        self.api = MockAPI()

    async def run(self):
        """框架启动时自动调用，用于挂载与平台服务器的持久通信"""
        print("Mock Adapter 启动，正在连接...")
        # 模拟收到了一条原始的平台消息
        await self._on_raw_receive('{"user": "1001", "text": "你好，Myfish"}')

    async def _on_raw_receive(self, raw_payload: str):
        """解码：将平台数据转换为核心 Event 并上报"""
        data = json.loads(raw_payload)
        
        # 将平台的字符串组装成框架标准的通用消息链
        msg_chain = MessageChain([Text(text=data["text"])])
        
        # 组装标准事件上下文
        event = MessageEvent(
            cid=f"group_{data['user']}",
            sender_id=data["user"],
            sender_name="MockUser",
            messages=msg_chain,
            raw_payload=data
        )
        
        # 将事件投递给Bot的事件总线
        if self.callback:
            await self.callback(event)

    async def send(self, target_id: str, message: MessageChain, cid: str = ""):
        """编码：接收引擎的回复指令，打包发送给平台"""
        # 将通用的 MessageChain 转换为当前平台特定的格式
        plain_text = "".join([seg.text for seg in message if isinstance(seg, Text)])
        
        # 构造底层 payload
        payload = {"to": target_id, "content": plain_text}
        print(f"[发送] -> {payload}")
        # 使用ws或http发送数据包
        # await self.ws.send(json.dumps(payload))
```


## TODO

- [ ] **补环境对抗**：尝试实现纯js补环境，在本地全自动计算`x5sec`过滑块。
- [ ] **API 扩展**：增加更多开箱即用的业务api如自动擦亮等移动端api。
- [ ] **内置插件开发**: 增加更多实用内置插件

## 🙏 致谢

感谢 [cv-cat/XianYuApis](https://github.com/cv-cat/XianYuApis) 为本项目提供了极具价值的核心sign加密与消息解包算法的javascript代码片段参考，Myfish在其基础上完成了纯python算法的升华。

感谢 [nonebot/nonebot2](https://github.com/nonebot/nonebot2) 为本项目的开发工作提供了很棒的架构思维，特别是依赖注入、动态加载、适配器模式、插件等前沿的开发设计模式思想。其作为我的python启蒙项目为我注入了源源不断的灵感！

---
最后本项目欢迎任何有能力的开发者Pull Request，感谢你们对开源社区以及本项目的贡献。