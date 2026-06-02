from typing import Dict, Type
from myfish.core.adapter import BaseAdapter


class AdapterRegistry:
    _adapters: Dict[str, Type[BaseAdapter]] = {}

    @classmethod
    def register(cls, adapter_class: Type[BaseAdapter]) -> Type[BaseAdapter]:
        """
        适配器注册装饰器
        """
        if not hasattr(adapter_class, "meta_data") or adapter_class.meta_data is None:
            raise ValueError(
                f"❌ 注册失败: 适配器类 [{adapter_class.__name__}] 缺失 meta_data 属性, 请按照规范定义适配器元数据"
            )

        adapter_id = adapter_class.meta_data.id
        if adapter_id in cls._adapters:
            raise ValueError(
                f"❌ 注册冲突: 适配器 ID [{adapter_id}] 已存在，请检查是否有重复加载的适配器或id拼写冲突"
            )

        cls._adapters[adapter_id] = adapter_class
        return adapter_class

    @classmethod
    def build(cls, adapter_id: str, **kwargs) -> BaseAdapter:
        adapter_class = cls._adapters.get(adapter_id)
        if not adapter_class:
            raise ValueError(
                f"未知适配器 [{adapter_id}]！请检查 config.toml 拼写或是否遗漏了 import。"
            )

        return adapter_class.setup(**kwargs)
