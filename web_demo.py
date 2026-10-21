"""ChunkSafe web demo: a small local UI on top of the real compiler and runner.

    python web_demo.py            # then open http://127.0.0.1:8000
    python web_demo.py --port 9000

Uses only the standard library. Database lives in out/web/app.sqlite.
"""
import argparse
import json
import sqlite3
import sys
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import demo
from compiler import compile_to_sql
from compiler.errors import CompileError
from compiler.passes import sizing
from compiler.runner import Interrupted, parse_blocks, run

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "out" / "web"
DB = WORK / "app.sqlite"
ORDERS = 50_000

DEFAULT_SOURCE = """-- try: change p, or break it with  UPDATE stats SET visits = visits + 1;
REINDEX orders ON (region, ts);
REPARTITION trips BY zone;
UPDATE stats SET views = visits * 2;
"""

STATE = {"sql": None, "log": []}


def reset_db():
    WORK.mkdir(parents=True, exist_ok=True)
    demo.build_db(DB, ORDERS)
    STATE["sql"] = None


def do_compile(body):
    src, p = body["source"], float(body["p"])
    params = dict(throughput=float(body["throughput"]))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            sql = compile_to_sql(src, str(DB), p, **params)
        except CompileError as e:
            STATE["sql"] = None
            return {"ok": False, "error": str(e)}
    STATE["sql"] = sql
    rows, t_opt = sizing.chunk_rows_for(p, **params)
    d = sizing.expected_drop_time(p, 60.0)
    _, blocks = parse_blocks(sql)
    return {"ok": True, "sql": sql, "warnings": [str(x.message) for x in w],
            "plan": {"D": d, "t_opt": t_opt, "rows": rows,
                     "chunks": sum(b.kind == "chunk" for b in blocks), "blocks": len(blocks)}}


