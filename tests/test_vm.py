"""Tests for the bytecode VM: closures, traps, objects, GC interaction."""

import unittest

from minivm import assemble, VM, Heap, UncaughtError, to_str
from minivm.objects import HObj, HArray, HStr


def run(src, **heap_kw):
    vm = VM(assemble(src), Heap(**heap_kw))
    return vm, vm.run()


class ArithmeticTest(unittest.TestCase):
    def test_arithmetic(self):
        _, res = run("""
fn main 0 0
  CONST 6
  CONST 7
  MUL
  RETURN
end
""")
        self.assertEqual(res, 42)

    def test_string_concat(self):
        _, res = run("""
fn main 0 0
  CONST "foo"
  CONST "bar"
  ADD
  RETURN
end
""")
        self.assertIsInstance(res, HStr)
        self.assertEqual(res.s, "foobar")

    def test_comparisons_and_jumps(self):
        _, res = run("""
fn main 0 1
  CONST 0
  STORE_LOCAL 0
loop:
  LOAD_LOCAL 0
  CONST 5
  LT
  JUMP_IF_FALSE done
  LOAD_LOCAL 0
  CONST 2
  ADD
  STORE_LOCAL 0
  JUMP loop
done:
  LOAD_LOCAL 0
  RETURN
end
""")
        self.assertEqual(res, 6)


class ClosureTest(unittest.TestCase):
    SRC = """
fn main 0 2
  CLOSURE make_counter
  CALL 0
  STORE_LOCAL 0
  CLOSURE make_counter
  CALL 0
  STORE_LOCAL 1
  LOAD_LOCAL 0
  CALL 0
  POP
  LOAD_LOCAL 0
  CALL 0
  POP
  LOAD_LOCAL 1
  CALL 0
  POP
  LOAD_LOCAL 0
  CALL 0
  CONST 100
  MUL
  LOAD_LOCAL 1
  CALL 0
  ADD
  RETURN
end
fn make_counter 0 1
  CONST 0
  STORE_LOCAL 0
  CLOSURE tick L0
  RETURN
end
fn tick 0 1
  LOAD_UPVALUE 0
  CONST 1
  ADD
  STORE_UPVALUE 0
  LOAD_UPVALUE 0
  RETURN
end
"""

    def test_independent_counters(self):
        _, res = run(self.SRC)
        self.assertEqual(res, 3 * 100 + 2)

    def test_shared_upvalue_between_siblings(self):
        # getter and setter capture the *same* local of make_cell
        _, res = run("""
fn main 0 3
  CLOSURE make_cell
  CALL 0
  STORE_LOCAL 0          # getter
  LOAD_GLOBAL "setter_of_cell"
  STORE_LOCAL 1
  LOAD_LOCAL 1
  CONST 99
  CALL 1
  POP
  LOAD_LOCAL 0
  CALL 0
  RETURN
end
fn make_cell 0 1
  CONST 0
  STORE_LOCAL 0
  CLOSURE set_impl L0
  STORE_GLOBAL "setter_of_cell"
  CLOSURE get_impl L0
  RETURN
end
fn get_impl 0 1
  LOAD_UPVALUE 0
  RETURN
end
fn set_impl 1 1
  LOAD_LOCAL 0
  STORE_UPVALUE 0
  CONST null
  RETURN
end
""")
        self.assertEqual(res, 99)


class TrapTest(unittest.TestCase):
    def test_division_by_zero_caught(self):
        _, res = run("""
fn main 0 0
  TRAP handler
  CONST 1
  CONST 0
  DIV
  POP
  UNTRAP
  CONST "no error"
  RETURN
handler:
  RETURN
end
""")
        self.assertEqual(to_str(res), "division by zero")

    def test_raise_value(self):
        _, res = run("""
fn main 0 0
  TRAP handler
  CONST "my-error"
  RAISE
handler:
  RETURN
end
""")
        self.assertEqual(to_str(res), "my-error")

    def test_uncaught_error(self):
        with self.assertRaises(UncaughtError):
            run("""
fn main 0 0
  CONST 1
  CONST 0
  DIV
  RETURN
end
""")

    def test_unwind_restores_stack(self):
        # error deep in a call unwinds frames and the value stack
        _, res = run("""
fn main 0 1
  TRAP handler
  CLOSURE deep
  CALL 0
  POP
  UNTRAP
  CONST "no error"
  RETURN
handler:
  POP
  CONST 7
  CONST 8
  ADD
  RETURN
end
fn deep 0 0
  CONST 5
  CONST 0
  DIV
  RETURN
end
""")
        # error unwound to the handler; the stack was restored and the
        # handler computed 7 + 8 = 15
        self.assertEqual(res, 15)


