"""Fine-grained crash breadcrumbs inside an op.

ops.handle() writes `BEGIN <op> <id>` / `END ...` around every request (breadcrumb.py).
CAM ops also call `step("generateToolpath Setup1")` right before each risky adsk.cam call,
which appends `STEP <op> <id> <text>` (fsync'd) to the same log. If Fusion dies, the next
add-in start (breadcrumb.startup) reports the op AND the last step it reached.

This module is reloadable; breadcrumb.py (never reloaded) owns the file and the parser.
"""

from . import breadcrumb

current = {"op": "-", "id": "-"}


def step(text: str) -> None:
    breadcrumb._write(f"STEP {current['op']} {current['id']} {text}".rstrip())
