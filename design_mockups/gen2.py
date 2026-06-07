"""Generate Aurora-family glassmorphism mockups (variations on neon glass)."""
from pathlib import Path

BODY = """
<h1 class="title">🔧 Parser <span class="badge">COMPRESSOR MONITOR</span></h1>
<div class="card">
  <label>Сайт</label>
  <select><option>all</option><option>pnevmo-sklad.ru</option><option>rutector.ru</option></select>
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
<title>{name}</title><style>{base}{css}</style></head>
<body><div class=wrap>{body}</div>
<div class=swatch>Вариант: <b>{name}</b></div></body></html>"""

# Shared structural base — only colors/gradients differ per theme.
BASE = """
*{box-sizing:border-box;margin:0}
body{font-family:'Inter',system-ui,sans-serif;min-height:100vh;padding:40px 20px}
.wrap{max-width:880px;margin:0 auto}
.title{font-weight:800;font-size:30px;display:flex;gap:14px;align-items:center;margin-bottom:26px}
.badge{font:600 12px ui-monospace,monospace;padding:5px 11px;border-radius:20px}
.card{backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border-radius:20px;
 padding:24px;margin-bottom:20px}
label{font-weight:600;display:block;margin-bottom:10px}
select,input[type=text]{border-radius:12px;padding:12px 16px;font-size:15px;min-width:260px}
.radio-group{display:flex;gap:24px;margin:16px 0}
.radio-group label{font-weight:500;display:flex;gap:8px;align-items:center;cursor:pointer}
input[type=radio]{width:16px;height:16px}
.btn{border:none;color:#fff;font:700 15px inherit;padding:13px 24px;border-radius:14px;
 margin-right:10px;cursor:pointer;transition:.15s}
.btn:hover{transform:translateY(-2px);filter:brightness(1.12)}
.btn:active{transform:translateY(0)}
.status{margin-top:14px;font:600 14px ui-monospace,monospace}
.log{border-radius:14px;padding:16px;font:13px ui-monospace,monospace;height:200px;overflow:auto}
.files{list-style:none}.files li{display:flex;justify-content:space-between;padding:10px 0}
.files a{text-decoration:none;font-weight:600}
.swatch{text-align:center;margin-top:10px;font:13px ui-monospace,monospace}
"""

def theme(bg, accent, accent2, danger, txt="#e8ecf4", dim="#8b97b5",
          card="rgba(255,255,255,.05)", cardb="rgba(255,255,255,.10)",
          inputbg="rgba(0,0,0,.3)", logbg="rgba(0,0,0,.4)"):
    return f"""
body{{color:{txt};background:#05070f;background-image:{bg};background-attachment:fixed}}
.title{{color:{txt}}}
.badge{{color:{accent};background:rgba(255,255,255,.06);border:1px solid {accent}}}
.card{{background:{card};border:1px solid {cardb};
 box-shadow:0 12px 40px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.08)}}
small{{color:{dim}}}
select,input[type=text]{{background:{inputbg};color:{txt};border:1px solid rgba(255,255,255,.15)}}
.radio-group{{color:{dim}}}
input[type=radio]{{accent-color:{accent}}}
.btn-primary{{background:linear-gradient(135deg,{accent},{accent2});box-shadow:0 6px 22px {accent}66}}
.btn-secondary{{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2)}}
.btn-danger{{background:linear-gradient(135deg,{danger},{danger});box-shadow:0 6px 22px {danger}55}}
.status{{color:{accent}}}
.log{{background:{logbg};border:1px solid rgba(255,255,255,.1);color:#c9d4e8}}
.log .ok{{color:#5ce0b0}}.log .warn{{color:#ffd27c}}.log .err{{color:#ff7c9c}}
.files li{{border-bottom:1px solid rgba(255,255,255,.08)}}
.files a{{color:{accent}}}
.swatch{{color:{dim}}}
"""

g = lambda *stops: ",".join(
    f"radial-gradient({s})" for s in stops)

THEMES = {
 "5_cyber_violet": (
  "Cyber Violet — фиолет + маджента",
  theme(
    g("900px 500px at 12% -5%,#5b21b6 0,transparent 55%",
      "800px 500px at 88% 10%,#be185d 0,transparent 55%",
      "700px 600px at 50% 110%,#1e1b4b 0,transparent 60%"),
    "#a78bfa","#ec4899","#f43f5e")),
 "6_ocean_teal": (
  "Ocean Teal — бирюза + голубой",
  theme(
    g("900px 500px at 10% 0%,#0e7490 0,transparent 55%",
      "800px 500px at 90% 15%,#1d4ed8 0,transparent 55%",
      "700px 600px at 50% 110%,#042f49 0,transparent 60%"),
    "#22d3ee","#3b82f6","#fb7185")),
 "7_sunset_ember": (
  "Sunset Ember — оранж + розовый (под компрессоры)",
  theme(
    g("900px 500px at 12% -5%,#c2410c 0,transparent 55%",
      "800px 500px at 88% 12%,#be123c 0,transparent 55%",
      "700px 600px at 50% 110%,#431407 0,transparent 60%"),
    "#fb923c","#f43f5e","#ef4444")),
 "8_emerald_mint": (
  "Emerald Mint — изумруд + мята",
  theme(
    g("900px 500px at 10% 0%,#047857 0,transparent 55%",
      "800px 500px at 90% 14%,#0d9488 0,transparent 55%",
      "700px 600px at 50% 110%,#022c22 0,transparent 60%"),
    "#34d399","#2dd4bf","#fb7185")),
 "9_aurora_borealis": (
  "Aurora Borealis — зелёно-сине-фиолетовое сияние",
  theme(
    g("800px 500px at 15% -5%,#10b981 0,transparent 50%",
      "800px 500px at 55% 5%,#3b82f6 0,transparent 50%",
      "800px 500px at 90% 15%,#8b5cf6 0,transparent 50%",
      "700px 600px at 50% 110%,#0b1026 0,transparent 60%"),
    "#5eead4","#818cf8","#f472b6")),
 "10_midnight_gold": (
  "Midnight Gold — тёмно-синий + золото (премиум)",
  theme(
    g("900px 500px at 12% 0%,#1e3a8a 0,transparent 55%",
      "800px 500px at 88% 12%,#854d0e 0,transparent 55%",
      "700px 600px at 50% 110%,#0c1330 0,transparent 60%"),
    "#fbbf24","#f59e0b","#fb7185", txt="#f1f5f9")),
 "11_rose_quartz": (
  "Rose Quartz — розово-фиолетовый, мягкий",
  theme(
    g("900px 500px at 12% -5%,#9d174d 0,transparent 55%",
      "800px 500px at 88% 12%,#6d28d9 0,transparent 55%",
      "700px 600px at 50% 110%,#2a0a3a 0,transparent 60%"),
    "#f9a8d4","#c084fc","#fb7185")),
 "12_arctic_ice": (
  "Arctic Ice — холодный сине-голубой, светлее",
  theme(
    g("900px 500px at 12% 0%,#0369a1 0,transparent 55%",
      "800px 500px at 88% 12%,#4f46e5 0,transparent 55%",
      "700px 600px at 50% 110%,#0a1733 0,transparent 60%"),
    "#7dd3fc","#a5b4fc","#fb7185",
    card="rgba(255,255,255,.07)", cardb="rgba(255,255,255,.14)")),
}

out = Path(__file__).parent
for key,(name,css) in THEMES.items():
    html = PAGE.format(name=name,base=BASE,css=css,body=BODY)
    (out/f"{key}.html").write_text(html,encoding="utf-8")
    print("wrote",key)
