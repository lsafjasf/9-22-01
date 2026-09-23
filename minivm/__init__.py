"""minivm: a small stack-based bytecode VM with a generational GC."""

from .assembler import assemble
from .bytecode import Op, Function, Program
from .heap import Heap, GCStats
from .objects import HObj, HArray, HStr, HClosure, HUpvalue
from .vm import VM, VMError, UncaughtError, to_str

__all__ = [
    "assemble", "Op", "Function", "Program", "Heap", "GCStats",
    "HObj", "HArray", "HStr", "HClosure", "HUpvalue",
    "VM", "VMError", "UncaughtError", "to_str",
]
