# Error interrupt: TRAP installs a handler, errors unwind to it.
fn main 0 1
  TRAP catch
  CONST 1
  CONST 0
  DIV                    # raises "division by zero"
  POP
  UNTRAP
  CONST "unreachable"
  RETURN
catch:                   # error value is pushed by the VM
  STORE_LOCAL 0
  LOAD_GLOBAL "print"
  CONST "caught:"
  LOAD_LOCAL 0
  CALL 2
  POP
  TRAP catch2
  CONST "boom"
  RAISE
  UNTRAP
catch2:
  STORE_LOCAL 0
  LOAD_GLOBAL "print"
  CONST "caught raise:"
  LOAD_LOCAL 0
  CALL 2
  POP
  CONST "recovered"
  RETURN
end
