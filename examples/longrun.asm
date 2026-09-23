# Long-running allocation churn: proves the heap does not grow without
# bound.  Each iteration allocates a cyclic object plus an array; only
# the last 64 arrays are kept (ring buffer), everything else is garbage.
# Every 97th object gets a release hook; every 5000th iteration forces
# a full GC from the script.
fn main 0 6
  CONST 100000
  STORE_LOCAL 4          # limit
  NEW_ARRAY
  STORE_LOCAL 1          # ring
  CONST 0
  STORE_LOCAL 5          # j
fill:
  LOAD_LOCAL 5
  CONST 64
  LT
  JUMP_IF_FALSE filled
  LOAD_LOCAL 1
  CONST null
  ARR_PUSH
  POP
  LOAD_LOCAL 5
  CONST 1
  ADD
  STORE_LOCAL 5
  JUMP fill
filled:
  CONST 0
  STORE_LOCAL 0          # i
loop:
  LOAD_LOCAL 0
  LOAD_LOCAL 4
  LT
  JUMP_IF_FALSE done
  NEW_OBJ
  STORE_LOCAL 2          # obj
  LOAD_LOCAL 2
  LOAD_LOCAL 0
  SET_FIELD "x"
  POP
  LOAD_LOCAL 2
  LOAD_LOCAL 2
  SET_FIELD "self"       # a cycle: obj -> obj
  POP
  NEW_ARRAY
  STORE_LOCAL 3          # arr = [i, obj]
  LOAD_LOCAL 3
  LOAD_LOCAL 0
  ARR_PUSH
  POP
  LOAD_LOCAL 3
  LOAD_LOCAL 2
  ARR_PUSH
  POP
  LOAD_LOCAL 0
  CONST 97
  MOD
  CONST 0
  EQ
  JUMP_IF_FALSE skip_fin
  LOAD_GLOBAL "on_finalize"
  LOAD_LOCAL 2
  CONST "garbage-object"
  CALL 2
  POP
skip_fin:
  LOAD_LOCAL 1
  LOAD_LOCAL 0
  CONST 64
  MOD
  LOAD_LOCAL 3
  SET_INDEX              # ring[i % 64] = arr (old <- young: barrier)
  POP
  LOAD_LOCAL 0
  CONST 5000
  MOD
  CONST 0
  EQ
  JUMP_IF_FALSE no_gc
  LOAD_GLOBAL "gc"
  CALL 0
  POP
no_gc:
  LOAD_LOCAL 0
  CONST 1
  ADD
  STORE_LOCAL 0
  JUMP loop
done:
  LOAD_GLOBAL "print"
  CONST "iterations done; finalized objects:"
  LOAD_GLOBAL "finalized_count"
  CALL 0
  CALL 2
  POP
  LOAD_GLOBAL "print"
  CONST "heap objects still live:"
  LOAD_GLOBAL "heap_objects"
  CALL 0
  CALL 2
  POP
  CONST null
  RETURN
end
