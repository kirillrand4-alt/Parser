"""Simple web UI to run the scraper.

Usage (local):
    pip install flask
    python webui.py
    Open http://localhost:5000

Usage (server, behind nginx + HTTPS):
    Set a login/password and bind to localhost; nginx proxies :443 → :5000.
    WEBUI_USER=admin WEBUI_PASSWORD=secret python webui.py

Auth: HTTP Basic. If WEBUI_PASSWORD is unset, the UI is OPEN (local use only).
Set WEBUI_USER / WEBUI_PASSWORD to require a login.
"""
from __future__ import annotations

import io
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, send_file

app = Flask(__name__)

# --- Auth ----------------------------------------------------------------
WEBUI_USER = os.getenv("WEBUI_USER", "admin")
WEBUI_PASSWORD = os.getenv("WEBUI_PASSWORD", "")  # empty → no auth (local only)


def _check_auth(user: str, pw: str) -> bool:
    # constant-time compare to avoid timing leaks
    return (secrets.compare_digest(user, WEBUI_USER)
            and secrets.compare_digest(pw, WEBUI_PASSWORD))


def requires_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not WEBUI_PASSWORD:  # auth disabled
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response(
                "Требуется авторизация.", 401,
                {"WWW-Authenticate": 'Basic realm="Parser UI"'})
        return f(*args, **kwargs)
    return wrapped

SITES = [
    "all",
    "rutector.ru",
    "v-p-k.ru",
    "pnevmoteh.ru",
    "pnevmo-sklad.ru",
    "aerocompressors.ru",
    "compressortyt.ru",
]

DATA_DIR = Path("data")
CACHE_DIR = Path(os.getenv("CHECKPOINT_DIR", "cache"))

# Active process + log queue (one scrape at a time)
_proc: subprocess.Popen | None = None
_log_queue: queue.Queue = queue.Queue()
_proc_lock = threading.Lock()

# Runtime env overrides — persisted to .runtime_settings.json (gitignored).
# Injected into subprocess env on each /start call.
_SETTINGS_FILE = Path(__file__).parent / ".runtime_settings.json"

def _load_runtime_env() -> dict:
    try:
        return json.loads(_SETTINGS_FILE.read_text())
    except Exception:
        return {}

def _save_runtime_env(d: dict) -> None:
    _SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2))

_runtime_env: dict[str, str] = _load_runtime_env()

HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Parser UI</title>
<style>
  * { box-sizing: border-box; margin: 0; }

  /* ── Animated aurora background ── */
  @keyframes float-orb-1 {
    0%,100% { transform: translate(0,0) scale(1); }
    33%     { transform: translate(60px,-40px) scale(1.15); }
    66%     { transform: translate(-40px,30px) scale(0.9); }
  }
  @keyframes float-orb-2 {
    0%,100% { transform: translate(0,0) scale(1); }
    40%     { transform: translate(-70px,50px) scale(1.1); }
    70%     { transform: translate(50px,-30px) scale(0.95); }
  }
  @keyframes float-orb-3 {
    0%,100% { transform: translate(0,0) scale(1); }
    50%     { transform: translate(30px,60px) scale(1.2); }
  }
  @keyframes card-in {
    from { opacity:0; transform:translateY(22px); }
    to   { opacity:1; transform:translateY(0); }
  }
  @keyframes glow-pulse {
    0%,100% { box-shadow: 0 6px 20px rgba(124,108,255,.5); }
    50%     { box-shadow: 0 6px 30px rgba(124,108,255,.85), 0 0 60px rgba(124,108,255,.25); }
  }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }

  body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    color: #e8ecf4; min-height: 100vh;
    padding: 40px 20px 60px;
    background: #07090f; overflow-x: hidden; position: relative;
  }

  /* Floating aurora orbs */
  .orb {
    position: fixed; border-radius: 50%; filter: blur(80px);
    pointer-events: none; z-index: 0;
  }
  .orb-1 {
    width: 600px; height: 600px; top: -120px; left: -100px;
    background: radial-gradient(circle, #1a2456 0%, transparent 70%);
    animation: float-orb-1 18s ease-in-out infinite;
  }
  .orb-2 {
    width: 500px; height: 500px; top: 50px; right: -80px;
    background: radial-gradient(circle, #3a1a56 0%, transparent 70%);
    animation: float-orb-2 22s ease-in-out infinite;
  }
  .orb-3 {
    width: 400px; height: 400px; bottom: -80px; left: 30%;
    background: radial-gradient(circle, #0a3a4a 0%, transparent 70%);
    animation: float-orb-3 16s ease-in-out infinite;
  }

  .wrap { max-width: 920px; margin: 0 auto; position: relative; z-index: 1; }

  h1 {
    font-weight: 800; font-size: 30px; letter-spacing: -.5px;
    display: flex; align-items: center; gap: 12px; margin-bottom: 26px;
    animation: card-in .6s ease both;
  }
  h1 .badge {
    font-family: ui-monospace, monospace; font-size: 12px; font-weight: 600;
    color: #7c9cff; background: rgba(124,156,255,.12);
    border: 1px solid rgba(124,156,255,.35); padding: 5px 11px; border-radius: 20px;
  }

  .card {
    background: rgba(255,255,255,.045);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    border: 1px solid rgba(255,255,255,.10); border-radius: 20px;
    padding: 24px; margin-bottom: 20px;
    box-shadow: 0 12px 40px rgba(0,0,0,.4), inset 0 1px 0 rgba(255,255,255,.08);
    transition: border-color .3s, box-shadow .3s;
    animation: card-in .6s ease both;
  }
  .card:nth-child(2) { animation-delay: .08s; }
  .card:nth-child(3) { animation-delay: .16s; }
  .card:nth-child(4) { animation-delay: .24s; }
  .card:nth-child(5) { animation-delay: .32s; }
  .card:hover {
    border-color: rgba(255,255,255,.18);
    box-shadow: 0 16px 48px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.14);
  }

  label { font-weight: 600; display: block; margin-bottom: 8px; color: #e8ecf4; }
  small { color: #8b97b5; }

  select, input[type=text], input[type=number] {
    background: rgba(0,0,0,.35); color: #e8ecf4;
    border: 1px solid rgba(255,255,255,.15); border-radius: 12px;
    padding: 11px 14px; font-size: 15px; font-family: inherit;
    transition: border-color .2s, box-shadow .2s;
  }
  select:focus, input:focus {
    outline: none; border-color: #7c9cff;
    box-shadow: 0 0 0 3px rgba(124,156,255,.2);
  }
  select { cursor: pointer; min-width: 240px; }

  /* Buttons with shine sweep */
  .btn {
    border: none; color: #fff; cursor: pointer;
    font-size: 15px; font-weight: 700; font-family: inherit;
    padding: 13px 24px; border-radius: 14px; margin-right: 10px;
    transition: transform .15s, box-shadow .15s, filter .15s;
    position: relative; overflow: hidden;
  }
  .btn::after {
    content: ''; position: absolute; top: 0; left: -100%; width: 60%; height: 100%;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.18), transparent);
    transition: left .4s ease;
  }
  .btn:hover::after { left: 140%; }
  .btn:hover:not(:disabled) { transform: translateY(-2px); filter: brightness(1.12); }
  .btn:active { transform: translateY(1px); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }

  /* btn-orange = primary start action */
  .btn-orange {
    background: linear-gradient(135deg, #7c9cff, #a06cff);
    animation: glow-pulse 3s ease-in-out infinite;
  }
  .btn-orange:hover { animation: none; box-shadow: 0 8px 28px rgba(124,108,255,.7); }
  .btn-orange:disabled { animation: none; }

  .btn-red {
    background: linear-gradient(135deg, #ff6b8a, #ff4757);
    box-shadow: 0 6px 20px rgba(255,71,87,.4);
  }
  .btn-blue {
    background: rgba(255,255,255,.1);
    border: 1px solid rgba(255,255,255,.2);
  }
  .btn-green {
    background: linear-gradient(135deg, #2bd9a0, #1aac7a);
    box-shadow: 0 6px 20px rgba(43,217,160,.35);
  }
  a.btn { display: inline-block; text-decoration: none; }

  .radio-group { display: flex; gap: 24px; margin: 16px 0; }
  .radio-group label {
    font-weight: 500; display: flex; align-items: center; gap: 8px;
    cursor: pointer; color: #8b97b5; font-weight: normal;
  }
  input[type=radio] { accent-color: #7c9cff; width: 16px; height: 16px; }

  #status {
    margin-top: 14px; font: 600 14px ui-monospace,monospace; color: #7c9cff;
    display: flex; align-items: center; gap: 8px;
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%; background: #7c9cff;
    animation: blink 1.4s ease-in-out infinite; flex-shrink: 0;
  }

  #log {
    background: rgba(0,0,0,.45); border: 1px solid rgba(255,255,255,.1);
    border-radius: 14px; padding: 16px;
    font: 13px ui-monospace,monospace; color: #c9d4e8;
    height: 440px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-all;
  }
  #log::-webkit-scrollbar { width: 8px; }
  #log::-webkit-scrollbar-thumb { background: #2d3848; border-radius: 4px; }
  .log-err  { color: #ff7c9c; }
  .log-warn { color: #ffd27c; }
  .log-ok   { color: #5ce0b0; }

  .files-list { list-style: none; padding: 0; margin: 0; }
  .files-list li {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 6px; border-bottom: 1px solid rgba(255,255,255,.08);
    border-radius: 6px; transition: background .2s;
  }
  .files-list li:hover { background: rgba(255,255,255,.04); }
  .files-list li:last-child { border-bottom: none; }
  .files-list a { color: #7c9cff; text-decoration: none; font-weight: 600; transition: color .15s; }
  .files-list a:hover { color: #a5bcff; }
  .files-list small { font-family: ui-monospace,monospace; }

  details > summary { user-select: none; }
  details[open] > summary { margin-bottom: 8px; }
</style>
</head>
<body>
<div class="orb orb-1"></div>
<div class="orb orb-2"></div>
<div class="orb orb-3"></div>
<div class="wrap">
<h1>🔧 Parser <span class="badge">COMPRESSOR MONITOR</span></h1>

<div class="card">
  <label>Сайт</label>
  <select id="site">
    {% for s in sites %}
    <option value="{{ s }}">{{ s }}</option>
    {% endfor %}
  </select>

  <div class="radio-group" style="margin-top:16px;">
    <label><input type="radio" name="mode" value="resume" checked> Продолжить с последнего места</label>
    <label><input type="radio" name="mode" value="fresh"> Скачать заново (с нуля)</label>
  </div>

  <div style="margin-top:16px;">
    <button class="btn btn-orange" id="btnStart" onclick="startScrape()">▶ Запустить</button>
    <button class="btn btn-red"    id="btnStop"  onclick="stopScrape()" disabled>⏹ Остановить</button>
  </div>
  <div id="status"><span class="status-dot" id="statusDot" style="display:none"></span><span id="statusText">Готов к запуску.</span></div>
</div>

<div class="card">
  <label>Лог</label>
  <div id="log"></div>
</div>

<div class="card">
  <details id="advanced">
    <summary style="cursor:pointer;font-weight:600;font-size:16px;outline:none">⚙️ Подробные настройки</summary>
    <p style="color:var(--text-dim);font-size:13px">Применяются к выбранному выше сайту: <b id="cfgSite">—</b>.</p>
    <div id="proxyWarning" style="display:none;background:rgba(255,209,102,.12);border:1px solid rgba(255,209,102,.4);color:#ffd166;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:10px">
      ⚠️ <b>Прокси сохранён, но скрапер уже запущен.</b> Остановите его и запустите заново — только тогда прокси применится.
    </div>
    <p style="color:var(--text-dim);font-size:12px;margin:0 0 8px">Настройки сохраняются на сервере (вне git). <b>Сохраните настройки ДО нажатия «Запустить»</b> — они применяются при старте скрапера.</p>

    <div style="margin-top:12px">
      <div style="display:flex;gap:32px;flex-wrap:wrap">
        <div>
          <label style="font-size:13px">Задержка между запросами, сек</label>
          <div style="display:flex;gap:10px;align-items:center">
            <input id="delayMin" type="number" step="0.1" min="0" placeholder="мин" style="width:90px;padding:8px;">
            <span style="color:var(--text-dim)">—</span>
            <input id="delayMax" type="number" step="0.1" min="0" placeholder="макс" style="width:90px;padding:8px;">
          </div>
        </div>
        <div>
          <label style="font-size:13px">Потоков (параллельных запросов)</label>
          <div style="display:flex;gap:8px;align-items:center">
            <input id="workers" type="number" step="1" min="1" max="16" placeholder="1" style="width:70px;padding:8px;">
            <small style="color:var(--text-dim)">1 = последовательно</small>
          </div>
        </div>
      </div>
    </div>

    <div style="display:grid;gap:10px;margin-top:14px;">
      <div>
        <label style="font-size:13px">Прокси<br><small style="font-weight:normal;color:var(--text-dim)">http://user:pass@host:port или socks5://...</small></label>
        <input id="proxyUrl" type="text" placeholder="http://..." style="width:100%;padding:8px;font-size:13px;box-sizing:border-box">
      </div>
      <div>
        <label style="font-size:13px">URL смены IP прокси<br><small style="font-weight:normal;color:var(--text-dim)">GET-запрос для ротации IP (опционально)</small></label>
        <input id="proxyRefresh" type="text" placeholder="https://.../refresh-ip" style="width:100%;padding:8px;font-size:13px;box-sizing:border-box">
      </div>
    </div>

    <button class="btn btn-blue" style="margin-top:14px" onclick="saveSettings()">💾 Сохранить</button>
    <span id="cfgStatus" style="margin-left:12px;font-size:13px;color:#28a745"></span>
  </details>
</div>

<div class="card">
  <label>Результаты (CSV)</label>
  <ul class="files-list" id="filesList"></ul>
  <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap">
    <button class="btn btn-blue" onclick="loadFiles()">🔄 Обновить список</button>
    <a id="btnMerge" class="btn btn-green" href="/download-all" style="text-decoration:none">⬇ Скачать общий список</a>
    <button class="btn btn-red" onclick="clearFiles()">🗑 Очистить все CSV</button>
  </div>
  <div id="mergeInfo" style="font-size:13px;color:var(--text-dim);margin-top:8px"></div>
</div>

<script>
let evtSource = null;

function startScrape() {
  const site = document.getElementById('site').value;
  const mode = document.querySelector('input[name=mode]:checked').value;
  document.getElementById('log').innerHTML = '';
  document.getElementById('statusText').textContent = 'Запускаю...'; document.getElementById('statusDot').style.display='inline-block';
  document.getElementById('btnStart').disabled = true;
  document.getElementById('btnStop').disabled = false;

  fetch('/start', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({site, mode})
  }).then(r => r.json()).then(d => {
    if (d.error) { appendLog('ERROR: ' + d.error, 'log-err'); return; }
    listenLog();
  });
}

function stopScrape() {
  fetch('/stop', {method:'POST'}).then(() => {
    document.getElementById('statusText').textContent = 'Остановлено.'; document.getElementById('statusDot').style.display='none';
  });
}

function listenLog() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/log-stream');
  evtSource.onmessage = e => {
    const data = JSON.parse(e.data);
    if (data.done) {
      evtSource.close();
      document.getElementById('btnStart').disabled = false;
      document.getElementById('btnStop').disabled = true;
      document.getElementById('statusText').textContent = 'Готово ✓'; document.getElementById('statusDot').style.display='none';
      loadFiles();
      return;
    }
    appendLog(data.line);
  };
}

function appendLog(line, cls) {
  const el = document.getElementById('log');
  const div = document.createElement('div');
  div.textContent = line;
  if (cls) div.className = cls;
  else if (/error|ERROR|критич/i.test(line)) div.className = 'log-err';
  else if (/warning|WARNING|WARN/i.test(line)) div.className = 'log-warn';
  else if (/✓|done|готово/i.test(line)) div.className = 'log-ok';
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

function clearFiles() {
  if (!confirm('Удалить все CSV-файлы из папки data/?')) return;
  fetch('/clear-files', {method:'POST'}).then(r => r.json()).then(d => {
    if (d.ok) loadFiles();
    else alert('Ошибка: ' + (d.error || '?'));
  });
}

function loadFiles() {
  fetch('/files').then(r => r.json()).then(files => {
    const ul = document.getElementById('filesList');
    const info = document.getElementById('mergeInfo');
    ul.innerHTML = '';
    // update merge button label with total row count
    const total = files.reduce((s, f) => s + (f.rows || 0), 0);
    info.textContent = files.length
      ? `Итого файлов: ${files.length}, строк данных: ${total.toLocaleString('ru')}`
      : '';
    if (!files.length) { ul.innerHTML = '<li>Файлов пока нет.</li>'; return; }
    files.forEach(f => {
      const li = document.createElement('li');
      const rows = f.rows > 0 ? ` · ${f.rows.toLocaleString('ru')} стр.` : '';
      li.innerHTML = `<span>${f.name} <small style="color:var(--text-dim)">${f.size}${rows}</small></span>
        <a href="/download/${encodeURIComponent(f.name)}">⬇ Скачать</a>`;
      ul.appendChild(li);
    });
  });
}

function currentSite() { return document.getElementById('site').value; }

function loadSettings() {
  const site = currentSite();
  document.getElementById('cfgSite').textContent = site;
  fetch('/settings?site=' + encodeURIComponent(site)).then(r => r.json()).then(d => {
    document.getElementById('proxyUrl').value = d.proxy || '';
    document.getElementById('proxyRefresh').value = d.proxy_refresh || '';
    document.getElementById('delayMin').value = d.delay_min || '';
    document.getElementById('delayMax').value = d.delay_max || '';
    document.getElementById('workers').value = d.workers || '';
  });
}

function saveSettings() {
  const payload = {
    site: currentSite(),
    proxy: document.getElementById('proxyUrl').value.trim(),
    proxy_refresh: document.getElementById('proxyRefresh').value.trim(),
    delay_min: document.getElementById('delayMin').value.trim(),
    delay_max: document.getElementById('delayMax').value.trim(),
    workers: document.getElementById('workers').value.trim(),
  };
  fetch('/settings', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).then(r => r.json()).then(d => {
    const st = document.getElementById('cfgStatus');
    if (d.ok) {
      st.style.color='#28a745'; st.textContent = '✓ Сохранено'; setTimeout(() => st.textContent = '', 3000);
      // warn if scraper is already running
      const running = !document.getElementById('btnStop').disabled;
      document.getElementById('proxyWarning').style.display = (running && payload.proxy) ? 'block' : 'none';
    } else { st.style.color='#dc3545'; st.textContent = 'Ошибка: ' + (d.error || '?'); }
  });
}

document.getElementById('site').addEventListener('change', loadSettings);
loadSettings();
loadFiles();
</script>
</div>
</body>
</html>
"""


@app.route("/")
@requires_auth
def index():
    return render_template_string(HTML, sites=SITES)


@app.route("/start", methods=["POST"])
@requires_auth
def start():
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            return jsonify({"error": "Скрапер уже запущен. Остановите его сначала."})

        data = request.get_json()
        site = data.get("site", "all")
        mode = data.get("mode", "resume")  # "resume" | "fresh"

        env = os.environ.copy()
        env.update(_runtime_env)
        env["RESUME"] = "1" if mode == "resume" else "0"

        cmd = [sys.executable, "-m", "monitor", "scrape",
               "--site", site, "--sequential" if site != "all" else "--parallel"]

        _proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(Path(__file__).parent),
        )
        # Drain stdout into queue in background thread
        threading.Thread(target=_drain, args=(_proc,), daemon=True).start()

    return jsonify({"ok": True})


def _drain(proc: subprocess.Popen) -> None:
    for line in proc.stdout:
        _log_queue.put(line.rstrip())
    proc.wait()
    _log_queue.put(None)  # sentinel → done


@app.route("/stop", methods=["POST"])
@requires_auth
def stop():
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
    return jsonify({"ok": True})


def _site_key(site: str) -> str:
    """Match the env-var suffix used by HttpClient / BaseScraper."""
    return site.replace(".", "_").replace("-", "_").upper()


# Maps the UI field name → env-var prefix for a given site.
_FIELD_ENV = {
    "proxy": "PROXY__",
    "proxy_refresh": "PROXY_REFRESH__",
    "delay_min": "DELAY_MIN__",
    "delay_max": "DELAY_MAX__",
    "workers": "WORKERS__",
}


@app.route("/settings", methods=["GET", "POST"])
@requires_auth
def settings():
    global _runtime_env
    # Global env var names (no site suffix) used when site == "all".
    _GLOBAL_ENV = {"proxy": "PROXY", "proxy_refresh": "PROXY_REFRESH",
                   "delay_min": "DELAY_MIN", "delay_max": "DELAY_MAX",
                   "workers": "WORKERS"}

    if request.method == "POST":
        data = request.get_json() or {}
        site = (data.get("site") or "").strip()
        use_global = (not site or site == "all")
        key = "" if use_global else _site_key(site)
        for field, prefix in _FIELD_ENV.items():
            env_name = _GLOBAL_ENV[field] if use_global else f"{prefix}{key}"
            v = str(data.get(field, "")).strip()
            if v:
                _runtime_env[env_name] = v
            else:
                _runtime_env.pop(env_name, None)
        _save_runtime_env(_runtime_env)
        return jsonify({"ok": True})

    site = (request.args.get("site") or "").strip()
    use_global = (not site or site == "all")
    key = "" if use_global else _site_key(site)
    return jsonify({field: _runtime_env.get(
                        _GLOBAL_ENV[field] if use_global else f"{prefix}{key}", "")
                    for field, prefix in _FIELD_ENV.items()})


@app.route("/log-stream")
@requires_auth
def log_stream():
    def generate():
        while True:
            try:
                item = _log_queue.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"  # SSE comment — keeps connection alive, not shown in log
                continue
            if item is None:
                yield "data: {\"done\": true}\n\n"
                break
            payload = json.dumps({"line": item}, ensure_ascii=False)
            yield f"data: {payload}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/files")
@requires_auth
def files():
    DATA_DIR.mkdir(exist_ok=True)
    result = []
    for f in sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        size = f.stat().st_size
        size_str = f"{size // 1024} КБ" if size >= 1024 else f"{size} Б"
        try:
            rows = max(0, f.read_text(encoding="utf-8", errors="replace").count("\n") - 1)
        except Exception:
            rows = 0
        result.append({"name": f.name, "size": size_str, "rows": rows})
    return jsonify(result)


@app.route("/download-all")
@requires_auth
def download_all():
    """Merge all CSVs in data/ into one file and stream it."""
    DATA_DIR.mkdir(exist_ok=True)
    csvs = sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime)
    if not csvs:
        return "Нет CSV-файлов.", 404

    buf = io.StringIO()
    header_written = False
    for path in csvs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = text.splitlines()
        if not lines:
            continue
        if not header_written:
            buf.write(lines[0] + "\n")
            header_written = True
        for line in lines[1:]:
            if line.strip():
                buf.write(line + "\n")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"all_prices_{ts}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.route("/clear-files", methods=["POST"])
@requires_auth
def clear_files():
    DATA_DIR.mkdir(exist_ok=True)
    deleted = 0
    for f in DATA_DIR.glob("*.csv"):
        try:
            f.unlink()
            deleted += 1
        except Exception:
            pass
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/download/<filename>")
@requires_auth
def download(filename: str):
    path = DATA_DIR / filename
    if not path.exists() or not path.resolve().is_relative_to(DATA_DIR.resolve()):
        return "Not found", 404
    return send_file(path.resolve(), as_attachment=True)


if __name__ == "__main__":
    # Behind nginx, bind to localhost (WEBUI_HOST=127.0.0.1). For direct LAN
    # access without a reverse proxy, set WEBUI_HOST=0.0.0.0 (and a password!).
    host = os.getenv("WEBUI_HOST", "127.0.0.1")
    port = int(os.getenv("WEBUI_PORT", "5000"))
    if not WEBUI_PASSWORD and host != "127.0.0.1":
        print("⚠️  WEBUI_PASSWORD не задан, а сервер слушает не только localhost!")
        print("    Установите WEBUI_PASSWORD, иначе интерфейс открыт всем.")
    print(f"Открывайте в браузере: http://{host}:{port}")
    app.run(debug=False, host=host, port=port, threaded=True)
