# Фактбаза для VPN-сервиса в РФ: задержки, хостинги, CDN-фронт, право, автовыбор

> ⚠️ Внутренний документ: не для публикации и не для отправки партнёрам.
> Здесь встречаются формулировки, недопустимые в публичных текстах (бот, канал, сайт).
> Публичные тексты — `docs/КАНАЛ.md` и `docs/legal/`, их проверяет тест
> `tests/test_public_texts_clean.py`.

Сборка: 5 октября 2026. Подтверждённое — со ссылкой, расчётное — «оценка», непроверенное — «не подтверждено».

**Методика оценок.** Оптоволокно ≈200 000 км/с ([GeoCables](https://geocables.com/internet-latency?lang=ru)) → **10 мс RTT на 1000 км** идеального пути; реальный RTT = волокно + 30–60 %.

## 1. Задержки (RTT) и маршрутизация

Публичный измерительный ряд «город РФ → зарубежная локация» есть только у WonderNetwork, и лишь для Москвы, Казани и Новосибирска ([города](https://wondernetwork.com/pings)). Наблюдённый замер 05.10.2026: **Новосибирск → Хельсинки = 65–67 мс** (24 ч, n=336, min 64,2 / max 110,0) — [источник](https://wondernetwork.com/pings/Novosibirsk/Helsinki). Москва → Хельсинки при сборе отдавала ошибку TLS — **не подтверждено**.

| Откуда | Хельсинки | Стокгольм/Балтия | Варшава | Амстердам/Франкфурт | Стамбул | Алматы/Астана | Ереван/Тбилиси | Дубай | Токио / Сеул | Гонконг / Сингапур |
|---|---|---|---|---|---|---|---|---|---|---|
| Калининград | 25–35 | 30–40 | 30–40 | 40–55 | 60–75 | н/п | н/п | н/п | н/п | н/п |
| Санкт-Петербург | 12–20 | 15–25 | 25–35 | 30–45 | 55–70 | н/п | н/п | н/п | н/п | н/п |
| Москва | 20–25 | 25–35 | 30–38 | 35–48 | 55–70 | 40–55 | 55–75 | 90–115 | 110–150 | 150–210 |
| Казань | 30–38 | 35–45 | 40–50 | 45–60 | 60–75 | 40–50 | н/п | н/п | н/п | н/п |
| Екатеринбург | 40–50 | 45–58 | 50–62 | 55–70 | 70–85 | 35–50 | н/п | н/п | н/п | н/п |
| Новосибирск | **65–70 (замер)** | 70–80 | 75–85 | 78–92 | 85–100 | 30–45 | н/п | н/п | 100–135 | 125–205 |
| Красноярск | 75–88 | 80–95 | 85–100 | 90–110 | 95–115 | 35–50 | н/п | н/п | 85–120 | 110–190 |
| Иркутск | 85–100 | 90–105 | 95–115 | 100–125 | 105–130 | 40–60 | н/п | н/п | 75–110 | 100–180 |
| Хабаровск | 120–145 | 125–150 | 130–155 | 135–170 | 140–170 | н/п | н/п | н/п | 55–95 | 90–175 |
| Владивосток | 130–155 | 135–160 | 140–165 | 145–180 | 150–180 | н/п | н/п | н/п | 40–85 | 85–170 |

Н/п — не подтверждено; все числа в мс. В объединённых колонках («Токио / Сеул», «Гонконг / Сингапур») указан диапазон, покрывающий обе локации.

**Маршрутные патологии (подтверждено):**
- Азия–Европа через РФ — кратчайший путь: RTD Гонконг–Франкфурт **~145 мс** против 280 мс через Индию и 310 мс через Тихий+Атлантику (ComNews / RIPE NCC, [Алматы, 25.09.2025](https://www.ripe.net/participate/forms/uploads/fobi_plugins/file/capif-4-presentation-archive/RIPE%20NCC_Russia%20backbone%20connectivity_Almaty_25-09-2025_6c4e5244-bf64-458a-95ee-b5fc948b6746.pdf)).
- TEA Next (11 700 км): проектный RTD **Москва–Владивосток < 85 мс** к 2027 (там же).
- Магистрали: Ростелеком 500 000 км, МТС 281 800, Билайн 190 800, МегаФон 146 500, ТТК 78 315 (там же) → «Сибирь → Европа» почти всегда через Москву.
- Переходы РФ–Казахстан: до 1 Тбит/с (там же) → Урал/Сибирь → ЦА → Турция/ОАЭ короче, чем через Москву.
- HSCS (570 км, NTT + ТТК): RTT Невельск → Исикари **20,3–22,1 мс** (σ=0,4 мс), обратно 54–94 мс — асимметрия ([GeoCables](https://geocables.com/cable/hokkaido-sakhalin-cable-system-hscs?lang=ru)) → ДВ быстрее к Японии/Корее, чем к Москве.
- РКН: цель — **92 % эффективности блокировки VPN по сигнатурам к 2030**; на АСБИ/ТСПУ 20 млрд ₽ в 2026 и столько же в 2027–2028; 400+ сервисов заблокированы ([Настоящее Время, 04.05.2026](https://www.currenttime.tv/a/rkn-postavil-zadachu-zablokirovat-92-vpn-k-2030-godu/33748705.html)). Влияние на RTT — не подтверждено.

## 2. Хостинги, дружественные к VPN и оплате из РФ

| Провайдер | Локации и цена 1–2 ГБ | VPN в правилах | Оплата из РФ / API |
|---|---|---|---|
| Aeza | много, вкл. РФ; от €4,94 ([обзор](https://www.hostcps.com/48959.html)) | **нет де-факто**: с 03.12.2025 блокирует по спискам РКН за 24 ч, включая Xray в «чистых» сетях ([Habr](https://habr.com/ru/news/973644/), [LowEndTalk](https://lowendtalk.com/discussion/comment/4659823/)) | н/п |
| PQ.Hosting, VDSina, Timeweb, FirstVDS, ihc.ru, Zomro | ЕС, часто KZ/TR; от ~300–500 ₽ ([vps.today](https://vps.today/yaponiya)) | не подтверждено | МИР/ЮMoney заявлены; **VDSina — API есть** ([pdf](https://vdsina.ru/files/docs/public_api.pdf)) |
| Vultr, Akamai/Linode, Melbicom | Токио, Сеул, Гонконг, Сингапур; 1 ГБ ~420–505 ₽ ([vds.menu](https://vds.menu/companies/vultr-com/1-gb)), Melbicom KVM-1-TYO €5,70 ([сайт](https://www.melbicom.net/virtualserver/tokyo/)) | не подтверждено | у Vultr карт РФ нет ([docs](https://docs.vultr.com/support/platform/billing/what-payment-methods-do-you-accept)); API есть |
| bunny.net | CDN/edge | VPN прямо не запрещён ([AUP](https://bunny.net/acceptable-use/)), но регистрация через VPN/прокси → блокировка ([docs](https://bunny.net/docs/account/account-suspended.md)) | не подтверждено; API есть |
| Gcore | CDN + edge | не подтверждено | карт РФ нет, есть посредники ([Raketa Pay](https://raketapay.ru/oplata/gcore-cloud), [docs](https://docs.gcore.com/hosting/payments/pay-for-gcore-services-payment-methods)); API есть |

**Япония/Корея за рубли.** vps.today: 105 тарифов в Японии от 10 хостеров, минимум **300 ₽/мес**, далее 380, 425 (Vultr 1 ГБ), 510, 523, 594, 849 ₽ (2 ГБ) — [источник](https://vps.today/yaponiya). Рублёвые цены там — калькуляция валютного прайса, не гарантия приёма карты МИР. Корея — фильтр «оплата МИР» на [hostinghub](https://hostinghub.ru/vps-search/pay/karty-mir-47/server_location/koreya-506/virtualization/kvm-81). Фактические условия оплаты — **не подтверждено**.

## 3. CDN-фронт для первой мили

- **Cloudflare в РФ.** На карте сети — три города РФ: Москва, Санкт-Петербург, **Красноярск** ([cloudflare.com/network](https://www.cloudflare.com/network/)); Екатеринбурга, Новосибирска, Владивостока нет. DME (Москва) жив: плановые работы 13.03.2026 ([статус](https://cloudflare.statuspage.io/incidents/k5ndvf5gd3jm)).
- **ToS Cloudflare: VPN запрещён** — «use the Services to provide a virtual private network or other similar proxy services» (документ обновлён 05.05.2026) — [разбор с цитатой](https://conductatlas.com/platform/cloudflare/cloudflare-terms-of-use/provision/CA-P-069161/prohibition-on-vpn-or-proxy-service-use/), [оригинал](https://www.cloudflare.com/terms/); плюс право удалить аккаунт без уведомления.
- **Практика: даже с PoP первая миля не спасает.** Cloudflare официально фиксирует, что российские ISP throttlят трафик к Cloudflare до **~16 КБ на соединение** и восстановить связность они не могут ([док, 23.04.2026](https://developers.cloudflare.com/support/troubleshooting/general-troubleshooting/service-disruption/)).
- **Альтернативы** (Gcore, Bunny, Selectel, VK Cloud, Yandex Cloud, EdgeCenter, DDoS-Guard, Qrator, StormWall): PoP в РФ у Gcore/Selectel/VK/Yandex заявлены их сайтами, но **разрешение проксировать VPN-трафик не подтверждено ни у одного**.
- **Если CDN нельзя**: anycast-вход собственной AS, XHTTP/WebSocket/gRPC поверх разрешённого домена, релейные входы в KZ/AM/GE/TR. Опубликованных замеров устойчивости этих схем к ТСПУ на 2024–2026 — **не подтверждено**.

## 4. Ноды внутри РФ: риски и сценарии

- **ОРИ (ФЗ-149 ст. 10.1)**: уведомление РКН, хранение данных, установка СОРМ; обязательность привязана к порогам — [текст 149-ФЗ в ред. от 24.06.2025](https://www.consultant.ru/cons/cgi/online.cgi?from=511583-0&req=doc&base=LAW&n=508807).
- **Хостинг-провайдеры**: реестр РКН действует с 01.12.2023 ([прокуратура](https://epp.genproc.gov.ru/ru/proc_16/activity/legal-education/explain/e181923/)); работа вне реестра — штраф **до 1 млн ₽** ([новость](https://www.law.ru/news/44153-gosduma-ustanovila-shtraf-v-1-mln-rubley-za-rabotu-hosting-provayderov-vne-reestra-rkn)), состав — КоАП ст. 13.54 ([текст](https://sudact.ru/law/koap/razdel-ii/glava-13/statia-13.54/)).
- **276-ФЗ (популяризация обхода блокировок)**: подписан 31.07.2025, в силе с **01.09.2025**, рекламные договоры — до 01.03.2026; ответственность по КоАП 14.3 ([обзор](https://www.alrud.ru/publications/1784), [news](https://rima.media/document/2025-07-31-mediazona-348485-putin-podpisal-zakon-o-zaprete-poiska-ekstremistskikh)).
- **Практика**: кейс Aeza (декабрь 2025) — РКН прислал список IP из 138.124/16, хостер потребовал удалить VPN за 24 часа; детект не только по WireGuard/OpenVPN ([Habr](https://habr.com/ru/news/973644/)).
- **Защитимые сценарии**: корпоративный VPN для доступа сотрудников к ресурсам своей компании (не обход блокировок) либо обычный хостинг/CDN без функции обхода; обязательства по 149-ФЗ/152-ФЗ и СОРМ сохраняются. Разграничение «корпоративный VPN» / «средство обхода» — **не подтверждено**.

## 5. Автовыбор локации на клиенте

| Клиент | Автовыбор | Механизм | Кто настраивает | Формат подписки |
|---|---|---|---|---|
| sing-box (SFA/SFI/SFM/desktop) | да | outbound `urltest` | сервер (JSON) | sing-box JSON |
| mihomo / Clash Meta | да | proxy-group `url-test` | сервер (YAML) | Clash/Mihomo YAML |
| v2rayNG (Xray) | ограниченно | ручной тест задержки; авто — через `observatory`/`leastPing` | вручную | base64-список `vless://`/`vmess://` |
| Hiddify | зависит от ядра | наследует urltest/url-test ядра | сервер/вручную | Clash YAML или sing-box JSON |
| Happ | не подтверждено | — | — | — |

Параметры sing-box `urltest`: `url` = `https://www.gstatic.com/generate_204`, `interval` = `3m`, `tolerance` = `50` мс, `idle_timeout` = `30m` ([док](https://sing-box.sagernet.org/configuration/outbound/urltest/), [исходный md v1.9.0-rc.20](https://go-mod-viewer.appspot.com/github.com/sagernet/sing-box@v1.9.0-rc.20/docs/configuration/outbound/urltest.zh.md)). mihomo: `type: url-test`, `url: 'https://www.gstatic.com/generate_204'`, `interval: 300`, `tolerance: 50`, `lazy: true` ([док](https://wiki.metacubex.one/en/config/proxy-groups/url-test/)).

**Что нужно от сервера:** подписка в формате, порождающем группу авто-выбора (Clash YAML или sing-box JSON — плоский base64-список её не создаёт); HTTP-204 endpoint без авторизации; ≥2 outbound в группе; отдача разных форматов по User-Agent с одного URL.

## Не подтверждено

Все клетки таблицы, кроме «Новосибирск → Хельсинки», — оценки, не измерения. Также не подтверждены: RTT из Калининграда, СПб, Екатеринбурга, Красноярска, Иркутска, Хабаровска, Владивостока; Москва → Хельсинки; разрешение VPN-трафика в AUP любого CDN с PoP в РФ; приём МИР/СБП у конкретных JP/KR/HK-хостеров; автовыбор в Happ и актуальном v2rayNG; генерация `url-test`-групп панелями 3x-ui/Marzban ([issue #5095](https://github.com/MHSanaei/3x-ui/issues/5095)); разграничение «корпоративный VPN» / «средство обхода»; влияние ТСПУ на RTT.

## Что проверить самому после покупки серверов

1. `mtr -rwzbc100` с клиентских точек (Москва, Новосибирск, Владивосток) до каждой ноды: реальные RTT и видно, где трафик «тромбонит» через Москву.
2. Замер с двух ISP на город (Ростелеком + МТС/Билайн): маршруты различаются, особенно Сибирь/ДВ.
3. Владивосток → Токио/Сеул: подтвердить асимметрию HSCS и не уходит ли обратный трафик через Европу; сверка через [RIPE Atlas](https://atlas.ripe.net/).
4. Письменное подтверждение от хостера (Bunny/Gcore/Selectel/VK), что VPN-нагрузка допустима по AUP, — до запуска.
5. Проверить, что подписка отдаёт Clash YAML и sing-box JSON с готовой `url-test`/`urltest`-группой, а тестовый URL отвечает 204 без авторизации.
6. Накопительная проверка блокировки входного IP на ТСПУ через 1–2 недели работы, а не единичный тест.
