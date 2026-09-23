# Cross-generation references: `holder` is promoted to the old
# generation; young objects stored into it afterwards must survive
# minor collections (write barrier + remembered set).
fn main 0 4
  NEW_OBJ
  STORE_LOCAL 0          # holder
  LOAD_GLOBAL "gc"
  CALL 0
  POP
  LOAD_GLOBAL "gc"
  CALL 0
  POP                    # holder is old now
  CONST 0
  STORE_LOCAL 1          # i
loop:
  LOAD_LOCAL 1
  CONST 1000
  LT
  JUMP_IF_FALSE done
  NEW_OBJ
  STORE_LOCAL 2          # young child
  LOAD_LOCAL 2
  LOAD_LOCAL 1
  SET_FIELD "n"
  POP
  LOAD_LOCAL 0
  LOAD_LOCAL 2
  SET_FIELD "slot"       # old <- young store: write barrier fires
  POP
  CONST null
  STORE_LOCAL 2          # drop the direct root; only holder keeps it
  LOAD_LOCAL 1
  CONST 1
  ADD
  STORE_LOCAL 1
  JUMP loop
done:
  LOAD_GLOBAL "gc"
  CALL 0
  POP
  LOAD_GLOBAL "print"
  CONST "holder.slot.n="
  LOAD_LOCAL 0
  GET_FIELD "slot"
  GET_FIELD "n"
  CALL 2
  POP
  CONST null
  RETURN
end
