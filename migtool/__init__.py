"""migtool：声明式数据迁移工具（仅标准库）。"""
from .model import Migration, Step, load_migration
from .engine import Engine, SimulatedKill
from .compat import evaluate, Finding, SAFE, RISKY, BREAKING
from .dryrun import dry_run

__all__ = ["Migration", "Step", "load_migration", "Engine", "SimulatedKill",
           "evaluate", "Finding", "SAFE", "RISKY", "BREAKING", "dry_run"]
__version__ = "0.1.0"
