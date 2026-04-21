#!/usr/bin/env python3
"""
confidence_tracker.py — rolling percentile tracker for trade confidence.

Persists the last N confidence scores to JSON so service restarts don't
reset the distribution. Used to decide when a newly-opened trade lands
in the top 5% (or whatever percentile) so the autopilot can fire a
high-conviction alert email.

Cold-start behavior: if we have fewer than `min_sample` values, fall
back to a fixed cutoff so alerts can still fire during the first days
after deploy.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional


class ConfidenceTracker:
    def __init__(self, path: str, max_size: int = 200):
        self.path = path
        self.max_size = max_size
        self._lock = threading.Lock()
        self._values: list[float] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._values = [float(v) for v in data if isinstance(v, (int, float))][-self.max_size:]
        except Exception:
            self._values = []

    def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._values, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def record(self, value: Optional[float]) -> None:
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._values.append(v)
            if len(self._values) > self.max_size:
                self._values = self._values[-self.max_size:]
            self._save()

    def percentile(self, p: float = 95.0) -> Optional[float]:
        with self._lock:
            if not self._values:
                return None
            xs = sorted(self._values)
            if len(xs) == 1:
                return xs[0]
            k = (p / 100.0) * (len(xs) - 1)
            lo = int(k)
            hi = min(lo + 1, len(xs) - 1)
            frac = k - lo
            return xs[lo] * (1 - frac) + xs[hi] * frac

    def sample_size(self) -> int:
        with self._lock:
            return len(self._values)

    def is_top(self, value: Optional[float], *, percentile: float = 95.0,
               min_sample: int = 50, cold_cutoff: float = 85.0) -> tuple[bool, str]:
        """Return (is_high_conviction, reason_string)."""
        if value is None:
            return False, "no confidence"
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False, "bad value"
        n = self.sample_size()
        if n < min_sample:
            if v >= cold_cutoff:
                return True, f"cold-start ({n} samples, cutoff {cold_cutoff:.0f})"
            return False, f"cold-start below {cold_cutoff:.0f} ({n} samples)"
        cut = self.percentile(percentile)
        if cut is None:
            return False, "empty window"
        if v >= cut:
            return True, f"top {100 - percentile:.0f}% over last {n} (cut={cut:.1f})"
        return False, f"below {percentile:.0f}th pct ({cut:.1f}) over last {n}"