class ObjectArrayTest(unittest.TestCase):
    def test_fields_and_index(self):
        _, res = run("""
fn main 0 2
  NEW_OBJ
  STORE_LOCAL 0
  LOAD_LOCAL 0
  CONST 41
  SET_FIELD "x"
  POP
  NEW_ARRAY
  STORE_LOCAL 1
  LOAD_LOCAL 1
  LOAD_LOCAL 0
  GET_FIELD "x"
  ARR_PUSH
  POP
  LOAD_LOCAL 1
  CONST 1
  ARR_PUSH
  POP
  LOAD_LOCAL 1
  CONST 0
  GET_INDEX
  LOAD_LOCAL 1
  CONST 1
  GET_INDEX
  ADD
  RETURN
end
""")
        self.assertEqual(res, 42)

    def test_index_out_of_range_raises(self):
        with self.assertRaises(UncaughtError):
            run("""
fn main 0 1
  NEW_ARRAY
  STORE_LOCAL 0
  LOAD_LOCAL 0
  CONST 5
  GET_INDEX
  RETURN
end
""")


class GCInteractionTest(unittest.TestCase):
    def test_tiny_young_gen_stress(self):
        """Force a minor GC on nearly every allocation: all live values
        (stack temporaries, locals, constants) must survive intact."""
        _, res = run("""
fn main 0 3
  NEW_ARRAY
  STORE_LOCAL 0
  CONST 0
  STORE_LOCAL 1
loop:
  LOAD_LOCAL 1
  CONST 50
  LT
  JUMP_IF_FALSE done
  LOAD_LOCAL 0
  LOAD_LOCAL 1
  CONST 2
  MUL
  ARR_PUSH
  POP
  LOAD_LOCAL 1
  CONST 1
  ADD
  STORE_LOCAL 1
  JUMP loop
done:
  CONST 0
  STORE_LOCAL 2
  CONST 0
  STORE_LOCAL 1
sum:
  LOAD_LOCAL 1
  CONST 50
  LT
  JUMP_IF_FALSE end
  LOAD_LOCAL 2
  LOAD_LOCAL 0
  LOAD_LOCAL 1
  GET_INDEX
  ADD
  STORE_LOCAL 2
  LOAD_LOCAL 1
  CONST 1
  ADD
  STORE_LOCAL 1
  JUMP sum
end:
  LOAD_LOCAL 2
  RETURN
end
""", young_size=8, old_threshold=32)
        self.assertEqual(res, sum(range(0, 100, 2)))  # 2450

    def test_constants_survive_gc(self):
        _, res = run("""
fn main 0 2
  CONST 0
  STORE_LOCAL 0
loop:
  LOAD_LOCAL 0
  CONST 100
  LT
  JUMP_IF_FALSE done
  LOAD_GLOBAL "str"
  CONST "prefix-"
  CALL 1
  POP
  LOAD_LOCAL 0
  CONST 1
  ADD
  STORE_LOCAL 0
  JUMP loop
done:
  CONST "prefix-"
  CONST "suffix"
  ADD
  RETURN
end
""", young_size=4, old_threshold=16)
        self.assertEqual(to_str(res), "prefix-suffix")

    def test_closure_state_survives_gc(self):
        # captured variable must survive many minor GCs (closed upvalue
        # holding a heap value, old-to-young via STORE_UPVALUE barrier)
        vm, res = run("""
fn main 0 2
  CLOSURE make_acc
  CALL 0
  STORE_LOCAL 0
  CONST 0
  STORE_LOCAL 1
loop:
  LOAD_LOCAL 1
  CONST 200
  LT
  JUMP_IF_FALSE done
  LOAD_LOCAL 0
  NEW_OBJ
  CALL 1
  POP
  LOAD_LOCAL 1
  CONST 1
  ADD
  STORE_LOCAL 1
  JUMP loop
done:
  LOAD_LOCAL 0
  CONST null
  CALL 1
  LEN
  RETURN
end
fn make_acc 0 1
  NEW_ARRAY
  STORE_LOCAL 0
  CLOSURE acc L0
  RETURN
end
fn acc 1 1
  LOAD_UPVALUE 0
  LOAD_LOCAL 0
  ARR_PUSH
  POP
  LOAD_UPVALUE 0
  RETURN
end
""", young_size=16, old_threshold=64)
        self.assertEqual(res, 201)


class LongRunTest(unittest.TestCase):
    def test_heap_does_not_grow(self):
        """The long-running example: bounded live set, all release hooks
        fire, memory does not grow without bound."""
        with open("examples/longrun.asm", encoding="utf-8") as f:
            program = assemble(f.read())
        vm = VM(program, Heap(young_size=1024, old_threshold=4096))
        vm.run()
        vm.heap.full_collect()
        st = vm.heap.stats
        # 100000 iterations, finalizer on every 97th object
        self.assertEqual(st.finalized, 100000 // 97 + 1)
        # live set stays tiny although 200k objects were allocated
        self.assertLess(st.peak_objects, 3 * 1024)
        self.assertLess(vm.heap.live_objects(), 512)
        self.assertGreater(st.minor_collections, 100)
        self.assertGreater(st.major_collections, 0)


if __name__ == "__main__":
    unittest.main()
