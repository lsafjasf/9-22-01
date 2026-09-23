"""Stack-based bytecode virtual machine.

Values: ints, floats, bools and None are immediates; objects, arrays,
strings, closures and upvalues are heap-allocated HCell instances managed
by the generational GC in heap.py.

GC roots (registered with the heap as (container, key) slots so the
copying collector can rewrite them):
  * the value stack (temporaries, call frames' locals and callee slots)
  * the globals table
  * every function's constant pool
  * the list of open upvalues
"""

from .bytecode import Op, CAP_LOCAL
from .heap import Heap
from .objects import HObj, HArray, HStr, HClosure, HUpvalue


class VMError(Exception):
    """A script-level runtime error (can be caught with TRAP)."""


class _Raise(Exception):
    def __init__(self, value):
        super().__init__("script raise")
        self.value = value


class UncaughtError(Exception):
    def __init__(self, value):
        super().__init__("uncaught script error: %s" % to_str(value))
        self.value = value


class Frame:
    __slots__ = ("closure", "ip", "base")

    def __init__(self, closure, ip, base):
        self.closure = closure
        self.ip = ip
        self.base = base


def to_str(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, HStr):
        return v.s
    if isinstance(v, HObj):
        return "<object>"
    if isinstance(v, HArray):
        return "<array:%d>" % len(v.items)
    if isinstance(v, HClosure):
        return "<closure %s>" % v.fn.name
    if isinstance(v, HUpvalue):
        return "<upvalue>"
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return str(v)


def _truthy(v):
    if v is None or v is False:
        return False
    if v == 0:
        return False
    if isinstance(v, HStr) and v.s == "":
        return False
    return True


