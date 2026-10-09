# Инвентарь экранов бота Kometa (по коду)

**Дата замера:** 08.10.2026, 18:14–18:20 · **коммит:** `981a1cf` (рабочее дерево грязное — файлы бота изменены, см. `HANDOFF.md`)
**Источники фактов:** [`app/bot/keyboards.py`](../../app/bot/keyboards.py), [`app/bot/texts.py`](../../app/bot/texts.py), `app/bot/handlers/*.py`, [`app/bot/gate.py`](../../app/bot/gate.py), [`app/bot/middlewares.py`](../../app/bot/middlewares.py), плюс тексты, которые хендлеры берут из сервисов (`app/services/gift.py`, `app/services/stats.py`, `app/services/digest.py`).

> **Замер привязан к снимку (важно).** Во время работы над инвентарём другой участник правил код бота: `app/bot/keyboards.py` изменился в 18:14 (появился параметр `namespace` у `admin_order_kb`), добавился [`app/bot/handlers/notify_admin.py`](../../app/bot/handlers/notify_admin.py) — бот уведомлений. Все цифры ниже — по снимку с хэшами:
>
> | Файл | SHA-256 (первые 16) |
> |---|---|
> | `app/bot/texts.py` | `bdc13e0a750382e7` |
> | `app/bot/keyboards.py` | `e7a2c83adf60696d` |
> | `app/bot/gate.py` | `4ec44a90b822e923` |
> | `app/bot/middlewares.py` | `317e54e0b913728e` |
> | `app/bot/handlers/notify_admin.py` | `6bae908cb89ff947` |
> | `app/bot/handlers/buy.py` | `b6aaa2695f73b9ed` (изменился в 18:15 — уже после основного снимка; отсюда минус один `💰`) |
>
> Проверить актуальность: `shasum -a 256 app/bot/texts.py app/bot/keyboards.py`. Если хэши другие — цифры надо пересчитать скриптом (раздел 8), а расхождения — свести в этот файл.
> **Граница подсчёта эмодзи:** только `app/bot/**/*.py`. Эмодзи в `app/services/digest.py` (`📈📊🔴🟡⚪️💰⌛️⏳`, 15 длинных строк) в перепись не входят, хотя их видит человек в боте уведомлений.
**Все цифры воспроизводимы** скриптом из раздела 8 (запускается на `.venv/bin/python`, ничего не пишет в репозиторий).
**Значения подписей кнопок посчитаны на живом коде** с реальными тарифами из `data/kometa.db` (1 месяц — 120 ₽ / 110 ⭐, 3 месяца — 299 ₽ / 270 ⭐, 6 месяцев — 539 ₽ / 485 ⭐, 12 месяцев — 959 ₽ / 860 ⭐).

> Локальный `.env` этой копии: `SALES_ENABLED=false` (заглушка на Mac), `TRIAL_ENABLED=true`, `GIFT_ENABLED=true`, `STARS_ENABLED=true`, скидка рефералки 30 % (макс. 240 ₽). На проде `SALES_ENABLED=true`. Поэтому в таблицах у части экранов два варианта — «оплата подключена» и «скоро».

## Сводка

| Показатель | Значение |
|---|---|
| Экранов/сообщений всего в инвентаре | **120**: 76 клиентских + 33 админских + 11 в боте уведомлений |
| Хендлеров-роутов (декораторов) в `app/bot/handlers/*.py` | **88** (80 было до появления `notify_admin.py`) |
| Функций клавиатур в `keyboards.py` | 26 (+ `gate.markup`) |
| Кнопок в самой большой клавиатуре | **8** (главное меню клиента с активной подпиской и подарком) |
| Рядов в самой высокой клавиатуре | **8** (`providers_kb` / `gift_pay_kb`) |
| Самая длинная подпись кнопки | **33** символа (`plans_kb` со скидкой) |
| Кнопок длиннее 24 символов | **15** подписей |
| Эмодзи в пользовательских строках | **226 вхождений, 59 разных** |
| Эмодзи в комментариях/докстрингах | 0 (все — в видимых строках) |
| Максимум эмодзи в одном сообщении | **6** (`REFERRAL`) |
| Непустых строк в `texts.py` | 277, из них **133 (48 %) длиннее 38 символов**, 29 длиннее 100 |
| Самая длинная строка | **189** символов (`PAYMENT_SOON`, строка 3) |
| Использований `style=` / `disabled=` / `icon_custom_emoji_id` | **0** (проверено `grep -rn` по `app/bot/`) |
| Мёртвых текстовых констант | 7 |
| Экранов без кнопки возврата | клиентских: 8 из 76; админских: 25 из 33; в боте уведомлений: 7 из 11 |

---

## 1. Пользовательские экраны

Формат: **Экран** → обработчик (файл:строка) → текст → клавиатура → `callback_data` → кнопок / самая длинная подпись.

### 1.1 Вход, меню, гейт канала

| № | Экран/сообщение | Обработчик | Текст (константа) | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| 1 | Приветствие + меню (нет подписки) | `start.py:82` `cmd_start` → `main_menu_view` (`start.py:29`) | `WELCOME` + `MENU_NO_SUB` (+`MENU_DISCOUNT_HINT`) | `main_menu(False, False, min_price)` `keyboards.py:25` | `trial:start`, `plans`, `sub:howto`, `sub:reserve`, `ref:show`, `legal:show`, `help` | 7 / 23 |
| 2 | Меню (подписка активна) | `start.py:146` `cb_main_menu` | `MENU_ACTIVE` | `main_menu(True, True)` | `sub:show`, `plans`, `gift:show`, `sub:howto`, `sub:reserve`, `ref:show`, `legal:show`, `help` | **8 / 21** |
| 3 | Меню (подписка истекла) | `start.py:146` | `MENU_EXPIRED` | `main_menu(True, False)` | `plans`, `sub:howto`, `sub:reserve`, `ref:show`, `legal:show`, `help` | 6 / 21 |
| 4 | Подсказка постоянного меню | `start.py:142`, `channel.py:45` | `QUICK_MENU_HINT` | `reply_menu()` `keyboards.py:54` | reply-кнопки (не callback) | 6 / 21 |
| 5 | Меню по `/menu` | `start.py:153` `cmd_menu` | как № 1–3 | как № 1–3 | — | 6–8 / 23 |
| 6 | Гейт: «Остался один шаг» | `gate.py:22` `text()` ← `middlewares.py:166` `_show_gate`, `start.py:137` | `CHANNEL_GATE_TITLE` + `CHANNEL_GATE_BODY` | `gate.markup()` `gate.py:35` | url канала, `channel:check`, url поддержки | 2–3 / 22 |
| 7 | Гейт: «подписки пока не видно» | `channel.py:48` | `CHANNEL_GATE_NOT_YET` (alert) + перерисовка № 6 | `gate.markup()` | `channel:check` | 2–3 / 22 |
| 8 | Гейт: «не удалось проверить» | `channel.py:59` | `CHANNEL_GATE_UNAVAILABLE` (alert) | `gate.markup()` | `channel:check` | 2–3 / 22 |
| 9 | Гейт: «подписка подтверждена» | `channel.py:41` | `CHANNEL_GATE_OK` (alert) → `open_menu` | `main_menu` | как № 1–3 | 6–8 / 23 |
| 10 | Тост при нажатии до подписки | `middlewares.py:174` | `CHANNEL_GATE_SHORT` (alert) | — | — | 0 |

### 1.2 Пробный доступ

| № | Экран/сообщение | Обработчик | Текст | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| 11 | Пробный доступ выдан | `trial.py:65-76` | `TRIAL_STARTED` (+`TRIAL_BONUS_LINE`) | `subscription_kb` `keyboards.py:206` | `sub:link`, `plans`, `sub:refresh`, `menu:main` | 4 / 17 |
| 12 | Пробный уже использован | `trial.py:52` | `TRIAL_ALREADY_USED` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 13 | Пробный закрыт | `trial.py:30` | `TRIAL_CLOSED` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 14 | Тост «сервис недоступен» | `trial.py:48` | inline-строка alert | — | — | 0 |

