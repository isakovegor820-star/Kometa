# Визуальные возможности Telegram-бота: Bot API 10.3 (факты, октябрь 2026)

Дата проверки: **2026-10-08**. Актуальная версия Bot API — **10.3 от 24 августа 2026** ([core.telegram.org/bots/api](https://core.telegram.org/bots/api), [changelog](https://core.telegram.org/bots/api-changelog)).

Все утверждения ниже — из официальных источников (`core.telegram.org/bots/api`, `core.telegram.org/bots/api-changelog`, `core.telegram.org/bots/features`, `core.telegram.org/bots/webapps`, `core.telegram.org/stickers`, `core.telegram.org/api/*`, `telegram.org/blog`, `telegram.org/faq`). Спорное помечено **«не подтверждено»**.

---

## 1. Кастомные (premium) эмодзи в сообщениях и на кнопках

**В тексте сообщения — да, через сущность `custom_emoji`.**

- `MessageEntity.type` поддерживает значение `custom_emoji`, а поле `MessageEntity.custom_emoji_id` содержит «unique identifier of the custom emoji. Use `getCustomEmojiStickers` to get full information about the sticker» ([api#messageentity](https://core.telegram.org/bots/api#messageentity)).
- Способы задать: массив `entities` (или `caption_entities`), `parse_mode=HTML` — тег `<tg-emoji emoji-id="5368324170671202286"></tg-emoji>`, либо `parse_mode=MarkdownV2` — `![](tg://emoji?id=5368324170671202286)` ([formatting options](https://core.telegram.org/bots/api#formatting-options)).
- Обязателен «валидный emoji как альтернативное значение»: он показывается там, где кастомный эмодзи отобразить нельзя (системные уведомления), **а также если сообщение форварднёт non-premium пользователь** ([formatting options](https://core.telegram.org/bots/api#formatting-options)).
- Появилось: сущность и `custom_emoji_id` — Bot API **6.2** (12.08.2022); возможность задавать их через HTML/MarkdownV2 — Bot API **6.7** (21.04.2023) ([changelog](https://core.telegram.org/bots/api-changelog)).

**`getCustomEmojiStickers`** — принимает `custom_emoji_ids`: «A JSON-serialized list of custom emoji identifiers. **At most 200** custom emoji identifiers can be specified» ([api#getcustomemojistickers](https://core.telegram.org/bots/api#getcustomemojistickers)). Возвращает массив `Sticker`; у `Sticker` есть поля `type` (`regular`/`mask`/`custom_emoji`), `custom_emoji_id`, `needs_repainting`, `emoji`, `set_name` ([api#sticker](https://core.telegram.org/bots/api#sticker)).

**Кто имеет право использовать кастомные эмодзи (владелец бота), ровно две формулировки в доках:**

> «Custom emoji entities can only be used by bots that purchased additional usernames on Fragment **or** in the messages directly sent by the bot to private, group and supergroup chats **if the owner of the bot has a Telegram Premium subscription**» ([formatting options](https://core.telegram.org/bots/api#formatting-options), то же в [changelog 9.4](https://core.telegram.org/bots/api-changelog) и в описании `icon_custom_emoji_id`).

Следствия:

- **Премиум владельца бота** даёт право только в **private / group / supergroup** и только в сообщениях, отправленных самим ботом. **Каналы в этот перечень не входят** — для постов в канале нужен путь через Fragment (прямого запрета в доках нет, но и разрешения для каналов нет — считаем «не подтверждено/не разрешено»).
- **Владение эмодзи-паком не требуется**: докупать нужно не пак, а *использовать* — требование сформулировано через право бота, а не через принадлежность стикера.
- **Бусты не требуются** ни в одном из двух путей (бусты фигурируют только для супергрупп, см. §2).
- Покупка дополнительных username на Fragment как условие зафиксирована в changelog 6.7: «for bots that purchased additional usernames on Fragment» ([changelog](https://core.telegram.org/bots/api-changelog)). Сколько именно username нужно — **не подтверждено** (в доках числа нет).

**Надписи на inline-кнопках — только иконка, не текст.**

- Текст кнопки — обычное поле `InlineKeyboardButton.text` типа `String`, без `entities`, поэтому встроить `custom_emoji` внутрь подписи нельзя ([api#inlinekeyboardbutton](https://core.telegram.org/bots/api#inlinekeyboardbutton)).
- Единственный механизм — `icon_custom_emoji_id`: «Unique identifier of the custom emoji **shown before the text** of the button. Can only be used by bots that purchased additional usernames on Fragment or in the messages directly sent by the bot to private, group and supergroup chats if the owner of the bot has a Telegram Premium subscription» ([api#inlinekeyboardbutton](https://core.telegram.org/bots/api#inlinekeyboardbutton), аналогично у `KeyboardButton` ([api#keyboardbutton](https://core.telegram.org/bots/api#keyboardbutton))). Добавлено в Bot API **9.4** (09.02.2026).
- Кастомный эмодзи **в подписи кнопки** возможен только в rich-сообщениях: `RichMessageButton.text` имеет тип `RichText` и «May contain only plain text, `RichTextCustomEmoji` and `RichTextDateTime` entities» ([api#richmessagebutton](https://core.telegram.org/bots/api#richmessagebutton), Bot API 10.1–10.3).
- Прочие ограничения: кастомные эмодзи разрешены в `TextQuote`/`ReplyParameters.quote`, в заголовке чек-листа и в тексте задачи, в тексте подарка (`sendGift`, `giftPremiumSubscription`) — там перечислены допустимые типы сущностей ([api#textquote](https://core.telegram.org/bots/api#textquote), [api#replyparameters](https://core.telegram.org/bots/api#replyparameters), [api#inputchecklist](https://core.telegram.org/bots/api#inputchecklist), [api#sendgift](https://core.telegram.org/bots/api#sendgift)).

---

## 2. Создание собственных эмодзи/стикер-паков

**Методы:** `createNewStickerSet` → `addStickerToSet` (+ `uploadStickerFile`, `replaceStickerInSet`, `setStickerPositionInSet`, `deleteStickerFromSet`) ([api#createnewstickerset](https://core.telegram.org/bots/api#createnewstickerset)).

`createNewStickerSet` ([api#createnewstickerset](https://core.telegram.org/bots/api#createnewstickerset)):

| Параметр | Значение |
|---|---|
| `user_id` | Обязателен. «User identifier of created sticker set owner» — **пак принадлежит пользователю, не боту** |
| `name` | 1–64 символа, только латиница/цифры/`_`, начинается с буквы, без двойных `_`, обязан заканчиваться на `_by_<bot_username>` (это же имя используется в `t.me/addstickers/…`) |
| `title` | 1–64 символа |
| `stickers` | 1–50 начальных `InputSticker` |
| `sticker_type` | `regular` \| `mask` \| `custom_emoji` (по умолчанию `regular`) |
| `needs_repainting` | только для `custom_emoji`: перекрашивать в цвет текста / акцентный цвет статуса / белый на аватарах |

- `addStickerToSet`: «Emoji sticker sets can have up to **200** stickers. Other sticker sets can have up to **120** stickers» ([api#addstickertoset](https://core.telegram.org/bots/api#addstickertoset)).
- `InputSticker`: `sticker`, `format` (`static` — .WEBP/.PNG, `animated` — .TGS, `video` — .WEBM), `emoji_list` (1–20 эмодзи), `keywords` (0–20 слов, суммарно до 64 символов, только для `regular` и `custom_emoji`), `mask_position` ([api#inputsticker](https://core.telegram.org/bots/api#inputsticker)).
- Бот может редактировать **только свои** паки: «these methods will only work on packs created by the bot that is calling them» ([features](https://core.telegram.org/bots/features)).
- Дополнительно: `setCustomEmojiStickerSetThumbnail`, `setStickerSetTitle`, `setStickerEmojiList`, `setStickerKeywords`, `deleteStickerSet` (Bot API **6.6**, 09.03.2023; именно 6.6 добавил «the creation of custom emoji sticker sets in createNewStickerSet») ([changelog](https://core.telegram.org/bots/api-changelog)).
- `sticker_type` и `StickerSet.sticker_type` появились в Bot API **6.2** (12.08.2022) ([changelog](https://core.telegram.org/bots/api-changelog)).
- `needs_repainting` («adaptive emoji»): «they will always match the current context (e.g., white on photos, accent color when used as status)» ([features](https://core.telegram.org/bots/features), [stickers](https://core.telegram.org/stickers)).

**Требования к аккаунту-владельцу и бусты:**

- «**Everyone can create** new custom emoji, however, **adding and using custom sets is currently an exclusive feature of Telegram Premium users**» ([stickers](https://core.telegram.org/stickers)). То есть создание как таковое Premium/бустов не требует, а использование — требует.
- В документации Bot API **нет** требования бустов или Premium для вызова `createNewStickerSet` со `sticker_type=custom_emoji`; ограничение привязано к *использованию* эмодзи в сообщениях (см. §1) — **«не подтверждено»**: серверные ошибки для неподходящего владельца в официальных доках не описаны.
- Бусты относятся к **супергруппам**, а не к созданию паков: «After reaching at least the boost level specified in the `group_emoji_stickers_level_min` config parameter, supergroups gain the ability to associate a custom emoji stickerset, which can be used by all users of the group (**including non-Premium users!**)» ([core.telegram.org/api/boost](https://core.telegram.org/api/boost)). **Конкретное число бустов не фиксировано** — задаётся серверным конфигом (`*_level_min`), поэтому ответ «сколько именно» — **не подтверждено**.
- FAQ подтверждает смысл бустов для групп: «Members can boost a group with their Premium subscription to unlock features for everyone — like posting stories, **custom emoji packs**, voice-to-text and custom group appearance» ([telegram.org/faq](https://telegram.org/faq)).
- **Fragment:** боты, купившие дополнительные username, могут использовать кастомные эмодзи вне зависимости от Premium-пути ([changelog 6.7](https://core.telegram.org/bots/api-changelog)); с Bot API **9.3** бот может отключить основной username, если у него есть активные дополнительные с Fragment ([changelog 9.3](https://core.telegram.org/bots/api-changelog)). Иных Fragment-требований для паков в доках нет.

---

## 3. Внешний вид inline-кнопок

**`style`** — Bot API **9.4** (09.02.2026): «Added the field `style` to the classes `KeyboardButton` and `InlineKeyboardButton`, allowing bots to change the color of buttons» ([changelog 9.4](https://core.telegram.org/bots/api-changelog)). Значения: «Must be one of “danger” (red), “success” (green) or “primary” (blue). **If omitted, then an app-specific style is used**» ([api#inlinekeyboardbutton](https://core.telegram.org/bots/api#inlinekeyboardbutton), [api#keyboardbutton](https://core.telegram.org/bots/api#keyboardbutton)). Анонс: «Bot developers can now assign colors and emoji to their buttons — to make interfaces more intuitive or to highlight specific actions» ([telegram.org/blog/crafting-android-design-and-more](https://telegram.org/blog/crafting-android-design-and-more), 09.02.2026).

**`icon_custom_emoji_id`** — та же версия 9.4, то же условие доступа, что и для кастомных эмодзи в сообщениях (Fragment-username **или** Premium владельца бота в private/group/supergroup).

**Rich-сообщения (10.1+)** дают расширенный набор: `RichMessageButton.style` — `danger`, `success`, `primary` **или `link`** («the button is shown as a regular link without borders… The style “link” is allowed only for callback buttons»), а `text` — `RichText`, допускающий plain text, `RichTextCustomEmoji`, `RichTextDateTime` ([api#richmessagebutton](https://core.telegram.org/bots/api#richmessagebutton)). Кнопки можно ставить **внутри текста**: `<tg-button type="url" style="success" url="https://t.me">url</tg-button>` и группировать в строки `<tg-button-row align="left|center|right">` ([rich formatting options](https://core.telegram.org/bots/api#rich-message-formatting-options)).

**Прочее в `InlineKeyboardButton`:** `text`, `url`, `callback_data` (1–64 байта), `web_app` (**только private chats**, не поддерживается для business-сообщений), `login_url`, `switch_inline_query*`, `copy_text` (Bot API **7.11**, 31.10.2024), `callback_game` (обязан быть первой кнопкой первого ряда), `pay` (только в invoice-сообщениях), `disabled` (`DisabledButton`, Bot API **10.3**, 24.08.2026) ([api#inlinekeyboardbutton](https://core.telegram.org/bots/api#inlinekeyboardbutton), [changelog](https://core.telegram.org/bots/api-changelog)). В `InlineKeyboardMarkup`/`ReplyKeyboardMarkup` с 10.3 есть `force_reply` ([changelog 10.3](https://core.telegram.org/bots/api-changelog)).

**Лимиты:** в `InlineKeyboardMarkup` документирован только формат «Array of button rows» — **максимум кнопок/рядов не документирован** («не подтверждено»). Для rich-блоков кнопок лимит явный: `InputRichBlockButtons.buttons` — «List of **1-8** buttons to send» ([api#inputrichblockbuttons](https://core.telegram.org/bots/api#inputrichblockbuttons)).

**Старые клиенты:** официального описания фолбэка для `style`/`icon_custom_emoji_id` нет — **«не подтверждено»**. Косвенные официальные ориентиры: (а) для сущностей Telegram прямо писал «Older clients will display unsupported message» (changelog **5.6**, spoiler) ([changelog](https://core.telegram.org/bots/api-changelog)); (б) в обновлении от 25.08.2026 добавлены «placeholders for app updates» — заглушка с кнопкой обновления, «which appears when outdated apps can't render a new feature» ([telegram.org/blog/welcome-messages-buttons-TG-13](https://telegram.org/blog/welcome-messages-buttons-TG-13)).

---

## 4. Визуал на уровне сообщения

**`link_preview_options`** (`LinkPreviewOptions`, Bot API **7.0**, 29.12.2023; заменил `disable_web_page_preview`) — поля `is_disabled`, `url`, `prefer_small_media`, `prefer_large_media`, **`show_above_text`** («link preview must be shown above the message text») ([api#linkpreviewoptions](https://core.telegram.org/bots/api#linkpreviewoptions); тогда же `ReplyParameters` заменил `reply_to_message_id`, а `blockquote` стал доступен в HTML/MarkdownV2).

**Медиа + подпись + клавиатура в одном вызове — да:** `sendPhoto`/`sendVideo`/`sendAnimation`/`sendDocument` принимают `caption`, `parse_mode`, `caption_entities`, `show_caption_above_media` (Bot API **7.4**, 28.05.2024), `has_spoiler` (6.4), `reply_markup`, `reply_parameters`, `message_effect_id` ([api#sendphoto](https://core.telegram.org/bots/api#sendphoto), [api#sendanimation](https://core.telegram.org/bots/api#sendanimation), [changelog 7.4](https://core.telegram.org/bots/api-changelog)).

- Лимиты подписи — **0–1024 символа** после разбора сущностей (у всех типов медиа) ([api#inputmediaphoto](https://core.telegram.org/bots/api#inputmediaphoto)).
- `InputMediaPhoto`: `type`, `media` (file_id / HTTP URL / `attach://`), `caption`, `parse_mode`, `caption_entities`, `show_caption_above_media`, `has_spoiler` ([api#inputmediaphoto](https://core.telegram.org/bots/api#inputmediaphoto)).
- Альбомы: `sendMediaGroup` — «must include **2-10** items»; параметра `reply_markup` у метода **нет** (в отличие от одиночных send*-методов) ([api#sendmediagroup](https://core.telegram.org/bots/api#sendmediagroup)).
- Загрузка: до **10 MB** фото, до **50 MB** прочие файлы в облачном Bot API (локальный сервер — до 2000 MB); по HTTP URL — 5 MB для фото и 20 MB для остального ([api#sending-files](https://core.telegram.org/bots/api#sending-files), [local bot api](https://core.telegram.org/bots/api#using-a-local-bot-api-server)).

**Эффекты сообщений.** В `sendMessage` (и в sendPhoto/sendVideo/sendAnimation/sendSticker/… ) параметр называется **`message_effect_id`**, а в объекте `Message` поле — **`effect_id`**; оба — Bot API **7.4** (28.05.2024) ([changelog 7.4](https://core.telegram.org/bots/api-changelog)). Ограничение: «Unique identifier of the message effect to be added to the message; **for private chats only**» ([api#sendmessage](https://core.telegram.org/bots/api#sendmessage)). С 9.3 `message_effect_id` добавлен также в `forwardMessage`/`copyMessage` ([changelog 9.3](https://core.telegram.org/bots/api-changelog)).

**Как бот получает валидные effect id:** в Bot API **нет** метода-каталога (класса `MessageEffect` тоже нет). Официальный источник списка — MTProto `messages.getAvailableEffects`, но это «**Only users can use this method**» ([core.telegram.org/method/messages.getAvailableEffects](https://core.telegram.org/method/messages.getAvailableEffects), [core.telegram.org/api/effects](https://core.telegram.org/api/effects)). Практический вывод: id приходится хардкодить, наблюдая их в клиенте; официального бот-API-способа получения списка **нет** — **«не подтверждено»**, что существует документированный каталог id для ботов.

**`reply_parameters`** (`ReplyParameters`): `message_id`, `chat_id`, `ephemeral_message_id` (10.2), `allow_sending_without_reply`, `quote` (**0–1024** символа, обязан быть точной подстрокой исходного сообщения; в цитате сохраняются только `bold`, `italic`, `underline`, `strikethrough`, `spoiler`, `custom_emoji`, `date_time`), `quote_parse_mode`, `quote_entities`, `quote_position`, `checklist_task_id` (9.2), `poll_option_id` (9.6) ([api#replyparameters](https://core.telegram.org/bots/api#replyparameters)).

**Форматирование (обычные сообщения).** Поддерживаются `bold`, `italic`, `underline`, `strikethrough`, `spoiler`, `blockquote`, `expandable_blockquote`, `code`, `pre`, `text_link`, `text_mention`, `custom_emoji`, `date_time` ([api#messageentity](https://core.telegram.org/bots/api#messageentity)).

- HTML: `<blockquote>…</blockquote>` и `<blockquote expandable>…</blockquote>` ([formatting options](https://core.telegram.org/bots/api#formatting-options)); `blockquote` — Bot API **7.0**, `expandable_blockquote` — Bot API **7.4**; spoiler — Bot API **5.6** ([changelog](https://core.telegram.org/bots/api-changelog)).
- `date_time` — Bot API **9.5** (01.03.2026): `<tg-time unix="1647531900" format="wDT">…</tg-time>`, спецификация формата — регулярка `r|w?[dD]?[tT]?` (`r` — относительное время, `w` — день недели, `d`/`D` — дата, `t`/`T` — время) ([api#messageentity](https://core.telegram.org/bots/api#messageentity), [changelog 9.5](https://core.telegram.org/bots/api-changelog)).
- Вложенность: `blockquote`/`expandable_blockquote` **нельзя вкладывать** друг в друга; `bold`/`italic`/`underline`/`strikethrough`/`spoiler` вкладываются свободно, кроме `pre`/`code` ([formatting options](https://core.telegram.org/bots/api#formatting-options)).
- Кастомные эмодзи **в подписях** работают: подписи принимают `parse_mode`/`caption_entities`, а ограничения на `custom_emoji` — те же, что для текста ([api#inputmediaphoto](https://core.telegram.org/bots/api#inputmediaphoto), [formatting options](https://core.telegram.org/bots/api#formatting-options)).
- Лимиты текста: обычное сообщение — **1–4096** символов; подпись — **0–1024** ([api#sendmessage](https://core.telegram.org/bots/api#sendmessage), [api#inputmediaphoto](https://core.telegram.org/bots/api#inputmediaphoto)). Для rich-сообщений в блоге заявлено «up to **32,768** characters now, with a merciful Show More button after the first 8,000 or so» ([telegram.org/blog/watch-apps-and-more](https://telegram.org/blog/watch-apps-and-more)); в справочнике API это число **не указано** — «не подтверждено» на уровне API-доков.

**Rich Messages (Bot API 10.1, 11.06.2026; развиты в 10.2/10.3)** — отдельный слой вёрстки: метод `sendRichMessage` с `InputRichMessage` (`html` | `markdown` | `blocks`, ровно одно), `media` (`InputRichMessageMedia` для загрузки собственных файлов), `is_rtl`, `skip_entity_detection` ([api#sendrichmessage](https://core.telegram.org/bots/api#sendrichmessage), [api#inputrichmessage](https://core.telegram.org/bots/api#inputrichmessage)). Поддерживаются заголовки, таблицы (выравнивание, `bordered`, `striped`, `compact`, `colspan`/`rowspan`), `<details>`, футноты, LaTeX (`<tg-math>`, `<tg-math-block>`), карты `<tg-map>`, коллажи `<tg-collage>`, слайд-шоу `<tg-slideshow>`, чек-боксы, `<aside>`/`<cite>`, `<footer>`, `<hr/>`, медиа-блоки с подписями и кредитами, **кнопки внутри текста** ([rich formatting options](https://core.telegram.org/bots/api#rich-message-formatting-options), [features](https://core.telegram.org/bots/features)). Медиа: «Media blocks support only HTTP and HTTPS URLs» при описании в разметке, но при передаче через массив `media` допускается загрузка своих файлов (10.2) ([api#inputrichmessagemedia](https://core.telegram.org/bots/api#inputrichmessagemedia)).

**Стриминг и черновики:** `sendMessageDraft` (Bot API **9.3**, 31.12.2025; с **9.5** доступен всем ботам) — «temporary 30-second preview», пустой `text` показывает «Thinking…»; `sendRichMessageDraft` (10.1) + тег `<tg-thinking>`; в 10.3 добавлены `can_stop`/`keep_on_stop` и update `MessageGenerationStopped` ([api#sendmessagedraft](https://core.telegram.org/bots/api#sendmessagedraft), [changelog](https://core.telegram.org/bots/api-changelog)).

---

## 5. Стикеры как UI, профиль бота, меню

**Технические требования (официальная страница stickers):**

| Формат | Требования |
|---|---|
| Статика (.PNG/.WEBP) | стикер: одна сторона ровно **512 px**, вторая ≤512; эмодзи: ровно **100×100** ([stickers](https://core.telegram.org/stickers)) |
| Анимация (.TGS, Lottie/Bodymovin) | холст ровно **512×512**, объекты не выходят за холст, длина ≤**3 c**, обязательный луп, файл ≤**64 KB**, **60 FPS**; запрещены Auto-bezier keys, Expressions, Masks, Layer Effects, Images, Solids, Texts, 3D Layers, Merge Paths, Star Shapes, Gradient Strokes, Repeaters, Time Stretching/Remapping, Auto-Oriented Layers ([stickers](https://core.telegram.org/stickers)) |
| Видео (.WEBM) | стикер: одна сторона ровно **512 px**; эмодзи: ровно **100×100**; ≤**3 c**, до **30 FPS**, ≤**256 KB**, кодек **VP9**, без аудиодорожки; «Requires Telegram **8.5** or higher» ([stickers](https://core.telegram.org/stickers)) |

**Отправка и переиспользование:**

- `sendSticker` принимает `.WEBP`, `.TGS`, `.WEBM`; «Pass a `file_id` … to send a file that exists on the Telegram servers (**recommended**)»; «**Video and animated stickers can't be sent via an HTTP URL**»; `emoji` — только для только что загруженных стикеров ([api#sendsticker](https://core.telegram.org/bots/api#sendsticker)).
- `file_id` привязан к конкретному боту: «the `file_id` field is tied to a single bot id, so your test instance cannot use a shared `file_id` database… files must be individually reuploaded» ([features](https://core.telegram.org/bots/features)).
- Управление паками: `getStickerSet`, `setStickerSetTitle`, `setStickerSetThumbnail`/`setCustomEmojiStickerSetThumbnail`, `deleteStickerSet`, `setStickerEmojiList`, `setStickerKeywords`, `setStickerMaskPosition` ([api](https://core.telegram.org/bots/api), [changelog 6.6](https://core.telegram.org/bots/api-changelog)).
- Кастомные эмодзи можно отправлять и как обычный стикер (`Sticker.type = custom_emoji`, `custom_emoji_id`) — отдельного запрета в доках нет; **«не подтверждено»** как гарантированное поведение.

**Профиль и «анонимность» бота:**

- `setMyProfilePhoto` / `removeMyProfilePhoto` — Bot API **9.4**; `InputProfilePhotoStatic` (.JPG) или `InputProfilePhotoAnimated` (MPEG4 + `main_frame_timestamp` для кадра-превью) ([api#setmyprofilephoto](https://core.telegram.org/bots/api#setmyprofilephoto), [changelog 9.4](https://core.telegram.org/bots/api-changelog)).
- `setMyName` (0–64 символа, Bot API **6.7**), `setMyDescription` (**0–512**, Bot API **6.6**), `setMyShortDescription` (**0–120**, Bot API **6.6**), все с `language_code` для локализации. **Параметра форматирования у них нет** — только plain text (и обычные Unicode-эмодзи) ([api#setmyname](https://core.telegram.org/bots/api#setmyname), [api#setmydescription](https://core.telegram.org/bots/api#setmydescription), [api#setmyshortdescription](https://core.telegram.org/bots/api#setmyshortdescription)).
- Медиа в блоке «What can this bot do?» настраивается через @BotFather — Bot API **6.1** (20.06.2022) ([changelog](https://core.telegram.org/bots/api-changelog)).
- `setChatMenuButton`/`getChatMenuButton` (Bot API **6.0**, 16.04.2022) и `MenuButton`: `MenuButtonCommands`, `MenuButtonDefault` или `MenuButtonWebApp` (`type`, `text`, `web_app`) — кнопка меню может запускать Mini App; `chat_id` необязателен (иначе меняется дефолт) ([api#setchatmenubutton](https://core.telegram.org/bots/api#setchatmenubutton), [api#menubuttonwebapp](https://core.telegram.org/bots/api#menubuttonwebapp)).
- С Bot API **9.3** бот может отключить основной username при наличии активных дополнительных с Fragment ([changelog 9.3](https://core.telegram.org/bots/api-changelog)).

---

## 6. Новое в 2025–2026, что можно эксплуатировать в UI

- **Rich Messages** (10.1, 11.06.2026; 10.2 — блочная модель и загрузка медиа; 10.3 — `InputRichBlockExpandableBlockQuotation`, `RichBlockDocument`, `is_compact` у таблиц) ([changelog](https://core.telegram.org/bots/api-changelog), [blog](https://telegram.org/blog/watch-apps-and-more)).
- **Кнопки внутри сообщения** (10.3 + клиентское обновление 25.08.2026): «Developers can now put **multiple buttons right inside a message**… Such messages can be posted **anywhere**, including groups or channels» ([blog](https://telegram.org/blog/welcome-messages-buttons-TG-13)).
- **Ephemeral messages** (10.2): сообщение в группе, видимое только одному пользователю; параметры `receiver_user_id`/`callback_query_id`, методы `editEphemeralMessage*`, `deleteEphemeralMessage`; окно ответа — 15 секунд ([changelog 10.2](https://core.telegram.org/bots/api-changelog), [api#ephemeral-messages](https://core.telegram.org/bots/api#ephemeral-messages-and-commands)).
- **Чек-листы** (`Checklist`, `InputChecklist`; 9.1, 03.07.2025): `sendChecklist`/`editMessageChecklist` работают **только от имени подключённого бизнес-аккаунта** (`business_connection_id` обязателен). Лимиты: заголовок 1–255, задач 1–30, текст задачи 1–100, в заголовке допустимы `bold/italic/underline/strikethrough/spoiler/custom_emoji/date_time` ([api#sendchecklist](https://core.telegram.org/bots/api#sendchecklist), [api#inputchecklist](https://core.telegram.org/bots/api#inputchecklist)).
- **Опросы с медиа** (10.0, 08.05.2026): `sendPoll` c `media` и `explanation_media`, `InputPollOption.media`, классы `InputMediaSticker`/`InputMediaLocation`/`InputMediaVenue`/`InputMediaLink` — варианты ответа могут быть картинками/стикерами/ссылками; минимум вариантов снижен до 1 ([changelog 10.0](https://core.telegram.org/bots/api-changelog)).
- **Paid media**: `sendPaidMedia` (7.6, 01.07.2024), `InputPaidMedia`/`InputPaidMediaPhoto`/`InputPaidMediaVideo` (+ `InputPaidMediaLivePhoto` в 10.0), цена `star_count` **1–25000**, до **10** элементов, `caption`, `payload` до 128 байт ([api#sendpaidmedia](https://core.telegram.org/bots/api#sendpaidmedia)).
- **Подарки (визуальный брендинг и статусы)**: `sendGift` с текстом 0–128 символов и сущностями (в т.ч. `custom_emoji`), `giftPremiumSubscription` (9.0), `getUserGifts`/`getChatGifts` (9.3), `UniqueGiftColors`/`GiftBackground` (9.3), апгрейд и передача подарков в бизнес-режиме ([api#sendgift](https://core.telegram.org/bots/api#sendgift), [changelog](https://core.telegram.org/bots/api-changelog)).
- **Розыгрыши (giveaway)**: бот только **получает** сообщения `giveaway`, `giveaway_created`, `giveaway_winners`, `giveaway_completed`; метода создания розыгрыша в Bot API нет ([api#giveaway](https://core.telegram.org/bots/api#giveaway)).
- **Бизнес-режим**: `BusinessConnection` (7.2, 31.03.2024) + `BusinessBotRights` (9.0) + методы `setBusinessAccountName`/`setBusinessAccountUsername`/`setBusinessAccountBio`/`setBusinessAccountProfilePhoto` (9.0); с 10.0 бизнес-боты могут управлять аккаунтами **без Telegram Premium** ([api#businessconnection](https://core.telegram.org/bots/api#businessconnection), [changelog](https://core.telegram.org/bots/api-changelog)).
- **Suggested posts** (9.2) и **direct messages topics** (9.2): `SuggestedPostParameters`, `DirectMessagesTopic`, параметр `direct_messages_topic_id` в send*-методах ([changelog 9.2](https://core.telegram.org/bots/api-changelog)).
- **`savePreparedInlineMessage`** (8.0, 17.11.2024): Mini App сохраняет готовое сообщение, которое пользователь потом отправляет через `shareMessage`; флаги `allow_user_chats`/`allow_bot_chats`/`allow_group_chats`/`allow_channel_chats` ([api#savepreparedinlinemessage](https://core.telegram.org/bots/api#savepreparedinlinemessage)). В 9.6 добавлен `savePreparedKeyboardButton` ([changelog 9.6](https://core.telegram.org/bots/api-changelog)).
- **Live photos** (10.0) — фото с коротким видео: `sendLivePhoto`, `InputMediaLivePhoto`, `PaidMediaLivePhoto` ([changelog 10.0](https://core.telegram.org/bots/api-changelog)).
- **Новые типы `MessageEntity`**: `custom_emoji` (6.2), `blockquote` (7.0), `expandable_blockquote` (7.4), `date_time` (9.5) ([api#messageentity](https://core.telegram.org/bots/api#messageentity)).
- **Сообщества (Communities)** и апдейты по подпискам (10.2) — пока сервисные сущности, для вёрстки незначимы ([changelog 10.2](https://core.telegram.org/bots/api-changelog)).

**Mini App как «настоящий дизайн»:**

- Запуск: `InlineKeyboardButton.web_app` (**только private chats**, не поддерживается для business-сообщений), `KeyboardButton.web_app` (**только private chats**), `MenuButtonWebApp`, кнопка в inline-режиме (`InlineQueryResultsButton`), прямая ссылка `https://t.me/botusername/appname?startapp=…` ([api#inlinekeyboardbutton](https://core.telegram.org/bots/api#inlinekeyboardbutton), [api#keyboardbutton](https://core.telegram.org/bots/api#keyboardbutton), [webapps](https://core.telegram.org/bots/webapps)).
- Main Mini App даёт профилю бота «Launch app» и медиа-превью: «you can add a prominent **Launch app** button as well as high-quality **demo videos and screenshots** to the bot's profile», превью можно локализовать ([webapps](https://core.telegram.org/bots/webapps)).
- Дизайн-API: `themeParams` (`bg_color`, `text_color`, `hint_color`, `link_color`, `button_color`, `button_text_color`, `secondary_bg_color`, `header_bg_color`, `bottom_bar_bg_color`, `accent_text_color`, `section_bg_color`, `section_header_text_color`, `subtitle_text_color`, `destructive_text_color`, `section_separator_color`) + CSS-переменные `var(--tg-theme-*)`; `setHeaderColor`, `setBackgroundColor`, `setBottomBarColor` (7.10+); `colorScheme` ([webapps](https://core.telegram.org/bots/webapps)).
- Полноэкранный режим `requestFullscreen`/`exitFullscreen`, `lockOrientation`, `disableVerticalSwipes`, `safeAreaInset`/`contentSafeAreaInset` (8.0), `addToHomeScreen` ([webapps](https://core.telegram.org/bots/webapps)).
- Кнопки Mini App: `BottomButton` (переименован из `MainButton` в 8.0) и `SecondaryButton`; поля `text`, `color`, `textColor`, `hasShineEffect` (7.10+), `position` (7.10+), **`iconCustomEmojiId`** (9.5, 01.03.2026) ([webapps](https://core.telegram.org/bots/webapps), [changelog 9.5](https://core.telegram.org/bots/api-changelog)).
- Ограничения: с 10.2 «Hardened the security of Mini Apps by disallowing the usage of Mini App methods from origins different from the original Mini App domain» (включено для всех с 20.07.2026, отключается в @BotFather) ([changelog 10.2](https://core.telegram.org/bots/api-changelog)).

---

## 7. Практика: анимированный бренд без Premium и без бустов

Что реально доступно боту, у которого **нет** Premium у владельца и **нет** Fragment-username и бустов:

1. **Цветные кнопки — да.** `style: primary|success|danger` на inline- и reply-кнопках не имеет условия по Premium/Fragment (в отличие от `icon_custom_emoji_id`) — Bot API 9.4 ([api#inlinekeyboardbutton](https://core.telegram.org/bots/api#inlinekeyboardbutton), [blog](https://telegram.org/blog/crafting-android-design-and-more)).
2. **Rich-сообщения — да** (10.1+): таблицы, заголовки, блок-квоты, `<details>`, коллажи, слайд-шоу, футноты, LaTeX, кнопки внутри текста. Это самый мощный легальный способ выглядеть «дорого» без Premium ([api#sendrichmessage](https://core.telegram.org/bots/api#sendrichmessage)).
3. **Медиа-сообщения — да:** фото/видео/animation/document с `caption` + `parse_mode` + инлайн-клавиатурой в одном вызове; альбомы 2–10 элементов (без клавиатуры); `show_caption_above_media`, `has_spoiler` (размытие «18+»/интрига), `link_preview_options.show_above_text` ([api#sendphoto](https://core.telegram.org/bots/api#sendphoto), [api#sendmediagroup](https://core.telegram.org/bots/api#sendmediagroup)).
4. **Стикеры как анимированные brand-элементы — да:** создать **regular** пак (`sticker_type=regular`, без Premium/бустов), загрузить TGS/WEBM/PNG по требованиям 512 px/≤3 c/≤64 KB/60 FPS (TGS) или ≤256 KB/30 FPS/VP9 (WEBM) и слать `sendSticker` по `file_id` ([api#sendsticker](https://core.telegram.org/bots/api#sendsticker), [stickers](https://core.telegram.org/stickers)).
5. **HTML/MarkdownV2 — да:** bold/italic/underline/strikethrough/spoiler/`<blockquote>`/`<blockquote expandable>`/`code`/`pre`/`date_time`; **кастомные эмодзи — нет** (нужен Premium владельца или Fragment-username), их место занимают обычные Unicode-эмодзи ([formatting options](https://core.telegram.org/bots/api#formatting-options)).
6. **Профиль бота — да:** статичное или **анимированное** (MPEG4) фото профиля `setMyProfilePhoto`, имя, описание, short description, локализация, медиа в «What can this bot do?» через @BotFather, Main Mini App с demo-видео ([api#setmyprofilephoto](https://core.telegram.org/bots/api#setmyprofilephoto), [webapps](https://core.telegram.org/bots/webapps)).
7. **Mini App — да, «полный дизайн»:** собственные шрифты, анимации, графика; темизация через `themeParams`/`setHeaderColor`/`setBackgroundColor`/`setBottomBarColor`, fullscreen, свои цвета кнопок; запуск из inline-кнопки и из меню (private chats) ([webapps](https://core.telegram.org/bots/webapps)).
8. **Стриминг текста — да:** `sendMessageDraft` (с 9.5 — всем ботам) с плейсхолдером «Thinking…» и `sendRichMessageDraft` + `<tg-thinking>` ([api#sendmessagedraft](https://core.telegram.org/bots/api#sendmessagedraft)).
9. **Эффекты сообщений — условно:** параметр доступен ботам только в private chats, но каталога id в Bot API нет (MTProto-метод только для пользователей) — использовать можно лишь с захардкоженными id, это хрупко ([api#sendmessage](https://core.telegram.org/bots/api#sendmessage), [core.telegram.org/api/effects](https://core.telegram.org/api/effects)).
10. **Опросы-картинки — да** (10.0): варианты ответа как фото/стикер/ссылка — визуальный выбор без Premium ([changelog 10.0](https://core.telegram.org/bots/api-changelog)).

**Фолбэк для клиентов старее поддерживающей версии:**

| Что | Поведение на старом клиенте |
|---|---|
| Новые типы сущностей (прецедент spoiler, 5.6) | «Older clients will display unsupported message» — сообщение показывается как неподдерживаемое ([changelog](https://core.telegram.org/bots/api-changelog)) |
| Кастомные эмодзи | Показывается альтернативный Unicode-эмодзи из тега/`emoji`-поля — в уведомлениях и при форварде non-premium пользователем ([formatting options](https://core.telegram.org/bots/api#formatting-options)) |
| Rich-сообщения | Официально: «a stylish placeholder… appears when outdated apps can't render a new feature — and gives you a button for instantly downloading the latest update» ([blog 25.08.2026](https://telegram.org/blog/welcome-messages-buttons-TG-13)) |
| `style` / `icon_custom_emoji_id` на кнопках | Официально не описано — **«не подтверждено»**; разумно ожидать отрисовку в дефолтном стиле без иконки |
| TGS/WEBM-стикеры | Клиенты без поддержки (WEBM — «Requires Telegram 8.5 or higher») ([stickers](https://core.telegram.org/stickers)) |
| Mini App API | Проверка через `Telegram.WebApp.isVersionAtLeast(version)` и поле `version` ([webapps](https://core.telegram.org/bots/webapps)) |

---

## Ограничения и что НЕ работает

1. **Кастомные эмодзи без Premium/Fragment** — нельзя вообще: бот с обычным владельцем не может использовать `custom_emoji`/`<tg-emoji>`, пока владелец не оплатит Premium либо бот не купит дополнительные username на Fragment ([formatting options](https://core.telegram.org/bots/api#formatting-options)).
2. **Кастомные эмодзи в постах канала** — Premium-путь ограничен «private, group and supergroup chats»; каналы в формулировке отсутствуют ([formatting options](https://core.telegram.org/bots/api#formatting-options)).
3. **Кастомный эмодзи в тексте inline-кнопки** — невозможен: `text` это `String` без `entities`; доступна только иконка `icon_custom_emoji_id` ([api#inlinekeyboardbutton](https://core.telegram.org/bots/api#inlinekeyboardbutton)). Внутри подписи rich-кнопки — можно (`RichTextCustomEmoji`, 10.1+) ([api#richmessagebutton](https://core.telegram.org/bots/api#richmessagebutton)).
4. **Бот не владеет паком:** `createNewStickerSet` требует `user_id` — владелец пак-это пользователь, бот лишь редактор созданного им пака ([api#createnewstickerset](https://core.telegram.org/bots/api#createnewstickerset), [features](https://core.telegram.org/bots/features)).
5. **Редактировать чужие паки нельзя:** «these methods will only work on packs created by the bot that is calling them» ([features](https://core.telegram.org/bots/features)).
6. **Создать чек-лист обычным ботом нельзя** — `sendChecklist`/`editMessageChecklist` только с `business_connection_id` ([api#sendchecklist](https://core.telegram.org/bots/api#sendchecklist)).
7. **Нельзя создать розыгрыш (giveaway)** — в Bot API есть только приём соответствующих сообщений ([api#giveaway](https://core.telegram.org/bots/api#giveaway)).
8. **Нельзя получить список `message_effect_id`** — MTProto-метод `messages.getAvailableEffects` помечен «Only users can use this method» ([core.telegram.org/method/messages.getAvailableEffects](https://core.telegram.org/method/messages.getAvailableEffects)); эффекты доступны только в private chats ([api#sendmessage](https://core.telegram.org/bots/api#sendmessage)).
9. **Нет форматирования в имени/описании бота** — `setMyName`/`setMyDescription`/`setMyShortDescription` без `parse_mode`/`entities` ([api#setmydescription](https://core.telegram.org/bots/api#setmydescription)).
10. **Анимированные и video-стикеры нельзя задать HTTP-ссылкой** — только загрузка или `file_id` ([api#sendsticker](https://core.telegram.org/bots/api#sendsticker), [api#inputsticker](https://core.telegram.org/bots/api#inputsticker)).
11. **Жёсткие лимиты размера/длины:** фото ≤10 MB, прочие файлы ≤50 MB (облачный API), подпись ≤1024, текст ≤4096 ([api#sending-files](https://core.telegram.org/bots/api#sending-files), [api#sendmessage](https://core.telegram.org/bots/api#sendmessage)).
12. **`web_app`-кнопки только в приватных чатах** — и для inline-кнопок, и для reply-кнопок; в business-сообщениях inline `web_app` не поддерживается ([api#inlinekeyboardbutton](https://core.telegram.org/bots/api#inlinekeyboardbutton), [api#keyboardbutton](https://core.telegram.org/bots/api#keyboardbutton)).
13. **`reply_markup` нельзя прикрепить к альбому** — у `sendMediaGroup` такого параметра нет ([api#sendmediagroup](https://core.telegram.org/bots/api#sendmediagroup)).
14. **`blockquote`/`expandable_blockquote` не вкладываются** друг в друга ([formatting options](https://core.telegram.org/bots/api#formatting-options)).
15. **Bot API не даёт менять тему/шрифт сообщений** — оформление ограничено перечисленными сущностями, медиа, клавиатурами и rich-блоками; произвольный CSS возможен только в Mini App ([webapps](https://core.telegram.org/bots/webapps)).

---

## Версии и даты

| Возможность | Bot API | Дата | Поддержка клиентами |
|---|---|---|---|
| Media в «What can this bot do?» (@BotFather) | 6.1 | 20.06.2022 | не документировано |
| `WebAppInfo`, `KeyboardButton.web_app`, `InlineKeyboardButton.web_app`, `MenuButton`, `setChatMenuButton` | 6.0 | 16.04.2022 | не документировано |
| `custom_emoji` entity, `custom_emoji_id`, `getCustomEmojiStickers`, `sticker_type` | 6.2 | 12.08.2022 | Telegram 8.9+ (авг. 2022) для custom emoji ([stickers](https://core.telegram.org/stickers)) |
| Кастомные эмодзи через HTML/MarkdownV2 (Fragment-username) | 6.7 | 21.04.2023 | не документировано |
| Создание `custom_emoji`-паков, `needs_repainting`, thumbnail пака | 6.6 | 09.03.2023 | не документировано |
| `setMyName` | 6.7 | 21.04.2023 | не документировано |
| `setMyDescription`, `setMyShortDescription` | 6.6 | 09.03.2023 | не документировано |
| Spoiler-сущность | 5.6 | 30.12.2021 | «Telegram versions released after December 30, 2021»; старые — «unsupported message» |
| `blockquote` | 7.0 | 29.12.2023 | не документировано |
| `link_preview_options` (`show_above_text`, `prefer_large_media`), `ReplyParameters` | 7.0 | 29.12.2023 | не документировано |
| `BusinessConnection`, `getBusinessConnection` | 7.2 | 31.03.2024 | не документировано |
| `message_effect_id` / `Message.effect_id`, `show_caption_above_media`, `expandable_blockquote` | 7.4 | 28.05.2024 | не документировано |
| `sendPaidMedia`, `InputPaidMedia` | 7.6 | 01.07.2024 | не документировано |
| `setBottomBarColor`, `hasShineEffect`, `position` (Mini App) | 7.10 | 06.09.2024 | не документировано |
| `copy_text` на inline-кнопке | 7.11 | 31.10.2024 | не документировано |
| `savePreparedInlineMessage`, `requestFullscreen`, `safeAreaInset` | 8.0 | 17.11.2024 | не документировано |
| `giftPremiumSubscription`, права бизнес-бота, `postStory` | 9.0 | 11.04.2025 | не документировано |
| Чек-листы (`sendChecklist`, `InputChecklist`) | 9.1 | 03.07.2025 | не документировано |
| `direct_messages_topic`, suggested posts, `reply_to_checklist_task_id` | 9.2 | 15.08.2025 | не документировано |
| `sendMessageDraft` (стриминг), `message_effect_id` в forward/copy | 9.3 | 31.12.2025 | не документировано |
| **`style` и `icon_custom_emoji_id` на кнопках**, кастомные эмодзи в сообщениях при Premium владельца, `setMyProfilePhoto` | 9.4 | 09.02.2026 | клиентское обновление 09.02.2026 ([blog](https://telegram.org/blog/crafting-android-design-and-more)); номер версии не указан |
| `date_time` entity, `sendMessageDraft` для всех ботов, `iconCustomEmojiId` в BottomButton | 9.5 | 01.03.2026 | не документировано |
| Managed bots, `savePreparedKeyboardButton`, мульти-ответные квизы | 9.6 | 03.04.2026 | не документировано |
| Гостевая модель, live photos, медиа в опросах, `InputPaidMediaLivePhoto` | 10.0 | 08.05.2026 | не документировано |
| **Rich Messages** (`sendRichMessage`, блоки, `<tg-button>`) | 10.1 | 11.06.2026 | клиентское обновление 11.06.2026 ([blog](https://telegram.org/blog/watch-apps-and-more)); лимит «32 768 символов» заявлен только в блоге |
| Ephemeral messages, Communities, `InputRichMessageMedia`, security Mini Apps | 10.2 | 14.07.2026 | не документировано |
| Кнопки внутри сообщений, `DisabledButton`/`disabled`, `force_reply` в markup, `RichMessageButton` | 10.3 | 24.08.2026 | клиентское обновление 25.08.2026 ([blog](https://telegram.org/blog/welcome-messages-buttons-TG-13)) |
| WEBM-стикеры | — | — | «Requires Telegram 8.5 or higher» ([stickers](https://core.telegram.org/stickers)) |
| Ассоциация custom-emoji-пака с супергруппой | — | — | уровень бустов из серверного конфига `group_emoji_stickers_level_min` ([api/boost](https://core.telegram.org/api/boost)); точное число — не подтверждено |
