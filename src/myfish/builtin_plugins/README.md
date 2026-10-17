# 插件目录结构

内置 `builtin_plugins/` 和外部 `plugins/` 均支持两种入口：

- 单文件：`example.py`，模块中暴露一个 `Plugin` 实例。
- 包目录：`example/__init__.py`，包入口中定义或导出一个 `Plugin` 实例。

例如，将处理函数放进子模块：

```text
example/
├── __init__.py
├── handlers.py
└── config.py
```

`__init__.py` 中写 `from .handlers import plugin`；在 `handlers.py` 中创建
`plugin = Plugin(...)` 并注册处理函数，配置可以通过 `from .config import ...`
相对导入。也可以在 `__init__.py` 中创建插件，再导入注册处理函数的子模块。

加载器只扫描当前层的入口，不递归加载包内辅助模块。以 `_` 或 `.` 开头的
入口及缺少 `__init__.py` 的目录会被忽略。单个插件导入失败不会阻止其他插件；
同名插件沿用先加载者，同名 `.py` 文件与包并存时优先加载包。
