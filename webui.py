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
  <details id="advanced">
    <summary style="cursor:pointer;font-weight:600;font-size:16px;outline:none">⚙️ Подробные настройки</summary>
    <p style="color:#666;font-size:13px">Применяются к выбранному выше сайту: <b id="cfgSite">—</b>. Хранятся только в памяти — не сохраняются на диск и не попадают в git. Нужно вводить заново после перезапуска сервера.</p>

    <div style="margin-top:12px">
      <label style="font-size:13px">Задержка между запросами, сек</label>
      <div style="display:flex;gap:10px;align-items:center">
        <input id="delayMin" type="number" step="0.1" min="0" placeholder="мин" style="width:90px;padding:8px;border:1px solid #ccc;border-radius:4px">
        <span style="color:#888">—</span>
        <input id="delayMax" type="number" step="0.1" min="0" placeholder="макс" style="width:90px;padding:8px;border:1px solid #ccc;border-radius:4px">
      </div>
    </div>

    <div style="display:grid;gap:10px;margin-top:14px;">
      <div>
        <label style="font-size:13px">Прокси<br><small style="font-weight:normal;color:#888">http://user:pass@host:port или socks5://...</small></label>
        <input id="proxyUrl" type="text" placeholder="http://..." style="width:100%;padding:8px;border:1px solid #ccc;border-radius:4px;font-size:13px;box-sizing:border-box">
      </div>
      <div>
        <label style="font-size:13px">URL смены IP прокси<br><small style="font-weight:normal;color:#888">GET-запрос для ротации IP (опционально)</small></label>
        <input id="proxyRefresh" type="text" placeholder="https://.../refresh-ip" style="width:100%;padding:8px;border:1px solid #ccc;border-radius:4px;font-size:13px;box-sizing:border-box">
      </div>
    </div>

    <button class="btn btn-blue" style="margin-top:14px" onclick="saveSettings()">💾 Сохранить</button>
    <span id="cfgStatus" style="margin-left:12px;font-size:13px;color:#28a745"></span>
  </details>
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

function currentSite() { return document.getElementById('site').value; }

function loadSettings() {
  const site = currentSite();
  document.getElementById('cfgSite').textContent = site;
  fetch('/settings?site=' + encodeURIComponent(site)).then(r => r.json()).then(d => {
    document.getElementById('proxyUrl').value = d.proxy || '';
    document.getElementById('proxyRefresh').value = d.proxy_refresh || '';
    document.getElementById('delayMin').value = d.delay_min || '';
    document.getElementById('delayMax').value = d.delay_max || '';
  });
}

function saveSettings() {
  const payload = {
    site: currentSite(),
    proxy: document.getElementById('proxyUrl').value.trim(),
    proxy_refresh: document.getElementById('proxyRefresh').value.trim(),
    delay_min: document.getElementById('delayMin').value.trim(),
    delay_max: document.getElementById('delayMax').value.trim(),
  };
  fetch('/settings', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).then(r => r.json()).then(d => {
    const st = document.getElementById('cfgStatus');
    if (d.ok) { st.style.color='#28a745'; st.textContent = '✓ Сохранено'; setTimeout(() => st.textContent = '', 3000); }
    else { st.style.color='#dc3545'; st.textContent = 'Ошибка: ' + (d.error || '?'); }
  });
}

document.getElementById('site').addEventListener('change', loadSettings);
loadSettings();
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


def _site_key(site: str) -> str:
    """Match the env-var suffix used by HttpClient / BaseScraper."""
    return site.replace(".", "_").replace("-", "_").upper()


# Maps the UI field name → env-var prefix for a given site.
_FIELD_ENV = {
    "proxy": "PROXY__",
    "proxy_refresh": "PROXY_REFRESH__",
    "delay_min": "DELAY_MIN__",
    "delay_max": "DELAY_MAX__",
}


@app.route("/settings", methods=["GET", "POST"])
@requires_auth
def settings():
    global _runtime_env
    if request.method == "POST":
        data = request.get_json() or {}
        site = (data.get("site") or "").strip()
        if not site or site == "all":
            return jsonify({"error": "Выберите конкретный сайт (не «all»)."})
        key = _site_key(site)
        for field, prefix in _FIELD_ENV.items():
            env_name = f"{prefix}{key}"
            v = str(data.get(field, "")).strip()
            if v:
                _runtime_env[env_name] = v
            else:
                _runtime_env.pop(env_name, None)
        return jsonify({"ok": True})

    site = (request.args.get("site") or "").strip()
    key = _site_key(site)
    return jsonify({field: _runtime_env.get(f"{prefix}{key}", "")
                    for field, prefix in _FIELD_ENV.items()})


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
