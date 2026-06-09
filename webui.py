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
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512 MB upload limit

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

# Active process (one scrape at a time). Log lines are buffered in a list so
# the SSE stream can replay them after a page reload while a run is active.
_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_log_lines: list[str] = []      # all lines of the current/last run
_log_done: bool = True          # True when no run is in progress

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

# ── URL-fetch job queue ───────────────────────────────────────────────────────
# POST /fetch-urls stores the URL list here; GET /fetch-urls-stream reads it.
_fetch_job_urls: list[str] = []
_fetch_job_resume: bool = False   # if True, use cached index before live fetch
_fetch_job_lock = threading.Lock()

# ── Price-check index (lazy, rebuilt when CSV files change) ──────────────────
_price_index: dict[str, list[dict]] = {}   # normalized_key → [scraped rows]
_url_index: dict[str, dict] = {}           # product_url (normalized) → row
_price_index_mtime: float = 0.0
_price_index_lock = threading.Lock()

def _norm_key(s: str) -> str:
    import re as _re
    return _re.sub(r"[^A-ZА-ЯЁ0-9]", "", s.upper())

def _extract_brand_model(raw: str) -> tuple[str, str]:
    import html as _html, re as _re
    name = _html.unescape(raw)
    m = _re.search(r'"([^"]+)"\s+(.+)', name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", name.strip()

def _norm_url(url: str) -> str:
    """Normalise a URL for lookup: strip scheme, www, trailing slash."""
    import re as _re
    u = url.strip().lower()
    u = _re.sub(r'^https?://(www\.)?', '', u)
    return u.rstrip('/')

def _build_price_index() -> None:
    """(Re)load all CSVs from data/ into _price_index and _url_index."""
    global _price_index, _url_index, _price_index_mtime
    import csv as _csv
    csvs = list(DATA_DIR.glob("*.csv")) if DATA_DIR.exists() else []
    if not csvs:
        _price_index = {}; _url_index = {}; _price_index_mtime = 0.0
        return
    max_mtime = max(f.stat().st_mtime for f in csvs)
    if max_mtime <= _price_index_mtime and _price_index:
        return
    idx: dict[str, list[dict]] = {}
    uidx: dict[str, dict] = {}
    for path in csvs:
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                for row in _csv.DictReader(fh):
                    k = row.get("normalized_key", "").strip()
                    if k:
                        idx.setdefault(k, []).append(row)
                    pu = row.get("product_url", "").strip()
                    if pu:
                        uidx[_norm_url(pu)] = row
        except Exception:
            pass
    _price_index = idx
    _url_index = uidx
    _price_index_mtime = max_mtime

def _lookup_prices(query_line: str) -> dict:
    """Given one text line from user, find all competitor prices."""
    import re as _re
    line = query_line.strip()
    if not line:
        return {}

    # Try to generate normalized key from the line
    # Case 1: full prokompressor name with "Brand" Model
    brand, model = _extract_brand_model(line)
    candidates = []
    if brand and model:
        candidates.append(_norm_key(brand + model))
    # Case 2: treat the whole line as a key fragment (partial match)
    candidates.append(_norm_key(line))
    # Case 3: URL slug → last path segment
    slug_m = _re.search(r"/([^/]+)/?$", line)
    if slug_m:
        candidates.append(_norm_key(slug_m.group(1)))

    for key in candidates:
        if key and key in _price_index:
            return {"key": key, "rows": _price_index[key]}

    # Partial / fuzzy: find keys that contain the candidate as substring
    for key in candidates:
        if not key or len(key) < 4:
            continue
        matches = [k for k in _price_index if key in k]
        if len(matches) == 1:
            return {"key": matches[0], "rows": _price_index[matches[0]]}
        if matches:
            return {"key": key, "rows": [], "ambiguous": matches[:10]}

    return {}

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
  /* keep text clear of the corner filigree (all four edges) */
  html[data-theme="ivory"] .card { padding: 28px 34px 40px; }
  html[data-theme="ivory"] .card > label:first-of-type,
  html[data-theme="ivory"] .card > details > summary,
  html[data-theme="ivory"] #status { padding-left: 34px; }
  /* lining numerals so digits are evenly readable in the serif font */
  html[data-theme="ivory"] body { font-variant-numeric: lining-nums tabular-nums; }

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
    min-width: 0; font-size: 13px; padding: 7px 10px 7px 12px;
    border-radius: 20px; cursor: pointer;
    border: 1px solid var(--input-border);
    background-color: var(--input-bg); color: var(--input-text);
    font-family: var(--font-body);
    box-shadow: 0 2px 8px rgba(0,0,0,.25);
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
  /* Custom arrow + no browser default arrow */
  select {
    cursor: pointer; min-width: 240px;
    appearance: none; -webkit-appearance: none;
    padding-right: 38px;
    background-image: var(--select-arrow);
    background-repeat: no-repeat;
    background-position: right 13px center;
    background-size: 11px 7px;
  }
  :root {
    /* chevron arrow for dark Aurora theme (light) */
    --select-arrow: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 11 7'%3E%3Cpath d='M1 1l4.5 5L10 1' stroke='%238b97b5' stroke-width='1.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  }
  html[data-theme="ivory"] {
    /* chevron arrow for light Ivory theme (gold) */
    --select-arrow: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 11 7'%3E%3Cpath d='M1 1l4.5 5L10 1' stroke='%23b8902f' stroke-width='1.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  }

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

  /* ── Price checker ── */
  #checkInput {
    width: 100%; height: 130px; resize: vertical;
    background: var(--input-bg); color: var(--input-text);
    border: 1px solid var(--input-border); border-radius: var(--input-radius);
    padding: 10px 14px; font: 14px ui-monospace,monospace;
    transition: border-color .2s, box-shadow .2s;
  }
  #checkInput:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  #checkResults { margin-top: 14px; }
  .cr-item {
    border: 1px solid var(--list-border); border-radius: 8px;
    margin-bottom: 10px; overflow: hidden;
  }
  .cr-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 9px 14px; background: var(--list-hover); cursor: pointer;
    gap: 12px;
  }
  .cr-header:hover { background: var(--card-border); }
  .cr-query { font-weight: 600; font-size: 14px; flex: 1; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cr-badge { font-size: 12px; padding: 3px 9px; border-radius: 12px; white-space: nowrap; flex-shrink: 0; }
  .cr-badge-found   { background: rgba(40,167,69,.18);  color: #28a745; }
  .cr-badge-miss    { background: rgba(220,53,69,.14);  color: #dc3545; }
  .cr-badge-ambig   { background: rgba(255,193,7,.18);  color: #b8860b; }
  .cr-body { display: none; padding: 10px 14px 14px; }
  .cr-item.open .cr-body { display: block; }
  .cr-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .cr-table th { text-align: left; padding: 5px 8px; color: var(--label-color);
    font-family: var(--font-head); font-size: 11px; letter-spacing: .8px;
    text-transform: uppercase; border-bottom: 1px solid var(--list-border); }
  .cr-table td { padding: 6px 8px; border-bottom: 1px solid var(--list-border);
    vertical-align: top; }
  .cr-table tr:last-child td { border-bottom: none; }
  .cr-table tr:hover td { background: var(--list-hover); }
  .cr-price { font-weight: 700; color: var(--accent); font-family: ui-monospace,monospace; }
  .cr-specs { color: var(--dim); font-size: 12px; }
  .cr-key { font: 11px ui-monospace,monospace; color: var(--dim); margin-bottom: 8px; }
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
  <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    <button class="btn btn-blue" onclick="loadFiles()">🔄 Обновить список</button>
    <a id="btnMerge" class="btn btn-green" href="/download-all" style="text-decoration:none">⬇ Скачать общий список</a>
    <button class="btn btn-red" onclick="clearFiles()">🗑 Очистить все CSV</button>
    <label class="btn btn-blue" style="cursor:pointer;margin:0" title="Загрузить CSV в базу (для проверки цен)">
      📂 Загрузить CSV в базу
      <input type="file" id="uploadCsvInput" accept=".csv" style="display:none" onchange="uploadCsv(this)">
    </label>
  </div>
  <div id="mergeInfo" style="font-size:13px;color:var(--text-dim);margin-top:8px"></div>
  <div id="uploadStatus" style="font-size:13px;margin-top:6px;display:none"></div>
</div>

<div class="card">
  <label>Проверка цен конкурентов</label>
  <p style="font-size:13px;color:var(--text-dim);margin-bottom:10px">
    Вставьте ссылки конкурентов — по одной на строку. Скрипт откроет каждую страницу
    и вернёт актуальные цену, характеристики и наличие.
  </p>
  <textarea id="checkInput" placeholder="https://www.pnevmo-sklad.ru/shop/oborudovanie/...
https://www.compressortyt.ru/stanciya/...
https://rutector.ru/products/..."></textarea>
  <div style="margin-top:10px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <button class="btn btn-orange" id="btnFetch" onclick="fetchUrls()">🔍 Проверить</button>
    <button class="btn btn-red"    id="btnFetchStop" onclick="stopFetch()" disabled>⏹ Стоп</button>
    <button class="btn btn-blue" onclick="document.getElementById('checkInput').value='';document.getElementById('checkResults').innerHTML='';document.getElementById('checkStatus').textContent=''">✕ Очистить</button>
    <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--dim);cursor:pointer;font-family:var(--font-body);text-transform:none;letter-spacing:normal;font-weight:normal;margin:0">
      <input type="checkbox" id="fetchResume" checked style="accent-color:var(--accent);width:14px;height:14px">
      Брать из базы если есть
    </label>
    <span id="checkStatus" style="font-size:13px;color:var(--dim)"></span>
  </div>
  <div id="checkResults"></div>
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
    listenLog(0);
  });
}

