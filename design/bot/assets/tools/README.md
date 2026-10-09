# tools/ — как пересобирается иконочный набор

Всё, что лежит в `design/bot/assets/icons/` и `design/bot/assets/emoji/`, собирается из
одного генератора — руками SVG не правим (иначе набор разъедется по толщине линии и ритму).

```bash
cd "/Users/egor/Downloads/VPN servise"

# 1) геометрия: 48 svg + 2 листа HTML
node design/bot/assets/tools/build-icons.mjs

# 2) PNG иконок 96×96 (#45E698, прозрачный фон)
cd design/bot/assets/icons
for s in *-mono.svg; do cairosvg "$s" -o "${s%-mono.svg}-mono.png" \
  --output-width 96 --output-height 96; done
cd -

# 3) PNG эмодзи 100×100 (медальоны: вектор пишется в /tmp, в репозиторий не попадает)
node design/bot/assets/tools/build-icons.mjs --emit-emoji-svg /tmp/kometa-emoji-svg
for s in /tmp/kometa-emoji-svg/*.svg; do cairosvg "$s" \
  -o "design/bot/assets/emoji/$(basename "${s%.svg}").png" \
  --output-width 100 --output-height 100; done

# 4) листы превью — только через Open Design (см. design/bot/README-BUILD.md)
OD_DATA="$HOME/Library/Application Support/Open Design/namespaces/release-stable/data/projects/kometa-admin"
OD_CLI="/Applications/Open Design.app/Contents/Resources/app/prebundled/daemon/daemon-cli.mjs"
cp design/bot/assets/icons/sheet.html "$OD_DATA/"
node "$OD_CLI" export sheet.html --project kometa-admin --format image --image-format png \
  --out "$PWD/design/bot/assets/icons/sheet.png" --daemon-url http://127.0.0.1:65445 --json
cp design/bot/assets/emoji/sheet.html "$OD_DATA/"
node "$OD_CLI" export sheet.html --project kometa-admin --format image --image-format png \
  --out "$PWD/design/bot/assets/emoji/sheet.png" --daemon-url http://127.0.0.1:65445 --json

# 5) самопроверка: 48 svg + 24 mono png + 12 эмодзи + 2 листа
node design/bot/assets/tools/build-icons.mjs --check
```

## Что где лежит

| Артефакт | Файл | Требование |
|---|---|---|
| Иконка, токенный цвет | `icons/<name>.svg` | `viewBox 0 0 24 24`, `stroke="currentColor"`, stroke 1.75, round caps |
| Иконка, жёсткий цвет | `icons/<name>-mono.svg` | тот же контур, `#45E698` (--brand-300) |
| PNG для превью/Telegram | `icons/<name>-mono.png` | 96×96, RGBA, фон прозрачный |
| Медальон premium-эмодзи | `emoji/<name>.png` | ровно 100×100, RGBA (требование Telegram к static custom_emoji) |
| Листы превью | `icons/sheet.html` + `.png`, `emoji/sheet.html` + `.png` | канва 1440px, экспорт Open Design ×2 → 2880px |
| Анимация | `emoji/SPEC-TGS.md` | 512×512, ≤3 с, луп, 60 FPS, ≤64 КБ |

## Правила набора

* Сетка 24×24, оптический шаг 2px: крайние точки геометрии — 2.5–3, чтобы видимый край
  обводки 1.75 попадал на линию 2px.
* Деталь тоньше 0.75px запрещена; заливка — только брендовые акценты (звезда/ромб/знак).
* Иконка обязана читаться в 20px — на листе `icons/sheet.png` есть отдельная строка
  с реальным размером 20px.
* Цвета — только из `design/bot/tokens.css`; в листах значения продублированы
  в `:root` (лист самодостаточен: Open Design копирует к себе один HTML-файл).
