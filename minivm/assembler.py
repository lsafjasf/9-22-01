"""A tiny text assembler for the mini VM instruction set.

Format (line based, '#' starts a comment):

    fn main 0 2                 # name, nargs, nlocals
      CONST 10                  # literals are auto-interned in the const pool
      STORE_LOCAL 0
    loop:                       # label
      LOAD_LOCAL 0
      CONST 0
      GT
      JUMP_IF_FALSE done
      LOAD_GLOBAL "print"       # strings need quotes
      LOAD_LOCAL 0
      CALL 1
      POP
      LOAD_LOCAL 0
      CONST 1
      SUB
      STORE_LOCAL 0
      JUMP loop
    done:
      CONST null
      RETURN
    end

    fn make_counter 0 1
      CONST 0
      STORE_LOCAL 0
      CLOSURE tick L0           # capture parent local 0 (U<n> = parent upvalue)
      RETURN
    end

Literals: integers, floats, "strings" (quotes required), true, false, null.
"""

import ast
import shlex

from .bytecode import Op, Function, Program, CAP_LOCAL, CAP_UPVALUE

_OPS_WITH_CONST = {
    "CONST", "LOAD_GLOBAL", "STORE_GLOBAL", "GET_FIELD", "SET_FIELD",
}
_OPS_WITH_JUMP = {"JUMP", "JUMP_IF_FALSE", "JUMP_IF_TRUE", "TRAP"}
_OPS_WITH_INT = {
    "LOAD_LOCAL", "STORE_LOCAL", "LOAD_UPVALUE", "STORE_UPVALUE", "CALL",
}
_OPS_NO_ARG = {
    "RETURN", "UNTRAP", "RAISE", "NEW_OBJ", "NEW_ARRAY", "ARR_PUSH",
    "GET_INDEX", "SET_INDEX", "LEN", "ADD", "SUB", "MUL", "DIV", "MOD",
    "EQ", "NE", "LT", "LE", "GT", "GE", "NOT", "NEG", "POP", "DUP", "HALT",
}


def _parse_literal(tok):
    if tok.startswith('"') or tok.startswith("'"):
        return ast.literal_eval(tok)
    if tok == "true":
        return True
    if tok == "false":
        return False
    if tok == "null":
        return None
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        pass
    raise ValueError("bad literal: %r (strings need quotes)" % tok)


class _FnBuilder:
    def __init__(self, name, nargs, nlocals):
        self.name = name
        self.nargs = nargs
        self.nlocals = nlocals
        self.constants = []
        self.const_map = {}
        self.labels = {}
        self.raw = []  # (opname, args, lineno)

    def intern(self, value):
        key = (type(value).__name__, value)
        if key in self.const_map:
            return self.const_map[key]
        idx = len(self.constants)
        self.constants.append(value)
        self.const_map[key] = idx
        return idx

    def add(self, opname, args, lineno):
        if opname in _OPS_NO_ARG | _OPS_WITH_INT | _OPS_WITH_CONST | _OPS_WITH_JUMP \
                or opname == "CLOSURE":
            self.raw.append((opname, args, lineno))
        else:
            raise ValueError("%s:%d: unknown op %r" % (self.name, lineno, opname))

    def build(self, fn_index):
        code = []
        for opname, args, lineno in self.raw:
            op = Op[opname]
            if opname in _OPS_NO_ARG:
                code.append((op,))
            elif opname in _OPS_WITH_INT:
                code.append((op, int(args[0])))
            elif opname in _OPS_WITH_CONST:
                code.append((op, self.intern(_parse_literal(args[0]))))
            elif opname in _OPS_WITH_JUMP:
                label = args[0]
                if label not in self.labels:
                    raise ValueError("%s:%d: unknown label %r"
                                     % (self.name, lineno, label))
                code.append((op, self.labels[label]))
            elif opname == "CLOSURE":
                target = args[0]
                if target not in fn_index:
                    raise ValueError("%s:%d: unknown function %r"
                                     % (self.name, lineno, target))
                caps = []
                for spec in args[1:]:
                    kind, num = spec[0].upper(), int(spec[1:])
                    if kind == "L":
                        caps.append((CAP_LOCAL, num))
                    elif kind == "U":
                        caps.append((CAP_UPVALUE, num))
                    else:
                        raise ValueError("%s:%d: bad capture %r"
                                         % (self.name, lineno, spec))
                code.append((op, fn_index[target], tuple(caps)))
        return Function(self.name, self.nargs, self.nlocals,
                        self.constants, code)


def assemble(text):
    builders = []
    cur = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        try:
            toks = shlex.split(raw, comments=True, posix=False)
        except ValueError as exc:
            raise ValueError("line %d: %s" % (lineno, exc))
        if not toks:
            continue
        head = toks[0]
        if head == "fn":
            if cur is not None:
                raise ValueError("line %d: nested fn" % lineno)
            cur = _FnBuilder(toks[1], int(toks[2]), int(toks[3]))
            builders.append(cur)
            continue
        if head == "end":
            if cur is None:
                raise ValueError("line %d: 'end' outside fn" % lineno)
            cur = None
            continue
        if cur is None:
            raise ValueError("line %d: instruction outside fn" % lineno)
        if head.endswith(":"):
            cur.labels[head[:-1]] = len(cur.raw)
            toks = toks[1:]
            if not toks:
                continue
        cur.add(toks[0].upper(), toks[1:], lineno)
    if cur is not None:
        raise ValueError("unterminated fn %r" % cur.name)
    fn_index = {b.name: i for i, b in enumerate(builders)}
    return Program([b.build(fn_index) for b in builders])