### 1.3 Тарифы и оплата

| № | Экран/сообщение | Обработчик | Текст | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| 15 | Тарифы (оплата подключена) | `buy.py:102` `cb_plans`, `buy.py:107` `msg_plans` | `PLANS_HEADER` + `REFERRAL_TEASER` | `plans_kb(show_stars=True, show_promo_button=True)` `keyboards.py:67` | `plan:1..4`, `promo:enter`, `menu:main` | 6 / 28 |
| 16 | Тарифы со скидкой | `buy.py:63` | `PLANS_HEADER_DISCOUNT` | `plans_kb(discount_percent=30, max=240)` | `plan:1..4`, `menu:main` | 5 / **33** |
| 17 | Тарифы (продажи закрыты) | `buy.py:54` | `PLANS_HEADER_SOON` | `plans_kb` без звёзд | `plan:1..4`, `menu:main` | 5 / 29 |
| 18 | Карточка тарифа | `buy.py:113` `cb_plan_card` → `_show_plan_card:140` | `PLAN_CARD` + `PRICE_LINE`/`PRICE_LINE_DISCOUNT` (+`STARS_LINE`) | `providers_kb` `keyboards.py:105` | `pay:{plan}:{code}`, `promo:enter`, `plans` | до 8 / 27 |
| 19 | Карточка тарифа «оплата скоро» | `buy.py:184` | `PLAN_CARD` + `PLAN_CARD_SOON_NOTE` | `providers_kb(sbp_soon=True)` | `sbp:soon:{plan}`, `promo:enter`, `plans` | 3 / 22 |
| 20 | «Оплата по СБП — скоро» | `buy.py:83` `show_payment_soon`, `buy.py:192` `cb_sbp_soon` | `PAYMENT_SOON` | `docs_back_kb` | `plans`, `legal:show`, `menu:main` | 3 / 18 |
| 21 | Счёт: перевод вручную | `buy.py:261` | `DISCOUNT_NOTE` + `ORDER_CREATED_MANUAL` | `manual_order_kb` `keyboards.py:147` | `order:manual:{id}`, `order:cancel:{id}` | 2 / 25 |
| 22 | Счёт: крипта | `buy.py:269` | `ORDER_CREATED_CRYPTO` | `crypto_order_kb` `keyboards.py:155` | url оплаты, `order:check:{id}`, `order:cancel:{id}` | 3 / 18 |
| 23 | Счёт: WATA | `buy.py:272` | `ORDER_CREATED_WATA` | `crypto_order_kb` | url, `order:check`, `order:cancel` | 3 / 18 |
| 24 | Счёт: Platega | `buy.py:275` | `ORDER_CREATED_PLATEGA` | `crypto_order_kb` | url, `order:check`, `order:cancel` | 3 / 18 |
| 25 | Счёт: Stars | `buy.py:282` | `ORDER_CREATED_STARS` (+`STARS_NO_BALANCE_HINT`) | `stars_order_kb` `keyboards.py:164` | url счёта, url перекупа, `order:cancel` | 3 / 20 |
| 26 | «Оплатил, но доступа нет» | `buy.py:320` `cb_manual_paid` | `ORDER_WAITING_CONFIRM` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 27 | Заказ отменён | `buy.py:305` `cb_cancel` | `ORDER_CANCELED` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 28 | Оплата получена | `buy.py:376-425` `finalize_order`, также `admin.py:136` | `ORDER_PAID` (+`ORDER_PAID_REFERRAL_BONUS`) | `connect_kb` `keyboards.py:175` | url Happ/v2rayNG/Hiddify, `sub:copy`, `menu:main` | 5 / 22 |
| 29 | Тост «Заказ уже оплачен» | `buy.py:349` | inline alert | — | — | 0 |
| 30 | Тост «Платёж пока не виден» | `buy.py:369` | `PAYMENT_NOT_FOUND` (alert) | — | — | 0 |
| 31 | Ошибка создания счёта | `buy.py:244` | `ERROR_GENERIC` + alert | `back_to_menu_kb` | `menu:main` | 1 / 9 |

### 1.4 Подписка и подключение

| № | Экран/сообщение | Обработчик | Текст | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| 32 | Подписка активна | `subscription.py:63` | `MY_SUB_ACTIVE` + `STATUS_*` | `subscription_kb(True)` | `sub:link`, `plans`, `sub:refresh`, `menu:main` | 4 / 17 |
| 33 | Подписки нет | `subscription.py:56` | `MY_SUB_NONE` | `main_menu(False, False)` | как № 1 | 7 / 23 |
| 34 | Подписка истекла | `subscription.py:59` | `MY_SUB_EXPIRED` | `subscription_kb` | `sub:link`, `plans`, `sub:refresh`, `menu:main` | 4 / 17 |
| 35 | Ссылка-подписка (инструкция) | `subscription.py:85` `cb_link` | `SUBSCRIPTION_LINK_HINT` | `connect_kb` | url ×3, `sub:copy`, `menu:main` | 5 / 22 |
| 36 | Ссылка для копирования | `subscription.py:101` `cb_copy_link` | `SUBSCRIPTION_COPY` | `connect_kb` | url ×3, `sub:copy`, `menu:main` | 5 / 22 |
| 37 | Тосты «Сначала получи доступ» / «Данные обновлены» / «Панель недоступна» | `subscription.py:82,106,113,122,129` | inline-строки | — | — | 0 |
| 38 | Как подключить | `subscription.py:134` | `HOWTO` | `support_kb` `keyboards.py:222` | url поддержки, url канала, `legal:show`, `menu:main` | 4 / 22 |
| 39 | «Если не открывается» (резерв) | `subscription.py:144` | `RESERVE_HELP` | `support_kb` | те же | 4 / 22 |
| 40 | Локации | `subscription.py:158` `cb_locations` | `LOCATIONS` + строки инбаундов | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 41 | Тосты: «Локации появятся после настройки нод», «Панель недоступна» | `subscription.py:122,168` | inline-строки | — | — | 0 |

### 1.5 Помощь и документы

| № | Экран/сообщение | Обработчик | Текст | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| 42 | Помощь | `referral.py:176` | `HELP` (+`SERVICE_STATUS_HINT`) | `support_kb` | url ×2, `legal:show`, `menu:main` | 4 / 22 |
| 43 | Документы и цены | `legal.py:148` `cb_show` | `LEGAL_HEADER` | `legal_kb` `keyboards.py:234` | `legal:pricing`, `legal:privacy`/url, `legal:terms`/url, `legal:support`, `menu:main` | 5 / **29** |
| 44 | Цены и тарифы | `legal.py:152` `cb_pricing` | `PRICING` + `PRICING_ITEM` | `docs_back_kb` `keyboards.py:263` | `plans`, `legal:show`, `menu:main` | 3 / 18 |
| 45 | Политика конфиденциальности | `legal.py:163` | `legal_texts.build_privacy` (+ `split_message`, лимит 3800) | `legal_kb` | как № 43 | 5 / 29 |
| 46 | Пользовательское соглашение | `legal.py:169` | `legal_texts.build_terms` | `legal_kb` | как № 43 | 5 / 29 |
| 47 | Поддержка | `legal.py:175` `cb_support` | `SUPPORT` / `SUPPORT_NO_CONTACT` | `support_kb` | url ×2, `legal:show`, `menu:main` | 4 / 22 |
| 48 | Поддержка по `/support` | `start.py:164` | `HELP` | `support_kb` | те же | 4 / 22 |

### 1.6 Рефералка и промокоды

