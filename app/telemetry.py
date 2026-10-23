"""Caching, metrics and structured (Cloud Logging compatible) audit logs."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import time
from collections import OrderedDict, deque

from .security import mask_pii


class TTLCache:
    """Thread-safe LRU cache with per-entry TTL."""

    def __init__(self, size: int, ttl_s: int):
        self.size, self.ttl = size, ttl_s
        self._d: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    @staticmethod
    def key(*parts: str) -> str:
        norm = "|".join(re.sub(r"\s+", " ", p.strip().lower().rstrip("?.!")) for p in parts)
        return hashlib.sha256(norm.encode()).hexdigest()

    def get(self, k: str):
        with self._lock:
            item = self._d.get(k)
            if item and time.monotonic() - item[0] < self.ttl:
                self._d.move_to_end(k)
                self.hits += 1
                return item[1]
            if item:
                del self._d[k]
            self.misses += 1
            return None

    def set(self, k: str, v) -> None:
        with self._lock:
            self._d[k] = (time.monotonic(), v)
            self._d.move_to_end(k)
            while len(self._d) > self.size:
                self._d.popitem(last=False)

    def invalidate_prefix(self, _dataset_id: str) -> None:
        with self._lock:
            self._d.clear()


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.started = time.time()
        self.requests = self.answered = self.blocked = self.failed = self.repairs = 0
        self.tokens_in = self.tokens_out = 0
        self.llm_calls = self.fallbacks = 0
        self.latencies: deque[float] = deque(maxlen=500)
        self.engine_counts: dict[str, int] = {}

    def record(self, *, outcome: str, ms: float, engine: str = "", tokens_in=0, tokens_out=0,
               llm_calls=0, repairs=0, fallback=False):
        with self._lock:
            self.requests += 1
            setattr(self, outcome, getattr(self, outcome) + 1)
            self.latencies.append(ms)
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
            self.llm_calls += llm_calls
            self.repairs += repairs
            self.fallbacks += int(fallback)
            if engine:
                self.engine_counts[engine] = self.engine_counts.get(engine, 0) + 1

    def snapshot(self, cache: TTLCache) -> dict:
        with self._lock:
            lat = sorted(self.latencies)
        def pct(p):
            return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1) if lat else 0
        total_lookups = cache.hits + cache.misses
        return {
            "uptime_s": int(time.time() - self.started),
            "requests": self.requests, "answered": self.answered, "blocked": self.blocked,
            "failed": self.failed, "self_repairs": self.repairs, "llm_calls": self.llm_calls,
            "fallbacks": self.fallbacks, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "avg": round(sum(lat) / len(lat), 1) if lat else 0},
            "cache": {"hits": cache.hits, "misses": cache.misses,
                      "hit_rate": round(cache.hits / total_lookups, 3) if total_lookups else 0},
            "engines": self.engine_counts,
        }


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"severity": record.levelname, "message": record.getMessage(),
                   "time": self.formatTime(record), "logger": record.name}
        if isinstance(getattr(record, "audit", None), dict):
            payload["audit"] = record.audit
        return json.dumps(payload, default=str)


def get_logger() -> logging.Logger:
    log = logging.getLogger("datapilot")
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(_JsonFormatter())
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def audit(event: str, **fields) -> None:
    """Emit a PII-masked audit event (picked up by Cloud Logging on Cloud Run)."""
    clean = {k: (mask_pii(v)[0] if isinstance(v, str) else v) for k, v in fields.items()}
    get_logger().info(event, extra={"audit": {"event": event, **clean}})
