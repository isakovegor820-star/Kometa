# Дизайн бота Kometa

Что здесь лежит и с чего начинать читать.

**Начни с [`GUIDE.md`](GUIDE.md)** — это главный документ: что не так на примерах конкурента,
что человек хочет увидеть после `/start`, вся дизайн-система, чем заменить дефолтные смайлики
и дорожная карта «что делать в каком порядке».

## Карта папки

| Хочу… | Смотри |
|---|---|
| Понять замысел целиком, с обоснованиями | [`GUIDE.md`](GUIDE.md) |
| Увидеть, как это выглядит | [`renders/00-board.png`](renders/00-board.png) — все 9 экранов сеткой |
| Посмотреть отдельный экран | [`renders/01-start-new.png`](renders/01-start-new.png) … [`renders/09-error-node.png`](renders/09-error-node.png) |
| Взять компонент (кнопка, карточка, статус) | [`renders/components.png`](renders/components.png), [`components.html`](components.html) |
| Взять цвет или радиус | [`tokens.css`](tokens.css) |
| Взять правило (сколько кнопок, какой тег, какой цвет) | [`DESIGN-SYSTEM.md`](DESIGN-SYSTEM.md) |
| Понять, что есть в боте сейчас | [`INVENTORY.md`](INVENTORY.md) |
| Взять готовый текст | [`START-COPY.md`](START-COPY.md) |
| Заменить конкретный эмодзи | [`EMOJI-MAP.md`](EMOJI-MAP.md) |
| Взять иконку | [`assets/icons/`](assets/icons) — 24 SVG + PNG, лист превью `sheet.png` |
| Взять баннер для первого сообщения | [`assets/banner/kometa-start-hero-1280x720.jpg`](assets/banner/kometa-start-hero-1280x720.jpg) || Понять, что можно сделать премиум-эмодзи | [`assets/emoji/SPEC-TGS.md`](assets/emoji/SPEC-TGS.md) |
| Проверить, что вообще позволяет Telegram | [`research/telegram-visual-capabilities.md`](research/telegram-visual-capabilities.md) |
| Пересобрать макет или ассет | [`README-BUILD.md`](README-BUILD.md) |

## Что здесь есть, в цифрах

* **9 экранов бота** + сводный борд — HTML-исходники в [`screens/`](screens), рендеры в [`renders/`](renders).
* **1 UI-кит** — все компоненты и раздел «как НЕ надо».
* **24 фирменные иконки** вместо дефолтных смайликов: SVG на `currentColor` + моно-PNG 96×96.
* **12 заготовок premium-эмодзи** (медальоны 100×100) + спецификация анимации TGS/WebM.
* **1 баннер** для первого сообщения бота (16:9 и квадрат).
* **Токены** дизайн-системы, из которых соберётся и веб-панель.

### Баннер для `/start`

| Файл | Размер | Зачем |
|---|---|---|
| [`assets/banner/kometa-start-hero-1280x720.jpg`](assets/banner/kometa-start-hero-1280x720.jpg) | 1280×720, 84 КБ | **основной** — уходит в первое сообщение бота (`sendPhoto` + caption + кнопки) |
| [`assets/banner/kometa-start-hero-1080.jpg`](assets/banner/kometa-start-hero-1080.jpg) | 1080×1080, 74 КБ | то же в квадрате: в ленте занимает больше места на телефоне |
| [`assets/banner/master/`](assets/banner/master) | 2560×1440 | мастер-файл без сжатия — для канала, обложек, печати |

Исходник баннера — [`screens/00-hero-start.html`](screens/00-hero-start.html) (SVG, правится текстом).
Если меняешь текст в баннере, помни: он обещает то же, что подпись под ним. В пробном доступе
**одно** устройство, поэтому в баннере стоит «3 дня бесплатно», а не «до 3 устройств».

## Важно: код бота здесь не меняется

Эта папка — **дизайн-предложение**. Ни один файл в `app/` в рамках этой работы не правился:
тексты и клавиатуры в [`START-COPY.md`](START-COPY.md) и [`EMOJI-MAP.md`](EMOJI-MAP.md) готовы
к вставке, но вставка — отдельный шаг (этапы A–C в [`GUIDE.md`](GUIDE.md)).

Пока собирались макеты, в `app/**` шли **посторонние правки** (другой процесс: бот уведомлений,
`notify_admin.py`, namespace в `admin_order_kb`) — поэтому ссылки на строки в
[`INVENTORY.md`](INVENTORY.md) привязаны к снимку с SHA-256 и после новых правок кода
пересчитываются встроенным в инвентарь скриптом.

## Как пересобирать

Всё воспроизводимо:

```bash
# макеты: HTML из частей → PNG через Open Design (одна команда на всё)
python3 design/bot/tools/screens/build.py     # 9 экранов + борд в design/bot/screens/
bash    design/bot/tools/screens/render.sh    # они же в PNG (нужен запущенный Open Design)

# регрессия: не разъехались ли тексты макетов с реальным ботом
python3 design/bot/tools/screens/check_texts.py    # 34 проверки, расхождений должно быть 0

# иконки и медальоны: один генератор на всю геометрию
node design/bot/assets/tools/build-icons.mjs          # собрать
node design/bot/assets/tools/build-icons.mjs --check   # проверить
```

Подробности и требования к файлам — в [`README-BUILD.md`](README-BUILD.md)
и [`assets/tools/README.md`](assets/tools/README.md).
