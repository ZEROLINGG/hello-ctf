import time
from typing import Callable

from ctf.utils.log import debug_log





def wait(
    func: Callable[[], bool],
    timeout: float = 15,
    interval: float = 0.2,
) -> bool:
    if timeout < 0:
        debug_log("timeout 必须 >= 0")
        return False
    if interval <= 0:
        debug_log("interval 必须 > 0")
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if func():
                return True
        except Exception as e:
            debug_log(str(e))
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    return False

