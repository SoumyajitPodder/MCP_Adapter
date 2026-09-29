"""Log-safe exception summaries.

Exception messages can embed customer data (validation errors quote inputs, drivers quote
SQL parameters). The summary keeps the type and the code locations only; it reads frame
metadata, never source files or the message.
"""

import traceback

MAX_FRAMES = 12


def safe_exception_summary(exc: BaseException) -> str:
    frames = [
        f"{frame.f_code.co_filename}:{lineno} in {frame.f_code.co_name}"
        for frame, lineno in traceback.walk_tb(exc.__traceback__)
    ][-MAX_FRAMES:]
    kind = f"{type(exc).__module__}.{type(exc).__qualname__}"
    return " <- ".join([kind, *reversed(frames)])