| № | Экран/сообщение | Обработчик | Текст | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| 49 | Пригласить друга | `referral.py:41-67` | `REFERRAL` (+`REFERRAL_BALANCE_LINE`) | `referral_kb` `keyboards.py:126` | url «Поделиться», `ref:code`, `ref:friends`, `menu:main` | 4 / 20 |
| 50 | Мой промокод | `referral.py:70` | `PROMO_MY` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 51 | Мои друзья (пусто) | `referral.py:89-93` | `REFERRAL_FRIENDS_EMPTY` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 52 | Мои друзья (список) | `referral.py:95-106` | `REFERRAL_FRIENDS` + `REFERRAL_FRIEND_ITEM` + `REFERRAL_STATUS_*` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 53 | Ввод промокода | `referral.py:112` `ask_promo` | `PROMO_ENTER` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 54 | Промокод принят | `referral.py:162-170` | `PROMO_OK` + `PROMO_EXAMPLE`, затем экран тарифов | `back_to_menu_kb` → `plans_kb` | `menu:main` → `plan:*` | 1 / 9, затем 6 / 28 |
| 55 | Промокод не принят (7 причин) | `referral.py:147` | `PROMO_FAIL[not_found/inactive/expired/uses_over/already_used/not_first/self]` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 56 | Приветствие приглашённого | `start.py:68` `send_referral_greeting` | `REFERRAL_GREETING` + `REFERRAL_GREETING_EXAMPLE` | `plans_button_kb` `keyboards.py:96` | `plans`, `menu:main` | 2 / 26 |
| 57 | Персональная ссылка | `start.py:103` | `PERSONAL_LINK_GREETING` | `plans_button_kb` | `plans`, `menu:main` | 2 / 26 |

### 1.7 Подарки

| № | Экран/сообщение | Обработчик | Текст | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| 58 | Витрина подарков | `gift.py:50` `show_gifts` | f-строка в коде (не константа) | `gifts_kb` `keyboards.py:293` | `gift:plan:{code}`, `menu:main` | 5 / 19 при 4 тарифах |
| 59 | Подарок: «кому дарим» | `gift.py:85` | f-строка | `gift_recipient_kb` `keyboards.py:306` | `gift:skip_name`, `gift:show` | 2 / **31** |
| 60 | Подарок: текст открытки | `gift.py:112` | f-строка | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 61 | Подарок: способы оплаты | `gift.py:175` | f-строка | `gift_pay_kb` `keyboards.py:315` | `gift:pay:{order}:{code}`, `menu:main` | 7 / 27 |
| 62 | Подарок: счёт | `gift.py:300-322` | `ORDER_CREATED_MANUAL` / f-строка / `ORDER_CREATED_STARS` | `manual_order_kb` / `crypto_order_kb` / `stars_order_kb` | как № 21–25 | 3 / 25 |
| 63 | Подарок активирован (получатель) | `gift.py:242` → `app/services/gift.py:313` | `activation_text` (2 варианта: продлено / в запас) | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 64 | Подарок уже активирован | `gift.py:242` (та же функция, ветка `already_activated`) | inline-строка в `gift.py:316` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 65 | Подарок не найден | `gift.py:228` | inline-строка | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 66 | Ошибка активации подарка | `gift.py:239` | `GiftError.message` (`app/services/gift.py:43`) | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 67 | Благодарность покупателю | `gift.py:251` → `gift.py:333` | `buyer_thanks_text` | — | — | 0 |
| 68 | Подарок оплачен (админу) | `buy.py:395` | inline | — | — | 0 |

### 1.8 Системные сообщения клиенту

| № | Экран/сообщение | Обработчик | Текст | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| 69 | `/id` | `start.py:159` | inline `<code>` | — | — | 0 |
| 70 | Общая ошибка | `buy.py:244`, `trial.py:47`, `gift.py:291` | `ERROR_GENERIC` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| 71 | Антифлуд | `middlewares.py:131,133` | `RATE_LIMITED` | — | — | 0 |
| 72 | Фолбэк «не понял» | `fallback.py:21` | inline-строка | `support_kb` | url ×2, `legal:show`, `menu:main` | 4 / 22 |
| 73 | Фолбэк «только текст и кнопки» | `fallback.py:34` | inline-строка | `support_kb` | те же | 4 / 22 |
| 74 | Компенсация простоя | `admin.py:262` | `DOWNTIME_COMPENSATION` | — | — | 0 |
| 75 | Начисление дней клиенту | `admin.py:246` | inline-строка | — | — | 0 |
| 76 | Заказ отклонён клиенту | `admin.py:168` | inline-строка | — | — | 0 |

**Пользовательских экранов: 76.**

---

## 2. Админские экраны

| № | Экран/сообщение | Обработчик | Текст | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| A1 | `/admin` (панель) | `admin.py:53` | `stats.as_text()` (`app/services/stats.py:39`) + адрес веб-панели | `admin_panel_kb` `keyboards.py:281` | `admin:orders`, `admin:stats`, `admin:nodes`, url панели | 4 / 12 |
| A2 | `/stats` | `admin.py:70` | `stats.as_text()` | — | — | 0 |
| A3 | `admin:stats` | `admin.py:75` | `stats.as_text()` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| A4 | Заявки: список | `admin.py:82` (`admin:orders`, `/orders`) | `ADMIN_NEW_ORDER` ×N | `admin_order_kb` | `admin:confirm:{id}`, `admin:reject:{id}` | 2 / 13 |
| A5 | Заявки: пусто | `admin.py:86` | «Ожидающих заявок нет ✅» | — | — | 0 |
| A6 | «Подтвердить?» админу | `buy.py:337` | inline-строка | `admin_order_kb` | `admin:confirm`, `admin:reject` | 2 / 13 |
| A7 | Заказ подтверждён | `admin.py:120` | `ADMIN_CONFIRMED` | — | — | 0 |
| A8 | Заказ отклонён | `admin.py:161` | `ADMIN_REJECTED` | — | — | 0 |
| A9 | `/user` | `admin.py:175` | inline-отчёт | — | — | 0 |
| A10 | `/grant` | `admin.py:214-246` | inline-строки (4 варианта) | — | — | 0 |
| A11 | `/downtime` | `admin.py:295` | inline-отчёт (история) | — | — | 0 |
| A12 | `/downtime_start` | `admin.py:302-316` | inline-строки | — | — | 0 |
| A13 | `/downtime_end` | `admin.py:319-331` | `GrantResult.as_text()` + «Сообщение доставлено: N» | — | — | 0 |
| A14 | `/downtime_grant` | `admin.py:334-350` | то же | — | — | 0 |
| A15 | `/emergency` | `admin.py:354` | `emergency_check.run().as_text()` | — | — | 0 |
| A16 | `/referrals` | `admin.py:367-383` | inline-отчёт + топ | — | — | 0 |
| A17 | `/promos` | `admin.py:387-402` | inline-список; пусто — `admin.py:390` | — | — | 0 |
| A18 | `/promo` (создание) | `admin.py:406-436` | inline-строки | — | — | 0 |
| A19 | `/promo_off` | `admin.py:439-451` | inline-строки | — | — | 0 |
| A20 | `/block`, `/unblock` | `admin.py:454-470` | inline-строка | — | — | 0 |
| A21 | `/watch` | `admin.py:474` | `watchdog.format_report` | — | — | 0 |
| A22 | `/sync` | `admin.py:484-499` | inline-отчёт | — | — | 0 |
| A23 | `/nodes` и `admin:nodes` | `admin.py:502-524` | inline-отчёт; при callback — `back_to_menu_kb` | `back_to_menu_kb` | `menu:main` | 1 / 9 |
| A24 | `/broadcast` (запрос текста) | `admin.py:528` | inline-строка | — | — | 0 |
| A25 | `/cancel` | `admin.py:534` | «Отменено» | — | — | 0 |
| A26 | Рассылка: старт/итог | `admin.py:544,551` | `ADMIN_BROADCAST_START` / `ADMIN_BROADCAST_DONE` | — | — | 0 |
| A27 | `/help_admin` | `admin.py:555` | inline-список команд | — | — | 0 |
| A28 | Админ-фолбэк | `admin.py:572-582` | меню или «Не понял сообщение» | `main_menu` (для команд) | как № 1 | 0–8 / 23 |
| A29 | «Панель не выдала доступ» | `buy.py:384` | inline-алерт | — | — | 0 |
| A30 | «Оплата по отменённому заказу» | `buy.py:407` | inline-алерт | — | — | 0 |
| A31 | «Крупная оплата звёздами» | `buy.py:499` | inline-алерт | — | — | 0 |
| A32 | «Подарок оплачен» | `buy.py:395-401` | inline-алерт | — | — | 0 |
| A33 | Заявка на оплату (создание) | `buy.py:324-334` | `ADMIN_NEW_ORDER` | `admin_order_kb` | `admin:confirm`, `admin:reject` | 2 / 13 |

