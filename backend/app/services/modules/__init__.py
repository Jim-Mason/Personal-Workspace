"""模块接入内核。

`ModuleHub` 是唯一需要被外部引用的入口；其余文件是实现细节。
"""

from .hub import ModuleHub
from .registry import ModuleRegistry, ModuleRuntime
from .spec import ModuleSpec, ModuleSpecError
from .supervisor import ModuleStartError, ModuleSupervisor

__all__ = [
    "ModuleHub",
    "ModuleRegistry",
    "ModuleRuntime",
    "ModuleSpec",
    "ModuleSpecError",
    "ModuleStartError",
    "ModuleSupervisor",
]
