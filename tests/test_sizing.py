import math

import pytest
from compiler.errors import SizingError
from compiler.ir import Kind, Task, ReindexSpec
from compiler.passes import sizing


def test_young_formula_worked_example():
    d = sizing.expected_drop_time(0.5, 60)
    assert d == pytest.approx(86.56, abs=0.01)
    assert sizing.young_interval(0.02, d) == pytest.approx(math.sqrt(2 * 0.02 * d))
    rows, t = sizing.chunk_rows_for(0.5)
    assert rows == int(t * 5000) == 9303


def test_higher_drop_prob_gives_smaller_chunks():
    sizes = [sizing.chunk_rows_for(p)[0] for p in (0.05, 0.2, 0.5, 0.8, 0.95)]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == len(sizes)


def test_clamping():
    assert sizing.chunk_rows_for(0.99, throughput=1, min_rows=100)[0] == 100
    assert sizing.chunk_rows_for(0.001, max_rows=500)[0] == 500


@pytest.mark.parametrize("p", [0, 1, -0.1, 1.5])
def test_bad_probability(p):
    with pytest.raises(SizingError):
        sizing.chunk_rows_for(p)


def _task():
    return Task(1, Kind.REINDEX, "t", frozenset({"a"}), frozenset(), ReindexSpec(("a",)))


def test_chunks_cover_range_exactly_once():
    t = sizing.size([_task()], {"t": (5, 1004)}, 0.9, throughput=100)[0]
    rows = t.plan.chunk_rows
    assert t.chunks[0].lo == 5 and t.chunks[-1].hi == 1005
    assert all(a.hi == b.lo for a, b in zip(t.chunks, t.chunks[1:]))
    assert all(c.hi - c.lo <= rows for c in t.chunks)
    assert len(t.chunks) == t.plan.est_chunks == math.ceil(1000 / rows)


def test_empty_table_has_no_chunks():
    assert sizing.size([_task()], {"t": None}, 0.5)[0].chunks == []
