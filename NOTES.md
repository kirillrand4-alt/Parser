# Разведка сайтов — результаты

> Обновлено: 2026-06-05 (реальная разведка через Google Colab)

## Итог разведки (проверено)

| Сайт | Server | YML-фид | Источник |
|---|---|---|---|
| **compressortyt.ru** | nginx | ✅ `/yml/` (12 МБ, ~17k товаров, есть цены) | **YML-фид** + добор specs со страниц |
| aerocompressors.ru | ddos-guard | ❌ (XML-пути отдают `method="notfound"`) | HTML (осторожно, ddos-guard) |
| pnevmo-sklad.ru | nginx | ❌ только sitemap.xml | HTML + sitemap |
| pnevmoteh.ru | openresty | ❌ только sitemap.xml | HTML + sitemap |
| rutector.ru | nginx | ❌ только sitemap.xml | HTML + sitemap |
| v-p-k.ru | nginx | ❌ только sitemap.xml | HTML + sitemap |

### Структура YML-фида compressortyt.ru
`<offer id available>`: `url, price, currencyId(RUR), categoryId, picture, name, vendor`.
Характеристик (`param`) в фиде НЕТ — добор со страниц товара (флаг `COMPRESSORTYT_ENRICH=1`).
Категории — дерево через `parentId`, путь восстанавливается рекурсивно.

### Важно
- aerocompressors.ru за **ddos-guard** — возможна JS-проверка/блок; держать низкий темп.
- У всех 4 «HTML-сайтов» есть `sitemap.xml` — можно брать URL товаров оттуда.

---

## РЕАЛИЗОВАНО — итоговые источники и селекторы (проверено в Colab)

| Сайт | Discovery | Цена | Характеристики | Бренд |
|---|---|---|---|---|
| **compressortyt.ru** | YML `/yml/` (~15 853 оффера) | `<price>` в фиде | добор со страниц (`COMPRESSORTYT_ENRICH=1`) | `<vendor>` |
| **pnevmo-sklad.ru** | sitemap.xml → `/shop/` | `.pricebox__price` | `table.charstable` (Бренд:/Артикул:/…) | из таблицы |
| **pnevmoteh.ru** | sitemap `?page=N` → корневые слаги | `[itemprop=price]`/`.ui-price-price` | `dl.clearfix` (dt/dd) | H1 после `//` |
| **rutector.ru** | sitemap-iblock-4.xml → `/products/` | `[itemprop=price]` (0=по запросу) | `table.zebra` | `/brands/` ссылка → словарь |
| **v-p-k.ru** | sitemap-iblock-248.xml → `/product/` | `[itemprop=price]` (0=По запросу) | `.properties-group__item` | словарь по названию |
| **aerocompressors.ru** | sitemap.xml → `/katalog_produkcii/` глубина ≥5 | `[itemprop=price]`/`.price` | `table.tech` | словарь по названию |

### Особенности
- **compressortyt**: фид даёт цены/бренд/категории; характеристик в фиде нет.
- **pnevmoteh** (Drupal Commerce): товары и категории в корне; категории отсеиваются по отсутствию цены.
- **rutector / v-p-k**: дорогая техника часто «по запросу» (цена 0) → статус «под заказ».
- **aerocompressors**: категории имеют `itemprop=price` («от …»), но без `table.tech` → отсев по наличию характеристик; фраза «снято с производства» есть в меню каталога на всех страницах → статус «снято» определяем только по H1 товара.
- Лимиты для тестов: `<SITE>_MAX=N` (напр. `RUTECTOR_MAX=30`).

---

> Историческая разметка ниже (гипотезы до разведки).

---

## 1. pnevmo-sklad.ru

| Параметр | Значение |
|---|---|
| Предполагаемый движок | 1С-Битрикс (характерная структура URL `/catalog/`, Bitrix session) |
| Выбранный источник | **HTML листинга** (фид не обнаружен публично) |
| Проверить фиды | `/yml/`, `/catalog.yml`, `/upload/iblock/`, `sitemap.xml` |
| Пагинация | `?PAGEN_1=N` (типичный Bitrix) |
| Категории компрессоров | `/catalog/kompressory/` и подкатегории |
| Антибот | Cloudflare / стандартный — проверить при первом запуске |
| Статус | TODO: уточнить при локальной разведке |

### Команды для ручной разведки
```bash
curl -sI https://pnevmo-sklad.ru/ | grep -i 'server\|x-powered\|set-cookie\|bitrix'
curl -s 'https://pnevmo-sklad.ru/robots.txt'
curl -s 'https://pnevmo-sklad.ru/sitemap.xml' | head -50
curl -s 'https://pnevmo-sklad.ru/yml/' -o /tmp/pnevmo_test.xml && head -30 /tmp/pnevmo_test.xml
```

---

## 2. pnevmoteh.ru

