"""Chunk Sizing Pass (design.md section 5): Young's formula T = sqrt(2*C*D)."""
import math
import warnings
from typing import Dict, List, Optional, Tuple

from ..errors import SizingError
from ..ir import Chunk, ChunkPlan, Task


def expected_drop_time(drop_prob: float, window: float) -> float:
    """D such that P(drop within `window` seconds) = drop_prob, for Poisson drops."""
    if not 0.0 < drop_prob < 1.0:
        raise SizingError(f"drop probability must be in (0, 1), got {drop_prob}")
    if window <= 0:
        raise SizingError("window must be positive")
    return -window / math.log(1.0 - drop_prob)


def young_interval(checkpoint_cost: float, drop_time: float) -> float:
    """Optimal seconds of work between checkpoints."""
    if checkpoint_cost <= 0:
        raise SizingError("checkpoint cost must be positive")
    return math.sqrt(2.0 * checkpoint_cost * drop_time)


def chunk_rows_for(drop_prob, checkpoint_cost=0.02, throughput=5000.0, window=60.0,
                   min_rows=100, max_rows=1_000_000) -> Tuple[int, float]:
    """Return (rows per chunk, T_opt seconds)."""
    if throughput <= 0:
        raise SizingError("throughput must be positive")
    if not 0 < min_rows <= max_rows:
        raise SizingError("need 0 < min_rows <= max_rows")
    d = expected_drop_time(drop_prob, window)
    t = young_interval(checkpoint_cost, d)
    if t >= d / 2:  # Young's approximation assumes T << D
        warnings.warn(f"T_opt={t:.2f}s is not << D={d:.2f}s; capping at D/2")
        t = d / 2
    return max(min_rows, min(max_rows, int(t * throughput))), t


def size(tasks: List[Task], ranges: Dict[str, Optional[Tuple[int, int]]], drop_prob: float,
         **params) -> List[Task]:
    """Split each task's table key range [min, max] into contiguous half-open chunks."""
    rows, t_opt = chunk_rows_for(drop_prob, **params)
    for task in tasks:
        rng = ranges.get(task.table)
        task.chunks = []
        if rng is not None:
            lo, hi = rng[0], rng[1] + 1
            task.chunks = [Chunk(i, a, min(a + rows, hi))
                           for i, a in enumerate(range(lo, hi, rows))]
        task.plan = ChunkPlan(rows, t_opt, len(task.chunks))
    return tasks
