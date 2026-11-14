"""
Shared benchmark harness.

Starts REAL processes exactly as the README does
(`python main.py --port P --capacity C` per node and
`python -m uvicorn router.router:app --port P` for the router), each in
an isolated working directory under bench/.work/, then drives them with
an async httpx load generator.

CPU pinning (taskset): servers on CPUs 0-2, load generator on CPU 3,
so the client and the system under test do not steal each other's core.

Nothing in here changes core cache/router logic: configuration is only
passed through the environment variables the router already reads.
"""

import asyncio
import json
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import time

import httpx
import numpy as np

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH_DIR)
WORK = os.path.join(BENCH_DIR, ".work")
RESULTS = os.path.join(BENCH_DIR, "results")

sys.path.insert(0, REPO)

ROUTER_PORT = 9100
NODE_PORT_BASE = 9101

SERVER_CPUS = os.environ.get("BENCH_SERVER_CPUS", "0-2")
CLIENT_CPU = int(os.environ.get("BENCH_CLIENT_CPU", "3"))

HAS_TASKSET = shutil.which("taskset") is not None


def pin_client():
    """Pin the current (load-generator) process to CLIENT_CPU."""

    if hasattr(os, "sched_setaffinity") and CLIENT_CPU < os.cpu_count():
        os.sched_setaffinity(0, {CLIENT_CPU})


def _pinned(cmd):
    if HAS_TASKSET:
        return ["taskset", "-c", SERVER_CPUS] + cmd
    return cmd


# ---------------------------------------------------------------------
# Machine specs
# ---------------------------------------------------------------------

def machine_specs() -> dict:

    cpu = platform.processor() or "unknown"

    try:
        with open("/proc/cpuinfo") as handle:
            for line in handle:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass

    ram = None
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemTotal"):
                    ram = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
    except OSError:
        pass

    os_name = platform.platform()
    try:
        with open("/etc/os-release") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME"):
                    os_name = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass

    import fastapi
    import uvicorn
    import sqlite3

    return {
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "ram_gib": ram,
        "os": os_name,
        "kernel": platform.release(),
        "python": platform.python_version(),
        "fastapi": fastapi.__version__,
        "uvicorn": uvicorn.__version__,
        "httpx": httpx.__version__,
        "sqlite": sqlite3.sqlite_version,
        "cpu_pinning": (
            f"servers on CPUs {SERVER_CPUS}, load generator on CPU {CLIENT_CPU}"
            if HAS_TASKSET else "none (taskset missing)"
        ),
    }


# ---------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------

def pct(values, p):
    if len(values) == 0:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), p))


def summarize_ms(latencies_s) -> dict:
    ms = [v * 1000.0 for v in latencies_s]
    return {
        "n": len(ms),
        "p50_ms": pct(ms, 50),
        "p95_ms": pct(ms, 95),
        "p99_ms": pct(ms, 99),
        "mean_ms": statistics.fmean(ms) if ms else None,
        "max_ms": max(ms) if ms else None,
    }


def median_of(runs, field):
    vals = [r[field] for r in runs if r.get(field) is not None]
    return statistics.median(vals) if vals else None


def save(name: str, payload: dict):
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"{name}.json")
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2)
    print(f"[saved] {os.path.relpath(path, REPO)}")
    return path


# ---------------------------------------------------------------------
# Cluster (real processes)
# ---------------------------------------------------------------------

