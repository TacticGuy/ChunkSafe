import pytest
from compiler.semantic import Catalog


@pytest.fixture
def catalog():
    return Catalog({
        "orders": {"id", "region", "ts", "total"},
        "trips": {"id", "zone", "dist"},
        "stats": {"id", "visits", "views"},
    })