function stopScrape() {
  fetch('/stop', {method:'POST'}).then(() => {
    document.getElementById('statusText').textContent = 'Остановлено.'; document.getElementById('statusDot').style.display='none';
  });
}

function listenLog(from) {
  if (evtSource) evtSource.close();
  document.getElementById('log').innerHTML = '';
  evtSource = new EventSource('/log-stream?from=' + (from || 0));
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

function uploadCsv(input) {
  const file = input.files[0];
  if (!file) return;
  const st = document.getElementById('uploadStatus');
  st.style.display = 'block';
  st.style.color = 'var(--dim)';
  st.textContent = `Загружаю ${file.name} (${(file.size/1024/1024).toFixed(1)} МБ)…`;
  const fd = new FormData();
  fd.append('file', file);
  fetch('/upload-csv', {method:'POST', body: fd})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        st.style.color = '#28a745';
        st.textContent = `✓ ${d.filename} загружен (${d.rows.toLocaleString('ru')} строк). Индекс перестроен: ${d.index_size.toLocaleString('ru')} URL.`;
        loadFiles();
      } else {
        st.style.color = '#dc3545';
        st.textContent = '⚠ Ошибка: ' + (d.error || '?');
      }
      input.value = '';
    })
    .catch(e => { st.style.color='#dc3545'; st.textContent='Ошибка: '+e; input.value=''; });
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

