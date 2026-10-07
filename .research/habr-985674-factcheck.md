# Фактчек статьи Habr 985674 (adlayers, 15.01.2026)

**Дата проверки:** 07.10.2026. **Задача:** независимо проверить утверждения статьи по первоисточникам.
**Итог ревью:** `docs/РЕВЬЮ-ХАБР-985674.md`. **Сырые комментарии:** `.research/habr-985674-comments.md`.

Обозначения: **[П]** проверено по первоисточнику (исходники/доки/релизы), **[Ч]** частично,
**[О]** опровергнуто, **[НП]** не проверяемо (нет методики).

---

## Главное: конфиг из статьи не запустится

`"flow": "xtls-rprx-vision"` вместе с `"network": "xhttp"` — **жёсткая ошибка**, а не «работает
иначе». В Xray-core v25.12.8:

* `proxy/vless/inbound/inbound.go:580` и `proxy/vless/outbound/outbound.go:282` возвращают
  `errors.New("XTLS only supports TLS and REALITY directly for now.")` для flow на не-RAW
  транспорте (xhttp/ws/grpc) — запрос отклоняется;
* [REALITY docs](https://xtls.github.io/en/config/transports/reality.html): «REALITY can only be
  used together with RAW, XHTTP and gRPC»; Vision требует, чтобы соединение было `*tls.Conn` /
  `*reality.Conn`, а xhttp оборачивает TLS;
* ограничение снято только [PR #6629](https://github.com/XTLS/Xray-core/pull/6629) (15.08.2026,
  закрыт через 34 минуты с комментарием «meaningless») — через семь месяцев после публикации;
* панельный симптом: [3x-ui #3678](https://github.com/MHSanaei/3x-ui/issues/3678) «can't set the
  flow option — warning in log»; связанные баги — [#3712](https://github.com/MHSanaei/3x-ui/issues/3712),
  [#4511](https://github.com/MHSanaei/3x-ui/issues/4511).

## Разбор утверждений

| № | Утверждение | Вердикт | Суть |
|---|---|---|---|
| 1 | ТСПУ «морозят» сессию после 15–20 КБ, без RST | **[Ч]/[НП]** | Явление (шейпинг/заморозка вместо RST на зарубежный трафик) фиксируется сообществом с конца 2025; порог «15–20 КБ» измеримой методикой не подтверждён — личное наблюдение. Формулировка «ТСПУ не рвут сессию» неверна как универсальная: RST-блокировки тоже документированы |
| 2 | «Xray-core минимум 25.12.8 под Aparecium» | **[О]** (номер) | v25.12.8 существует (08.12.2025). [Aparecium](https://github.com/ban6cat6/aparecium) — реальный PoC (создан 31.05.2025), детектит ShadowTLS v3 и REALITY через расхождение TLS 1.3 post-handshake `NewSessionTicket`. Но фикс в `xtls/reality` — commit 21af070 (07.06.2025), признание в [issue #4778](https://github.com/XTLS/Xray-core/issues/4778) (03–07.06.2025), и он попал уже в **v25.6.7/v25.6.8**: `go.mod` v25.6.8 ссылается на `reality v0.0.0-20250608132114`. На дату статьи актуальным было ядро 26.1.x |
| 3 | `packet-up` для плеча «нода → нода» ради памяти | **[О]** | Требований к памяти в документации нет — тезис не подтверждается. [Discussion #4113](https://github.com/XTLS/Xray-core/discussions/4113): `packet-up` — «совместимость сильнее всего», `stream-up` — потоковый аплоад (H2-стриминг, замена gRPC), `stream-one` — единый путь. `packet-up` требует `seq` от клиента и ассоциации UUID за 30 с и **несовместим с mux** (issue #6645) |
| 4 | `serverName: vkvideo.ru` для Reality | **[Ч]** | Требования к target: TLS 1.3 + HTTP/2, сертификат покрывает SNI; доки прямо предупреждают не брать target за Cloudflare. RU-SNI при зарубежном IP — гео-аномалия, плюс Reality «спамит» чужой сервер и ложится вместе с ним |
| 5 | Лимит VPN-процесса на iOS 50 MiB | **[П]** — самая крепкая цифра | 50 MiB — реальный per-process cap у packet-tunnel расширения: разбор JetsamEvent показывает убийства ровно на 50 MiB с reason `per-process-limit` ([queqiao commit 5c553bb](https://github.com/bojieli/queqiao/commit/5c553bb88e2eb73a8aa814f49175ca75aa90ff2f), 28.08.2026). Уточнения: падение **не при старте**, а через 12–21 минуту (SIGKILL, `stopTunnel` не вызывается, туннель висит «подключённым»); корень — Go runtime + gVisor; лечение — `GOMEMLIMIT` ~28 MiB, облегчённые rule-set |
| 6 | `sysctl`: `somaxconn=9000000`, `tcp_max_syn_backlog=9000000`, `tcp_syncookies=0` | **[О]** | Эффективный backlog = `min(somaxconn, backlog из listen())`; приложения передают своё значение → 9 млн бесполезны. `syncookies=0` снимает защиту от SYN-flood (при переполнении очереди соединения отбрасываются), а `tcp_max_syn_backlog` при включённых syncookies игнорируется. Разумно: syncookies 1–2, somaxconn 4096–65535 ([Red Hat](https://access.redhat.com/solutions/6439141), с RHEL 8 поле u32 — commit becb74f0) |
| 7 | Роутинг: `regexp:`, `full:`, `domainStrategy: IPIfNonMatch`, `geosite:category-ru/yandex/private` | **[П]** синтаксис | Валидные префиксы и существующие теги (проверено парсингом `geosite.dat`, 1542 тега). Пробелы: нет правила для приватных сетей (RFC1918 → петли); правило DIRECT стоит без `inboundTag`; `geosite:category-ru-blocked` **не существует** — актуальный тег `CATEGORY-MEDIA-RU-BLOCKED` (такое правило уронит загрузку конфига). `full:cp.cloudflare.com` как «фикс вечного таймаута» — комьюнити-практика без первоисточника |
| 8 | «Цепочка через РФ-VPS — единственный способ» | **[О]** как «единственный» | Работает только при попадании входного IP в белые префиксы. Прямое опровержение «YC — универсальный вход»: `AS Yandex.Cloud LLC` ≠ `AS YANDEX LLC`, YC-VM режется при белых списках ([Habr 1021160](https://habr.com/ru/articles/1021160/), UPD автора 09.04.2026). Альтернативы 2025–2026: AmneziaWG, VLESS+WS+TLS за CDN (домашние сети), TURN/SFU-релеи, Shadowsocks-2022 |
| 9 | Каталог сервисов «из обсуждений» | **[О]** частично | Ссылок на сервисы в статье нет — прямых признаков партнёрского размещения не найдено. Но утверждение «в обсуждениях выделяли» не подтверждается дампом 267 комментариев: `Voxiproxy`, `MamontVPN`, `SayVPN`, `DuckVPN` — 0 упоминаний, `hynet.cloud` — 2 упоминания, оба отрицательные. `DuckVPN`, вероятно, перепутан с западным `duckvpn.com` |
| 10 | Дата публикации | **[П]** | 15.01.2026 (метаданные `datePublished` 23:59:27+03:00; на странице отображается 20:59 UTC). Есть англоязычный перевод — habr.com/en/articles/990206 |

## Белые списки на LTE: что подтверждается отдельно

* Режим действует с осени 2025, в списках >150 ресурсов; тест «Российской газеты» (17.03.2026)
  на МТС/Мегафон/Билайн/Т2 показал: **VPN надо выключать**, иначе не грузятся даже разрешённые сайты —
  [cableman.ru](https://www.cableman.ru/content/test-konnekt-operatory-bolshoi-chetverki-proshli-ispytaniya-belym-spiskom-v-tsentre-moskvy).
* Cloudflare в белые списки мобильных операторов не входит (CDN-fronting на LTE не работает) —
  совпадает с нашим корпусом (`docs/УСТОЙЧИВОСТЬ.md:79–97`).

## Источники

Xray: [releases](https://github.com/XTLS/Xray-core/releases) · [v25.12.8](https://github.com/XTLS/Xray-core/releases/tag/v25.12.8) ·
[PR #6629](https://github.com/XTLS/Xray-core/pull/6629) · [issue #4778](https://github.com/XTLS/Xray-core/issues/4778) ·
[discussion #4113](https://github.com/XTLS/Xray-core/discussions/4113) · [REALITY docs](https://xtls.github.io/en/config/transports/reality.html) ·
[routing docs](https://xtls.github.io/en/config/routing.html) · [Aparecium](https://github.com/ban6cat6/aparecium).
Прочее: [Habr 1021160](https://habr.com/ru/articles/1021160/) · [queqiao 5c553bb](https://github.com/bojieli/queqiao/commit/5c553bb88e2eb73a8aa814f49175ca75aa90ff2f) ·
[Red Hat somaxconn](https://access.redhat.com/solutions/6439141) · [cableman](https://www.cableman.ru/content/test-konnekt-operatory-bolshoi-chetverki-proshli-ispytaniya-belym-spiskom-v-tsentre-moskvy).