**Админских экранов: 33.**

### 2.1 Бот уведомлений (staff-facing, появился 08.10.2026)

Отдельный бот: [`app/bot/handlers/notify_admin.py`](../../app/bot/handlers/notify_admin.py), роутер подключается из [`app/services/notify_bot.py:145`](../../app/services/notify_bot.py). Тексты сводок живут в [`app/services/digest.py`](../../app/services/digest.py) — это **второй интерфейс**, и на него правила `DESIGN-SYSTEM.md` распространяются так же, как на клиентский.

| № | Экран/сообщение | Обработчик | Текст | Клавиатура | callback_data | Кнопок / макс. |
|---|---|---|---|---|---|---|
| N1 | Визитка бота уведомлений (`/start`, `/help`) | `notify_admin.py:57` | inline + `chat_id` для `NOTIFY_CHAT_IDS` | — | — | 0 |
| N2 | `/status` | `notify_admin.py:83` | `stats.as_text()` + `🌐` админ-панель | — | — | 0 |
| N3 | `/money` | `notify_admin.py:94` | `digest.build_money` (`digest.py:202`) | — | — | 0 |
| N4 | `/nodes` | `notify_admin.py:99` | `digest.build_infra` (`digest.py:240`) | — | — | 0 |
| N5 | `/digest` | `notify_admin.py:104` | `digest.build_daily` (`digest.py:263`) | — | — | 0 |
| N6 | Заявка: «Подтвердить» | `notify_admin.py:118` `cb_confirm_order` | inline-итог | клавиатура **снимается** (`notify_admin.py:243`) | `adm:confirm:{id}` | 0 (было 2 / 13) |
| N7 | Заявка: «Отклонить» | `notify_admin.py:201` `cb_reject_order` | inline-итог | клавиатура снимается | `adm:reject:{id}` | 0 (было 2 / 13) |
| N8 | Сводка «деньги» | `digest.py:202` (`build_money`) | inline | — | — | 0 |
| N9 | Сводка «люди» | `digest.py:218` (`build_people`) | inline | — | — | 0 |
| N10 | Сводка «инфраструктура» | `digest.py:240` (`build_infra`) | inline, статусы `🔴🟡⚪️` | — | — | 0 |
| N11 | Сводка за сутки / подсказка | `digest.py:263,282` (`build_daily`, `build_hint`) | inline (`📊`) | — | — | 0 |

Кнопки заявок в чате уведомлений собирает `keyboards.admin_order_kb(order_id, namespace=notify_bot.namespace())` (`buy.py:337`) — то есть `adm:confirm:` / `adm:reject:`; после решения клавиатура снимается, двойное нажатие невозможно. Это единственное место в боте, где состояние кнопки меняется после действия (образец для состояний из `DESIGN-SYSTEM.md`, раздел 6).

**Экранов в боте уведомлений: 11.**

**Всего в инвентаре: 76 + 33 + 11 = 120 экранов/сообщений.**

---

## 3. Все эмодзи бота: перепись и решение

Считаны только строковые литералы (то, что видит человек). В комментариях и докстрингах эмодзи нет (0).
Вердикты: **оставить** (слот сохраняется, ставим иконку из `assets/icons/`) · **слить** (дубль смысла → одна иконка) · **убрать** (декор/клише/вне палитры).

