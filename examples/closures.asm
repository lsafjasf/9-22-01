# Closures: counters capturing a shared-enough variable by reference.
fn main 0 3
  CLOSURE make_counter
  CALL 0
  STORE_LOCAL 0          # c1
  CLOSURE make_counter
  CALL 0
  STORE_LOCAL 1          # c2
  LOAD_LOCAL 0
  CALL 0
  POP                    # c1 -> 1
  LOAD_LOCAL 0
  CALL 0
  POP                    # c1 -> 2
  LOAD_LOCAL 1
  CALL 0
  POP                    # c2 -> 1
  LOAD_GLOBAL "print"
  CONST "c1="
  LOAD_LOCAL 0
  CALL 0
  CALL 2
  POP
  LOAD_GLOBAL "print"
  CONST "c2="
  LOAD_LOCAL 1
  CALL 0
  CALL 2
  POP
  CONST null
  RETURN
end

fn make_counter 0 1
  CONST 0
  STORE_LOCAL 0          # captured cell
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
