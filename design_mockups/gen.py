"""Generate standalone design mockups of the Parser UI (no backend)."""
from pathlib import Path

BODY = """
<h1 class="title">🔧 Parser <span class="badge">COMPRESSOR MONITOR</span></h1>

<div class="card">
  <label>Сайт</label>
  <select>
    <option>all</option><option>pnevmo-sklad.ru</option><option>rutector.ru</option>
  </select>
  <div class="radio-group">
    <label><input type="radio" name="m" checked> Продолжить с последнего места</label>
    <label><input type="radio" name="m"> Скачать заново (с нуля)</label>
  </div>
  <div style="margin-top:18px">
    <button class="btn btn-primary">▶ Запустить</button>
    <button class="btn btn-danger">⏹ Остановить</button>
  </div>
  <div class="status">15% · 1671/11279 · 2.4s/prod · err=0</div>
</div>

<div class="card">
  <label>Лог</label>
  <div class="log"><div>pnevmo-sklad.ru : 36%| 3337/9326 [2.06prod/s, blocked=0, err=0]</div>
<div class="ok">[pnevmo-sklad.ru] switched to PROXY</div>
<div class="warn">[pnevmo-sklad.ru] HTTP 500 on /shop/oborudovanie/...</div>
<div class="err">[pnevmo-sklad.ru] 3 consecutive blocks — stopping.</div>
<div>pnevmo-sklad.ru : 37%| 3400/9326 [2.10prod/s, blocked=0, err=1]</div></div>
</div>

<div class="card">
  <label>Результаты (CSV)</label>
  <ul class="files">
    <li><span>prices_20260606.csv <small>1751 КБ · 8 240 стр.</small></span><a href="#">⬇ Скачать</a></li>
    <li><span>prices_20260605.csv <small>409 КБ · 1 920 стр.</small></span><a href="#">⬇ Скачать</a></li>
  </ul>
  <div style="margin-top:14px">
    <button class="btn btn-secondary">🔄 Обновить</button>
    <button class="btn btn-primary">⬇ Общий список</button>
    <button class="btn btn-danger">🗑 Очистить</button>
  </div>
</div>
"""

PAGE = """<!DOCTYPE html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{name}</title><style>{css}</style></head>
<body><div class=wrap>{body}</div>
<div class=swatch>Вариант: <b>{name}</b></div></body></html>"""

THEMES = {}

# ---------------------------------------------------------------- 1. Aurora
THEMES["1_aurora_glass"] = """
*{box-sizing:border-box;margin:0}
body{font-family:'Inter',system-ui,sans-serif;color:#e8ecf4;min-height:100vh;
 background:#0a0e1a;
 background-image:radial-gradient(900px 500px at 15% 0%,#1a2456 0,transparent 55%),
  radial-gradient(900px 500px at 85% 20%,#3a1a56 0,transparent 55%),
  radial-gradient(700px 500px at 50% 100%,#0a3a4a 0,transparent 55%);
 padding:40px 20px}
.wrap{max-width:880px;margin:0 auto}
.title{font-weight:800;font-size:30px;display:flex;gap:14px;align-items:center;margin-bottom:26px}
.badge{font:600 12px ui-monospace,monospace;color:#7c9cff;background:rgba(124,156,255,.12);
 border:1px solid rgba(124,156,255,.35);padding:5px 11px;border-radius:20px}
.card{background:rgba(255,255,255,.04);backdrop-filter:blur(16px);
 border:1px solid rgba(255,255,255,.1);border-radius:20px;padding:24px;margin-bottom:20px;
 box-shadow:0 12px 40px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.08)}
label{font-weight:600;display:block;margin-bottom:10px}
small{color:#8b97b5}
select,input[type=text]{background:rgba(0,0,0,.3);color:#e8ecf4;border:1px solid rgba(255,255,255,.15);
 border-radius:12px;padding:12px 16px;font-size:15px;min-width:260px}
.radio-group{display:flex;gap:24px;margin:16px 0;color:#8b97b5}
.radio-group label{font-weight:500;display:flex;gap:8px;align-items:center;cursor:pointer}
input[type=radio]{accent-color:#7c9cff;width:16px;height:16px}
.btn{border:none;color:#fff;font:700 15px inherit;padding:13px 24px;border-radius:14px;
 margin-right:10px;cursor:pointer;transition:.15s}
.btn-primary{background:linear-gradient(135deg,#7c9cff,#a06cff);
 box-shadow:0 6px 20px rgba(124,108,255,.5)}
.btn-secondary{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18)}
.btn-danger{background:linear-gradient(135deg,#ff6b8a,#ff4757);box-shadow:0 6px 20px rgba(255,71,87,.4)}
.btn:hover{transform:translateY(-2px);filter:brightness(1.1)}
.btn:active{transform:translateY(0)}
.status{margin-top:14px;font:600 14px ui-monospace,monospace;color:#7c9cff}
.log{background:rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.1);border-radius:14px;
 padding:16px;font:13px ui-monospace,monospace;color:#c9d4e8;height:200px;overflow:auto}
.log .ok{color:#5ce0b0}.log .warn{color:#ffd27c}.log .err{color:#ff7c9c}
.files{list-style:none}.files li{display:flex;justify-content:space-between;padding:10px 0;
 border-bottom:1px solid rgba(255,255,255,.08)}
.files a{color:#7c9cff;text-decoration:none;font-weight:600}
.swatch{text-align:center;color:#8b97b5;margin-top:10px;font:13px ui-monospace,monospace}
"""

