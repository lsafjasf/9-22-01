# Cyclic garbage: two objects reference each other; once the root is
# dropped the cycle is unreachable and the GC releases it (hook runs).
fn main 0 3
  CONST 0
  STORE_LOCAL 2          # i
loop:
  LOAD_LOCAL 2
  CONST 5
  LT
  JUMP_IF_FALSE done
  NEW_OBJ
  STORE_LOCAL 0          # a
  NEW_OBJ
  STORE_LOCAL 1          # b
  LOAD_LOCAL 0
  LOAD_LOCAL 1
  SET_FIELD "peer"
  POP
  LOAD_LOCAL 1
  LOAD_LOCAL 0
  SET_FIELD "peer"
  POP
  LOAD_GLOBAL "on_finalize"
  LOAD_LOCAL 0
  CONST "a-cycle"
  CALL 2
  POP
  LOAD_GLOBAL "on_finalize"
  LOAD_LOCAL 1
  CONST "b-cycle"
  CALL 2
  POP
  CONST null
  STORE_LOCAL 0          # drop the only root into the cycle
  CONST null
  STORE_LOCAL 1
  LOAD_LOCAL 2
  CONST 1
  ADD
  STORE_LOCAL 2
  JUMP loop
done:
  LOAD_GLOBAL "gc"
  CALL 0
  POP
  LOAD_GLOBAL "print"
  CONST "finalized after full gc:"
  LOAD_GLOBAL "finalized_count"
  CALL 0
  CALL 2
  POP
  CONST null
  RETURN
end