def _wait_http(url, timeout=20.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return True
        except httpx.HTTPError as error:
            last = error
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up: {last!r}")


class Cluster:
    """
    N cache-node processes + 1 router process, isolated working dir.

    Router env (only variables router/config.py already reads):
        CACHE_NODES               the N node URLs
        CACHE_DB_PATH             <workdir>/cache.db (fresh SQLite)
        CLUSTER_BOOTSTRAP_STATE   ready  (skip the boot gate so the
                                  measurement starts immediately)
        WARMUP_ENABLED / SEED_ENABLED  off (nothing to warm in a
                                  fresh, empty database anyway)
    """

    def __init__(self, nodes=3, capacity=5000, name="run",
                 router_port=ROUTER_PORT, node_port_base=NODE_PORT_BASE,
                 extra_env=None, health_monitor=True):

        self.n = nodes
        self.capacity = capacity
        self.router_port = router_port
        self.ports = [node_port_base + i for i in range(nodes)]
        self.workdir = os.path.join(WORK, name)
        self.extra_env = extra_env or {}
        self.procs = {}
        self.router = None
        self.db_path = os.path.join(self.workdir, "cache.db")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.router_port}"

    def node_url(self, port):
        return f"http://127.0.0.1:{port}"

    def start_node(self, port):
        log = open(os.path.join(self.workdir, f"node-{port}.log"), "ab")
        cmd = _pinned([sys.executable, "main.py", "--port", str(port),
                       "--capacity", str(self.capacity)])
        self.procs[port] = subprocess.Popen(
            cmd, cwd=REPO, stdout=log, stderr=subprocess.STDOUT
        )
        _wait_http(f"{self.node_url(port)}/health")

    def __enter__(self):

        shutil.rmtree(self.workdir, ignore_errors=True)
        os.makedirs(self.workdir, exist_ok=True)

        for port in self.ports:
            self.start_node(port)

        env = dict(os.environ)
        env.update({
            "CACHE_NODES": ",".join(self.node_url(p) for p in self.ports),
            "CACHE_DB_PATH": self.db_path,
            "CLUSTER_BOOTSTRAP_STATE": "ready",
            "WARMUP_ENABLED": "off",
            "SEED_ENABLED": "off",
            # The router's port pool must not collide with bench nodes.
            "NODE_PORT_BASE": str(self.ports[0]) if self.ports else "9101",
            "MAX_NODES": str(max(self.n, 1)),
        })
        env.update(self.extra_env)

        self.router_log_path = os.path.join(self.workdir, "router.log")
        log = open(self.router_log_path, "ab")
        cmd = _pinned([sys.executable, "-m", "uvicorn", "router.router:app",
                       "--port", str(self.router_port)])
        self.router = subprocess.Popen(
            cmd, cwd=REPO, stdout=log, stderr=subprocess.STDOUT, env=env
        )
        _wait_http(f"{self.url}/health")
        return self

    def kill_node(self, port, sig=signal.SIGKILL):
        self.procs[port].send_signal(sig)

    def __exit__(self, *exc):
        procs = list(self.procs.values()) + ([self.router] if self.router else [])
        for p in procs:
            try:
                p.send_signal(signal.SIGCONT)
            except Exception:
                pass
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        return False

    # -- data helpers ------------------------------------------------

    def preload_db(self, keys, value_size=100):
        """Write rows straight into SQLite (DB only, caches stay cold)."""
        from database.repository import DatabaseRepository
        repo = DatabaseRepository(db_path=self.db_path)
        payload = "x" * value_size
        repo.set_many((k, {"k": k, "v": payload}) for k in keys)

    def telemetry(self):
        return httpx.get(f"{self.url}/dashboard/aggregate", timeout=5).json()

    def reset_telemetry(self):
        httpx.post(f"{self.url}/dashboard/telemetry/reset", timeout=5)

    def status(self):
        return httpx.get(f"{self.url}/cluster/status", timeout=5).json()


# ---------------------------------------------------------------------
# Async load generator
# ---------------------------------------------------------------------

async def _worker(client, base, keys_iter, out, stop_at, timeout):
    while True:
        if stop_at is not None and time.perf_counter() >= stop_at:
            return
        try:
            key = next(keys_iter)
        except StopIteration:
            return
        t0 = time.perf_counter()
        status = None
        node = None
        try:
            r = await client.get(f"{base}/cache/{key}", timeout=timeout)
            status = r.status_code
            if status == 200:
                body = r.json()
                node = body.get("node")
                src = body.get("source")
            else:
                src = None
        except httpx.HTTPError as error:
            status = type(error).__name__
            src = None
        t1 = time.perf_counter()
        out.append((t0, t1 - t0, status, node, src))


# httpx's connection pool scales badly with pool size (one client with
# 100 connections collapsed to ~150-450 req/s against a no-op server on
# this machine), so workers are sharded over several AsyncClients of at
# most CONNS_PER_CLIENT connections each (~1.9k req/s ceiling at c=100;
# see loadgen_ceiling in results/throughput.json).
CONNS_PER_CLIENT = 5


async def _run_load(base, keys, concurrency, duration, timeout):
    per = min(CONNS_PER_CLIENT, concurrency)
    n_clients = (concurrency + per - 1) // per
    limits = httpx.Limits(max_connections=per, max_keepalive_connections=per)
    clients = [httpx.AsyncClient(limits=limits) for _ in range(n_clients)]
    out = []
    keys_iter = iter(keys)
    try:
        t_start = time.perf_counter()
        stop_at = t_start + duration if duration else None
        await asyncio.gather(*[
            _worker(clients[i % n_clients], base, keys_iter, out, stop_at,
                    timeout)
            for i in range(concurrency)
        ])
        t_end = time.perf_counter()
    finally:
        for client in clients:
            await client.aclose()
    return out, t_start, t_end


def run_load(base, keys, concurrency=1, duration=None, timeout=10.0):
    """
    Closed-loop load: `concurrency` workers, each sends GET /cache/{key}
    and waits for the answer before sending the next.  Stops when the
    key iterator is exhausted or `duration` seconds have passed.
    Returns (records, t_start, t_end); record = (t0, latency_s, status,
    node, source).
    """
    return asyncio.run(_run_load(base, keys, concurrency, duration, timeout))


def cycle(keys):
    while True:
        for k in keys:
            yield k