# ------------------------------------------------------------ 2. Neo light
THEMES["2_soft_neumorphism"] = """
*{box-sizing:border-box;margin:0}
body{font-family:'Inter',system-ui,sans-serif;color:#3d4759;background:#e4e9f2;
 padding:40px 20px;min-height:100vh}
.wrap{max-width:880px;margin:0 auto}
.title{font-weight:800;font-size:30px;display:flex;gap:14px;align-items:center;margin-bottom:26px;color:#2d3748}
.badge{font:600 12px ui-monospace,monospace;color:#5b6cff;background:#e4e9f2;padding:6px 12px;
 border-radius:20px;box-shadow:inset 3px 3px 6px #c5cbd6,inset -3px -3px 6px #fff}
.card{background:#e4e9f2;border-radius:24px;padding:26px;margin-bottom:22px;
 box-shadow:9px 9px 18px #c5cbd6,-9px -9px 18px #ffffff}
label{font-weight:600;display:block;margin-bottom:10px;color:#2d3748}
small{color:#8b95a8}
select,input[type=text]{background:#e4e9f2;color:#3d4759;border:none;border-radius:14px;
 padding:13px 16px;font-size:15px;min-width:260px;
 box-shadow:inset 4px 4px 8px #c5cbd6,inset -4px -4px 8px #fff}
.radio-group{display:flex;gap:24px;margin:16px 0;color:#6b7589}
.radio-group label{font-weight:500;display:flex;gap:8px;align-items:center;cursor:pointer}
input[type=radio]{accent-color:#5b6cff;width:16px;height:16px}
.btn{background:#e4e9f2;border:none;color:#5b6cff;font:700 15px inherit;padding:14px 26px;
 border-radius:16px;margin-right:12px;cursor:pointer;transition:.12s;
 box-shadow:6px 6px 12px #c5cbd6,-6px -6px 12px #fff}
.btn-primary{color:#fff;background:linear-gradient(135deg,#6c7cff,#5b6cff)}
.btn-danger{color:#fff;background:linear-gradient(135deg,#ff7a8a,#f0556a)}
.btn:active{box-shadow:inset 5px 5px 10px #c5cbd6,inset -5px -5px 10px #fff;transform:translateY(1px)}
.status{margin-top:14px;font:600 14px ui-monospace,monospace;color:#5b6cff}
.log{background:#2d3340;border-radius:16px;padding:16px;font:13px ui-monospace,monospace;
 color:#c9d4e0;height:200px;overflow:auto;box-shadow:inset 4px 4px 10px #1a1f29}
.log .ok{color:#5ce0b0}.log .warn{color:#ffd27c}.log .err{color:#ff7c9c}
.files{list-style:none}.files li{display:flex;justify-content:space-between;padding:11px 0;
 border-bottom:1px solid #d3d9e4}
.files a{color:#5b6cff;text-decoration:none;font-weight:600}
.swatch{text-align:center;color:#8b95a8;margin-top:10px;font:13px ui-monospace,monospace}
"""

