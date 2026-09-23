"""Generational heap with precise reachability.

Layout
------
* young generation: a list used as a bump-allocated semi-space.  Collected
  with Cheney's copying algorithm into a fresh semi-space.
* old generation: a list of promoted objects, collected with mark & sweep.

Roots
-----
The heap does not know the VM; the VM registers ``root_provider``, a
callable yielding ``(container, key)`` pairs for every root slot (value
stack, call-frame locals which live on the stack, globals, constant pools,
open upvalues).  The collector reads and *rewrites* those slots, which is
what makes the copying collector precise: every reference to a moved
object is updated.

Write barrier
-------------
``write_barrier(container, value)`` must be called after **every** store of
a heap pointer into a heap object (field store, array-element store, closed
upvalue store).  When ``container`` is old and ``value`` is young the
container is added to the remembered set.  The remembered set is
scanned during minor collections, so old-to-young pointers are never
missed.  Stores into young containers need no barrier (a minor GC traces
the whole young generation anyway), and stores of immediates (ints,
floats, bools, None) need no barrier either.

Promotion
---------
An object that survives ``promote_age`` minor collections is promoted to
the old generation.  If a promoted (or already old) object ends up
pointing at young objects after a minor GC, it is (re-)entered into the
remembered set.
"""

import time
from dataclasses import dataclass

from .objects import YOUNG, OLD, HCell, HObj, HArray, HClosure, HUpvalue


@dataclass
class GCStats:
    allocated: int = 0          # total objects ever allocated
    finalized: int = 0          # objects whose release hook ran
    minor_collections: int = 0
    major_collections: int = 0
    minor_pause_s: float = 0.0
    major_pause_s: float = 0.0
    max_pause_s: float = 0.0
    peak_objects: int = 0
    young_live: int = 0         # reachable young objects after last minor GC
    old_live: int = 0           # old objects after last sweep


class _AttrBox:
    """Adapter so an object attribute looks like a ``container[key]`` slot."""

    __slots__ = ("obj", "attr")

    def __init__(self, obj, attr):
        self.obj = obj
        self.attr = attr

    def __getitem__(self, key):
        return getattr(self.obj, self.attr)

    def __setitem__(self, key, value):
        setattr(self.obj, self.attr, value)


def _pointer_slots(obj):
    """Return [(container, key)] for every slot of *obj* that may hold a
    heap reference.  This is what makes reachability *precise*: the GC
    knows exactly which fields are pointers."""
    if isinstance(obj, HObj):
        return [(obj.fields, k) for k in obj.fields]
    if isinstance(obj, HArray):
        return [(obj.items, i) for i in range(len(obj.items))]
    if isinstance(obj, HClosure):
        return [(obj.upvalues, i) for i in range(len(obj.upvalues))]
    if isinstance(obj, HUpvalue):
        # An open upvalue points at a VM stack slot; the stack itself is a
        # root, so only closed values need tracing here.
        if obj.closed:
            return [(_AttrBox(obj, "value"), None)]
        return []
    return []  # HStr: no outgoing pointers


