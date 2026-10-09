# Как собирать макеты бота (Open Design)

Проверенная процедура. Все макеты бота — **живой HTML**, а не картинки: HTML правится,
PNG пересобирается одной командой. Это же и дымовой тест: если HTML сломался, сломается
и рендер.

## 1. Что откуда берётся

| Что | Откуда |
|---|---|
| Цвета, радиусы, кегли | [`tokens.css`](tokens.css) — **единственный источник правды**, значения не переопределяем |
| Знак, баннер, аватар | [`../../brand/`](../../brand/README.md): `banner/kometa-welcome-1280x720.png`, `avatar/kometa-avatar-512.png` |
| Иконки | `assets/icons/*.svg` (моно-линейные, `currentColor`) |
| Правила и лимиты текста | [`DESIGN-SYSTEM.md`](DESIGN-SYSTEM.md) |
| Что есть в боте сейчас | [`INVENTORY.md`](INVENTORY.md) |
| Как звучит бот | [`START-COPY.md`](START-COPY.md) |

## 2. Требования к HTML

1. **Ширина канвы 1440px**, фон `#05080a`. Экспорт идёт с `deviceScaleFactor = 2`,
   поэтому PNG получается 2880px по ширине: мелкий текст ≥ 13px, иконки ≥ 20px.
2. **Мокап клиента Telegram**: карточка 420px, фон чата `var(--surface-chat)`, пузыри,
   клавиатура высотой ряда 44px, радиус 14px. Полный «телефон» рисуем только там,
   где нужен контекст; для компонентов — отдельные фрагменты.
3. **Самодостаточность**: никаких CDN, внешних шрифтов и сетевых картинок. Картинки —
   либо `data:image/...;base64`, либо файл рядом (тогда копируем его вместе с HTML).
   Шрифт — системный стек из токенов (`--font-ui`).
4. **Никаких «фигур вместо текста»**: подписи кнопок пишем настоящим текстом.

## 3. Отрисовка PNG

```bash
cd "/Users/egor/Downloads/VPN servise"

OD_DATA="$HOME/Library/Application Support/Open Design/namespaces/release-stable/data/projects/kometa-admin"
OD_CLI="/Applications/Open Design.app/Contents/Resources/app/prebundled/daemon/daemon-cli.mjs"

# 1) положить страницу в проект Open Design
cp design/bot/<файл>.html "$OD_DATA/"

# 2) отрендерить PNG
node "$OD_CLI" export <файл>.html --project kometa-admin \
  --format image --image-format png \
  --out "$PWD/design/bot/renders/<имя>.png" \
  --daemon-url http://127.0.0.1:65445 --json
```

* Демон Open Design должен быть запущен (приложение открыто). Проверка:
  `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:65445/` → любой ответ, кроме
  «connection refused», значит живой.
* Команда возвращает JSON с `path` и `bytes`. Если `bytes` нет — рендер не удался.
* **Всегда открывай PNG глазами** (`read_image`) и правь то, что обрезано или наехало.

## 4. Линтер Open Design (анти-слоп)

```bash
node "$OD_CLI" lint design/bot/<файл>.html --json --daemon-url http://127.0.0.1:65445
```

Полезен как проверка «нет ли синтетического мусора»: лишние декоративные градиенты,
пустые блоки, случайные шрифты.

## 5. Правила правки

* Один экран — один HTML-файл. Имена: `NN-имя.html` (`01-start-new.html`).
* Правка дизайна = правка HTML + пересборка PNG. PNG в репозитории — артефакт,
  руками его не редактируем.
* Цвета в HTML не хардкодим: берём из токенов (`var(--brand-300)`). Единственное
  исключение — фон канвы.
