"""migtool：声明式数据迁移工具（库 + CLI，仅依赖标准库）。"""

from .engine import Engine, NeedsConfirmation, RollbackError, dry_run
from .spec import Spec, SpecError, Step, load_spec, parse_spec
from .store import Store

__all__ = [
    "Engine", "NeedsConfirmation", "RollbackError", "dry_run",
    "Spec", "SpecError", "Step", "load_spec", "parse_spec",
    "Store",
]

__version__ = "0.1.0"