| Эмодзи | Код | Вхождений | Где (файлы) | Ключевые константы | Вердикт |
|---|---|---|---|---|---|
| `✅` | U+2705 | 23 | admin, buy, notify_admin, subscription, keyboards, texts | `STATUS_ACTIVE`, `ORDER_PAID`, `CHANNEL_GATE_BTN_CHECK` | оставить (статус «ок») |
| `⬅️` | U+2B05+FE0F | 16 | keyboards | все «назад/в меню» | оставить как единый back (иконка) |
| `🎁` | U+1F381 | 14 | buy, gift, keyboards, texts | `BTN_TRIAL`, `BTN_GIFT`, `STATUS_TRIAL` | оставить; **развести триал и подарок** (решение владельца P2) |
| `💎` | U+1F48E | 11 | keyboards, texts | `BTN_PLANS`, `PLANS_HEADER*`, `LEGAL_PRICING_BTN` | оставить → иконка `tariffs` |
| `⚠️` | U+26A0+FE0F | 15 | admin, buy, notify_admin, texts | `NODE_DOWN`, алерты | оставить (внимание) |
| `💳` | U+1F4B3 | 10 | buy, texts | `PROVIDER_WATA`, `ORDER_CREATED_*` | оставить → единая иконка `pay` |
| `🎟` | U+1F39F | 9 | admin, keyboards, texts | `PROMO_ENTER`, `PROMO_MY` | оставить (промокод) |
| `🎉` | U+1F389 | 9 | trial, keyboards, texts | `DISCOUNT_NOTE`, `MENU_DISCOUNT_HINT`, `PROMO_OK` | **убрать** (декор; смысл несёт текст «скидка») |
| `👇` | U+1F447 | 8 | keyboards, texts | `MENU_*`, `MY_SUB_ACTIVE` | **убрать** (указатель-клише) |
| `⭐` | U+2B50 | 7 | buy, keyboards, texts | `STARS_LINE`, `STARS_NO_BALANCE_HINT` | **слить** с `⭐️` (один эмодзи в двух написаниях) |
| `📄` | U+1F4C4 | 6 | keyboards, texts | `BTN_LEGAL`, `LEGAL_HEADER` | оставить → иконка `legal` |
| `🌐` | U+1F310 | 6 | admin, notify_admin, keyboards, texts | `SERVICE_STATUS_HINT` | оставить → иконка `status` (в вебе) |
| `👥` | U+1F465 | 5 | admin, keyboards, texts | `BTN_REFERRAL`, `REFERRAL_FRIENDS` | оставить → иконка `referral` |
| `🛟` | U+1F6DF | 5 | admin, keyboards, texts | `BTN_RESERVE`, `RESERVE_HELP`, `DOWNTIME_COMPENSATION` | оставить (резерв — узнаваемый слот) |
| `☎️` | U+260E+FE0F | 5 | buy, legal, texts | `LEGAL_SUPPORT_BTN`, `SUPPORT` | оставить → иконка `support` |
| `⛔️` | U+26D4+FE0F | 4 | admin, texts | `STATUS_BLOCKED`, `/promo_off` | оставить (запрет) |
| `⭐️` | U+2B50+FE0F | 4 | buy, keyboards, texts | `PROVIDER_STARS`, `ORDER_CREATED_STARS` | оставить как канон для Stars |
| `🟣` | U+1F7E3 | 4 | keyboards, texts | `SBP_SOON_BTN`, `PAYMENT_SOON`, `PLAN_CARD_SOON_NOTE` | **убрать** (цвет вне палитры + цвет как носитель смысла) |
| `🔄` | U+1F504 | 3 | admin, keyboards | «Обновить», «Синхронизация» | оставить (действие) |
| `🪙` | U+1FA99 | 3 | keyboards, texts | `PROVIDER_CRYPTO`, `ORDER_CREATED_CRYPTO` | **слить** в `pay` |
| `🔗` | U+1F517 | 3 | keyboards, texts | `ORDER_PAID`, `SUBSCRIPTION_LINK_HINT` | **убрать** (ссылка и так ссылка) |
| `📣` | U+1F4E3 | 3 | keyboards, texts | `CHANNEL_GATE_BTN_SUBSCRIBE`, `CHANNEL_GATE_TITLE` | оставить → иконка `channel` |
| `💬` | U+1F4AC | 2 | gate, keyboards | «Поддержка», «Написать в поддержку» | **слить** в `support` (`☎️`) |
| `🖥` | U+1F5A5 | 2 | admin, keyboards | «Ноды», «Панели и ноды» | оставить → иконка `nodes` |
| `💙` | U+1F499 | 2 | admin, texts | `DOWNTIME_COMPENSATION` | **убрать** (декор) |
| `📡` | U+1F4E1 | 2 | keyboards, texts | `BTN_MY_SUB`, `MY_SUB_ACTIVE` | оставить → иконка `subscription` |
| `❓` | U+2753 | 2 | keyboards, texts | `BTN_HELP`, `HELP` | оставить → иконка `help` |
| `📱` | U+1F4F1 | 2 | keyboards, texts | `BTN_HOWTO`, `HOWTO` | оставить → иконка `howto` |
| `🛒` | U+1F6D2 | 2 | keyboards | «Купить N ⭐» | оставить → иконка `cart` |
| `🚫` | U+1F6AB | 2 | keyboards, texts | «Отклонить», `ADMIN_REJECTED` | **слить** в `⛔️` |
| `📋` | U+1F4CB | 2 | keyboards, texts | `SUBSCRIPTION_COPY` | оставить → иконка `copy` |
| `🧾` | U+1F9FE | 2 | keyboards, texts | `ORDER_CREATED_MANUAL`, «Заявки» | оставить → иконка `receipt` |
| `⏳` | U+23F3 | 2 | texts | `ORDER_WAITING_CONFIRM` | **слить** в `⌛️` (два похожих «часов» на разные смыслы путают) |
| `💡` | U+1F4A1 | 2 | texts | `ORDER_PAID`, `STARS_NO_BALANCE_HINT` | **убрать** → `<blockquote>` |
| `⌛️` | U+231B+FE0F | 2 | texts | `MY_SUB_EXPIRED`, `STATUS_EXPIRED` | оставить (истекла) |
| `👋` | U+1F44B | 2 | texts | `REFERRAL_GREETING`, `PERSONAL_LINK_GREETING` | **убрать** (декор) |
| `👤` | U+1F464 | 1 | admin | `/user` | оставить (админ, вне лимита) |
| `❌` | U+274C | 1 | admin | ошибка панели в `/nodes` | оставить (админ) |
| `💰` | U+1F4B0 | 1 | notify_admin | алерт об оплате | оставить (админ) |
| `🤔` | U+1F914 | 1 | fallback | «Не понял сообщение» | **убрать** |
| `🆘` | U+1F198 | 1 | keyboards | «Оплатил, но доступа нет» | оставить (тревожная кнопка) |
| `📊` | U+1F4CA | 2 | notify_admin, keyboards | «Статистика» | оставить → иконка `stats` |
| `⏭` | U+23ED | 1 | keyboards | «Без имени — пришлю ссылку сам» | **убрать** → слово «Пропустить» |
| `📤` | U+1F4E4 | 1 | keyboards | «Поделиться ссылкой» | оставить → иконка `share` |
| `🟢` | U+1F7E2 | 1 | keyboards | «Подключить в Happ» | **убрать** (цветной кружок вне палитры) |
| `🔵` | U+1F535 | 1 | keyboards | «Подключить в v2rayNG» | **убрать** |
| `🚀` | U+1F680 | 1 | texts | `WELCOME` | **убрать** (клише; место бренда — баннер) |
| `🏦` | U+1F3E6 | 1 | texts | `PROVIDER_MANUAL` | **слить** в `pay` |
| `🚧` | U+1F6A7 | 1 | texts | `SALES_CLOSED` (не используется) | оставить (закрыто/скоро) |
| `🌍` | U+1F30D | 1 | texts | `LOCATIONS` | оставить → иконка `globe` |
| `🔒` | U+1F512 | 1 | texts | `LEGAL_PRIVACY_BTN` | **слить** в `legal` (`📄`) |
| `📜` | U+1F4DC | 1 | texts | `LEGAL_TERMS_BTN` | **слить** в `legal` (`📄`) |
| `1️⃣` | 0031+FE0F+20E3 | 1 | texts | `REFERRAL` | **убрать** → «1.» |
| `2️⃣` | 0032+FE0F+20E3 | 1 | texts | `REFERRAL` | **убрать** → «2.» |
| `3️⃣` | 0033+FE0F+20E3 | 1 | texts | `REFERRAL` | **убрать** → «3.» |
| `🔁` | U+1F501 | 1 | texts | `REFERRAL` | **убрать** |
| `🙂` | U+1F642 | 1 | texts | `REFERRAL_SELF_ERROR` (не используется) | **убрать** |
| `🔔` | U+1F514 | 2 | notify_admin, texts | `ADMIN_NEW_ORDER` | оставить (админ) |
| `🙏` | U+1F64F | 1 | texts | `RATE_LIMITED` | **убрать** |

### Итог по эмодзи

Арифметика сходится: **32 + 8 + 19 = 59** разных знаков, **160 + 19 + 47 = 226** вхождений.

| Действие | Разных | Вхождений | Что входит |
|---|---|---|---|
| **Оставить** | 32 | 160 | 12 смысловых слотов + 15 сервисных глифов + 5 админских/веб |
| **Слить** (дубли смысла) | 8 | 19 | `⭐`→`⭐️`, `🪙`→`💳`, `🏦`→`💳`, `💬`→`☎️`, `🚫`→`⛔️`, `⏳`→`⌛️`, `🔒`→`📄`, `📜`→`📄` |
| **Убрать** (декор, клише, вне палитры) | 19 | 47 | `🎉`×9, `👇`×8, `🌐`×6, `🟣`×4, `🔗`×3, `💙`×2, `💡`×2, `👋`×2, `🤔`, `⏭`, `🟢`, `🔵`, `🚀`, `1️⃣`, `2️⃣`, `3️⃣`, `🔁`, `🙂`, `🙏` |
| **Всего** | **59** | **226** | — |

Целевой набор (32 знака):

* **12 смысловых слотов** — станут иконками из [`assets/icons/`](assets/icons/) (это и есть «свой дизайн вместо дефолтных смайликов»): tariffs `💎`, subscription `📡`, gift/trial `🎁`, pay `💳`, stars `⭐️`, referral `👥`, support `☎️`, legal `📄`, help `❓`, howto `📱`, reserve `🛟`, promo `🎟`;
* **15 сервисных глифов:** `✅` успех, `⬅️` назад, `⚠️` внимание, `⛔️` запрет, `⌛️` истёк, `🚧` скоро/закрыто, `🔄` обновить, `🆘` тревога, `📣` канал, `🖥` ноды, `🛒` купить звёзды, `📋` копировать, `🧾` чек/заказ, `📤` поделиться, `🌍` локации;
* **5 админских/веб:** `👤`, `❌`, `💰`, `🔔`, `📊`.

Чего в переписи нет: эмодзи `📈🔴🟡⚪️` из `app/services/digest.py` (бот уведомлений) — они вне границы подсчёта `app/bot/**`; при сведении дизайна воедино их нужно добавить в целевую таблицу отдельной строкой.

---

## 4. Кнопки длиннее 24 символов (риск обрезки в Telegram)

Все — из живого кода с реальными тарифами. Норма по дизайн-системе — ≤ 30 символов, цель 20–24.

