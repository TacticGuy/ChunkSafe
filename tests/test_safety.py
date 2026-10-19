import pytest
from compiler import compile_frontend
from compiler.errors import UnsafeRetryError
from compiler.ir import Kind


def test_reads_writes_derivation(catalog):
    t = compile_frontend("REINDEX orders ON (region, ts);", catalog)[0]
    assert t.kind is Kind.REINDEX and t.reads == {"region", "ts"} and t.writes == frozenset()


def test_unsafe_update_rejected(catalog):
    with pytest.raises(UnsafeRetryError) as e:
        compile_frontend("UPDATE stats SET visits = visits + 1;", catalog)
    assert "stats.visits" in str(e.value) and e.value.line == 1


def test_swap_assignment_rejected(catalog):
    with pytest.raises(UnsafeRetryError):
        compile_frontend("UPDATE stats SET visits = views, views = visits;", catalog)


def test_safe_updates(catalog):
    ts = compile_frontend("UPDATE stats SET visits = 0; UPDATE stats SET views = visits * 2;", catalog)
    assert ts[0].writes == {"visits"} and ts[1].reads == {"visits"}
    assert ts[1].deps == [1]


def test_unsafe_in_second_statement_reports_location(catalog):
    with pytest.raises(UnsafeRetryError) as e:
        compile_frontend("REINDEX orders ON (ts);\nUPDATE stats SET visits = visits + 1;", catalog)
    assert e.value.line == 2
