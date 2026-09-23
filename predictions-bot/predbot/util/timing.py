"""Stage timings for one generation: wall-clock and CPU seconds per stage (shown in /lastrun)."""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Dict


class Timings:
    def __init__(self) -> None:
        self.stages: Dict[str, Dict[str, float]] = {}

    @contextmanager
    def stage(self, name: str):
        w, c = time.perf_counter(), time.process_time()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - w, time.process_time() - c)

    def add(self, name: str, wall: float, cpu: float) -> None:
        old = self.stages.get(name, {"wall": 0.0, "cpu": 0.0})
        self.stages[name] = {"wall": round(old["wall"] + wall, 4), "cpu": round(old["cpu"] + cpu, 4)}

    def to_dict(self) -> Dict[str, Dict[str, float]]:
        return dict(self.stages)
