# Макеты экранов бота — как пересобрать

Здесь живёт **генератор** десяти макетов бота: 9 экранов + сводный борд 3×3.
PNG в `design/bot/renders/` — **артефакт**: он собирается из этих HTML и руками
не правится (как и сами HTML — они генерируются из `parts/`).

## Одна команда

```bash
# из корня репозитория
python3 design/bot/tools/screens/build.py          # собрать 9 экранов + борд в design/bot/screens/
bash   design/bot/tools/screens/render.sh          # отрендерить их в PNG через Open Design
```

`render.sh` без аргументов рендерит всё; можно указать конкретные экраны:

```bash
bash design/bot/tools/screens/render.sh 01-start-new 00-board
python3 design/bot/tools/screens/build.py --only 01-start-new        # только один HTML
python3 design/bot/tools/screens/build.py --out /tmp/проверка        # ничего не перезаписывая
```

## Что где лежит

| Файл | Что это |
|---|---|
| `build.py` | Генератор. Собирает самодостаточные HTML: токены и кит внутри `<style>`, картинки бренда — data-URI base64 |
| `kit.css` | Визуальный кит: страница-«дизайн-лист», мокап клиента Telegram 420 px, пузыри, клавиатуры, чипы, борд |
| `parts/NN-slug.html` | Содержимое одного экрана. Мокап обёрнут маркерами `<!--PHONE--> … <!--/PHONE-->` |
| `render.sh` | Копирует HTML в проект Open Design `kometa-admin` и экспортирует PNG (deviceScaleFactor=2) |
| `check_texts.py` | Регрессия по текстам: фразы макетов сверяются с `app/bot/texts.py` |
| `../../screens/*.html` | **Результат** генератора (не править руками — правьте `parts/` и пересоберите) |
| `../../renders/*.png` | **Результат** рендера (не править руками вообще) |

Сводный борд собирается из **тех же** фрагментов мокапов, что и отдельные экраны
(`build.py` вырезает блок между маркерами), поэтому борд и экран не могут разойтись.

## Откуда берутся значения

* цвета, радиусы, кегли — `design/bot/tokens.css` (значения скопированы в `kit.css`
  дословно: каждый HTML самодостаточен и не ходит за внешними файлами);
* тексты и подписи кнопок — `design/bot/START-COPY.md` (копия) и `app/bot/texts.py`
  (то, что уже в коде); цены — `seed_plans` в `app/db/session.py`;
* картинки — `design/bot/assets/banner/kometa-start-hero-1280x720.jpg` (баннер hero,
  вшивается в экран 01) и `brand/avatar/kometa-avatar-round-512.png` (аватар в шапке
  чата; генератор уменьшает его до 96 px, иначе data-URI на борде из девяти
  мокапов перестаёт открываться в Chromium).

**Важно про баннер:** он вшит в HTML экрана 01 в момент сборки. Если баннер
перерисовали — пересоберите HTML (`build.py`) и заново отрендерите `01-start-new.png`
и `00-board.png`, иначе PNG останется со старым кадром.

## Проверки

```bash
python3 design/bot/tools/screens/check_texts.py     # 0 расхождений с texts.py — норма
```

Скрипт проверяет каждую фразу из макетов дважды: есть ли она дословно в `texts.py`
(это ошибка, код возврата 1) и есть ли она в `parts/*.html` (это предупреждение:
макет может осознанно сжимать строку в карточку или пару «ключ → значение»).

Анти-слоп линтер Open Design (нужен запущенный Open Design):

```bash
node "/Applications/Open Design.app/Contents/Resources/app/prebundled/daemon/daemon-cli.mjs" \
  lint design/bot/screens/01-start-new.html --json --daemon-url http://127.0.0.1:65445
```

На всех десяти файлах линтер даёт `p0: 0, p1: 0, p2: 0`.

## Требования

* Python 3.10+; для уменьшения аватара — Pillow (без него генератор возьмёт
  исходный 512 px, HTML станет тяжелее, но соберётся);
* для PNG — запущенный Open Design (демон на `127.0.0.1:65445`) и macOS-путь
  приложения; подробности процедуры — `design/bot/README-BUILD.md`.

## Чего здесь нет

`design/bot/screens/00-hero-start.html` и `design/bot/renders/components*.png`,
`hero-start@2x.png` — зона других задач (UI-кит и hero-баннер), этот генератор
их не собирает и не перезаписывает.
