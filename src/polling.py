"""Generation polling for cam_generate. Pure logic (the clock, sleep and calls are injected), so
it is unit-tested without Fusion.

The add-in only *starts* toolpath generation (cam_generate_start) and returns a job id; Fusion
generates in the background while its main thread stays free. The server then asks
cam_generate_poll every `interval_s` with a short main-thread call until the job completes or
`timeout_s` passes. A timeout here does not stop generation: the job keeps running in Fusion
and can be polled again (or checked with cam_status).
"""

import time


def poll_generation(call, job: str, timeout_s: float = 600, interval_s: float = 0.5,
                    poll_timeout_s: float = 15, sleep=time.sleep, clock=time.monotonic) -> dict:
    """call(op, args, timeout) -> result. Returns the final poll result, or the last one with
    timed_out=True."""
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")
    t0 = clock()
    polls = 0
    last = None
    while True:
        last = call("cam_generate_poll", {"job": job}, poll_timeout_s)
        polls += 1
        if last.get("completed"):
            last["polls"] = polls
            return last
        if clock() - t0 >= timeout_s:
            last["timed_out"] = True
            last["polls"] = polls
            last["hint"] = (f"generation still running after {timeout_s:g}s; it continues in Fusion. "
                            f"Call cam_generate(job='{job}') to keep waiting, or cam_status.")
            return last
        sleep(min(interval_s, max(0.0, timeout_s - (clock() - t0))))