| Параметр | Значение |
|---|---|
| Предполагаемый движок | OpenCart / самописный |
| Выбранный источник | **Проверить YML-фид**, затем HTML |
| Проверить фиды | `/yml.php`, `/feed/yml`, `/export/yandex.xml` |
| Пагинация | `?page=N` или `&limit=N&start=N` |
| Категории | `/catalog/kompressory/` |
| Статус | TODO |

### Команды для ручной разведки
```bash
curl -sI https://pnevmoteh.ru/ | grep -i 'server\|x-powered'
curl -s 'https://pnevmoteh.ru/robots.txt'
curl -s 'https://pnevmoteh.ru/yml.php' -o /tmp/pnevmoteh_yml.xml && head -30 /tmp/pnevmoteh_yml.xml
```

---

## 3. rutector.ru

| Параметр | Значение |
|---|---|
| Предполагаемый движок | 1С-Битрикс |
| Выбранный источник | **YML-фид** (Bitrix сайты часто имеют `/upload/export/` или модуль маркетплейс) |
| Проверить фиды | `/upload/yandex/`, `/yml/`, `/bitrix/catalog_export/` |
| Пагинация | `?PAGEN_1=N` |
| Статус | TODO |

### Команды для ручной разведки
```bash
curl -sI https://rutector.ru/ | grep -i 'server\|bitrix\|x-powered'
curl -s 'https://rutector.ru/robots.txt'
curl -s 'https://rutector.ru/sitemap.xml' | grep -i 'yml\|feed\|export'
```

---

## 4. aerocompressors.ru

| Параметр | Значение |
|---|---|
| Предполагаемый движок | Предположительно Bitrix или Insales |
| Выбранный источник | **Проверить YML**, затем HTML |
| Проверить фиды | `/yml/`, `/export.yml`, `/yandex-market.xml` |
| Особенности | Специализирован на компрессорах — вероятно богатые характеристики |
| Статус | TODO |

### Команды для ручной разведки
```bash
curl -sI https://aerocompressors.ru/ | grep -i 'server\|x-powered'
curl -s 'https://aerocompressors.ru/robots.txt'
curl -s 'https://aerocompressors.ru/sitemap.xml' | head -50
for path in /yml/ /export.yml /catalog.yml /yandex.xml /price.xml; do
  code=$(curl -sI "https://aerocompressors.ru$path" -o /dev/null -w '%{http_code}')
  echo "$path -> $code"
done
```

---

## 5. v-p-k.ru

| Параметр | Значение |
|---|---|
| Предполагаемый движок | Bitrix (домен v-p-k — «вентиляция-пневматика-компрессоры») |
| Выбранный источник | **HTML листинга** с возможным JSON API |
| Проверить фиды | `/yml/`, `/local/feed/`, sitemap |
| Пагинация | Bitrix: `?PAGEN_1=N` |
| Статус | TODO |

### Команды для ручной разведки
```bash
curl -sI https://v-p-k.ru/ | grep -i 'server\|x-powered'
curl -s 'https://v-p-k.ru/robots.txt'
# Смотрим XHR в DevTools при загрузке категории
```

---

## 6. compressortyt.ru

| Параметр | Значение |
|---|---|
| Движок | Кастомный / Bitrix |
| Выбранный источник | **HTML листинга** (из брифа) |
| Структура URL | `/stanciya/kompr/<категория>/<бренд>/<модель>/` |
| Пагинация | Уточнить: `?page=N` или `/page/N/` |
| Особенности цен | U+200B (zero-width space) и nbsp в ценах — чистить |
| Картинки | Lazy-load с плейсхолдером `ll.png` — брать `data-src` |
| Объём | ~17k товаров в категории Компрессоры |
| Статус | Реализован первым (см. бриф) |

### Проверенные пути категорий
- Винтовые: `/stanciya/kompr/vintovye/`
- Поршневые: `/stanciya/kompr/porshnevye/`
- Спиральные: `/stanciya/kompr/spiralnye/`
- Безмасляные: `/stanciya/kompr/bezmaslyany/`

---

## Итоговая таблица выбора источников

| Сайт | Источник (план) | Приоритет проверки |
|---|---|---|
| pnevmo-sklad.ru | HTML (Bitrix) | Проверить YML сначала |
| pnevmoteh.ru | YML или HTML | Проверить `/yml.php` |
| rutector.ru | HTML (Bitrix) | Проверить `/upload/` export |
| aerocompressors.ru | YML или HTML | Проверить sitemap |
| v-p-k.ru | HTML (Bitrix) | Проверить JSON XHR |
| compressortyt.ru | HTML | Задокументировано в брифе |

---

## Инструкция по уточнению разведки

Запусти перед первым полным парсингом:
```bash
python -m monitor --recon
```
Это проверит все типовые пути фидов и запишет результаты обратно в этот файл.
