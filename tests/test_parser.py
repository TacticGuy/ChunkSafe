import pytest
from compiler import ast_nodes as A
from compiler.errors import ParseError
from compiler.parser import parse


def test_three_statements():
    p = parse("REINDEX orders ON (region, ts);\nREPARTITION trips BY zone;\n"
              "UPDATE stats SET visits = visits + 1;")
    r, rp, u = p.statements
    assert r == A.Reindex("orders", ["region", "ts"], 1, 1)
    assert rp.table == "trips" and rp.key == "zone" and rp.line == 2
    assert u.assigns[0].column == "visits" and isinstance(u.assigns[0].expr, A.BinOp)


def test_case_insensitive_and_comments():
    p = parse("-- hi\nreindex orders on (ts); -- tail")
    assert p.statements[0].columns == ["ts"]


def test_precedence():
    e = parse("UPDATE s SET a = 1 + 2 * 3;").statements[0].assigns[0].expr
    assert e.op == "+" and e.right.op == "*"


@pytest.mark.parametrize("src", ["REINDEX orders ON (a)", "REPARTITION t zone;", "SELECT 1;", ""])
def test_syntax_errors(src):
    with pytest.raises(ParseError):
        parse(src)