// ── Live URL fetcher ───────────────────────────────────────────────────────
let _fetchEvt = null;

function fetchUrls() {
  const urls = document.getElementById('checkInput').value.trim();
  if (!urls) return;
  const st  = document.getElementById('checkStatus');
  const out = document.getElementById('checkResults');
  out.innerHTML = ''; st.textContent = 'Запускаю…';
  document.getElementById('btnFetch').disabled = true;
  document.getElementById('btnFetchStop').disabled = false;

  const total = urls.split(/\\r?\\n/).filter(l => l.trim()).length;
  let done = 0, ok = 0, err = 0;

  const resume = document.getElementById('fetchResume').checked;
  // Step 1: POST the URL list
  fetch('/fetch-urls', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({urls, resume})
  }).then(r => r.json()).then(d => {
    if (d.error) { st.textContent = '⚠ ' + d.error; resetFetchBtns(); return; }
    // Step 2: open EventSource to stream results
    if (_fetchEvt) _fetchEvt.close();
    _fetchEvt = new EventSource('/fetch-urls-stream');
    _fetchEvt.onmessage = e => {
      const data = JSON.parse(e.data);
      if (data.done) {
        _fetchEvt.close(); _fetchEvt = null;
        st.textContent = `Готово: ${ok} ОК, ${err} ошибок из ${total}`;
        resetFetchBtns(); return;
      }
      done++;
      st.textContent = `${done} / ${total}…`;
      if (data.status === 'ok') ok++; else err++;
      out.insertAdjacentHTML('afterbegin', buildFetchItem(data));
    };
    _fetchEvt.onerror = () => {
      st.textContent = `Ошибка соединения (обработано: ${done}/${total})`;
      resetFetchBtns();
    };
  }).catch(e => { st.textContent = 'Ошибка: ' + e; resetFetchBtns(); });
}

