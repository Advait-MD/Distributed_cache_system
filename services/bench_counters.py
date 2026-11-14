"""
Optional benchmark instrumentation - OFF unless DCS_BENCH_INSTRUMENT=1.

Counts failed data-plane attempts per node (a GET the router sent to a
node that refused, reset or timed out) with the wall-clock time of the
first and last failure.  bench/bench_failover.py reads it through
GET /bench/counters (registered only when the flag is set) to measure
how long the router kept trying a dead node before routing around it.

Disabled = record_node_error() is a single attribute check; no state,
no endpoint, no behaviour change.
"""

import os
import threading
import time

ENABLED = os.environ.get("DCS_BENCH_INSTRUMENT", "") == "1"

_lock = threading.Lock()
_node_errors = {}


def record_node_error(node: str) -> None:
    if not ENABLED:
        return
    now = time.time()
    with _lock:
        entry = _node_errors.get(node)
        if entry is None:
            _node_errors[node] = {"count": 1, "first": now, "last": now}
        else:
            entry["count"] += 1
            entry["last"] = now


def snapshot() -> dict:
    with _lock:
        return {node: dict(v) for node, v in _node_errors.items()}


def reset() -> None:
    with _lock:
        _node_errors.clear()
