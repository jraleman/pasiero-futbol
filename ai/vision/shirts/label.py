"""
A page to check the real test crops by eye (real.py's test/): for each crop,
the dataset's number for the player, and whether a person can read it there.

    python3 ai/vision/shirts/label.py ai/vision/shirts/real/test     # then open http://127.0.0.1:8765/

Enter: the number shown is legible on the shirt; N: no number is legible
(a side view, too blurred, turned away); digits then Enter: another number
is legible (the dataset's is someone else's); ← →: back and forth. Each
answer is saved at once to checked.json (numbers_eval's format: the number,
or null where none is legible), which numbers_eval compare --labels reads.
--limit picks that many crops, spread over the folder in a fixed order, so
everyone checks the same ones. Standard library only; it listens on
127.0.0.1 alone.
"""

from __future__ import annotations

import argparse
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shirt numbers check</title>
<style>
:root { --bg: #f6f5f8; --fg: #1d1a24; --muted: #6b6578; --card: #fff; --line: #ddd8e6; --accent: #5b2d8e; --ok: #1f7a4d; --no: #a3332b; }
@media (prefers-color-scheme: dark) { :root { --bg: #15121b; --fg: #ece8f3; --muted: #a29bb2; --card: #201b29; --line: #3a3347; --accent: #b892ea; --ok: #5ec594; --no: #ef8a80; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.4 system-ui, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 16px; }
header { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
h1 { font-size: 20px; margin: 0; }
.progress { color: var(--muted); font-variant-numeric: tabular-nums; }
.bar { height: 6px; background: var(--line); border-radius: 3px; margin: 10px 0 16px; overflow: hidden; }
.bar div { height: 100%; background: var(--accent); width: 0; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; display: grid; grid-template-columns: auto 1fr; gap: 20px; }
@media (max-width: 560px) { .card { grid-template-columns: 1fr; } }
img { height: 448px; max-width: 100%; object-fit: contain; image-rendering: auto; border-radius: 6px; background: #000; display: block; }
.ask { font-size: 15px; color: var(--muted); margin: 0 0 6px; }
.number { font-size: 56px; font-weight: 700; margin: 0 0 16px; font-variant-numeric: tabular-nums; }
.state { margin: 0 0 16px; min-height: 1.4em; font-weight: 600; }
.state.ok { color: var(--ok); } .state.no { color: var(--no); }
button { font: inherit; padding: 10px 14px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); cursor: pointer; margin: 0 8px 8px 0; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
input { font: inherit; width: 90px; padding: 9px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); }
kbd { font: 12px ui-monospace, monospace; border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; }
.keys { color: var(--muted); font-size: 14px; margin-top: 12px; }
.meta { color: var(--muted); font-size: 13px; margin-top: 12px; word-break: break-all; }
</style></head><body><main>
<header><h1>Shirt numbers check</h1><span class="progress" id="progress"></span></header>
<div class="bar"><div id="bar"></div></div>
<div class="card">
  <img id="crop" alt="A player's crop">
  <div>
    <p class="ask">The dataset says this player wears</p>
    <p class="number" id="dataset"></p>
    <p class="state" id="state"></p>
    <button class="primary" id="yes">Legible <kbd>Enter</kbd></button>
    <button id="no">Not legible <kbd>N</kbd></button><br>
    <label>Another number is legible: <input id="other" inputmode="numeric" placeholder="e.g. 18"></label>
    <div class="keys"><kbd>←</kbd> <kbd>→</kbd> back and forth · digits then <kbd>Enter</kbd> for another number</div>
    <div class="meta" id="meta"></div>
  </div>
</div>
</main>
<script>
let items = [], i = 0;
const $ = id => document.getElementById(id);
async function load() {
  items = await (await fetch("api/items")).json();
  i = items.findIndex(x => !("checked" in x)); if (i < 0) i = 0;
  show();
}
function show() {
  const x = items[i], done = items.filter(x => "checked" in x).length;
  $("crop").src = "crops/" + encodeURIComponent(x.name);
  $("dataset").textContent = "#" + x.dataset;
  const s = $("state");
  if (!("checked" in x)) { s.textContent = "Not checked yet"; s.className = "state"; }
  else if (x.checked === null) { s.textContent = "Checked: not legible"; s.className = "state no"; }
  else { s.textContent = "Checked: #" + x.checked + " legible"; s.className = "state ok"; }
  $("progress").textContent = `${i + 1} of ${items.length} · ${done} checked`;
  $("bar").style.width = (100 * done / items.length) + "%";
  $("meta").textContent = x.name;
  $("other").value = "";
}
async function answer(value) {
  const x = items[i];
  const r = await fetch("api/label", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name: x.name, number: value})});
  if (!r.ok) { alert("Not saved: " + await r.text()); return; }
  x.checked = value;
  if (i < items.length - 1) i++;
  show();
}
$("yes").onclick = () => answer(items[i].dataset);
$("no").onclick = () => answer(null);
document.addEventListener("keydown", e => {
  const typing = document.activeElement === $("other");
  if (e.key === "Enter") {
    e.preventDefault();
    const v = $("other").value.trim();
    if (v) { const n = parseInt(v, 10); if (n >= 0 && n <= 999) answer(n); } else answer(items[i].dataset);
  } else if ((e.key === "n" || e.key === "N") && !typing) { answer(null); }
  else if (e.key === "ArrowRight" && !typing) { if (i < items.length - 1) { i++; show(); } }
  else if (e.key === "ArrowLeft" && !typing) { if (i > 0) { i--; show(); } }
  else if (/^[0-9]$/.test(e.key) && !typing) { $("other").focus(); }
});
load();
</script></body></html>
"""


def serve(folder: Path, limit: int, port: int) -> None:
    dataset = json.loads((folder / "labels.json").read_text())
    names = sorted(dataset)
    random.Random(7).shuffle(names)               # spread over both matches and halves, the same for everyone
    names = names[:limit] if limit else names
    path = folder / "checked.json"
    checked = json.loads(path.read_text()) if path.is_file() else {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
            if self.path == "/api/items":
                items = [{"name": n, "dataset": dataset[n], **({"checked": checked[n]} if n in checked else {})} for n in names]
                return self.send(200, json.dumps(items).encode(), "application/json")
            if self.path.startswith("/crops/"):
                from urllib.parse import unquote

                name = unquote(self.path[len("/crops/"):])
                if name in dataset and "/" not in name:
                    return self.send(200, (folder / name).read_bytes(), "image/jpeg")
            self.send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/label":
                return self.send(404, b"not found", "text/plain")
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            name, number = body.get("name"), body.get("number")
            if name not in dataset or not (number is None or (isinstance(number, int) and 0 <= number <= 999)):
                return self.send(400, b"a crop's name, and a number or null", "text/plain")
            with lock:
                checked[name] = number
                part = path.with_suffix(".part")
                part.write_text(json.dumps(checked, indent=1, sort_keys=True))
                part.replace(path)
            self.send(204, b"", "text/plain")

    print(f"{len(names)} crops to check ({len(checked)} already): http://127.0.0.1:{port}/  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="real.py's test/ (labels.json: the dataset's numbers)")
    parser.add_argument("--limit", type=int, default=300, help="crops to check (0: all)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.folder, args.limit, args.port)


if __name__ == "__main__":
    main()