# --------------------------------------------------------- 3. Terminal green
THEMES["3_terminal_matrix"] = """
*{box-sizing:border-box;margin:0}
body{font-family:'JetBrains Mono',ui-monospace,monospace;color:#b8f5c8;background:#0a0f0a;
 padding:40px 20px;min-height:100vh;
 background-image:linear-gradient(rgba(0,255,100,.03) 1px,transparent 1px),
  linear-gradient(90deg,rgba(0,255,100,.03) 1px,transparent 1px);background-size:24px 24px}
.wrap{max-width:880px;margin:0 auto}
.title{font-weight:700;font-size:26px;display:flex;gap:14px;align-items:center;margin-bottom:26px;
 color:#3dff7a;text-shadow:0 0 12px rgba(61,255,122,.5)}
.badge{font-size:12px;color:#0a0f0a;background:#3dff7a;padding:5px 11px;border-radius:3px;
 box-shadow:0 0 14px rgba(61,255,122,.6)}
.card{background:#0d140d;border:1px solid #1d3d24;border-radius:6px;padding:22px;margin-bottom:18px;
 box-shadow:0 0 0 1px rgba(61,255,122,.08),0 8px 30px rgba(0,0,0,.6)}
label{font-weight:600;display:block;margin-bottom:10px;color:#3dff7a;text-transform:uppercase;
 font-size:13px;letter-spacing:1px}
small{color:#5a8a66}
select,input[type=text]{background:#0a0f0a;color:#b8f5c8;border:1px solid #1d3d24;border-radius:4px;
 padding:11px 14px;font-size:14px;min-width:260px;font-family:inherit}
.radio-group{display:flex;gap:24px;margin:16px 0;color:#6aaa78}
.radio-group label{font-weight:400;display:flex;gap:8px;align-items:center;cursor:pointer;
 text-transform:none;letter-spacing:0;font-size:14px}
input[type=radio]{accent-color:#3dff7a}
.btn{background:#0a0f0a;border:1px solid #3dff7a;color:#3dff7a;font:700 14px inherit;
 padding:12px 22px;border-radius:4px;margin-right:10px;cursor:pointer;transition:.12s;text-transform:uppercase}
.btn-primary{background:#3dff7a;color:#0a0f0a;box-shadow:0 0 18px rgba(61,255,122,.5)}
.btn-danger{border-color:#ff4d4d;color:#ff4d4d}
.btn-secondary{border-color:#2d9cdb;color:#2d9cdb}
.btn:hover{box-shadow:0 0 22px currentColor;filter:brightness(1.15)}
.btn:active{transform:translateY(2px)}
.status{margin-top:14px;font-size:14px;color:#3dff7a}
.log{background:#050805;border:1px solid #1d3d24;border-radius:4px;padding:16px;font-size:13px;
 color:#8ad99a;height:200px;overflow:auto}
.log .ok{color:#3dff7a}.log .warn{color:#ffe14d}.log .err{color:#ff5c5c}
.files{list-style:none}.files li{display:flex;justify-content:space-between;padding:10px 0;
 border-bottom:1px solid #1d3d24}
.files a{color:#3dff7a;text-decoration:none}
.swatch{text-align:center;color:#5a8a66;margin-top:10px;font-size:13px}
"""

# ------------------------------------------------------- 4. Clean SaaS light
THEMES["4_clean_saas"] = """
*{box-sizing:border-box;margin:0}
body{font-family:'Inter',system-ui,sans-serif;color:#1e293b;background:#f1f5f9;
 padding:40px 20px;min-height:100vh}
.wrap{max-width:880px;margin:0 auto}
.title{font-weight:800;font-size:30px;display:flex;gap:14px;align-items:center;margin-bottom:26px;color:#0f172a}
.badge{font:600 12px ui-monospace,monospace;color:#2563eb;background:#dbeafe;padding:5px 11px;border-radius:20px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:26px;margin-bottom:20px;
 box-shadow:0 1px 3px rgba(0,0,0,.06),0 8px 24px rgba(15,23,42,.05)}
label{font-weight:600;display:block;margin-bottom:10px;color:#334155}
small{color:#94a3b8}
select,input[type=text]{background:#f8fafc;color:#1e293b;border:1px solid #cbd5e1;border-radius:10px;
 padding:12px 15px;font-size:15px;min-width:260px}
select:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.15)}
.radio-group{display:flex;gap:24px;margin:16px 0;color:#64748b}
.radio-group label{font-weight:500;display:flex;gap:8px;align-items:center;cursor:pointer}
input[type=radio]{accent-color:#2563eb;width:16px;height:16px}
.btn{border:none;color:#fff;font:600 15px inherit;padding:13px 24px;border-radius:11px;
 margin-right:10px;cursor:pointer;transition:.14s}
.btn-primary{background:#2563eb;box-shadow:0 4px 12px rgba(37,99,235,.3)}
.btn-secondary{background:#fff;color:#334155;border:1px solid #cbd5e1;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.btn-danger{background:#ef4444;box-shadow:0 4px 12px rgba(239,68,68,.3)}
.btn:hover{transform:translateY(-1px);filter:brightness(1.05)}
.btn:active{transform:translateY(1px)}
.status{margin-top:14px;font:600 14px ui-monospace,monospace;color:#2563eb}
.log{background:#0f172a;border-radius:12px;padding:16px;font:13px ui-monospace,monospace;
 color:#cbd5e1;height:200px;overflow:auto}
.log .ok{color:#34d399}.log .warn{color:#fbbf24}.log .err{color:#f87171}
.files{list-style:none}.files li{display:flex;justify-content:space-between;padding:11px 0;border-bottom:1px solid #f1f5f9}
.files a{color:#2563eb;text-decoration:none;font-weight:600}
.swatch{text-align:center;color:#94a3b8;margin-top:10px;font:13px ui-monospace,monospace}
"""

out = Path(__file__).parent
names = {
 "1_aurora_glass":"Aurora Glass — стекло + неоновые градиенты",
 "2_soft_neumorphism":"Soft Neumorphism — мягкий 3D, светлый",
 "3_terminal_matrix":"Terminal — зелёный 'хакерский' моно",
 "4_clean_saas":"Clean SaaS — светлый минимал",
}
for key,css in THEMES.items():
    html = PAGE.format(name=names[key],css=css,body=BODY)
    (out/f"{key}.html").write_text(html,encoding="utf-8")
    print("wrote",key)