| Подпись сейчас | Символов | Где | Предлагаемое сокращение | Станет |
|---|---|---|---|---|
| `12 месяцев — 719 ₽ вместо 959 ₽ 🎉` | **33** | `keyboards.py:83` (`plans_kb`, со скидкой) | `12 мес — 719 ₽` (+ строка «вместо 959 ₽» в тексте) | 15 |
| `6 месяцев — 378 ₽ вместо 539 ₽ 🎉` | 32 | `keyboards.py:83` | `6 мес — 378 ₽` | 14 |
| `3 месяца — 210 ₽ вместо 299 ₽ 🎉` | 31 | `keyboards.py:83` | `3 мес — 210 ₽` | 14 |
| `⏭ Без имени — пришлю ссылку сам` | **31** | `keyboards.py:309` (`gift_recipient_kb`) | `Пропустить — пришлю сам` | 24 |
| `12 месяцев — 959 ₽ (959 ₽/мес)` | 30* | `keyboards.py:87` (`plans_kb`, продажи закрыты) | `12 мес — 959 ₽` | 15 |
| `1 месяц — 84 ₽ вместо 120 ₽ 🎉` | 29 | `keyboards.py:83` | `1 мес — 84 ₽` | 13 |
| `🔒 Политика конфиденциальности` | 29 | `texts.py:383` (`legal_kb`) | `Политика` (полное имя — в тексте экрана) | 8 |
| `📜 Пользовательское соглашение` | 29 | `texts.py:384` | `Соглашение` | 10 |
| `3 месяца — 299 ₽ (100 ₽/мес)` | 28 | `keyboards.py:87` | `3 мес — 299 ₽` | 14 |
| `12 месяцев — 959 ₽ или 860 ⭐` | 28 | `keyboards.py:85` | `12 мес — 959 ₽ / 860 ⭐` | 23 |
| `🏦 Перевод по СБП / на карту` | 27 | `texts.py:132` (`PROVIDER_MANUAL`) | `Перевод по СБП` | 15 |
| `1 месяц — 120 ₽ (120 ₽/мес)` | 27 | `keyboards.py:87` | `1 месяц — 120 ₽` | 15 |
| `💎 Выбрать тариф со скидкой` | 26 | `keyboards.py:99` (`plans_button_kb`) | `Выбрать тариф` | 13 |
| `🆘 Оплатил, но доступа нет` | 25 | `keyboards.py:149` (`manual_order_kb`) | `Оплатил, доступа нет` | 21 |
| `1 месяц — 120 ₽ или 110 ⭐` | 25 | `keyboards.py:85` | `1 месяц — 120 ₽ / 110 ⭐` | 24 |

\* фактическая длина 29–30 у разных тарифов (измерено: 27, 28, 28, 29).

Пограничные (24 и меньше) — в норме, но с запасом: `🎁 Попробовать бесплатно` (23), `🔵 Подключить в v2rayNG` (22), `📣 Подписаться на канал` (22), `🎟 У меня есть промокод` (22), `💬 Написать в поддержку` (22), `🛟 Если не открывается` (21).

---

## 5. Экраны без выхода назад и тупики

### 5.1 Клиентские (8)

| Экран | Файл:строка | Почему тупик | Что делать |
|---|---|---|---|
| Витрина подарков | `gift.py:61-69` | Выход есть (`menu:main`), но нет возврата в меню подписки — после «Подарить» клиент теряет контекст своего тарифа | добавить «⬅️ Назад» на `sub:show` для активных |
| Шаг «кому дарим» | `gift.py:85-91` | Кнопка возврата ведёт на `gift:show`, а не в меню; имя вводится текстом, кнопки «Отмена» нет | добавить «⬅️ В меню» третьей кнопкой |
| Шаг «текст открытки» | `gift.py:112-117` | Единственная кнопка — «⬅️ В меню»: она **теряет** выбранный тариф (FSM не сбрасывается) | сбрасывать FSM при выходе; добавить «Пропустить» |
| Счёт: manual / crypto / wata / platega / stars | `keyboards.py:147-172` | Есть только «Отменить»; «В меню» нет — чтобы вернуться, надо отменить заказ | добавить нейтральную «⬅️ В меню» (заказ живёт) |
| Экран «Оплата по СБП — скоро» | `buy.py:83-99` | Выход есть (`docs_back_kb` → «Тарифы»), но нет «Продлить/назад к карточке тарифа» | добавить «⬅️ Назад к тарифу» |
| Ввод промокода | `referral.py:112-121` | Кнопка «В меню» **не сбрасывает** `PromoForm.code`: следующее текстовое сообщение клиента уйдёт в проверку промокода | `state.clear()` в обработчике `menu:main` или отдельная «Отмена» |
| Локации | `subscription.py:167-169` | При пустом списке инбаундов — только alert, экран не меняется (мягкий тупик) | показать экран с объяснением и кнопкой «В меню» |
| `/id` | `start.py:159-160` | Сообщение без клавиатуры | добавить `back_to_menu_kb` |

### 5.2 Админские (25)

| Группа | Файлы:строки | Суть |
|---|---|---|
| Панель и статистика | `admin.py:53,70,75` | `/stats` — без клавиатуры; `admin:stats` — «В меню» (меню клиента, не админки) |
| Заявки | `admin.py:86,120,161` | Пустой список — без кнопок; после подтверждения/отклонения сообщение становится инертным (нет «к списку заявок») |
| Команды-отчёты | `admin.py:175,214,270,302,320,335,354,367,387,406,440,456,474,484,504,528,534,555` | Ни одна команда-отчёт не даёт кнопки «в админ-панель»: вернуться можно только текстом `/admin` |
| `/nodes` | `admin.py:502-524` | Из команды — без клавиатуры; из callback — «В меню» (клиентское меню) |
| Алерты | `buy.py:384,395,407,499` | Сообщения-алерты без клавиатуры (осознанно, но действий «выдать доступ»/«написать клиенту» нет) |

Правило дизайн-системы R11 (выход на каждом экране) сейчас выполняется: у клиента — 58 из 76 экранов, у админа — 3 из 33, в боте уведомлений — 4 из 11 (кнопка возврата есть только у двух решений по заявке — и та снимается после нажатия; остальные команды-отчёты живут без навигации).

---

## 6. Мёртвые тексты, неиспользуемые клавиатуры, расхождения

| Что | Файл:строка | Факт |
|---|---|---|
| `SALES_CLOSED` | `texts.py:196` | 0 ссылок в `app/` и `tests/`: продажи закрываются заголовком `PLANS_HEADER_SOON`, а не отдельным экраном |
| `NODE_DOWN` | `texts.py:570` | 0 ссылок: алерты нод формируют текст в `app/services/alerts.py`, константа не используется |
| `NODE_UP` | `texts.py:571` | 0 ссылок |
| `ORDER_EXPIRED` | `texts.py:261` | 0 ссылок: истечение заказа нигде не показывается клиенту |
| `ADMIN_ONLY` | `texts.py:555` | 0 ссылок: не-админ на админ-команду получает меню через фолбэк |
| `LEGAL_NOT_PUBLISHED` | `texts.py:388` | 0 ссылок: при пустом `PRIVACY_URL` кнопка показывает текст в чате (`legal.py:163-172`), константа не задействована |
| `REFERRAL_SELF_ERROR` | `texts.py:540` | 0 ссылок: своя ссылка обрабатывается молча (`start.py:116-117`) |
| `🖥` «Ноды» / `📊` «Статистика» | `keyboards.py:284-286` | Кнопки админ-панели не имеют возврата в главное меню клиента/админку |

Использование полей Telegram (`style`, `disabled`, `icon_custom_emoji_id`) — **0 раз** во всём `app/bot/`, хотя aiogram 3.31.0 их поддерживает. То есть цветовая семантика кнопок в боте сейчас не реализована вообще.

---

## 7. Топ-10 слабых мест текущего дизайна