function stopFetch() {
  if (_fetchEvt) { _fetchEvt.close(); _fetchEvt = null; }
  fetch('/fetch-urls', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{"urls":""}'});
  resetFetchBtns();
  document.getElementById('checkStatus').textContent = 'Остановлено.';
}
function resetFetchBtns() {
  document.getElementById('btnFetch').disabled = false;
  document.getElementById('btnFetchStop').disabled = true;
}

function buildFetchItem(d) {
  const urlShort = d.url.replace(/https?:\/\/(www\.)?/, '').replace(/\/$/, '');
  if (d.status !== 'ok') {
    const cls = d.status === 'skipped' ? 'cr-badge-ambig' : 'cr-badge-miss';
    const label = d.status === 'skipped' ? 'Пропущено' : (d.status === 'unknown_site' ? 'Неизвестный сайт' : 'Ошибка');
    return `<div class="cr-item">
      <div class="cr-header">
        <span class="cr-query"><a href="${esc(d.url)}" target="_blank" style="color:var(--link)">${esc(urlShort)}</a></span>
        <span class="cr-badge ${cls}">${label}: ${esc(d.error||'')}</span>
      </div></div>`;
  }
  const p = d.product;
  const price = p.price ? `${Number(p.price).toLocaleString('ru')} ₽` : (p.availability || 'по запросу');
  const old_price = p.old_price ? `<s style="color:var(--dim);font-size:12px">${Number(p.old_price).toLocaleString('ru')} ₽</s> ` : '';
  const cachedBadge = d.cached ? `<span style="font-size:11px;background:rgba(124,156,255,.18);color:var(--accent);padding:2px 7px;border-radius:8px;margin-left:4px">из базы</span>` : '';
  // format specs
  let specsHtml = '';
  if (p.specs && Object.keys(p.specs).length) {
    const rows = Object.entries(p.specs).map(([k,v]) =>
      `<tr><td style="color:var(--dim);padding:3px 8px;font-size:12px;white-space:nowrap">${esc(k)}</td><td style="padding:3px 8px;font-size:13px">${esc(String(v))}</td></tr>`
    ).join('');
    specsHtml = `<table style="margin-top:8px;border-collapse:collapse">${rows}</table>`;
  }
  const avail = p.availability ? `<span style="font-size:12px;color:var(--dim)"> · ${esc(p.availability)}</span>` : '';
  return `<div class="cr-item found-item open">
    <div class="cr-header" onclick="this.parentElement.classList.toggle('open')">
      <span class="cr-query" style="font-weight:normal">${esc(p.name || urlShort)}</span>
      <span style="display:flex;align-items:center;gap:8px;flex-shrink:0">
        ${old_price}<span class="cr-price">${esc(price)}</span>${cachedBadge}${avail}
        <a href="${esc(d.url)}" target="_blank" style="color:var(--link);font-size:13px">↗</a>
      </span>
    </div>
    <div class="cr-body">${specsHtml}</div>
  </div>`;
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

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

// Reconnect to a running scrape after a page reload (replay the buffered log).
fetch('/running').then(r => r.json()).then(d => {
  if (d.running) {
    document.getElementById('btnStart').disabled = true;
    document.getElementById('btnStop').disabled = false;
    document.getElementById('statusText').textContent = 'Выполняется…';
    document.getElementById('statusDot').style.display = 'inline-block';
    listenLog(0);  // replay from start of buffer, then follow live
  }
}).catch(() => {});

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

        # Reset the log buffer for the new run.
        global _log_lines, _log_done
        _log_lines = []
        _log_done = False

        _proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(Path(__file__).parent),
        )
        # Drain stdout into the buffer in a background thread
        threading.Thread(target=_drain, args=(_proc,), daemon=True).start()

    return jsonify({"ok": True})


def _drain(proc: subprocess.Popen) -> None:
    global _log_done
    for line in proc.stdout:
        _log_lines.append(line.rstrip())
    proc.wait()
    _log_done = True


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


@app.route("/running")
@requires_auth
def running():
    """Tell the page whether a scrape is in progress (for reconnect on reload)."""
    is_running = _proc is not None and _proc.poll() is None
    return jsonify({"running": is_running, "lines": len(_log_lines)})


@app.route("/log-stream")
@requires_auth
def log_stream():
    """Stream the run log via SSE. ?from=N replays buffered lines from index N
    (0 = from the beginning), then follows live until the run finishes."""
    try:
        start = int(request.args.get("from", "0"))
    except ValueError:
        start = 0

    def generate():
        idx = max(0, start)
        idle = 0
        while True:
            if idx < len(_log_lines):
                line = _log_lines[idx]; idx += 1; idle = 0
                payload = json.dumps({"line": line}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                continue
            # caught up to the buffer
            if _log_done:
                yield "data: {\"done\": true}\n\n"
                break
            idle += 1
            if idle >= 30:
                idle = 0
                yield ": keepalive\n\n"  # SSE comment — not shown in log
            time.sleep(1)

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


@app.route("/upload-csv", methods=["POST"])
@requires_auth
def upload_csv():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Файл не выбран"}), 400
    fname = f.filename
    if not fname.lower().endswith(".csv"):
        return jsonify({"error": "Только .csv файлы"}), 400
    # sanitise filename
    import re as _re
    fname = _re.sub(r"[^\w\.\-]", "_", fname)
    DATA_DIR.mkdir(exist_ok=True)
    dest = DATA_DIR / fname
    f.save(str(dest))
    # count rows
    try:
        rows = max(0, dest.read_text(encoding="utf-8", errors="replace").count("\n") - 1)
    except Exception:
        rows = 0
    # force index rebuild
    global _price_index_mtime
    with _price_index_lock:
        _price_index_mtime = 0.0
        _build_price_index()
    return jsonify({"ok": True, "filename": fname, "rows": rows,
                    "index_size": len(_url_index)})


@app.route("/debug-url-index")
@requires_auth
def debug_url_index():
    with _price_index_lock:
        _build_price_index()
    sample_keys = list(_url_index.keys())[:20]
    return jsonify({
        "url_index_size": len(_url_index),
        "price_index_size": len(_price_index),
        "sample_urls": sample_keys,
    })


@app.route("/fetch-urls", methods=["POST"])
@requires_auth
def fetch_urls():
    """Store URL list for the SSE stream endpoint."""
    global _fetch_job_urls, _fetch_job_resume
    data = request.get_json() or {}
    urls = [u.strip() for u in (data.get("urls") or "").splitlines() if u.strip()]
    if not urls:
        return jsonify({"error": "Список пуст"}), 400
    with _fetch_job_lock:
        _fetch_job_urls = urls
        _fetch_job_resume = bool(data.get("resume", False))
    return jsonify({"ok": True, "count": len(urls)})


@app.route("/fetch-urls-stream")
@requires_auth
def fetch_urls_stream():
    """SSE: scrape stored URLs — parallel per site, sequential within each site."""
    with _fetch_job_lock:
        urls = list(_fetch_job_urls)
        resume = _fetch_job_resume

    def generate():
        import dataclasses as _dc
        from urllib.parse import urlparse
        from monitor.registry import ALL_SCRAPERS
        import re as _re

        env = os.environ.copy()
        env.update(_runtime_env)

        # If resume mode — build/refresh the local CSV index
        if resume:
            with _price_index_lock:
                _build_price_index()

        def _row_to_product(r: dict) -> dict:
            try:
                specs = json.loads(r["specs"]) if r.get("specs") and r["specs"] not in ("{}", "") else {}
            except Exception:
                specs = {}
            return {
                "site": r.get("site", ""), "name": r.get("name", ""),
                "brand": r.get("brand", ""), "model": r.get("model", ""),
                "price": r.get("price", ""), "old_price": r.get("old_price", ""),
                "availability": r.get("availability", ""),
                "specs": specs, "product_url": r.get("product_url", ""),
            }

        # Group URLs by site key, preserving order within each site
        from collections import defaultdict
        site_urls: dict[str, list[str]] = defaultdict(list)
        unknown: list[str] = []
        cached_results: list[dict] = []

        for url in urls:
            host = urlparse(url).netloc.lstrip("www.")
            site_key = next((k for k in ALL_SCRAPERS
                             if host == k or host.endswith("." + k)), None)
            if not site_key:
                unknown.append(url)
                continue

            # Resume: look up by exact product_url first
            if resume:
                row = _url_index.get(_norm_url(url))
                if row:
                    cached_results.append({
                        "url": url, "status": "ok", "cached": True,
                        "product": _row_to_product(row),
                    })
                    continue  # skip live fetch

            site_urls[site_key].append(url)

        # Emit cached hits immediately
        for item in cached_results:
            yield _sse(item)

        # Emit unknown-site errors
        for url in unknown:
            host = urlparse(url).netloc.lstrip("www.")
            yield _sse({"url": url, "status": "unknown_site",
                        "error": f"Сайт «{host}» не поддерживается"})

        if not site_urls:
            yield 'data: {"done": true}\n\n'
            return

        result_q: queue.Queue = queue.Queue()

        # CSV file to append live-scraped results into
        import csv as _csv
        DATA_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d")
        csv_path = DATA_DIR / f"prices_checked_{ts}.csv"
        csv_lock = threading.Lock()
        CSV_FIELDS = [
            "site","brand","series","name","model","sku","price","old_price",
            "discount_pct","currency","availability","series_status",
            "replacement_model","specs","category_path","product_url",
            "image_url","normalized_key","scraped_at",
        ]
        # Write header if file is new
        if not csv_path.exists():
            with open(csv_path, "w", encoding="utf-8", newline="") as fh:
                _csv.DictWriter(fh, fieldnames=CSV_FIELDS).writeheader()

        def _append_to_csv(row_dict: dict) -> None:
            with csv_lock:
                with open(csv_path, "a", encoding="utf-8", newline="") as fh:
                    w = _csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
                    w.writerow(row_dict)
            # invalidate URL index so next resume picks up the new row
            global _price_index_mtime
            _price_index_mtime = 0.0

        def scrape_site(site_key: str, site_url_list: list[str]) -> None:
            try:
                # Ensure proxy env vars are visible to HttpClient.__init__ so the
                # proxy (and IP-refresh) are set up the normal way, before any
                # request is made.
                ukey = site_key.replace(".", "_").replace("-", "_").upper()
                proxy = env.get(f"PROXY__{ukey}") or env.get("PROXY", "")
                refresh = env.get(f"PROXY_REFRESH__{ukey}") or env.get("PROXY_REFRESH", "")
                if proxy:
                    os.environ[f"PROXY__{ukey}"] = proxy
                    if refresh:
                        os.environ[f"PROXY_REFRESH__{ukey}"] = refresh
                cls = ALL_SCRAPERS[site_key]
                inst = cls()
                if proxy and not inst.client._using_proxy:
                    inst.client._proxy_url = proxy
                    inst.client._enable_proxy()
                # Always bypass the HTTP cache for live checks so a previously
                # cached (blocked) page is never served — every request goes
                # fresh through the proxy.
                _orig_get = inst.client.get
                def _fresh_get(*a, **kw):
                    kw.setdefault("force_refresh", True)
                    return _orig_get(*a, **kw)
                inst.client.get = _fresh_get
            except Exception as e:
                for url in site_url_list:
                    result_q.put({"url": url, "status": "error",
                                  "error": f"Скрапер {site_key}: {e}"})
                result_q.put({"_site_done": site_key})
                return

            for url in site_url_list:
                try:
                    product = inst.parse_product(url)
                    if product is None:
                        result_q.put({"url": url, "status": "skipped",
                                      "error": "Страница-серия или нет данных"})
                    else:
                        csv_row = product.to_dict() if hasattr(product, "to_dict") else (
                            _dc.asdict(product) if _dc.is_dataclass(product) else vars(product))
                        _append_to_csv(csv_row)
                        # For the UI: specs must be a dict, not a JSON string
                        ui_row = dict(csv_row)
                        if isinstance(ui_row.get("specs"), str):
                            try:
                                ui_row["specs"] = json.loads(ui_row["specs"])
                            except Exception:
                                ui_row["specs"] = {}
                        result_q.put({"url": url, "status": "ok", "product": ui_row})
                except Exception as e:
                    result_q.put({"url": url, "status": "error", "error": str(e)})
            result_q.put({"_site_done": site_key})

        # Launch one thread per site
        threads = []
        for site_key, site_url_list in site_urls.items():
            t = threading.Thread(target=scrape_site, args=(site_key, site_url_list),
                                 daemon=True)
            t.start()
            threads.append(t)

        sites_done = 0
        total_sites = len(site_urls)
        while sites_done < total_sites:
            try:
                item = result_q.get(timeout=120)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if "_site_done" in item:
                sites_done += 1
            else:
                yield _sse(item)

        yield 'data: {"done": true}\n\n'

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.route("/check-prices", methods=["POST"])
@requires_auth
def check_prices():
    data = request.get_json() or {}
    lines = [l.strip() for l in (data.get("lines") or "").splitlines() if l.strip()]
    if not lines:
        return jsonify({"error": "Список пуст"}), 400

    with _price_index_lock:
        _build_price_index()

    results = []
    for line in lines:
        res = _lookup_prices(line)
        rows = res.get("rows", [])
        key = res.get("key", "")
        ambiguous = res.get("ambiguous", [])

        if ambiguous:
            results.append({"query": line, "status": "ambiguous",
                            "message": f"Несколько совпадений: {', '.join(ambiguous)}"})
            continue
        if not rows:
            results.append({"query": line, "status": "not_found", "key": key})
            continue

        # Group by site, pick best (highest price if multiple rows per site)
        by_site: dict[str, dict] = {}
        for r in rows:
            site = r.get("site", "")
            existing = by_site.get(site)
            if existing is None:
                by_site[site] = r
            else:
                p_new = r.get("price", "")
                p_old = existing.get("price", "")
                try:
                    if float(p_new or 0) > float(p_old or 0):
                        by_site[site] = r
                except ValueError:
                    pass

        # Build a clean summary per site
        competitors = []
        for site, r in sorted(by_site.items()):
            price_raw = r.get("price", "").strip()
            try:
                price_val = float(price_raw)
                price_str = f"{price_val:,.0f} ₽".replace(",", " ")
            except (ValueError, TypeError):
                price_str = r.get("availability", "по запросу") or "по запросу"
            competitors.append({
                "site": site,
                "price": price_str,
                "url": r.get("product_url", ""),
                "name": r.get("name", ""),
                "specs": _format_specs_brief(r.get("specs", "")),
            })

        # Pick the canonical name/brand/model from rows
        brand = next((r.get("brand","") for r in rows if r.get("brand")), "")
        model = next((r.get("model","") for r in rows if r.get("model")), "")

        results.append({
            "query": line,
            "status": "found",
            "key": key,
            "brand": brand,
            "model": model,
            "competitors": competitors,
        })

    return jsonify({"results": results, "index_size": len(_price_index)})


def _format_specs_brief(specs_raw: str) -> str:
    """Return the 3 most important specs as a short string."""
    if not specs_raw or specs_raw.strip() in ("", "{}", "null"):
        return ""
    PRIORITY = ["Производительность", "Давление", "Мощность",
                "производительность", "давление", "мощность"]
    try:
        import json as _json
        d = _json.loads(specs_raw)
        parts = []
        for k in PRIORITY:
            if k in d and d[k]:
                parts.append(f"{k}: {d[k]}")
        return "; ".join(parts) if parts else "; ".join(
            f"{k}: {v}" for k, v in list(d.items())[:3] if v)
    except Exception:
        return ""


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