def state():
    out = {"has_job": STATE["sql"] is not None, "cells": [], "indexes": [], "shadows": [], "sample": []}
    con = sqlite3.connect(DB)
    try:
        out["indexes"] = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
        out["shadows"] = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '\\_cs\\_shadow%' ESCAPE '\\'")]
        out["sample"] = con.execute("SELECT id, visits, views FROM stats ORDER BY id LIMIT 3").fetchall()
        done = set()
        try:
            done = {(r[0], r[1]) for r in con.execute("SELECT task, chunk FROM _cs_progress")}
        except sqlite3.OperationalError:
            pass
        if STATE["sql"]:
            _, blocks = parse_blocks(STATE["sql"])
            for b in blocks:
                if b.kind == "chunk":
                    out["cells"].append({"t": b.task, "k": "chunk", "done": (b.task, b.idx) in done})
                elif b.kind == "swap":
                    out["cells"].append({"t": b.task, "k": "swap", "done": (b.task, -1) in done})
        out["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    return out


def do_run(body):
    if not STATE["sql"]:
        return {"ok": False, "error": "compile a job first"}
    n = body.get("max_blocks")
    try:
        ran = run(str(DB), STATE["sql"], max_blocks=n)
        return {"ok": True, "msg": f"finished: executed {ran} blocks"}
    except Interrupted as e:
        return {"ok": True, "msg": f"KILLED ({e}). Progress is saved; press Resume."}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/state":
            return self._send(200, state())
        if self.path == "/api/default":
            return self._send(200, {"source": DEFAULT_SOURCE})
        self._send(200, PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/compile":
                return self._send(200, do_compile(body))
            if self.path == "/api/run":
                return self._send(200, do_run(body))
            if self.path == "/api/reset":
                reset_db()
                return self._send(200, {"ok": True})
        except Exception as e:  # surface unexpected errors in the UI instead of hanging
            return self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>ChunkSafe demo</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f7f8fa;--card:#fff;--ink:#1f2937;--mut:#6b7280;--acc:#1d4ed8;--ok:#16a34a;--bad:#dc2626;--line:#e5e7eb;--code:#f3f4f6}
@media(prefers-color-scheme:dark){:root{--bg:#0f172a;--card:#1e293b;--ink:#e5e7eb;--mut:#94a3b8;--acc:#60a5fa;--ok:#4ade80;--bad:#f87171;--line:#334155;--code:#0f172a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}
header{padding:16px 24px;border-bottom:1px solid var(--line)}h1{margin:0;font-size:20px}header p{margin:2px 0 0;color:var(--mut)}
main{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px 24px;max-width:1300px;margin:auto}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}
textarea{width:100%;height:120px;font:13px ui-monospace,Consolas,monospace;background:var(--code);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:8px}
label{display:block;margin:8px 0 2px;color:var(--mut)}input[type=range]{width:100%}input[type=number]{width:110px;padding:4px}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer;margin:6px 6px 0 0}
button.alt{background:transparent;color:var(--acc);border:1px solid var(--acc)}button.kill{background:var(--bad)}button:disabled{opacity:.4;cursor:default}
pre{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:8px;margin:6px 0 0;overflow:auto;max-height:260px;font:12px/1.4 ui-monospace,Consolas,monospace;white-space:pre-wrap}
.msg{margin-top:8px;padding:8px;border-radius:6px;background:var(--code)}.msg.err{border-left:4px solid var(--bad)}.msg.good{border-left:4px solid var(--ok)}
.grid{display:flex;flex-wrap:wrap;gap:3px;margin-top:6px}.c{width:16px;height:16px;border-radius:3px;background:var(--line)}
.c.done{background:var(--ok)}.c.swap{border-radius:8px}.c.swap.done{background:var(--acc)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:6px}.stat b{display:block;font-size:18px}.stat span{color:var(--mut);font-size:12px}
.full{grid-column:1/-1}.legend{color:var(--mut);font-size:12px;margin-top:6px}
</style></head><body>
<header><h1>ChunkSafe</h1><p>Interruption-safe database reorganization: compile, kill, resume. Runs the real compiler on a 50,000-row SQLite database.</p></header>
<main>
<section class="card"><h2>1. Write a reorganization program</h2>
<textarea id="src" spellcheck="false"></textarea>
<label>Drop probability p = <b id="pv">0.9</b> (chance of a connectivity drop in 60 s)</label>
<input type="range" id="p" min="0.05" max="0.95" step="0.05" value="0.9">
<label>Throughput (rows/s, lower means more chunks to watch)</label><input type="number" id="thr" value="1500">
<div><button id="bc">Compile</button><button class="alt" id="br">Reset database</button></div>
<div id="cmsg"></div>
<div class="stats" id="plan" style="display:none">
<div class="stat"><b id="sD">-</b><span>D (s), mean time to drop</span></div>
<div class="stat"><b id="sT">-</b><span>T_opt = sqrt(2CD) (s)</span></div>
<div class="stat"><b id="sR">-</b><span>rows per chunk</span></div>
<div class="stat"><b id="sC">-</b><span>chunks (total)</span></div></div>
</section>
<section class="card"><h2>2. Run it, get interrupted, resume</h2>
<div><button class="kill" id="bk">Run 8 blocks then KILL</button><button id="bf">Resume / run to finish</button></div>
<div id="rmsg"></div>
<div class="grid" id="grid"></div>
<div class="legend">Squares: one per chunk (grey = pending, green = committed). Round = atomic swap (blue = table replaced).</div>
<div class="stats">
<div class="stat"><b id="nDone">0</b><span>chunks committed</span></div>
<div class="stat"><b id="nSw">0</b><span>tasks swapped</span></div>
<div class="stat"><b id="nSh">0</b><span>leftover shadow tables</span></div>
<div class="stat"><b id="nInt">-</b><span>integrity_check</span></div></div>
<label>Indexes now</label><pre id="idx" style="max-height:90px"></pre>
<label>stats rows (id, visits, views): views becomes visits*2 after the UPDATE task swaps</label><pre id="smp" style="max-height:70px"></pre>
</section>
<section class="card full"><h2>Generated SQL (the compiler's output)</h2><pre id="sql">Compile a program to see the SQL.</pre></section>
</main>
<script>
const $=id=>document.getElementById(id);
const post=(u,b)=>fetch(u,{method:'POST',body:JSON.stringify(b||{})}).then(r=>r.json());
const msg=(el,t,c)=>{$(el).innerHTML=t?`<div class="msg ${c||''}"></div>`:'';if(t)$(el).firstChild.textContent=t};
$('p').oninput=()=>$('pv').textContent=$('p').value;
async function refresh(){
  const s=await fetch('/api/state').then(r=>r.json());
  $('grid').innerHTML=s.cells.map(c=>`<div class="c ${c.k==='swap'?'swap ':''}${c.done?'done':''}" title="task ${c.t} ${c.k}"></div>`).join('');
  $('nDone').textContent=s.cells.filter(c=>c.k==='chunk'&&c.done).length+' / '+s.cells.filter(c=>c.k==='chunk').length;
  $('nSw').textContent=s.cells.filter(c=>c.k==='swap'&&c.done).length+' / '+s.cells.filter(c=>c.k==='swap').length;
  $('nSh').textContent=s.shadows.length;$('nInt').textContent=s.integrity;
  $('idx').textContent=s.indexes.join('\n');$('smp').textContent=s.sample.map(r=>'('+r.join(', ')+')').join('\n');
  $('bk').disabled=$('bf').disabled=!s.has_job;
}
$('bc').onclick=async()=>{
  const r=await post('/api/compile',{source:$('src').value,p:$('p').value,throughput:$('thr').value});
  if(!r.ok){msg('cmsg',r.error+'\n\nNothing was written; the database is untouched.','err');$('plan').style.display='none';$('sql').textContent='(rejected at compile time)';}
  else{msg('cmsg','Compiled OK.'+(r.warnings.length?'\nWarning: '+r.warnings.join('\n'):''),'good');
    $('plan').style.display='grid';$('sD').textContent=r.plan.D.toFixed(1);$('sT').textContent=r.plan.t_opt.toFixed(2);
    $('sR').textContent=r.plan.rows.toLocaleString();$('sC').textContent=r.plan.chunks;$('sql').textContent=r.sql;}
  msg('rmsg','');refresh();};
$('bk').onclick=async()=>{const r=await post('/api/run',{max_blocks:8});msg('rmsg',r.msg||r.error,r.ok?'err':'err');refresh();};
$('bf').onclick=async()=>{const r=await post('/api/run',{});msg('rmsg',r.msg||r.error,r.ok?'good':'err');refresh();};
$('br').onclick=async()=>{await post('/api/reset');msg('cmsg','');msg('rmsg','Database rebuilt.','good');$('plan').style.display='none';$('sql').textContent='Compile a program to see the SQL.';refresh();};
fetch('/api/default').then(r=>r.json()).then(d=>{$('src').value=d.source;});
refresh();
</script></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    reset_db()
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ChunkSafe web demo: http://127.0.0.1:{args.port}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
