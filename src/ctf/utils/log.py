import sys
import os

class Color:
    RESET   = "\033[0m"
    GREEN   = "\033[32m"
    BLUE    = "\033[34m"

__DEBUG: bool = False

def set_debug() -> None:
    global __DEBUG
    os.environ["EXP_DEBUG"] = "true"
    __DEBUG = True

def set_no_debug() -> None:
    global __DEBUG
    os.environ["EXP_DEBUG"] = ""
    __DEBUG = False

def debug_log(msg: str, tag: str = "") -> None:
    if not __DEBUG:
        return

    frame    = sys._getframe(1)
    module   = os.path.splitext(os.path.basename(frame.f_code.co_filename))[0]
    func     = frame.f_code.co_name
    locals_  = frame.f_locals

    if "self" in locals_:
        caller = f"{locals_['self'].__class__.__name__}.{func}"
    elif "cls" in locals_:
        caller = f"{locals_['cls'].__name__}.{func}"
    else:
        caller = func

    resolved_tag = tag or caller
    print(f"{Color.GREEN}[{module}]{Color.BLUE}[{resolved_tag}]{Color.RESET} {msg}")

if __name__ == "__main__":
    set_debug()
    debug_log("hello world")
    set_no_debug()