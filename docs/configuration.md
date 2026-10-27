# 配置声明与迁移

Myfish 从组件注册的 Pydantic 模型构建默认配置。字段默认值、校验规则和说明均在模型中定义，无需修改框架内的 TOML 模板。

## 命令

| 命令 | 行为 |
| --- | --- |
| `myfish init <目录>` | 创建工作区及默认配置 |
| `myfish config check` | 预览变化和校验问题，不写入文件；需要更新或校验失败时退出码为 1 |
| `myfish config sync` | 生成或迁移配置，不安装依赖 |
| `myfish sync` | 先迁移配置，再同步启用组件的依赖 |
| `myfish run` | 读取和校验配置，不迁移已有文件、不安装依赖 |

配置路径由 `MYFISH_CONFIG_PATH` 指定，默认是工作目录下的 `config.toml`。首次加载时文件不存在，仍会生成默认配置。

## 编程接口

公开接口统一从 `myfish.core.config` 导入。

```python
from pathlib import Path

from myfish.core.config import BotConfig, CompiledConfig, ConfigBuilder

builder = ConfigBuilder(BotConfig)
compiled: CompiledConfig = builder.compile(Path("config.toml"))
```

`compile()` 只读取配置，返回编译产物 `CompiledConfig`，包含 TOML 文本、结构记录、变更说明和校验结果；不会写入配置或安装依赖。`sync()` 编译并校验后执行备份和写入，返回对应产物。每次编译使用独立的文档状态，同一构建器可以用于不同配置文件。

## 配置入口

适配器使用 `myfish/adapters/<适配器 ID>/config.py`。插件统一使用包目录，运行入口为 `<插件目录>/__init__.py`，配置声明位于同目录的 `config.py`，无配置需求时可省略。

配置发现只执行明确使用 `ConfigRegistry.adapter` 或 `ConfigRegistry.plugin` 注册的配置入口，不导入组件的 `__init__.py`、协议客户端和插件处理器。已有插件的普通 `config.py` 不会被当作配置声明执行；配置侧文件也不会被插件加载器作为独立插件导入。

配置入口应只依赖标准库、Pydantic 和 Myfish 配置注册接口。不要导入 API、Web 服务或可选依赖，不要读取账号数据或发起网络请求；依赖安装前也需要执行配置发现。运行代码直接导入该配置模型，避免重复定义。

注册 ID 必须与适配器或插件的包目录名一致。注册装饰器支持为 `ConfigRegistry` 使用导入别名。

## 适配器配置

```python
from pydantic import Field

from myfish.core.config import BaseConfig, ConfigRegistry


@ConfigRegistry.adapter("example")
class ExampleConfig(BaseConfig, extra="forbid"):
    comment = (
        "Example 适配器配置。",
        "首次启用前填写访问令牌。",
    )

    token: str = Field(description="访问令牌", repr=False)
    timeout: float = Field(default=20, gt=0, description="请求超时秒数")
```

生成配置包含 `[[adapters]]` 的 `id`、`enabled` 和 `[adapters.config]`。`enabled` 属于框架挂载配置，不需要在适配器模型中再次定义。

新适配器默认禁用。`default_enabled=True` 仅影响新建配置；向已有配置添加适配器时一律禁用，避免意外启动账号。旧配置省略 `enabled` 时保持原有启用行为。

必填字段不会被伪造为空字符串，生成器只添加提示注释；启用组件后，缺失字段会在校验时报出。默认值为 `None` 的字段不写入 TOML。

## 插件配置

```python
from pydantic import Field, StrictBool

from myfish.core.config import BaseConfig, ConfigRegistry


@ConfigRegistry.plugin("example")
class Settings(BaseConfig):
    comment = "Example 插件设置。"

    enabled: StrictBool = Field(default=False, description="是否启用插件")
    interval: int = Field(default=60, ge=1, description="任务间隔秒数")
```

插件模型必须为 `enabled` 提供布尔默认值。注册模型的插件采用这个默认值；用户文件中的显式开关始终优先。未注册配置模型的插件仍可使用原有配置字典，其自定义设置会保留。

## 字段与配置块说明

所有配置模型继承 `BaseConfig`，通过类属性 `comment` 定义配置块说明，支持字符串或字符串元组；嵌套模型沿用相同规则。`comment` 不属于配置字段，不会写入 TOML 的键值。需要限制额外字段时，使用 `class Settings(BaseConfig, extra="forbid")`。字段说明使用 `Field(description=...)`。

```python
class ServiceConfig(BaseConfig):
    comment = "HTTP 服务设置。"
    port: int = Field(default=8787, ge=1, le=65535, description="监听端口")


@ConfigRegistry.plugin("example")
class Settings(BaseConfig):
    enabled: StrictBool = False
    service: ServiceConfig = Field(default_factory=ServiceConfig)
```

生成结果包含：

```toml
#####################
# HTTP 服务设置。
[plugins.example.service]
port = 8787 # 监听端口
```

配置块之间使用分隔线和空行。适配器说明位于 `[[adapters]]` 上方，字段说明与值同行，用一个空格连接 `#`。普通字典使用内联表，例如 `cookies = {}`；嵌套配置模型使用独立配置块。

生成器在结构记录中保存自动生成的注释文本，模型说明变化时据此更新。手写注释和字符串内容会保留；字段已有手写行尾注释时，追加在字段说明之后，以 `；` 分隔。

## 自动迁移

生成器在配置旁保存 `config.toml.schema.json`，记录组件的字段结构和生成的注释文本，不包含配置值或凭据。

- 新字段：填入默认值，不覆盖已有用户值。
- 已移除字段：只有旧结构记录证明该字段曾属于模型时才自动删除。
- 未识别字段：保留并提示；模型禁止额外字段时，需手动处理或声明明确的迁移规则。
- 动态字典：保留原有内容，例如 Cookie 和自定义键值。
- 未注册或暂不可用的组件：保留配置和原有结构记录。

首次迁移没有旧结构记录，不能可靠区分废弃字段与用户自定义字段，因此不会猜测删除。新增默认字段的类型变化也不会覆盖现有值；需要转换时应显式声明迁移。

迁移先在内存中构建并校验，再原子替换配置。校验失败或检测到构建期间发生编辑时不写入。发生配置变化时保存权限为 `0600` 的时间戳备份；已有结构记录也会保存在对应备份旁。回退时应同时恢复配置和结构记录。

## 字段更名

```python
timeout: float = Field(
    default=20,
    gt=0,
    json_schema_extra={"x-myfish-renamed-from": "request_timeout"},
)
```

迁移会将旧字段值转移到新字段。新旧字段同时存在时报告冲突，不擅自选择其中一个值。

## 自定义迁移

涉及类型或业务语义变化时，通过注册参数 `migrate` 提供转换函数：

```python
def migrate(values: dict[str, object]) -> dict[str, object]:
    value = values.get("send_enabled")
    if isinstance(value, str) and value in ("true", "false"):
        values["send_enabled"] = value == "true"
    return values


@ConfigRegistry.plugin("example", migrate=migrate)
class Settings(BaseConfig):
    enabled: StrictBool = False
    send_enabled: bool = False
```

函数接收原始配置字典，返回迁移结果；可以多次执行而不应反复改变结果。不要访问网络、修改原配置文件或记录配置值。转换后的内容仍会经过模型校验，转换异常不会写入配置。
