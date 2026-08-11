"""Проставить в чекпоинтах время сбора (done_at) по уже выгруженным CSV.

Зачем. Чекпоинт научился помнить, КОГДА собрана каждая ссылка, и «продолжить»
пропускает только свежие (см. BaseScraper._recent_done). Но у карточек, снятых
до этой правки, отметок нет — значит их возраст неизвестен, и они пойдут
собираться заново. Для июльских данных это правильно, а вот для сегодняшних
обидно: их только что собрали.

Этот скрипт читает CSV прогонов и берёт время из колонки `scraped_at` — то
самое, когда карточка реально снята. Запускается один раз после обновления
кода:

    python tools/seed_done_at.py                      # data/ и cache/ рядом
    python tools/seed_done_at.py --data D --cache C   # другие папки
    python tools/seed_done_at.py --dry-run            # только показать

Повторный запуск безопасен: время у ссылки берётся最 позднее из встреченных,
и уже записанные отметки не сдвигаются назад.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

csv.field_size_limit(10**9)


def parse_ts(raw: str) -> float | None:
    """`scraped_at` пишется в ISO 8601 UTC. Без даты запись бесполезна."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def collect(data_dir: Path) -> dict[str, dict[str, float]]:
    """site -> {url: время последнего сбора}."""
    by_site: dict[str, dict[str, float]] = defaultdict(dict)
    files = sorted(data_dir.glob("prices_*.csv"))
    if not files:
        print(f"в {data_dir} нет файлов prices_*.csv", file=sys.stderr)
    for fn in files:
        rows = 0
        try:
            with open(fn, encoding="utf-8-sig", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    url = (r.get("product_url") or "").strip()
                    site = (r.get("site") or "").strip()
                    ts = parse_ts(r.get("scraped_at", ""))
                    if not url or not site or ts is None:
                        continue
                    # позднее время побеждает: карточку могли снимать несколько раз
                    if ts > by_site[site].get(url, 0):
                        by_site[site][url] = ts
                    rows += 1
        except OSError as exc:
            print(f"  {fn.name}: не читается ({exc})", file=sys.stderr)
            continue
        print(f"  {fn.name:34} {rows:>8,} строк с датой")
    return by_site


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data", help="папка с CSV прогонов")
    ap.add_argument("--cache", default="cache", help="папка с чекпоинтами")
    ap.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    a = ap.parse_args()

    data_dir, cache_dir = Path(a.data), Path(a.cache)
    print(f"читаю CSV из {data_dir.resolve()}")
    by_site = collect(data_dir)
    if not by_site:
        print("нечего проставлять")
        return 1

    print(f"\nчекпоинты в {cache_dir.resolve()}")
    now = datetime.now(timezone.utc).timestamp()
    for site, urls in sorted(by_site.items(), key=lambda kv: -len(kv[1])):
        path = cache_dir / f"{site}.checkpoint.json"
        if not path.exists():
            print(f"  {site:24} чекпоинта нет — пропускаю ({len(urls):,} URL)")
            continue
        try:
            ckpt = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  {site:24} чекпоинт не читается ({exc})")
            continue

        done_at: dict = ckpt.get("done_at", {}) or {}
        added = 0
        for url, ts in urls.items():
            if ts > done_at.get(url, 0):
                done_at[url] = ts
                added += 1
        done_urls = set(ckpt.get("done_urls", [])) | set(urls)

        fresh_2d = sum(1 for t in done_at.values() if now - t <= 2 * 86400)
        print(f"  {site:24} отметок стало {len(done_at):>7,} (+{added:,}) | "
              f"моложе 2 дней: {fresh_2d:,} | done_urls {len(done_urls):,}")

        if a.dry_run:
            continue
        ckpt["done_at"] = done_at
        ckpt["done_urls"] = sorted(done_urls)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ckpt, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    print("\nготово" if not a.dry_run else "\nпробный прогон, ничего не записано")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