class VM:
    def __init__(self, program, heap=None):
        self.program = program
        self.heap = heap if heap is not None else Heap()
        self.heap.root_provider = self._root_slots
        self.stack = []
        self.frames = []
        self.handlers = []        # (frame_depth, stack_depth, handler_ip)
        self.open_upvalues = []   # list[HUpvalue], open ones only
        self.globals = {}
        self.finalized_log = []
        self._intern_constants()
        self._install_natives()

    # ------------------------------------------------------------------
    # GC roots
    # ------------------------------------------------------------------
    def _root_slots(self):
        for i in range(len(self.stack)):
            yield (self.stack, i)
        for k in self.globals:
            yield (self.globals, k)
        for fn in self.program.functions:
            for i in range(len(fn.constants)):
                yield (fn.constants, i)
        for i in range(len(self.open_upvalues)):
            yield (self.open_upvalues, i)

    def _intern_constants(self):
        # wrap string literals of every constant pool as heap strings
        for fn in self.program.functions:
            for i, c in enumerate(fn.constants):
                if isinstance(c, str):
                    fn.constants[i] = self.heap.alloc(HStr(c))

    # ------------------------------------------------------------------
    # natives
    # ------------------------------------------------------------------
    def _install_natives(self):
        def n_print(vm, args):
            print(" ".join(to_str(a) for a in args))
            return None

        def n_gc(vm, args):
            vm.heap.full_collect()
            return None

        def n_on_finalize(vm, args):
            obj, tag = args
            if not isinstance(obj, (HObj, HArray, HClosure)):
                raise VMError("on_finalize: not a heap object")
            tag_s = to_str(tag)
            vm.heap.set_finalizer(
                obj, lambda o, t=tag_s: vm.finalized_log.append(t))
            return None

        def n_finalized_count(vm, args):
            return vm.heap.stats.finalized

        def n_heap_objects(vm, args):
            return vm.heap.live_objects()

        def n_str(vm, args):
            return self.heap.alloc(HStr(to_str(args[0])))

        self.globals.update({
            "print": n_print,
            "gc": n_gc,
            "on_finalize": n_on_finalize,
            "finalized_count": n_finalized_count,
            "heap_objects": n_heap_objects,
            "str": n_str,
        })

    # ------------------------------------------------------------------
    # upvalues
    # ------------------------------------------------------------------
    def _capture(self, index):
        for uv in self.open_upvalues:
            if uv.location == index:
                return uv
        uv = self.heap.alloc(HUpvalue(index))
        self.open_upvalues.append(uv)
        return uv

    def _close_upvalues(self, base):
        keep = []
        for uv in self.open_upvalues:
            if uv.location >= base:
                uv.value = self.stack[uv.location]
                uv.closed = True
                uv.location = -1
            else:
                keep.append(uv)
        self.open_upvalues = keep

    # ------------------------------------------------------------------
    # calls
    # ------------------------------------------------------------------
    def _enter(self, closure, argc):
        base = len(self.stack) - argc
        if argc != closure.fn.nargs:
            raise VMError("%s expects %d args, got %d"
                          % (closure.fn.name, closure.fn.nargs, argc))
        self.frames.append(Frame(closure, 0, base))
        for _ in range(closure.fn.nlocals - argc):
            self.stack.append(None)

    def run(self):
        """Run main() and return its result."""
        main_closure = self.heap.alloc(HClosure(self.program.main, []))
        self.stack.append(main_closure)
        self._enter(main_closure, 0)
        try:
            return self._run()
        except _Raise as r:
            raise UncaughtError(r.value)
        except VMError as e:
            raise UncaughtError(self.heap.alloc(HStr(str(e))))

    def _unwind(self, value):
        if not self.handlers:
            raise _Raise(value)
        fdepth, sdepth, ip = self.handlers.pop()
        while len(self.frames) > fdepth:
            self._close_upvalues(self.frames[-1].base)
            self.frames.pop()
        del self.stack[sdepth:]
        self.stack.append(value)
        self.frames[-1].ip = ip

    # ------------------------------------------------------------------
    # main dispatch loop
    # ------------------------------------------------------------------
    def _run(self):
        stack = self.stack
        heap = self.heap
        while self.frames:
            frame = self.frames[-1]
            ins = frame.closure.fn.code[frame.ip]
            frame.ip += 1
            op = ins[0]
            try:
                if op is Op.CONST:
                    stack.append(frame.closure.fn.constants[ins[1]])
                elif op is Op.LOAD_LOCAL:
                    stack.append(stack[frame.base + ins[1]])
                elif op is Op.STORE_LOCAL:
                    stack[frame.base + ins[1]] = stack.pop()
                elif op is Op.LOAD_UPVALUE:
                    uv = frame.closure.upvalues[ins[1]]
                    stack.append(uv.value if uv.closed else stack[uv.location])
                elif op is Op.STORE_UPVALUE:
                    uv = frame.closure.upvalues[ins[1]]
                    v = stack.pop()
                    if uv.closed:
                        uv.value = v
                        heap.write_barrier(uv, v)
                    else:
                        stack[uv.location] = v
                elif op is Op.LOAD_GLOBAL:
                    name = frame.closure.fn.constants[ins[1]]
                    stack.append(self.globals.get(name.s))
                elif op is Op.STORE_GLOBAL:
                    name = frame.closure.fn.constants[ins[1]]
                    self.globals[name.s] = stack.pop()
                elif op is Op.CLOSURE:
                    fn = self.program.functions[ins[1]]
                    uvs = []
                    for kind, idx in ins[2]:
                        if kind == CAP_LOCAL:
                            uvs.append(self._capture(frame.base + idx))
                        else:
                            uvs.append(frame.closure.upvalues[idx])
                    stack.append(heap.alloc(HClosure(fn, uvs)))
                elif op is Op.CALL:
                    argc = ins[1]
                    callee = stack[-1 - argc]
                    if isinstance(callee, HClosure):
                        self._enter(callee, argc)
                    elif callable(callee):
                        args = stack[len(stack) - argc:]
                        del stack[len(stack) - argc - 1:]
                        stack.append(callee(self, args))
                    else:
                        raise VMError("cannot call %s" % to_str(callee))
                elif op is Op.RETURN:
                    result = stack.pop()
                    self._close_upvalues(frame.base)
                    self.frames.pop()
                    del stack[frame.base - 1:]
                    stack.append(result)
                    if not self.frames:
                        return result
                elif op is Op.HALT:
                    return stack.pop() if stack else None
                elif op is Op.JUMP:
                    frame.ip = ins[1]
                elif op is Op.JUMP_IF_FALSE:
                    if not _truthy(stack.pop()):
                        frame.ip = ins[1]
                elif op is Op.JUMP_IF_TRUE:
                    if _truthy(stack.pop()):
                        frame.ip = ins[1]
                elif op is Op.TRAP:
                    self.handlers.append((len(self.frames), len(stack), ins[1]))
                elif op is Op.UNTRAP:
                    self.handlers.pop()
                elif op is Op.RAISE:
                    self._unwind(stack.pop())
                elif op is Op.NEW_OBJ:
                    stack.append(heap.alloc(HObj()))
                elif op is Op.NEW_ARRAY:
                    stack.append(heap.alloc(HArray()))
                elif op is Op.ARR_PUSH:
                    v = stack.pop()
                    arr = stack.pop()
                    if not isinstance(arr, HArray):
                        raise VMError("ARR_PUSH on non-array")
                    arr.items.append(v)
                    heap.write_barrier(arr, v)
                    stack.append(arr)
                elif op is Op.GET_FIELD:
                    obj = stack.pop()
                    name = frame.closure.fn.constants[ins[1]]
                    if not isinstance(obj, HObj):
                        raise VMError("GET_FIELD on non-object")
                    stack.append(obj.fields.get(name.s))
                elif op is Op.SET_FIELD:
                    v = stack.pop()
                    obj = stack.pop()
                    name = frame.closure.fn.constants[ins[1]]
                    if not isinstance(obj, HObj):
                        raise VMError("SET_FIELD on non-object")
                    obj.fields[name.s] = v
                    heap.write_barrier(obj, v)
                    stack.append(v)
                elif op is Op.GET_INDEX:
                    idx = stack.pop()
                    arr = stack.pop()
                    stack.append(self._get_index(arr, idx))
                elif op is Op.SET_INDEX:
                    v = stack.pop()
                    idx = stack.pop()
                    arr = stack.pop()
                    if not isinstance(arr, HArray):
                        raise VMError("SET_INDEX on non-array")
                    if not isinstance(idx, int) or not 0 <= idx < len(arr.items):
                        raise VMError("array index out of range")
                    arr.items[idx] = v
                    heap.write_barrier(arr, v)
                    stack.append(v)
                elif op is Op.LEN:
                    v = stack.pop()
                    if isinstance(v, (HArray, HStr)):
                        stack.append(len(v.items) if isinstance(v, HArray)
                                     else len(v.s))
                    elif isinstance(v, HObj):
                        stack.append(len(v.fields))
                    else:
                        raise VMError("LEN of %s" % to_str(v))
                elif op in _BINOPS:
                    b = stack.pop()
                    a = stack.pop()
                    stack.append(self._binop(op, a, b))
                elif op is Op.NOT:
                    stack.append(not _truthy(stack.pop()))
                elif op is Op.NEG:
                    a = stack.pop()
                    if not isinstance(a, (int, float)) or isinstance(a, bool):
                        raise VMError("NEG of non-number")
                    stack.append(-a)
                elif op is Op.POP:
                    stack.pop()
                elif op is Op.DUP:
                    stack.append(stack[-1])
                else:
                    raise VMError("bad opcode %r" % (op,))
            except _Raise:
                raise
            except VMError as e:
                self._unwind(heap.alloc(HStr(str(e))))
        return None

    # ------------------------------------------------------------------
    # operators
    # ------------------------------------------------------------------
    def _get_index(self, arr, idx):
        if isinstance(arr, HArray):
            if not isinstance(idx, int) or not 0 <= idx < len(arr.items):
                raise VMError("array index out of range")
            return arr.items[idx]
        if isinstance(arr, HStr):
            if not isinstance(idx, int) or not 0 <= idx < len(arr.s):
                raise VMError("string index out of range")
            return self.heap.alloc(HStr(arr.s[idx]))
        raise VMError("GET_INDEX on non-array")

    def _binop(self, op, a, b):
        if op is Op.ADD:
            if isinstance(a, HStr) and isinstance(b, HStr):
                return self.heap.alloc(HStr(a.s + b.s))
            if _is_num(a) and _is_num(b):
                return a + b
            raise VMError("cannot add %s and %s" % (to_str(a), to_str(b)))
        if op in (Op.SUB, Op.MUL, Op.DIV, Op.MOD):
            if not (_is_num(a) and _is_num(b)):
                raise VMError("arithmetic on non-number")
            if op is Op.SUB:
                return a - b
            if op is Op.MUL:
                return a * b
            if b == 0:
                raise VMError("division by zero")
            if op is Op.DIV:
                if isinstance(a, int) and isinstance(b, int):
                    return a // b
                return a / b
            return a % b
        if op is Op.EQ:
            return _equals(a, b)
        if op is Op.NE:
            return not _equals(a, b)
        if isinstance(a, HStr) and isinstance(b, HStr):
            x, y = a.s, b.s
        elif _is_num(a) and _is_num(b):
            x, y = a, b
        else:
            raise VMError("cannot compare %s and %s" % (to_str(a), to_str(b)))
        if op is Op.LT:
            return x < y
        if op is Op.LE:
            return x <= y
        if op is Op.GT:
            return x > y
        return x >= y


_BINOPS = {Op.ADD, Op.SUB, Op.MUL, Op.DIV, Op.MOD,
           Op.EQ, Op.NE, Op.LT, Op.LE, Op.GT, Op.GE}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _equals(a, b):
    if isinstance(a, HStr) and isinstance(b, HStr):
        return a.s == b.s
    if _is_num(a) and _is_num(b):
        return a == b
    if a is None or b is None or isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a is b