1. **Главное меню активного клиента — 8 кнопок в 6 рядах** (`app/bot/keyboards.py:33-50`, `kb.adjust(2, 1, 1, 2, 1)` на строке 50): нарушает лимит «≤ 7 кнопок / ≤ 5 рядов»; «Моя подписка» и «Продлить» стоят рядом в одном ряду и выглядят равнозначными — главного действия нет.
2. **Подписи тарифов до 33 символов с двумя ценами и 🎉** (`keyboards.py:83`): клиент видит обрезанное «12 месяцев — 719 ₽ вмес…»; норма ≤ 30, цель — «12 мес — 719 ₽», старая цена уходит в текст.
3. **Экран выбора оплаты — до 8 рядов по одной кнопке** (`keyboards.py:105-123`): четыре разных иконки (`💳🪙🏦⭐`) обозначают одно действие «оплатить»; ряд из 8 кнопок ≈ 352 px — сообщение с ценой уезжает вверх.
4. **Эмодзи-шум: 226 вхождений, 59 разных, худшее сообщение — 6 эмодзи** (`texts.py:436-455`, `REFERRAL`: `1️⃣2️⃣3️⃣🎁🎟🔁`): вдвое больше лимита «≤ 3 на сообщение»; плюс один и тот же `⭐` написан двумя способами (`texts.py:125,127` против `texts.py:134,166`) — 7 и 4 вхождения; ⚠️ за сутки выросло с 10 до 15 вхождений (новый `notify_admin.py`).
5. **48 % строк длиннее 38 символов; максимум 189** (`texts.py:228-235`, `PAYMENT_SOON`, строка 3: «Перевод по СБП (QR-код или ссылка из приложения банка) подключим в ближайшие дни…»): на телефоне это «простыня» из 7 строк; норма — 32–38.
6. **Ни одной размеченной роли кнопки: `style=` не используется ни разу** (`app/bot/keyboards.py` целиком; проверка `grep -rn "style=" app/bot/`): «Отменить заказ» (`keyboards.py:150`) и «Отклонить» (`keyboards.py:276`) выглядят как «Помощь», хотя это danger; главные действия ничем не выделены.
7. **Нет состояний загрузки и блокировки кнопок при проверке оплаты** (`app/bot/handlers/buy.py:342-373`): пока идёт запрос к провайдеру, кнопка «Проверить оплату» остаётся активной — клиент жмёт её повторно; `disabled` (Bot API 10.3) не используется.
8. **Тупики без выхода:** счета (`keyboards.py:147-172` — только «Отменить», нет «В меню»), админ-заявки (`admin.py:86,120,161` — пустой список и результат без клавиатуры), все админ-отчёты (`admin.py:70,175,214,270,354,367,387,504` — вернуться можно только набрав `/admin`).
9. **Мёртвые тексты вместо состояний** (`texts.py:196,261,388,540,555,570,571`): клиент никогда не увидит `NODE_DOWN`/`NODE_UP`, «Срок оплаты истёк» и «Продажи закрыты» — то есть состояния «нода недоступна», «заказ протух», «продажи закрыты» для клиента в дизайне не описаны и не показаны.
10. **Цветные кружки вместо смысла: `🟢🔵🟣`** (`keyboards.py:196-198` — приложения, `keyboards.py:119` и `texts.py:222-226` — «СБП скоро»): цвет вне палитры (`tokens.css` не содержит ни зелёного-кружка, ни фиолетового) и работает как единственный различитель — на клиенте без эмодзи кнопки «Подключить в Happ/v2rayNG/Hiddify» станут неотличимы по оформлению.

### Вне топ-10, но требует решения владельца

* **Второй интерфейс без дизайн-контракта.** `app/bot/handlers/notify_admin.py` (бот уведомлений, подключён через `app/services/notify_bot.py:145`) — это 11 экранов, которые видит staff, и ни один из них не описан правилами: эмодзи в сводках (`app/services/digest.py`: `📈📊🔴🟡⚪️💰⌛️⏳`) вообще не попали в перепись, потому что лежат вне `app/bot`. Либо распространяем `DESIGN-SYSTEM.md` на второй бот (и расширяем границу подсчёта), либо фиксируем, что staff-интерфейс живёт по другим правилам — сейчас не выбрано ни то, ни другое.
* **Инвентарь устаревает за часы.** За время работы над этим файлом `keyboards.py` изменился (добавлен `namespace` у `admin_order_kb`, `keyboards.py:273`), появился `notify_admin.py`, ⚠️ выросло с 10 до 15 вхождений. Цифры привязаны к хэшам из шапки; перед решениями по дизайну инвентарь надо пересчитать.

---

## 8. Приложение: скрипт подсчёта (воспроизводимость)

Скрипт ничего не пишет в репозиторий, читает `app/bot/**/*.py` и `data/kometa.db`, печатает те же числа, что в этом файле (снимок — по хэшам из шапки).
**Граница подсчёта:** `app/bot/**`. Эмодзи и тексты, живущие в сервисах (`app/services/digest.py` — сводки бота уведомлений, `app/services/gift.py` — тексты подарка, `app/services/stats.py` — статистика), в перепись **не входят**; если границу расширить, цифры вырастут, а сама механика подсчёта не изменится. Сохраните как `/tmp/kometa_audit.py` и запустите:

```bash
cd "/Users/egor/Downloads/VPN servise"
.venv/bin/python /tmp/kometa_audit.py            # человекочитаемый отчёт
.venv/bin/python /tmp/kometa_audit.py --json     # машинный вид
```

