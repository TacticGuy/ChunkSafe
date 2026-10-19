import pytest
from compiler.errors import SemanticError
from compiler.parser import parse
from compiler.semantic import check


@pytest.mark.parametrize("src", [
    "REINDEX nope ON (a);",
    "REINDEX orders ON (bogus);",
    "REINDEX orders ON (ts, ts);",
    "REPARTITION trips BY bogus;",
    "UPDATE stats SET visits = 1, visits = 2;",
    "UPDATE stats SET visits = ghost;",
    "UPDATE stats SET visits = 'a' + 1;",
])
def test_rejected(src, catalog):
    with pytest.raises(SemanticError):
        check(parse(src), catalog)


def test_accepted(catalog):
    check(parse("REINDEX orders ON (region, ts); UPDATE stats SET views = visits * 2;"), catalog)
