"""Minimal local web UI for the human-in-the-loop stages. Stdlib only.

The loop still runs in the pipeline. When it needs a person, it publishes the
question here and blocks until the browser answers, so the UI is a replacement
for the terminal prompt, not a second control flow.

The answer the browser sends is the same short string the terminal accepts
("a <text>", "o <text>", "revert <reason>", "s", ""), so there is exactly one
place that interprets an operator reply.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = logging.getLogger("docgen.ui")

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>docgen review</title>
<style>
 :root{color-scheme:light dark}
 body{margin:0;font:14px/1.5 system-ui,sans-serif;background:#f6f6f6;color:#111}
 header{background:#222;color:#fff;padding:10px 16px;display:flex;gap:14px;align-items:baseline}
 header b{font-size:15px}
 header span{opacity:.75;font-size:12px}
 main{padding:16px;max-width:1600px}
 .card{background:#fff;border:1px solid #ddd;border-radius:6px;padding:14px;margin-bottom:14px}
 .stage{font-weight:700;font-size:16px;margin-bottom:8px}
 pre{background:#f2f2f2;border:1px solid #e0e0e0;border-radius:4px;padding:10px;
     overflow:auto;max-height:260px;margin:0 0 12px;font-size:12px;white-space:pre-wrap}
 .imgwrap{position:relative;display:inline-block;margin-bottom:8px;max-width:100%}
 img{max-width:100%;border:1px solid #ccc;border-radius:4px;display:block;cursor:crosshair}
 .sel{position:absolute;border:2px solid #1a5fb4;background:rgba(26,95,180,.18);pointer-events:none}
 .selinfo{font-size:12px;color:#1a5fb4;margin-bottom:8px}
 .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
 button{font:inherit;padding:7px 14px;border:1px solid #999;border-radius:4px;
        background:#fff;cursor:pointer}
 button:hover{background:#eee}
 button.primary{background:#1a5fb4;border-color:#1a5fb4;color:#fff}
 button.warn{background:#a51d2d;border-color:#a51d2d;color:#fff}
 input[type=text]{font:inherit;padding:7px;border:1px solid #bbb;border-radius:4px;flex:1;min-width:260px}
 .idle{color:#666}
 table{border-collapse:collapse;font-size:12px;width:100%}
 th,td{border:1px solid #ddd;padding:4px 8px;text-align:left}
 th{background:#eee}
 @media (prefers-color-scheme:dark){
   body{background:#161616;color:#eee}
   .card{background:#1f1f1f;border-color:#333}
   pre{background:#151515;border-color:#333}
   button{background:#2a2a2a;color:#eee;border-color:#555}
   button:hover{background:#3a3a3a}
   input[type=text]{background:#111;color:#eee;border-color:#555}
   th{background:#262626}th,td{border-color:#333}
 }
</style>
<header><b>docgen review</b><span id="sub">connecting...</span></header>
<main>
  <div class="card" id="panel"><div class="idle">대기 중...</div></div>
  <div class="card"><div class="stage">지난 라운드</div><div id="hist">아직 없음</div></div>
</main>
<script>
let current = null;
let region = null;

function esc(s){return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

function send(value){
  const box = document.getElementById('txt');
  let v = value;
  if (v === '@text') { v = (box ? box.value.trim() : ''); if (!v) { box.focus(); return; } }
  else if (v && v.endsWith(' @text')) {
    const t = box ? box.value.trim() : '';
    if (!t) { box.focus(); return; }
    v = v.slice(0, -6) + ' ' + t;
  }
  const sent = region;
  current = null; region = null;
  document.getElementById('panel').innerHTML = '<div class="idle">전송했습니다. 다음 단계를 기다립니다...</div>';
  fetch('answer', {method:'POST', headers:{'Content-Type':'application/json'},
                   body: JSON.stringify({answer: v, region: sent})});
}

function render(st){
  document.getElementById('sub').textContent =
    st.finished ? '실행 종료' : (st.pending ? '입력 대기 중' : '모델이 작업 중...');

  const hist = document.getElementById('hist');
  if (st.history && st.history.length){
    hist.innerHTML = '<table><tr><th>라운드</th><th>단계</th><th>보낸 답</th></tr>' +
      st.history.map(h => `<tr><td>${esc(h.round)}</td><td>${esc(h.stage)}</td>` +
        `<td>${h.answer ? esc(h.answer) : '(수락)'}</td></tr>`).join('') + '</table>';
  }

  const panel = document.getElementById('panel');
  if (!st.pending){
    if (!current) panel.innerHTML = st.finished
      ? '<div class="idle">실행이 끝났습니다. 이 창은 닫아도 됩니다.</div>'
      : '<div class="idle">모델이 작업 중입니다...</div>';
    return;
  }
  const p = st.pending;
  if (current === p.id) return;   // already rendered, keep typed text
  current = p.id;
  region = null;

  let html = `<div class="stage">${esc(p.title)}</div>`;
  if (p.image) html += `<div class="imgwrap" id="wrap">` +
      `<img id="shot" src="img?p=${encodeURIComponent(p.image)}&v=${esc(p.id)}" alt="">` +
      `</div><div class="selinfo" id="selinfo">` +
      (p.panels && p.panels.length
        ? '이미지 위를 드래그하면 그 영역만 고치라고 지정할 수 있습니다.'
        : '') + `</div>`;
  if (p.data)  html += `<pre>${esc(JSON.stringify(p.data, null, 2))}</pre>`;
  html += `<div>${esc(p.prompt)}</div>`;
  if (p.text) html += `<div class="row"><input type="text" id="txt" placeholder="의견 / 지시를 입력"></div>`;
  html += '<div class="row" id="btns"></div>';
  panel.innerHTML = html;

  // Buttons are built here rather than as inline onclick: a JSON-quoted value
  // inside a double-quoted HTML attribute would terminate the attribute.
  const btns = document.getElementById('btns');
  for (const c of (p.choices || [])){
    const b = document.createElement('button');
    b.textContent = c.label;
    if (c.style) b.className = c.style;
    b.addEventListener('click', () => send(c.value));
    btns.appendChild(b);
  }
  const box = document.getElementById('txt');
  if (box) box.addEventListener('keydown', e => {
    if (e.key === 'Enter' && p.enter_value) send(p.enter_value);
  });
  if (p.image && p.panels && p.panels.length) enableSelect(p.panels);
}

// --- drag a region on the composed sheet -----------------------------------
// The rect is normalised inside whichever panel the drag started in, so the
// same rect applies to a full-resolution scan and to an 800px render alike.
function enableSelect(panels){
  const wrap = document.getElementById('wrap');
  const img = document.getElementById('shot');
  const info = document.getElementById('selinfo');
  let box = null, start = null;

  const toNatural = ev => {
    const r = img.getBoundingClientRect();
    const sx = img.naturalWidth / r.width, sy = img.naturalHeight / r.height;
    return {x: (ev.clientX - r.left) * sx, y: (ev.clientY - r.top) * sy,
            dx: (ev.clientX - r.left), dy: (ev.clientY - r.top), scale: 1 / sx};
  };

  img.addEventListener('mousedown', ev => {
    ev.preventDefault();
    start = toNatural(ev);
    if (box) box.remove();
    box = document.createElement('div');
    box.className = 'sel';
    wrap.appendChild(box);
  });

  window.addEventListener('mousemove', ev => {
    if (!start || !box) return;
    const now = toNatural(ev);
    box.style.left = Math.min(start.dx, now.dx) + 'px';
    box.style.top = Math.min(start.dy, now.dy) + 'px';
    box.style.width = Math.abs(now.dx - start.dx) + 'px';
    box.style.height = Math.abs(now.dy - start.dy) + 'px';
  });

  window.addEventListener('mouseup', ev => {
    if (!start) return;
    const now = toNatural(ev);
    const s = start; start = null;
    const x0 = Math.min(s.x, now.x), y0 = Math.min(s.y, now.y);
    const w = Math.abs(now.x - s.x), h = Math.abs(now.y - s.y);
    if (w < 6 || h < 6) { if (box) box.remove(); box = null; region = null;
      info.textContent = '선택이 너무 작습니다. 다시 드래그하세요.'; return; }
    // Which panel did the drag start in?
    const pane = panels.find(q => s.x >= q.x && s.x <= q.x + q.width &&
                                  s.y >= q.y && s.y <= q.y + q.height) || panels[0];
    region = {
      panel: pane.label,
      x: Math.max(0, (x0 - pane.x) / pane.width),
      y: Math.max(0, (y0 - pane.y) / pane.height),
      w: Math.min(1, w / pane.width),
      h: Math.min(1, h / pane.height),
    };
    const pct = v => Math.round(v * 100) + '%';
    info.innerHTML = `선택 영역: <b>${esc(pane.label)}</b> ` +
      `x ${pct(region.x)}, y ${pct(region.y)}, 폭 ${pct(region.w)}, 높이 ${pct(region.h)} ` +
      `— 이 영역만 고치라고 함께 보냅니다. <button id="clr">선택 해제</button>`;
    document.getElementById('clr').addEventListener('click', () => {
      region = null; if (box) box.remove(); box = null;
      info.textContent = '선택을 해제했습니다.';
    });
  });
}

async function tick(){
  try { render(await (await fetch('state')).json()); }
  catch (e) {
    // After a normal shutdown the last good state already said 실행 종료.
    const sub = document.getElementById('sub');
    if (sub.textContent !== '실행 종료') sub.textContent = '연결 끊김';
  }
}
tick(); setInterval(tick, 1000);
</script>
"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "docgen-ui"

    def log_message(self, *args):
        pass

    @property
    def review(self) -> "ReviewServer":
        return self.server.review  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        route = path.path.rstrip("/") or "/"
        if route == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/state":
            self._send(200, json.dumps(self.review.state()).encode("utf-8"), "application/json")
        elif route == "/favicon.ico":
            # Browsers always ask; a 404 in the console is just noise.
            self._send(204, b"", "image/x-icon")
        elif route == "/img":
            rel = urllib.parse.parse_qs(path.query).get("p", [""])[0]
            data = self.review.read_image(rel)
            if data is None:
                self._send(404, b"not found", "text/plain")
            else:
                self._send(200, data, "image/png")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path.rstrip("/") != "/answer":
            self._send(404, b"not found", "text/plain")
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            answer = str(body.get("answer", ""))
            region = body.get("region")
        except (ValueError, UnicodeDecodeError):
            self._send(400, b'{"ok":false}', "application/json")
            return
        accepted = self.review.submit(answer, region)
        self._send(200, json.dumps({"ok": accepted}).encode("utf-8"), "application/json")


class ReviewServer:
    """Serves one question at a time and blocks the pipeline until it is answered."""

    def __init__(self, out_dir: str | Path, port: int = 0, timeout: int = 1800) -> None:
        self.out_dir = Path(out_dir).resolve()
        self.port = port
        self.timeout = timeout
        self._httpd: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()
        self._pending: dict | None = None
        self._answer: str | None = None
        self._region: dict | None = None
        self.last_region: dict | None = None
        self._event = threading.Event()
        self._history: list[dict] = []
        self._counter = 0
        self.finished = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> str:
        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        httpd.review = self  # type: ignore[attr-defined]
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{httpd.server_address[1]}/"

    def stop(self) -> None:
        self.finished = True
        # Let a polling browser see the final state before the socket closes.
        if self._httpd is not None:
            threading.Timer(2.0, self._httpd.shutdown).start()

    # ---------------------------------------------------------------- state

    def state(self) -> dict:
        with self._lock:
            return {
                "pending": self._pending,
                "history": self._history[-12:],
                "finished": self.finished,
            }

    def read_image(self, rel: str) -> bytes | None:
        """Only files inside out_dir are readable, whatever the query says."""
        if not rel:
            return None
        try:
            target = (self.out_dir / rel).resolve()
        except OSError:
            return None
        if not target.is_relative_to(self.out_dir) or target.suffix.lower() != ".png":
            LOG.warning("UI refused an image outside the run directory: %r", rel)
            return None
        try:
            return target.read_bytes()
        except OSError:
            return None

    def submit(self, answer: str, region: dict | None = None) -> bool:
        region = self._clean_region(region)
        with self._lock:
            if self._pending is None:
                return False
            self._history.append(
                {
                    "round": self._pending.get("round", ""),
                    "stage": self._pending.get("stage", ""),
                    "answer": answer + (" [영역 지정]" if region else ""),
                }
            )
            self._answer = answer
            self._region = region
            self._pending = None
        self._event.set()
        return True

    @staticmethod
    def _clean_region(region) -> dict | None:
        """Accept only a sane normalised rect; a bad one is dropped, not trusted."""
        if not isinstance(region, dict):
            return None
        out = {}
        for key in ("x", "y", "w", "h"):
            try:
                value = float(region.get(key))
            except (TypeError, ValueError):
                return None
            if not 0.0 <= value <= 1.0:
                return None
            out[key] = round(value, 4)
        if out["w"] <= 0 or out["h"] <= 0:
            return None
        label = region.get("panel")
        out["panel"] = str(label)[:60] if isinstance(label, str) else ""
        return out

    # ------------------------------------------------------------- prompter

    def ask(self, prompt: str, context: dict | None = None) -> str:
        """Publish a question and block until the browser answers or time runs out."""
        context = context or {}
        image = context.get("image")
        rel = ""
        if image:
            try:
                rel = str(Path(image).resolve().relative_to(self.out_dir))
            except (ValueError, OSError):
                rel = ""

        with self._lock:
            self._counter += 1
            self._answer = None
            self._region = None
            self.last_region = None
            self._event.clear()
            self._pending = {
                "id": str(self._counter),
                "stage": context.get("stage", ""),
                "round": context.get("round", ""),
                "title": context.get("title", prompt),
                "prompt": prompt,
                "data": context.get("data"),
                "image": rel,
                "panels": context.get("image_panels") or [],
                "choices": context.get("choices", []),
                "text": bool(context.get("text", True)),
                "enter_value": context.get("enter_value"),
            }

        if not self._event.wait(self.timeout):
            with self._lock:
                self._pending = None
            LOG.warning("UI: no answer within %ss; treating it as no input", self.timeout)
            return ""
        with self._lock:
            self.last_region = self._region
            self._region = None
            return self._answer or ""
