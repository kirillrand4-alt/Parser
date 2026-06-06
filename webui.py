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

# Runtime env overrides (never written to disk or git).
# Injected into subprocess env on each /start call.
_runtime_env: dict[str, str] = {}

HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Parser UI</title>
<style>
  body { font-family: sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }
  h1 { color: #333; }
  .card { background: #fff; border-radius: 8px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.1); }
  label { font-weight: 600; display: block; margin-bottom: 6px; }
  select, .btn { padding: 10px 18px; border-radius: 5px; border: 1px solid #ccc; font-size: 15px; cursor: pointer; }
  .btn { border: none; color: #fff; margin-right: 8px; }
  .btn-green  { background: #28a745; }
  .btn-orange { background: #fd7e14; }
  .btn-red    { background: #dc3545; }
  .btn-blue   { background: #007bff; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
  .radio-group { display: flex; gap: 20px; margin: 12px 0; }
  .radio-group label { font-weight: normal; display: flex; align-items: center; gap: 6px; cursor: pointer; }
  #log { background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 13px;
         padding: 16px; border-radius: 6px; height: 420px; overflow-y: auto;
         white-space: pre-wrap; word-break: break-all; }
  .log-err  { color: #f48771; }
  .log-warn { color: #dcdcaa; }
  .log-ok   { color: #4ec9b0; }
  #status { font-weight: 600; margin-top: 10px; }
  .files-list { list-style: none; padding: 0; }
  .files-list li { padding: 6px 0; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: center; }
  .files-list a { color: #007bff; text-decoration: none; }
</style>
</head>
<body>
<h1>🔧 Parser UI</h1>

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
    <button class="btn btn-green"  id="btnStart" onclick="startScrape()">▶ Запустить</button>
    <button class="btn btn-red"    id="btnStop"  onclick="stopScrape()" disabled>⏹ Остановить</button>
  </div>
  <div id="status">Готов к запуску.</div>
</div>

<div class="card">
  <label>Лог</label>
  <div id="log"></div>
</div>

<div class="card">
  <label>⚙️ Настройки прокси (pnevmo-sklad.ru)</label>
  <p style="color:#666;font-size:13px;margin-top:0">Значения хранятся только в памяти — не сохраняются на диск и не попадают в git. Нужно вводить заново после перезапуска сервера.</p>
  <div style="display:grid;gap:10px;margin-top:8px;">
    <div>
      <label style="font-size:13px">PROXY__PNEVMO_SKLAD_RU<br><small style="font-weight:normal;color:#888">socks5://user:pass@host:port</small></label>
      <input id="proxyUrl" type="text" placeholder="socks5://..." style="width:100%;padding:8px;border:1px solid #ccc;border-radius:4px;font-size:13px;box-sizing:border-box">
    </div>
    <div>
      <label style="font-size:13px">PROXY_REFRESH__PNEVMO_SKLAD_RU<br><small style="font-weight:normal;color:#888">URL для смены IP (GET-запрос)</small></label>
      <input id="proxyRefresh" type="text" placeholder="https://api.example.com/refresh-ip" style="width:100%;padding:8px;border:1px solid #ccc;border-radius:4px;font-size:13px;box-sizing:border-box">
    </div>
  </div>
  <button class="btn btn-blue" style="margin-top:12px" onclick="saveProxy()">💾 Сохранить</button>
  <span id="proxyStatus" style="margin-left:12px;font-size:13px;color:#28a745"></span>
</div>

<div class="card">
  <label>Результаты (CSV)</label>
  <ul class="files-list" id="filesList"></ul>
  <button class="btn btn-blue" style="margin-top:12px" onclick="loadFiles()">🔄 Обновить список</button>
</div>

<script>
let evtSource = null;

function startScrape() {
  const site = document.getElementById('site').value;
  const mode = document.querySelector('input[name=mode]:checked').value;
  document.getElementById('log').innerHTML = '';
  document.getElementById('status').textContent = 'Запускаю...';
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
    document.getElementById('status').textContent = 'Остановлено.';
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
      document.getElementById('status').textContent = 'Готово ✓';
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

function loadFiles() {
  fetch('/files').then(r => r.json()).then(files => {
    const ul = document.getElementById('filesList');
    ul.innerHTML = '';
    if (!files.length) { ul.innerHTML = '<li>Файлов пока нет.</li>'; return; }
    files.forEach(f => {
      const li = document.createElement('li');
      li.innerHTML = `<span>${f.name} <small style="color:#999">${f.size}</small></span>
        <a href="/download/${encodeURIComponent(f.name)}">⬇ Скачать</a>`;
      ul.appendChild(li);
    });
  });
}

function saveProxy() {
  const proxy = document.getElementById('proxyUrl').value.trim();
  const refresh = document.getElementById('proxyRefresh').value.trim();
  fetch('/settings', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({PROXY__PNEVMO_SKLAD_RU: proxy, PROXY_REFRESH__PNEVMO_SKLAD_RU: refresh})
  }).then(r => r.json()).then(d => {
    const st = document.getElementById('proxyStatus');
    if (d.ok) { st.textContent = '✓ Сохранено'; setTimeout(() => st.textContent = '', 3000); }
    else { st.style.color='#dc3545'; st.textContent = 'Ошибка: ' + (d.error || '?'); }
  });
}

// Load current settings on page load
fetch('/settings').then(r => r.json()).then(d => {
  if (d.PROXY__PNEVMO_SKLAD_RU) document.getElementById('proxyUrl').value = d.PROXY__PNEVMO_SKLAD_RU;
  if (d.PROXY_REFRESH__PNEVMO_SKLAD_RU) document.getElementById('proxyRefresh').value = d.PROXY_REFRESH__PNEVMO_SKLAD_RU;
});

loadFiles();
</script>
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


@app.route("/settings", methods=["GET", "POST"])
@requires_auth
def settings():
    global _runtime_env
    if request.method == "POST":
        data = request.get_json() or {}
        allowed = {"PROXY__PNEVMO_SKLAD_RU", "PROXY_REFRESH__PNEVMO_SKLAD_RU"}
        for k in allowed:
            v = data.get(k, "").strip()
            if v:
                _runtime_env[k] = v
            else:
                _runtime_env.pop(k, None)
        return jsonify({"ok": True})
    return jsonify({k: _runtime_env.get(k, "") for k in
                    ("PROXY__PNEVMO_SKLAD_RU", "PROXY_REFRESH__PNEVMO_SKLAD_RU")})


@app.route("/log-stream")
@requires_auth
def log_stream():
    def generate():
        while True:
            try:
                item = _log_queue.get(timeout=30)
            except queue.Empty:
                yield "data: {\"line\": \"[ping]\"}\n\n"
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
        result.append({"name": f.name, "size": size_str})
    return jsonify(result)


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
