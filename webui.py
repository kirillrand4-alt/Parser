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
<html lang="ru" data-theme="aurora">
<head>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&family=Cinzel:wght@600;700&display=swap" rel="stylesheet">
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
    0%,100% { box-shadow: 0 6px 20px var(--primary-glow); }
    50%     { box-shadow: 0 6px 30px var(--primary-glow), 0 0 60px var(--primary-glow); }
  }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }
  @keyframes gold-shimmer { 0%{background-position:0% 50%} 50%{background-position:100% 50%} 100%{background-position:0% 50%} }
  @keyframes corner-fade { from{opacity:0} to{opacity:.9} }

  /* ════ THEME: Aurora Glass (default) ════ */
  :root {
    --font-body: 'Inter', system-ui, -apple-system, sans-serif;
    --font-head: 'Inter', system-ui, sans-serif;
    --bg: #07090f;
    --bg-image: none;
    --text: #e8ecf4;
    --dim: #8b97b5;
    --text-dim: #8b97b5;
    --orb-display: block;
    --corner-display: none;
    --card-bg: rgba(255,255,255,.045);
    --card-blur: 20px;
    --card-border: rgba(255,255,255,.10);
    --card-border-hover: rgba(255,255,255,.18);
    --card-radius: 20px;
    --card-shadow: 0 12px 40px rgba(0,0,0,.4), inset 0 1px 0 rgba(255,255,255,.08);
    --card-shadow-hover: 0 16px 48px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.14);
    --label-color: #e8ecf4;
    --label-transform: none;
    --label-spacing: normal;
    --input-bg: rgba(0,0,0,.35);
    --input-border: rgba(255,255,255,.15);
    --input-text: #e8ecf4;
    --input-radius: 12px;
    --input-font: var(--font-body);
    --input-size: 15px;
    --accent: #7c9cff;
    --accent-soft: rgba(124,156,255,.2);
    --btn-radius: 14px;
    --btn-font: var(--font-body);
    --btn-size: 15px;
    --btn-spacing: normal;
    --primary-grad: linear-gradient(135deg, #7c9cff, #a06cff);
    --primary-glow: rgba(124,108,255,.5);
    --primary-text: #fff;
    --red-grad: linear-gradient(135deg, #ff6b8a, #ff4757);
    --green-grad: linear-gradient(135deg, #2bd9a0, #1aac7a);
    --secondary-bg: rgba(255,255,255,.1);
    --secondary-border: rgba(255,255,255,.2);
    --secondary-text: #fff;
    --status-color: #7c9cff;
    --log-bg: rgba(0,0,0,.45);
    --log-border: rgba(255,255,255,.1);
    --log-text: #c9d4e8;
    --badge-bg: rgba(124,156,255,.12);
    --badge-color: #7c9cff;
    --badge-border: rgba(124,156,255,.35);
    --list-border: rgba(255,255,255,.08);
    --list-hover: rgba(255,255,255,.04);
    --link: #7c9cff;
    --link-hover: #a5bcff;
  }

  /* ════ THEME: Ivory & Gold ════ */
  html[data-theme="ivory"] {
    --font-body: 'Cormorant Garamond', serif;
    --font-head: 'Cinzel', serif;
    --bg: #f3ecdc;
    --bg-image: radial-gradient(circle at 12% 8%, rgba(230,200,120,.18) 0, transparent 30%),
                radial-gradient(circle at 88% 80%, rgba(184,144,47,.12) 0, transparent 35%);
    --text: #3a2f1c;
    --dim: #9a8a6a;
    --text-dim: #9a8a6a;
    --orb-display: none;
    --corner-display: block;
    --card-bg: linear-gradient(180deg, #fffaf0, #f6efe0);
    --card-blur: 0px;
    --card-border: rgba(184,144,47,.4);
    --card-border-hover: rgba(184,144,47,.7);
    --card-radius: 10px;
    --card-shadow: 0 8px 30px rgba(120,90,30,.15), inset 0 0 0 1px rgba(255,255,255,.5), inset 0 0 50px rgba(230,200,120,.12);
    --card-shadow-hover: 0 12px 36px rgba(120,90,30,.22), inset 0 0 0 1px rgba(255,255,255,.6), inset 0 0 50px rgba(230,200,120,.18);
    --label-color: #b8902f;
    --label-transform: uppercase;
    --label-spacing: 1.5px;
    --input-bg: #fffdf6;
    --input-border: #b8902f;
    --input-text: #3a2f1c;
    --input-radius: 8px;
    --input-size: 17px;
    --accent: #b8902f;
    --accent-soft: rgba(230,200,120,.45);
    --btn-radius: 24px;
    --btn-size: 13px;
    --btn-spacing: 1px;
    --primary-grad: linear-gradient(135deg, #e6c878 0%, #b8902f 45%, #8f6f20 100%);
    --primary-glow: rgba(184,144,47,.45);
    --primary-text: #fff;
    --red-grad: linear-gradient(135deg, #b85050, #9c3b3b);
    --green-grad: linear-gradient(135deg, #caa85a, #b8902f);
    --secondary-bg: #fffdf6;
    --secondary-border: #b8902f;
    --secondary-text: #b8902f;
    --status-color: #b8902f;
    --log-bg: #2a2418;
    --log-border: #b8902f;
    --log-text: #d8cba0;
    --badge-bg: linear-gradient(135deg, #e6c878, #b8902f, #e6c878);
    --badge-color: #fff;
    --badge-border: transparent;
    --list-border: rgba(184,144,47,.4);
    --list-hover: rgba(230,200,120,.12);
    --link: #b8902f;
    --link-hover: #8f6f20;
  }
  html[data-theme="ivory"] .badge { background-size: 200% 100%; animation: gold-shimmer 4s linear infinite; }
  html[data-theme="ivory"] h1 { justify-content: center; }
  html[data-theme="ivory"] .btn { font-weight: 600; }

  body {
    font-family: var(--font-body);
    color: var(--text); min-height: 100vh;
    padding: 40px 20px 60px;
    background: var(--bg); background-image: var(--bg-image);
    overflow-x: hidden; position: relative;
    transition: background-color .4s, color .4s;
  }

  /* Theme switcher (top-right) */
  .theme-switch {
    position: fixed; top: 14px; right: 16px; z-index: 60;
    display: flex; align-items: center; gap: 6px;
  }
  .theme-switch select {
    min-width: 0; font-size: 13px; padding: 7px 10px;
    border-radius: 10px; cursor: pointer;
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

  .orb { display: var(--orb-display); }

  .wrap { max-width: 920px; margin: 0 auto; position: relative; z-index: 1; }

  h1 {
    font-family: var(--font-head);
    font-weight: 800; font-size: 30px; letter-spacing: .5px;
    display: flex; align-items: center; gap: 12px; margin-bottom: 26px;
    color: var(--label-color);
    animation: card-in .6s ease both;
  }
  h1 .badge {
    font-family: var(--font-head); font-size: 12px; font-weight: 600; letter-spacing: 1px;
    color: var(--badge-color); background: var(--badge-bg);
    border: 1px solid var(--badge-border); padding: 5px 12px; border-radius: 20px;
  }

  .card {
    position: relative;
    background: var(--card-bg);
    backdrop-filter: blur(var(--card-blur)); -webkit-backdrop-filter: blur(var(--card-blur));
    border: 1px solid var(--card-border); border-radius: var(--card-radius);
    padding: 24px; margin-bottom: 20px;
    box-shadow: var(--card-shadow);
    transition: border-color .3s, box-shadow .3s;
    animation: card-in .6s ease both;
  }
  .card:nth-child(2) { animation-delay: .08s; }
  .card:nth-child(3) { animation-delay: .16s; }
  .card:nth-child(4) { animation-delay: .24s; }
  .card:nth-child(5) { animation-delay: .32s; }
  .card:hover {
    border-color: var(--card-border-hover);
    box-shadow: var(--card-shadow-hover);
  }

  /* Gold filigree corners (Ivory theme only; injected by JS) */
  .corner {
    position: absolute; width: 46px; height: 46px;
    background-size: contain; background-repeat: no-repeat;
    display: var(--corner-display); opacity: .9; pointer-events: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cg fill='none' stroke='%23b8902f' stroke-width='2.2'%3E%3Cpath d='M4 96 L4 30 Q4 4 30 4 L96 4'/%3E%3Cpath d='M13 96 L13 33 Q13 13 33 13 L96 13'/%3E%3Cpath d='M13 52 Q34 52 34 31 Q34 13 52 13'/%3E%3Ccircle cx='24' cy='24' r='4.5' fill='%23b8902f'/%3E%3Cpath d='M24 96 Q24 68 42 58 Q56 50 52 36'/%3E%3Cpath d='M52 13 Q70 13 78 26'/%3E%3C/g%3E%3C/svg%3E");
    animation: corner-fade .7s ease both;
  }
  .corner.tl { top: 7px; left: 7px; }
  .corner.tr { top: 7px; right: 7px; transform: scaleX(-1); }
  .corner.bl { bottom: 7px; left: 7px; transform: scaleY(-1); }
  .corner.br { bottom: 7px; right: 7px; transform: scale(-1); }

  label {
    font-family: var(--font-head); font-weight: 600; display: block; margin-bottom: 8px;
    color: var(--label-color); text-transform: var(--label-transform); letter-spacing: var(--label-spacing);
  }
  small { color: var(--dim); }

  select, input[type=text], input[type=number] {
    background: var(--input-bg); color: var(--input-text);
    border: 1px solid var(--input-border); border-radius: var(--input-radius);
    padding: 11px 14px; font-size: var(--input-size); font-family: var(--font-body);
    transition: border-color .2s, box-shadow .2s;
  }
  select:focus, input:focus {
    outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-soft);
  }
  select { cursor: pointer; min-width: 240px; }

  /* Buttons with shine sweep */
  .btn {
    border: none; color: var(--primary-text); cursor: pointer;
    font-size: var(--btn-size); font-weight: 700; font-family: var(--font-head);
    letter-spacing: var(--btn-spacing);
    padding: 13px 24px; border-radius: var(--btn-radius); margin-right: 10px;
    transition: transform .15s, box-shadow .15s, filter .15s;
    position: relative; overflow: hidden;
  }
  .btn::after {
    content: ''; position: absolute; top: 0; left: -100%; width: 60%; height: 100%;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.3), transparent);
    transition: left .4s ease;
  }
  .btn:hover::after { left: 140%; }
  .btn:hover:not(:disabled) { transform: translateY(-2px); filter: brightness(1.08); }
  .btn:active { transform: translateY(1px); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }

  /* btn-orange = primary start action */
  .btn-orange {
    background: var(--primary-grad); color: var(--primary-text);
    box-shadow: 0 4px 16px var(--primary-glow), inset 0 1px 0 rgba(255,255,255,.4);
    animation: glow-pulse 3s ease-in-out infinite;
  }
  .btn-orange:hover { animation: none; }
  .btn-orange:disabled { animation: none; }

  .btn-red { background: var(--red-grad); color: #fff; box-shadow: 0 4px 16px rgba(0,0,0,.2); }
  .btn-blue {
    background: var(--secondary-bg); color: var(--secondary-text);
    border: 1px solid var(--secondary-border);
  }
  .btn-green { background: var(--green-grad); color: var(--primary-text); box-shadow: 0 4px 16px var(--primary-glow); }
  a.btn { display: inline-block; text-decoration: none; }

  .radio-group { display: flex; gap: 24px; margin: 16px 0; }
  .radio-group label {
    font-family: var(--font-body); font-weight: normal; display: flex; align-items: center; gap: 8px;
    cursor: pointer; color: var(--dim); text-transform: none; letter-spacing: normal;
  }
  input[type=radio] { accent-color: var(--accent); width: 16px; height: 16px; }

  #status {
    margin-top: 14px; font: 600 14px var(--font-body); color: var(--status-color);
    display: flex; align-items: center; gap: 8px;
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%; background: var(--status-color);
    animation: blink 1.4s ease-in-out infinite; flex-shrink: 0;
  }

  #log {
    background: var(--log-bg); border: 1px solid var(--log-border);
    border-radius: 14px; padding: 16px;
    font: 13px ui-monospace,monospace; color: var(--log-text);
    height: 440px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-all;
  }
  #log::-webkit-scrollbar { width: 8px; }
  #log::-webkit-scrollbar-thumb { background: rgba(128,128,128,.4); border-radius: 4px; }
  .log-err  { color: #ff7c9c; }
  .log-warn { color: #ffd27c; }
  .log-ok   { color: #5ce0b0; }

  .files-list { list-style: none; padding: 0; margin: 0; }
  .files-list li {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 6px; border-bottom: 1px solid var(--list-border);
    border-radius: 6px; transition: background .2s;
  }
  .files-list li:hover { background: var(--list-hover); }
  .files-list li:last-child { border-bottom: none; }
  .files-list a { color: var(--link); text-decoration: none; font-weight: 600; font-family: var(--font-head); transition: color .15s; }
  .files-list a:hover { color: var(--link-hover); }
  .files-list small { font-family: ui-monospace,monospace; }

  details > summary { user-select: none; color: var(--label-color); }
  details[open] > summary { margin-bottom: 8px; }
</style>
</head>
<body>
<div class="theme-switch">
  <select id="theme" onchange="setTheme(this.value)" title="Тема оформления">
    <option value="aurora">🌌 Aurora Glass</option>
    <option value="ivory">👑 Ivory &amp; Gold</option>
  </select>
</div>
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

// ── Theme switcher ──
function injectCorners() {
  document.querySelectorAll('.card').forEach(card => {
    if (card.querySelector('.corner')) return;  // already done
    ['tl','tr','bl','br'].forEach(p => {
      const d = document.createElement('div');
      d.className = 'corner ' + p;
      card.appendChild(d);
    });
  });
}
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('parserTheme', t); } catch(e) {}
}
injectCorners();
(function initTheme(){
  let t = 'aurora';
  try { t = localStorage.getItem('parserTheme') || 'aurora'; } catch(e) {}
  document.getElementById('theme').value = t;
  setTheme(t);
})();

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
