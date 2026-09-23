"""Heap-allocated object types for the mini VM.

Every heap object carries a small header (generation, age, mark bit,
forwarding pointer, optional finalizer) so the garbage collector can do
precise reachability, copying and promotion.
"""

YOUNG = 0
OLD = 1


class HCell:
    """Base class = object header shared by all heap objects."""

    __slots__ = ("gen", "age", "marked", "forward", "finalizer")

    def __init__(self):
        self.gen = YOUNG
        self.age = 0            # minor GCs survived (used for promotion)
        self.marked = False     # mark bit (major GC)
        self.forward = None     # forwarding pointer (minor GC / Cheney)
        self.finalizer = None   # optional resource-release hook: fn(obj)


class HObj(HCell):
    """A script object: a bag of named fields."""

    __slots__ = ("fields",)

    def __init__(self, fields=None):
        super().__init__()
        self.fields = fields if fields is not None else {}

    def copy(self):
        return HObj(dict(self.fields))


class HArray(HCell):
    """A script array."""

    __slots__ = ("items",)

    def __init__(self, items=None):
        super().__init__()
        self.items = items if items is not None else []

    def copy(self):
        return HArray(list(self.items))


class HStr(HCell):
    """An immutable heap string."""

    __slots__ = ("s",)

    def __init__(self, s):
        super().__init__()
        self.s = s

    def copy(self):
        # immutable: reuse the python string
        return HStr(self.s)


class HClosure(HCell):
    """A function value: code object + captured upvalues."""

    __slots__ = ("fn", "upvalues")

    def __init__(self, fn, upvalues):
        super().__init__()
        self.fn = fn              # bytecode.Function (immutable, not traced)
        self.upvalues = upvalues  # list[HUpvalue]

    def copy(self):
        return HClosure(self.fn, list(self.upvalues))


class HUpvalue(HCell):
    """A captured variable.

    While the owning frame is alive the upvalue is *open*: location is
    an absolute index into the VM value stack.  When the frame returns the
    upvalue is *closed*: the value is copied into value.
    """

    __slots__ = ("closed", "location", "value")

    def __init__(self, location=-1):
        super().__init__()
        self.closed = False
        self.location = location
        self.value = None

    def copy(self):
        uv = HUpvalue(self.location)
        uv.closed = self.closed
        uv.value = self.value
        return uv


def is_heap_ref(v):
    return isinstance(v, HCell)