class Heap:
    def __init__(self, young_size=1024, old_threshold=4096, promote_age=1):
        self.young_size = young_size
        self.old_threshold = old_threshold
        self.promote_age = promote_age
        self.young = []            # from-space (bump allocated)
        self.old = []              # old generation
        self.remembered = set()    # old objects that may point to young ones
        self.root_provider = None  # callable -> iterable of (container, key)
        self.stats = GCStats()

    # ------------------------------------------------------------------
    # allocation
    # ------------------------------------------------------------------
    def alloc(self, obj):
        """Register a freshly created object in the young generation."""
        if len(self.young) >= self.young_size:
            self.collect_minor()
        obj.gen = YOUNG
        self.young.append(obj)
        self.stats.allocated += 1
        live = len(self.young) + len(self.old)
        if live > self.stats.peak_objects:
            self.stats.peak_objects = live
        return obj

    # ------------------------------------------------------------------
    # write barrier
    # ------------------------------------------------------------------
    def write_barrier(self, container, value):
        """Record old->young stores.  Trigger: after any pointer store into
        a heap object (SET_FIELD / SET_INDEX / STORE_UPVALUE / natives)."""
        if container.gen == OLD and isinstance(value, HCell) and value.gen == YOUNG:
            self.remembered.add(container)

    # ------------------------------------------------------------------
    # roots
    # ------------------------------------------------------------------
    def _root_slots(self):
        if self.root_provider is None:
            return []
        return list(self.root_provider())

    # ------------------------------------------------------------------
    # minor collection: Cheney copying
    # ------------------------------------------------------------------
    def collect_minor(self):
        t0 = time.perf_counter()
        from_space = self.young
        to_space = []
        worklist = []

        def move(obj):
            fwd = obj.forward
            if fwd is not None:
                return fwd
            if obj.age >= self.promote_age:
                # promote in place (identity preserved)
                obj.gen = OLD
                self.old.append(obj)
                obj.forward = obj
                new = obj
            else:
                new = obj.copy()
                new.gen = YOUNG
                new.age = obj.age + 1
                new.finalizer = obj.finalizer  # release hook survives copying
                obj.forward = new
                to_space.append(new)
            worklist.append(new)
            return new

        # 1. move objects reachable from VM roots, rewriting the root slots
        for container, key in self._root_slots():
            v = container[key]
            if isinstance(v, HCell) and v.gen == YOUNG:
                container[key] = move(v)

        # 2. remembered set: old objects act as extra roots
        for obj in self.remembered:
            worklist.append(obj)
        self.remembered = set()

        # 3. breadth-first scan, moving young referents
        while worklist:
            obj = worklist.pop()
            for container, key in _pointer_slots(obj):
                v = container[key]
                if isinstance(v, HCell) and v.gen == YOUNG:
                    container[key] = move(v)
                    if obj.gen == OLD:
                        self.remembered.add(obj)

        # 4. anything left in from-space without a forwarding pointer is
        #    garbage: run its release hook
        for obj in from_space:
            if obj.forward is None:
                self._finalize(obj)
            else:
                obj.forward = None  # clear for the next cycle

        self.young = to_space
        st = self.stats
        st.minor_collections += 1
        st.young_live = len(to_space)
        st.old_live = len(self.old)
        dt = time.perf_counter() - t0
        st.minor_pause_s += dt
        if dt > st.max_pause_s:
            st.max_pause_s = dt

        # 5. old generation too big? then it is time for a major collection
        if len(self.old) > self.old_threshold:
            self.collect_major()
            self.old_threshold = max(self.old_threshold, len(self.old) * 2)

    # ------------------------------------------------------------------
    # major collection: mark & sweep over the old generation
    # ------------------------------------------------------------------
    def collect_major(self):
        t0 = time.perf_counter()

        # mark: full trace from the roots (young objects are traversed but
        # never swept here)
        stack = []
        for container, key in self._root_slots():
            v = container[key]
            if isinstance(v, HCell):
                stack.append(v)
        while stack:
            obj = stack.pop()
            if obj.marked:
                continue
            obj.marked = True
            for container, key in _pointer_slots(obj):
                v = container[key]
                if isinstance(v, HCell) and not v.marked:
                    stack.append(v)

        # sweep old generation
        survivors = []
        for obj in self.old:
            if obj.marked:
                obj.marked = False
                survivors.append(obj)
            else:
                self._finalize(obj)
        self.old = survivors

        # clear mark bits left on young objects and drop dead remembered
        for obj in self.young:
            obj.marked = False
        live_old = set(survivors)
        self.remembered = {o for o in self.remembered if o in live_old}

        st = self.stats
        st.major_collections += 1
        st.old_live = len(self.old)
        dt = time.perf_counter() - t0
        st.major_pause_s += dt
        if dt > st.max_pause_s:
            st.max_pause_s = dt

    def full_collect(self):
        """Minor + major: after this, exactly the reachable objects remain."""
        self.collect_minor()
        self.collect_major()

    # ------------------------------------------------------------------
    # finalization (resource release hooks)
    # ------------------------------------------------------------------
    def _finalize(self, obj):
        fn = obj.finalizer
        if fn is not None:
            obj.finalizer = None
            self.stats.finalized += 1
            fn(obj)

    def set_finalizer(self, obj, fn):
        obj.finalizer = fn

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def live_objects(self):
        return len(self.young) + len(self.old)
