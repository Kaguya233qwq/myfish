# 插件目录结构

内置 `builtin_plugins/` 和外部 `plugins/` 均使用包目录：`example/__init__.py`。包入口中定义或导出一个 `Plugin` 实例。

例如，将处理函数放进子模块：

```text
example/
├── __init__.py
├── handlers.py
├── config.py
└── requirements.txt
```

`__init__.py` 中写 `from .handlers import plugin`；在 `handlers.py` 中创建
`plugin = Plugin(...)` 并注册处理函数，配置可以通过 `from .config import ...`
相对导入。也可以在 `__init__.py` 中创建插件，再导入注册处理函数的子模块。

加载器只扫描当前层的入口，不递归加载包内辅助模块。以 `_` 或 `.` 开头的
入口及缺少 `__init__.py` 的目录会被忽略。单个插件导入失败不会阻止其他插件；
同名插件沿用先加载者。

## 启用与禁用

在 `config.toml` 中按包目录名配置，不使用插件显示名：

```toml
[plugins.example]
enabled = false
```

内建与外部插件使用相同规则。禁用的运行入口不会被导入，不会注册处理器或执行启动、关闭钩子。
修改后重启生效。Fishy 默认关闭，Echo 默认开启；注册配置模型的插件使用其 `enabled` 默认值，未注册的插件沿用默认加载。开启 Fishy 使用 `[plugins.fishy]` 的 `enabled = true`。

包插件可在 `config.py` 中使用 `@ConfigRegistry.plugin("插件 ID")` 注册继承 `BaseConfig` 的轻量配置模型，无配置需求时可省略 `config.py`。配置入口独立于运行入口加载，不应依赖可选依赖或启动业务。模型支持字段说明、`comment` 配置块注释及迁移规则，详见 [配置开发文档](../../../docs/configuration.md)。

回声插件默认开启，文本消息需以 `echo` 开头，图片消息直接触发回复。设置 `[plugins.echo]` 的 `enabled = false` 可关闭插件。


## 插件依赖

修改插件开关或依赖声明后，执行 `myfish sync`，再执行 `myfish run`。
使用 `myfish sync --upgrade` 更新锁定版本。启动不会自动安装依赖。

插件通过 `<插件目录>/requirements.txt` 声明依赖，无第三方依赖时可省略。支持本地 `-r` / `-c` 引用，引用文件变化也会触发重新解析。
依赖发现不会导入插件，禁用插件不参与同步。
内置 Fishy 使用框架的 `fishy` extra，不重复读取其 requirements.txt。

所有启用插件与框架依赖在同一环境中统一求解，冲突时同步失败。
不要在插件启动钩子中安装依赖。同步环境保存在工作区 `.myfish/venv`，
不应手动修改；可重新执行 `myfish sync` 修复。同步期间不要启动 Bot，也请先停止运行中的 Bot 再同步。
若同步进程异常退出留下 `.myfish/syncing`，确认没有同步进程后可删除该空目录再重试。
