import sqlite3

SOURCE = """
REINDEX orders ON (region, ts);
REPARTITION trips BY zone;
UPDATE stats SET views = visits * 2;
UPDATE orders SET total = id * 2;
"""


def make_db(path):
    path = str(path)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE orders(id INTEGER PRIMARY KEY, region TEXT, ts INTEGER, total INTEGER);
        CREATE INDEX idx_orders_region ON orders(region);
        CREATE TABLE trips(zone TEXT, dist REAL);               -- implicit rowid, no PK
        CREATE TABLE stats(id INTEGER PRIMARY KEY, visits INTEGER, views INTEGER);
    """)
    con.executemany("INSERT INTO orders VALUES(?,?,?,0)", [(i, f"r{i % 7}", i * 10) for i in range(1, 1001)])
    con.executemany("INSERT INTO trips VALUES(?,?)", [(f"z{i % 5}", i / 3) for i in range(500)])
    con.executemany("INSERT INTO stats VALUES(?,?,0)", [(i, i * 3) for i in range(1, 301)])
    con.commit()
    con.close()
    return path


def reference(path):
    """Apply SOURCE's effect with plain one-shot SQL."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE INDEX cs_idx_orders_region_ts ON orders(region, ts);
        CREATE INDEX cs_part_trips_zone ON trips(zone);
        UPDATE stats SET views = visits * 2;
        UPDATE orders SET total = id * 2;
    """)
    con.commit()
    con.close()


def snapshot(path):
    con = sqlite3.connect(path)
    try:
        data = {t: con.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall()
                for t in ("orders", "trips", "stats")}
        objs = sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
        leftovers = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE name LIKE '\\_cs\\_shadow%' ESCAPE '\\'")]
        return data, objs, leftovers
    finally:
        con.close()
