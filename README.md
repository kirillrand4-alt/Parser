# Competitor Price Monitor — компрессорное и пневмооборудование

Собирает цены, технические характеристики и статус актуальности серий с 6 сайтов-конкурентов.

## Сайты
| Сайт | Источник | Движок |
|---|---|---|
| compressortyt.ru | HTML листинга | Кастомный |
| pnevmo-sklad.ru | HTML листинга | Bitrix |
| pnevmoteh.ru | YML-фид (если есть) / HTML | OpenCart |
| rutector.ru | HTML листинга | Bitrix |
| aerocompressors.ru | YML-фид (если есть) / HTML | Bitrix/кастом |
| v-p-k.ru | HTML листинга + JSON XHR | Bitrix |

## Быстрый старт

```bash
# 1. Установить зависимости
pip install -r requirements.txt

# 2. Разведка: проверить наличие YML-фидов на всех сайтах
python -m monitor recon

# 3. Парсинг одного сайта (сначала)
python -m monitor scrape --site compressortyt.ru

# 4. Парсинг всех сайтов
python -m monitor scrape --site all

# 5. Сравнить два прогона
python -m monitor list-runs
python -m monitor diff 20240601_120000 20240602_120000

# Справка
python -m monitor --help
python -m monitor scrape --help
```

## Схема данных

Каждый товар содержит:
- `site`, `brand`, `series`, `name`, `model`, `sku`
- `price`, `old_price`, `discount_pct`, `currency`
- `availability` — как указано на сайте
- `series_status` — нормализованный: `в наличии` / `нет в наличии` / `под заказ` / `снято` / `неизвестно`
- `replacement_model` — модель-замена для снятых
- `specs` — JSON со всеми техническими характеристиками
- `category_path`, `product_url`, `image_url` (только URL, картинки не скачиваются)
- `normalized_key` — `BRAND+MODEL` для сопоставления с внутренним каталогом
- `scraped_at` — метка времени

## Вывод

- CSV: `data/prices_<run_id>.csv` (UTF-8 BOM, открывается в Excel)
- SQLite: `data/prices.db` — все прогоны, можно диффать
- Кеш HTTP: `cache/<site>.sqlite` — повторные прогоны не бьют по серверу

## GitHub Actions

`.github/workflows/scrape.yml` запускает парсинг по расписанию Пн–Пт в 03:00 UTC.
CSV-файлы сохраняются как артефакты (90 дней).

## Структура проекта

```
monitor/
  __main__.py      # CLI точка входа (click)
  models.py        # Dataclass Product, clean_price, normalize_key
  http_client.py   # requests + requests-cache + tenacity
  base_scraper.py  # Абстрактный BaseScraper
  registry.py      # Реестр всех скраперов
  storage.py       # CSV + SQLite запись, функция diff_runs
  recon.py         # Разведка фидов
  scrapers/
    compressortyt.py
    pnevmo_sklad.py
    pnevmoteh.py
    rutector.py
    aerocompressors.py
    vpk.py
NOTES.md           # Результаты разведки по сайтам
```

## Этические рамки

- Пауза 1–2 с между запросами + случайный джиттер
- Реалистичный User-Agent
- Соблюдение robots.txt
- Кеш на 6 часов — повторные прогоны не нагружают серверы
- Картинки не скачиваются — только URL
- Маркетинговые тексты не копируются — только фактические поля