```python
#!/usr/bin/env python3
"""Аудит дизайна бота Kometa: эмодзи-перепись + длины подписей кнопок + тексты."""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import sys
import tokenize
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path("/Users/egor/Downloads/VPN servise")
BOT = ROOT / "app" / "bot"

# Extended_Pictographic (emoji-data.txt, Unicode 15)
EP = [
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x2328, 0x2328), (0x2388, 0x2388), (0x23CF, 0x23CF),
    (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
    (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x2605),
    (0x2607, 0x2612), (0x2614, 0x2685), (0x2690, 0x2705), (0x2708, 0x2712),
    (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721),
    (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744), (0x2747, 0x2747),
    (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
    (0x2763, 0x2767), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0),
    (0x27BF, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1F0FF), (0x1F10D, 0x1F10F),
    (0x1F12F, 0x1F12F), (0x1F16C, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A), (0x1F1AD, 0x1F1E5), (0x1F201, 0x1F20F), (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F23C, 0x1F23F), (0x1F249, 0x1F3FA),
    (0x1F400, 0x1F53D), (0x1F546, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F774, 0x1F77F),
    (0x1F7D5, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F), (0x1F85A, 0x1F85F),
    (0x1F888, 0x1F88F), (0x1F8AE, 0x1F8FF), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945),
    (0x1F947, 0x1FAFF), (0x1FC00, 0x1FFFD),
]
MODS = {0xFE0F, 0xFE0E, 0x20E3}
SKIN = range(0x1F3FB, 0x1F400)
RI = range(0x1F1E6, 0x1F200)


def is_ep(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in EP)


def is_keycap(text: str, i: int) -> bool:
    return (i + 2 < len(text) and text[i] in "0123456789#*"
            and ord(text[i + 1]) == 0xFE0F and ord(text[i + 2]) == 0x20E3)


def iter_emoji(text: str):
    """Эмодзи-последовательности: ZWJ, FE0F, тон кожи, флаги, keycap."""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if not (is_ep(ord(ch)) or is_keycap(text, i)):
            i += 1
            continue
        seq, j = ch, i + 1
        if is_keycap(text, i):
            seq, j = text[i:i + 3], i + 3
        while j < n:
            cp = ord(text[j])
            if cp in MODS:
                seq += text[j]; j += 1
            elif cp == 0x200D and j + 1 < n:
                seq += text[j] + text[j + 1]; j += 2
            elif cp in SKIN or (cp in RI and ord(seq[0]) in RI):
                seq += text[j]; j += 1
            else:
                break
        yield seq
        i = j


def emoji_in(text: str) -> Counter:
    return Counter(iter_emoji(text))


def scan_emoji() -> dict:
    in_strings, in_comments = Counter(), Counter()
    where, where_comment = defaultdict(Counter), defaultdict(Counter)
    const_emoji: dict[str, list[str]] = {}
    files = sorted(BOT.rglob("*.py"))
    for path in files:
        rel = str(path.relative_to(ROOT))
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found = emoji_in(node.value)
                if found and id(node) not in docstrings:
                    in_strings += found
                    where[rel] += found
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                found = emoji_in(node.value.value)
                if found:
                    names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    if names:
                        const_emoji[names[0]] = list(found)
        with path.open("rb") as fh:
            try:
                for tok in tokenize.tokenize(fh.readline):
                    if tok.type == tokenize.COMMENT:
                        found = emoji_in(tok.string)
                        if found:
                            in_comments += found
                            where_comment[rel] += found
            except tokenize.TokenError:
                pass
    return {"files_scanned": [str(p.relative_to(ROOT)) for p in files],
            "strings": in_strings, "comments": in_comments,
            "by_file_strings": {k: dict(v) for k, v in sorted(where.items())},
            "by_file_comments": {k: dict(v) for k, v in sorted(where_comment.items())},
            "by_const": const_emoji}


def dump_keyboards() -> dict:
    sys.path.insert(0, str(ROOT))
    from app.bot import gate, keyboards
    from app.bot.handlers.buy import PROVIDER_TITLES
    from app.config import get_settings
    from app.db.models import Plan

    settings = get_settings()
    con = sqlite3.connect(ROOT / "data" / "kometa.db")
    plans = [Plan(id=r[0], code=r[1], title=r[2], days=r[3], price_rub=r[4],
                  price_stars=r[5], devices_limit=r[6], is_active=True)
             for r in con.execute("select id, code, title, days, price_rub, price_stars, "
                                  "devices_limit from plans order by sort_order, id")]
    providers = [(c, PROVIDER_TITLES[c]) for c in
                 ("platega_sbp", "platega_card", "crypto", "stars", "manual", "wata")
                 if c in PROVIDER_TITLES]
    boards = {
        "main_menu.new": keyboards.main_menu(False, False, 120),
        "main_menu.active": keyboards.main_menu(True, True, 120),
        "main_menu.expired": keyboards.main_menu(True, False, 120),
        "reply_menu": keyboards.reply_menu(),
        "plans_kb": keyboards.plans_kb(plans, show_stars=True, show_promo_button=True),
        "plans_kb.plain": keyboards.plans_kb(plans),
        "plans_kb.discount": keyboards.plans_kb(plans, discount_percent=settings.referral_discount_percent,
                                                max_discount_rub=settings.referral_discount_max_rub),
        "plans_button_kb": keyboards.plans_button_kb(),
        "providers_kb": keyboards.providers_kb(1, providers),
        "providers_kb.sbp_soon": keyboards.providers_kb(1, [], sbp_soon=True),
        "referral_kb": keyboards.referral_kb("https://t.me/bot?start=ref_AB12CD34"),
        "manual_order_kb": keyboards.manual_order_kb(101),
        "crypto_order_kb": keyboards.crypto_order_kb(101, "https://pay.example/abc"),
        "stars_order_kb": keyboards.stars_order_kb(101, "https://t.me/invoice/x", "https://t.me/stars", 270),
        "connect_kb": keyboards.connect_kb("https://vpn.example/sub/TOKEN123"),
        "subscription_kb.full": keyboards.subscription_kb(True),
        "subscription_kb.no_panel": keyboards.subscription_kb(False),
        "back_to_menu_kb": keyboards.back_to_menu_kb(),
        "support_kb": keyboards.support_kb(),
        "legal_kb.internal": keyboards.legal_kb(),
        "legal_kb.urls": keyboards.legal_kb("https://x/p", "https://x/t"),
        "docs_back_kb": keyboards.docs_back_kb(),
        "admin_order_kb": keyboards.admin_order_kb(101),
        "admin_panel_kb": keyboards.admin_panel_kb(3, True, "https://x/admin"),
        "gifts_kb": keyboards.gifts_kb([(p.code, p.title, p.price_rub) for p in plans]),
        "gift_recipient_kb": keyboards.gift_recipient_kb(),
        "gift_pay_kb": keyboards.gift_pay_kb(101, providers),
        "gate.markup": gate.markup(),
    }
    out = {}
    for name, kb in boards.items():
        rows = getattr(kb, "inline_keyboard", None) or getattr(kb, "keyboard", [])
        items = [{"label": b.text, "len": len(b.text),
                  "callback": getattr(b, "callback_data", None),
                  "url": (getattr(b, "url", None) or "")[:60],
                  "type": "url" if getattr(b, "url", None) else "callback"}
                 for row in rows for b in row]
        out[name] = {"rows": len(rows), "buttons": len(items),
                     "max_len": max((i["len"] for i in items), default=0), "items": items}
    return out


def dump_texts() -> dict:
    sys.path.insert(0, str(ROOT))
    from app.bot import texts
    out = {}
    for name in dir(texts):
        if name.startswith("_"):
            continue
        val = getattr(texts, name)
        if not isinstance(val, str):
            continue
        lines = val.split("\n")
        out[name] = {"len": len(val), "lines": len(lines),
                     "max_line": max((len(x) for x in lines), default=0),
                     "emoji": sum(emoji_in(val).values()), "emoji_list": sorted(emoji_in(val)),
                     "tags": sorted({t for t in ("<b>", "<i>", "<code>", "<s>",
                                                  "<blockquote>", "<u>", "<a ") if t in val})}
    return out


def dump_routes() -> list[dict]:
    rows = []
    for path in sorted((BOT / "handlers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                        and dec.func.attr in ("callback_query", "message", "pre_checkout_query"):
                    rows.append({"file": str(path.relative_to(ROOT)), "line": node.lineno,
                                 "func": node.name, "kind": dec.func.attr,
                                 "cond": ast.unparse(dec.args[0]) if dec.args else ""})
    return rows


def main() -> None:
    data = {"emoji": scan_emoji(), "keyboards": dump_keyboards(),
            "texts": dump_texts(), "routes": dump_routes()}
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=1, default=dict))
        return
    e = data["emoji"]
    print("=== ЭМОДЗИ В СТРОКАХ ===")
    for ch, n in e["strings"].most_common():
        files = [f for f, c in e["by_file_strings"].items() if ch in c]
        print(f"{ch}  U+{ord(ch[0]):04X}  x{n}  {', '.join(files)}")
    print(f"ИТОГО разных: {len(e['strings'])}, всего штук: {sum(e['strings'].values())}")
    print(f"в комментариях: {sum(e['comments'].values())}")
    print("\n=== КЛАВИАТУРЫ ===")
    for name, kb in data["keyboards"].items():
        print(f"\n-- {name}: рядов {kb['rows']}, кнопок {kb['buttons']}, max {kb['max_len']} симв.")
        for i in kb["items"]:
            print(f"   [{i['len']:>2}] {i['label']!r} -> {i['callback'] or i['url']}")
    print("\n=== ТЕКСТЫ (по длине строки) ===")
    for name, t in sorted(data["texts"].items(), key=lambda kv: -kv[1]["max_line"])[:15]:
        print(f"{name}: len={t['len']} lines={t['lines']} max_line={t['max_line']} emoji={t['emoji']}")
    print(f"\n=== РОУТЫ: {len(data['routes'])} ===")


if __name__ == "__main__":
    main()
```

Проверка «нет ли мёртвых констант» (отдельная команда, тоже воспроизводимая):

```bash
cd "/Users/egor/Downloads/VPN servise"
for c in SALES_CLOSED NODE_DOWN NODE_UP ORDER_EXPIRED ADMIN_ONLY LEGAL_NOT_PUBLISHED REFERRAL_SELF_ERROR; do
  printf "%-22s %s\n" "$c" "$(grep -rn "\b$c\b" app/ tests/ --include=*.py | wc -l | tr -d ' ')"
done   # у каждой — 1 (только собственное определение в texts.py)
```

---

*Файл фиксирует факты и не меняет код. Правила, выведенные из этих фактов, — в [`DESIGN-SYSTEM.md`](DESIGN-SYSTEM.md).*
