"""Instruction set and code objects for the mini VM.

The VM is stack based.  Each function has its own constant pool; heap
objects referenced from constant pools (strings) are traced as GC roots.

Instruction encoding: ``code`` is a list of tuples ``(op, *args)``.

  CONST k                 push constants[k]
  LOAD_LOCAL i            push local i
  STORE_LOCAL i           pop into local i
  LOAD_UPVALUE i          push captured variable i of current closure
  STORE_UPVALUE i         pop into captured variable i
  LOAD_GLOBAL k           push globals[constants[k]]   (name is a string const)
  STORE_GLOBAL k          pop into globals[constants[k]]
  CLOSURE f [(src,i)...]  build closure of function f capturing locals (L)
                          or parent upvalues (U); pushed on stack
  CALL n                  call callable (closure or native) with n args
  RETURN                  return top of stack to caller (halt if main)
  JUMP a                  ip = a
  JUMP_IF_FALSE a         pop; if falsy ip = a
  JUMP_IF_TRUE a          pop; if truthy ip = a
  TRAP a                  push error handler (frame depth, stack depth, a)
  UNTRAP                  pop error handler
  RAISE                   pop value and raise it as a script error
  NEW_OBJ                 push new object
  NEW_ARRAY               push new empty array
  ARR_PUSH                array, value -> append value to array
  GET_FIELD k             obj -> obj[constants[k]]
  SET_FIELD k             obj, value -> obj[constants[k]] = value
  GET_INDEX               array, index -> array[index]
  SET_INDEX               array, index, value -> array[index] = value
  LEN                     push len(array/string/object)
  ADD SUB MUL DIV MOD     arithmetic
  EQ NE LT LE GT GE       comparison
  NOT NEG                 unary logic / arithmetic
  POP DUP                 stack juggling
  HALT                    stop the machine
"""

from enum import IntEnum


class Op(IntEnum):
    CONST = 0
    LOAD_LOCAL = 1
    STORE_LOCAL = 2
    LOAD_UPVALUE = 3
    STORE_UPVALUE = 4
    LOAD_GLOBAL = 5
    STORE_GLOBAL = 6
    CLOSURE = 7
    CALL = 8
    RETURN = 9
    JUMP = 10
    JUMP_IF_FALSE = 11
    JUMP_IF_TRUE = 12
    TRAP = 13
    UNTRAP = 14
    RAISE = 15
    NEW_OBJ = 16
    NEW_ARRAY = 17
    ARR_PUSH = 18
    GET_FIELD = 19
    SET_FIELD = 20
    GET_INDEX = 21
    SET_INDEX = 22
    LEN = 23
    ADD = 24
    SUB = 25
    MUL = 26
    DIV = 27
    MOD = 28
    EQ = 29
    NE = 30
    LT = 31
    LE = 32
    GT = 33
    GE = 34
    NOT = 35
    NEG = 36
    POP = 37
    DUP = 38
    HALT = 39


# capture spec for CLOSURE: source is a parent local or a parent upvalue
CAP_LOCAL = 0
CAP_UPVALUE = 1


class Function:
    __slots__ = ("name", "nargs", "nlocals", "constants", "code")

    def __init__(self, name, nargs, nlocals, constants, code):
        self.name = name
        self.nargs = nargs
        self.nlocals = nlocals
        self.constants = constants  # constant pool (may hold heap refs)
        self.code = code

    def disassemble(self):
        out = ["fn %s/%d (locals=%d)" % (self.name, self.nargs, self.nlocals)]
        for i, ins in enumerate(self.code):
            out.append("  %4d  %s" % (i, " ".join(str(x) for x in ins)))
        return "\n".join(out)


class Program:
    def __init__(self, functions):
        self.functions = functions
        self.by_name = {f.name: i for i, f in enumerate(functions)}
        if "main" not in self.by_name:
            raise ValueError("program has no 'main' function")

    @property
    def main(self):
        return self.functions[self.by_name["main"]]
