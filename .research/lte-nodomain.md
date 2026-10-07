# Валидный TLS-вход и CDN-фронт БЕЗ покупки домена — состояние на 06.10.2026

**Дата подготовки:** 6 октября 2026. **Статус:** внутренний исследовательский отчёт.
**Задача:** найти реальные способы получить валидный TLS (SNI/сертификат) и/или CDN-фронт, не покупая домен (reg.ru требует паспортные данные у физлиц).

**Легенда достоверности:**
- **[ИЗМ]** — измерено мной в рамках этого исследования 06.10.2026 (dig/curl/анализ файлов), есть воспроизводимая методика.
- **[ПОДТВ]** — подтверждено официальной документацией вендора или официальным сообщением.
- **[ЗАЯВЛ]** — заявлено одним автором/вендором/сообществом, независимого подтверждения нет.
- **[НЕПРОВЕРЕНО]** — данных найти не удалось; цифра/вывод не выдуманы, а помечены как неизвестные.

---

## 0. Три факта 2026 года, которые меняют всю картину

### 0.1. Покупка домена в РФ стала ещё хуже: идентификация через Госуслуги с 01.09.2026

Федеральный закон **от 29.12.2025 № 569-ФЗ**: с **1 сентября 2026** регистрация **и продление** доменов в зонах **.ru / .рф / .su** возможна только после подтверждения личности через **ЕСИА («Госуслуги»)**. Требование распространяется и на новых, и на действующих администраторов, и на физлиц, и на компании, **включая нерезидентов РФ** [ПОДТВ — [tcinet.ru, 24.07.2026](https://tcinet.ru/press-centre/news/7862/)].

Вывод: вопрос «купить домен, но без паспорта» в 2026 в зоне .ru закрыт нормативно. Внешние зоны (.com и т.п.) формально не под ЕСИА, но: (а) реселлеры в РФ всё равно собирают паспортные данные, (б) у платёжеспособности и KYC свои требования. См. §5.3.

### 0.2. Let's Encrypt начал выпускать сертификаты на IP-адрес — это и есть «валидный TLS без домена»

- **15.01.2026** — IP-сертификаты и 6-дневные сертификаты **GA** [ПОДТВ — [letsencrypt.org](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)].
- IP-сертификат **обязан** быть short-lived: профиль **`shortlived`**, срок **160 часов (~6,6 суток)**, DNS-имена и IP в одном сертификате не смешиваются (в профиле `shortlived` Identifier Types = DNS, IP) [ПОДТВ — [letsencrypt.org/docs/profiles](https://letsencrypt.org/docs/profiles/), проверено 06.10.2026].
- Валидация для IP: **HTTP-01 или TLS-ALPN-01**; **DNS-01 для IP не работает** [ПОДТВ — [challenge-types](https://letsencrypt.org/docs/challenge-types/)].
- Клиент: **Certbot 5.3+** c `--ip-address`, обязателен `--preferred-profile shortlived`; работают плагины `webroot`, `standalone`, `manual`; **nginx/apache-плагины IP пока не поддерживают**, нужен `--deploy-hook` [ПОДТВ — [letsencrypt.org, 11.03.2026](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)].
- **Rate limit:** для IPv4 «registered domain» = сам адрес → **50 сертификатов/неделю на ваш IP**, и это ваш личный лимит, а не общий с кем-то [ПОДТВ — [rate-limits](https://letsencrypt.org/docs/rate-limits/), проверено 06.10.2026].
- Прочие лимиты LE (актуальны для всех схем ниже) [ПОДТВ — там же]: **300 new orders / аккаунт / 3 ч** (refill 1 за 36 с); **50 сертификатов / registered domain / неделю** (refill 1 за 202 мин); **5 сертификатов на один и тот же набор идентификаторов / 7 дней** (global); оверрайды по «certificates per registered domain» и «new orders per account» **запрашиваются**, по «new registrations per IP» — **не даются**.

Практический смысл: **можно поднять вход на «голом» IP с публично доверенным сертификатом и без единого домена.** Но: (1) SNI при этом пустой/IP — это сам по себе отличительный признак для DPI; (2) обновление раз в ~5 дней обязано быть автоматическим; (3) IP-сертификат не совместим с маскировкой под чужой домен (для этого есть Reality, §6).

### 0.3. Public Suffix List — замер: почти все «бесплатные поддомены» в PSL, а sslip.io/nip.io/traefik.me — нет

Замер [ИЗМ]: скачан `publicsuffix.org/list/public_suffix_list.dat` (16 501 строка) 06.10.2026, проверено точное вхождение.

| Домен | В PSL? | Что это значит для LE |
|---|---|---|
| **sslip.io, nip.io, traefik.me** | **НЕТ** | Весь домен = **один** registered domain → лимит **50 сертификатов/неделю на ВСЕХ пользователей sslip.io** |
| duckdns.org, dedyn.io, dynv6.net | ДА | Каждый `name.duckdns.org` = свой registered domain → свой лимит 50/нед |
| no-ip.info, no-ip.biz, ddns.net, hopto.org, zapto.org, sytes.net, myftp.org, servebeer.com | ДА | То же — свой лимит на каждый поддомен |
| dynu.net | ДА | То же |
| mooo.com / chickencam.com / **все shared-домены freedns.afraid.org** | **НЕТ** | Лимит 50/нед делится между всеми пользователями shared-домена |
| us.kg, pp.ua, eu.org | ДА | Каждый зарегистрированный домен ниже — свой лимит |
| workers.dev, pages.dev | ДА | Каждый `name.workers.dev` — свой registered domain |
| trycloudflare.com, ngrok-free.app, ngrok.io | ДА | То же |
| onrender.com, deno.dev, replit.app, up.railway.app, fly.dev, vercel.app, netlify.app, github.io, herokuapp.com | ДА | То же |
| **koyeb.app, glitch.me, loca.lt, serveo.net, bore.pub, pinggy.io, zrok.io, localhost.run** | **НЕТ** | Общий лимит на весь домен провайдера |
| **your-server.de (Hetzner PTR), aeza.net, vdsina.ru, timeweb.ru** | **НЕТ** | Хостерский хостнейм = общий лимит на всех клиентов хостера |

Отдельно по sslip.io [ПОДТВ — GitHub]: в июне–октябре 2024 пользователи стабильно ловили `429 ... too many certificates already issued for "sslip.io"`; мейнтейнер **04.10.2024** подал в LE заявку на повышение лимита **с 3 000–5 000 до 5 000–10 000 сертификатов/неделю** ([issue #57](https://github.com/cunnie/sslip.io/issues/57), закрыт 09.10.2024); попытка добавить sslip.io в PSL ([PR #2206](https://github.com/publicsuffix/list/pulls/2206)) **закрыта без мержа 09.10.2024** и по сей день домен в PSL отсутствует [ИЗМ — проверено в файле PSL 06.10.2026]. В README sslip.io есть благодарность сотруднику LE «who bumped the rate limits» [ЗАЯВЛ].
**Итог: HTTP-01 на `1.2.3.4.sslip.io` технически возможен, но лимит общий на весь sslip.io; действует ли сейчас оверрайд 5–10 тыс./нед — [НЕПРОВЕРЕНО]. Для одного-двух сертификатов риска почти нет, для парка входов — есть.**

Живая проверка DNS (мой замер 06.10.2026) [ИЗМ]:
`1.2.3.4.sslip.io → 1.2.3.4`; `1.2.3.4.nip.io → 1.2.3.4`; `1.2.3.4.traefik.me → 1.2.3.4`; `test.pp.ua → 51.83.143.43`; `foo.duckdns.org → 89.253.92.189`. **CAA-записей нет** ни на sslip.io, ни на nip.io, ни на traefik.me, ни на duckdns.org, ни на dedyn.io/us.kg/pp.ua/eu.org/workers.dev/trycloudflare.com/ngrok-free.app → **любой CA вправе выпускать** (блокирующего CAA нет).

---

## 1. Бесплатные DNS/поддомены без паспорта

| Вариант | Что даёт | Паспорт/ID? | Срок | Стабильность | Риски |
|---|---|---|---|---|---|
| **sslip.io / nip.io / traefik.me** | Хостнейм из IP (`1.2.3.4.sslip.io`), DNS-контроля нет | **Нет** | 0 минут | Живы (nip.io/sslip.io — 78.46.204.247; traefik.me — GitHub Pages) [ИЗМ] | Не в PSL → общий лимит LE; владелец может закрыть сервис; нет TXT → только HTTP-01, wildcard невозможен |
| **DuckDNS** | `name.duckdns.org` + DNS-контроль через HTTP API (токен), TXT для DNS-01 (хук `dns_duckdns` в acme.sh) | **Нет** (вход по OAuth-провайдеру) | 0–5 минут | Работает с 2010-х, живой сайт [ИЗМ] | В PSL → свой лимит LE. ToS про VPN прямо не запрещает, но [НЕПРОВЕРЕНО]; домен известен блокировщикам (ddns-паттерн) |
| **FreeDNS / afraid.org (shared-домены)** | `name.mooo.com` и др. + полный DNS-контроль, **есть API для TXT** — оф. FAQ №17 показывает curl для Let's Encrypt [ПОДТВ — [freedns.afraid.org/faq](https://freedns.afraid.org/faq/)] | **Нет** | 0 минут (+ иногда модерация) | Сервис живёт с 2001 | **Shared-домены НЕ в PSL** [ИЗМ] → лимит 50/нед на всех; заявка на добавление в PSL ([issue #88](https://github.com/publicsuffix/list/issues/88)) — статус не подтверждён [НЕПРОВЕРЕНО]; afraid.org исторически банит за абузы |
| **dynu.com** | `name.dynu.net` и др., DNS-контроль, API | **Нет** | 0–10 минут | Долгоживущий сервис | Бесплатный хостнейм требует периодического подтверждения [НЕПРОВЕРЕНО]; домен палится как DDNS |
| **no-ip.com** | `name.no-ip.info` (free), DNS-контроль | **Нет** | 0–10 минут | Живой [ИЗМ — страница /free отдаётся] | **Free-хостнеймы истекают каждые 30 дней** и требуют подтверждения по e-mail [ЗАЯВЛ, широко известно]; for-VPN политика [НЕПРОВЕРЕНО] |
| **eu.org** | Полноценный домен `name.eu.org` (не поддомен-хелпер), DNS-контроль | **Нет** (заявка вручную) | **Много месяцев** — модерация людьми | Работает с 1996, сайт живой [ИЗМ] | Очередь; домен в PSL (хорошо для LE); риск отказа; e-mail-подтверждение |
| **nic.us.kg** | Бесплатный домен `name.us.kg` (в PSL) | **Нет** | минуты-дни | **Лендинг nic.us.kg не отдал контент при замере 06.10.2026** [ИЗМ]; проект DigitalPlat критикуют за смену правил [ЗАЯВЛ — [сравнение](https://agentdeals.dev/compare/digitalplat-vs-freedns-afraid-org)] | **Высокий риск исчезновения зоны** — на таком домене нельзя строить вход; [НЕПРОВЕРЕНО] |
| **pp.ua** | Бесплатный домен третьего уровня `name.pp.ua`, в PSL | **Нет**, но нужна активация (обычно SMS/телефон) [НЕПРОВЕРЕНО] | часы-дни | Зона живая (`test.pp.ua → 51.83.143.43`) [ИЗМ] | Украинская зона: репутационные/политические риски при работе из РФ; [НЕПРОВЕРЕНО] по ToS |
| **deSEC.io / dedyn.io** | Самое ценное: **бесплатный поддомен `name.dedyn.io` + полноценный DNS-API** → **DNS-01 и wildcard-сертификат** | **Нет** (регистрация по e-mail) | 5–15 минут | Некоммерческий проект, финансируется фондами; в PSL [ИЗМ] | ToS про VPN [НЕПРОВЕРЕНО]; блокировка домена в РФ маловероятна, но IP германские |
| **Cloudflare-аккаунт без домена** | `*.workers.dev`, `*.pages.dev`, quick tunnels, бесплатный DNS | **Нет** (карта не нужна для free) | минуты | Cloudflare стабилен | См. §3 — **ToS прямо запрещает использовать сервисы как VPN/прокси**; ASN Cloudflare отсутствует в белых списках РФ |

**Ключевой вывод по §1.** Единственный бесплатный вариант, который даёт **и хостнейм, и DNS-API для DNS-01/wildcard, и находится в PSL** — это **deSEC/dedyn.io** (и, с оговорками, **DuckDNS**). Всё семейство `sslip.io/nip.io/traefik.me` годится только для HTTP-01 и одного-двух сертификатов, потому что лимит LE у них общий.

---

## 2. Бесплатные хостинги: поддомен + TLS + WebSocket

Критерий: нужен **долгоживущий двунаправленный WebSocket** (VLESS-over-WS) и/или **исходящий TCP** к произвольному host:port (если платформа — только релей до бэкенда).

| Платформа | Поддомен | WebSocket | Исходящий TCP | Лимиты free | ToS про прокси | Вердикт |
|---|---|---|---|---|---|---|
| **Cloudflare Workers** | `*.workers.dev` (в PSL) | **Да** (WS есть и на вход, и на выход) | **Да** — `connect()` из `cloudflare:sockets`, произвольный порт (в доках пример порт 5432) [ПОДТВ — [tcp-sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/), 06.10.2026] | **100 000 запросов/сутки**, CPU **10 мс/запрос** (ожидание сети не считается), 128 МБ, 50 subrequest'ов [ПОДТВ — [limits](https://developers.cloudflare.com/workers/platform/limits/)] | **Запрещено прямо**: §2.2.1(j) | **PARTIAL: технически лучший, юридически — запрещён** (§3) |
| **Cloudflare Pages** | `*.pages.dev` | Да (Functions) | Да (через Functions) | То же + статика | То же | PARTIAL, тот же риск |
| **Deno Deploy** | `*.deno.dev` (в PSL) | Да | **Нет** — только `fetch()`; сырой TCP к произвольному порту недоступен [НЕПРОВЕРЕНО для 2026 — статус классического Deploy менялся] | CPU/лимиты [НЕПРОВЕРЕНО] | AUP — получить markdown не удалось (404/пусто) [НЕПРОВЕРЕНО] | **NO/UNCLEAR** |
| **Vercel** | `*.vercel.app` | **Да, Beta на всех планах**, доки обновлены **10.08.2026** [ПОДТВ — [vercel.com/docs/functions/websockets](https://vercel.com/docs/functions/websockets)] | Нет для произвольного TCP через edge; Node-рантайм теоретически может открыть сокет, но long-lived соединение упирается в duration [НЕПРОВЕРЕНО] | Hobby: лимиты длительности функций [НЕПРОВЕРЕНО] | ToS про прокси [НЕПРОВЕРЕНО] | **PARTIAL/UNCLEAR** |
| **Netlify** | `*.netlify.app` | Functions — WS ограниченно; Edge — нет [НЕПРОВЕРЕНО] | Нет | 100 ГБ/мес, 300 минут build | [НЕПРОВЕРЕНО] | **NO** |
| **Render** | `*.onrender.com` | **Да** (обычный веб-сервис) | **Да** (полноценный контейнер) | Free: засыпает после ~15 мин простоя, 750 ч/мес [ЗАЯВЛ] | Прямого запрета VPN нет, но AUP (ред. **22.08.2025**) запрещает «Bypass Access or Usage Restrictions … using the Service to bypass network restrictions» [ПОДТВ — [render.com/acceptable-use](https://render.com/acceptable-use)] | **NO по бану на практике**: есть публичный отчёт о бане edgetunnel на Render ([zizifn/edgetunnel#116](https://github.com/zizifn/edgetunnel/issues/116) «Maybe a way to bypass the render ban») |
| **Glitch** | — | — | — | — | — | **МЁРТВ**: хостинг проектов закрыт [ПОДТВ — [blog.glitch.com «Until we meet again»](https://blog.glitch.com/post/goodbye-glitch)] |
| **Replit** | `*.replit.app/.dev` | Да (Node) | Да | Free: без always-on, сон [НЕПРОВЕРЕНО] | [НЕПРОВЕРЕНО] | **PARTIAL** |
| **Fly.io** | `*.fly.dev` | Да | Да (полноценные VM) | **Free-тарифы отменены** [ПОДТВ — [Discontinued Plans](https://flyio-landing.fly.dev/docs/about/discontinued-plans/)] | [НЕПРОВЕРЕНО] | **NO (платно)** |
| **GitHub Pages** | `*.github.io` | **Нет** (только статика) | Нет | — | — | **NO** |
| **Railway** | `*.up.railway.app` | Да | Да | **Только trial-кредиты**, «Free plan costs $1/month» [ЗАЯВЛ — [docs.railway.com/pricing/free-trial](https://docs.railway.com/pricing/free-trial)] | [НЕПРОВЕРЕНО] | **NO (платно)** |
| **Koyeb** | `*.koyeb.app` (**не в PSL**) | Да | **Да — есть отдельный TCP-прокси продукт** [ПОДТВ — [blog.koyeb.com](https://www.koyeb.com/blog/tcp-proxy-expose-tcp-ports-publicly)] | **Starter-план закрыт для новых регистраций** [ПОДТВ — [dev.to](https://dev.to/build996/koyebs-starter-plan-closed-to-new-signups-kept-for-existing-orgs-30mh)] | [НЕПРОВЕРЕНО] | **NO для новых аккаунтов** |
| **Heroku** | *.herokuapp.com | Да | Да | Free-тариф мёртв | — | **NO (платно)** |

**Вывод по §2.** Единственная бесплатная платформа, которая одновременно даёт WS **и** исходящий TCP (то есть может быть и фронтом, и релеем) — **Cloudflare Workers**, и ровно её ToS это прямо запрещает. Все остальные либо без сырого TCP, либо без free-тарифа, либо уже банят (Render). **Как «бесплатный хостинг для VPN-входа» в 2026 ничего из этого списка не является устойчивым решением.**

---

## 3. Cloudflare Workers как фронт для VLESS/WS + риск по ToS

### 3.1. Рабочие схемы (что реально используют в 2025–2026)

| Схема | Архитектура | Где живёт | Комментарий |
|---|---|---|---|
| **edgetunnel** ([zizifn](https://github.com/zizifn/edgetunnel), [cmliu/edgetunnel](https://www.chonglangbiji.com/clients/cmliu-edgetunnel/) — гайд 2026) | Worker сам является VLESS-over-WS сервером и через `connect()` ходит к цели | Workers | Классика; требует UUID/подписки |
| **EDtunnel** (3Kmfi6HP) | То же, «всё в одном файле» | Workers | Много форков |
| **BPB Panel** ([ali-hashemy/BPB-FA-Panel](https://github.com/ali-hashemy/BPB-FA-Panel), [enclepeng/bpb](https://github.com/enclepeng/bpb)) | Панель + подписки, деплой на Workers/Pages | Workers/Pages | Позиционируется как «zero-cost» |
| **Worker как WS-релей** к своему бэкенду | Worker держит WS и переливает в TCP через `connect()` | Workers | Требует своего сервера-выхода, т.е. домен не нужен, но нужен VPS |

### 3.2. Технические ограничения (проверено по докам 06.10.2026)

- **Free: 100 000 запросов/сутки**; WS-соединение = запрос (хендшейк). CPU **10 мс** на free / 5 мин на paid; «ожидание сети в CPU не входит» → длинный WS сам по себе CPU не жжёт, но **крипто-обработка VLESS в JS/WASM легко упирается в 10 мс** → ошибка **1102 «Worker exceeded resource limits»** [ПОДТВ — [limits](https://developers.cloudflare.com/workers/platform/limits/)].
- **Память 128 МБ** на изолят, 50 subrequest'ов на запрос (free) [ПОДТВ — там же].
- **`connect()` жив** в 2026 и принимает произвольный hostname:port (`secureTransport: off|on|starttls`) [ПОДТВ — [tcp-sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)]. То есть «Cloudflare блокирует не-CF порты на выход» в текущей документации **не подтверждается**.
- Ограничения на число одновременных WS и их idle-таймаут в докладе Workers не нашёл **[НЕПРОВЕРЕНО]**.
- Исходящие TCP-соединения Workers идут **из префикса, не входящего в публичные диапазоны Cloudflare** [ПОДТВ — там же] — это важно для репутации бэкенда.

### 3.3. ToS: прямой запрет, действующий с 03.12.2024

Текст (Self-Serve Subscription Agreement, §2.2.1 «Restrictions», пункт **(j)**):

> **«(j) use the Services to provide a virtual private network or other similar proxy services.»**
> [ПОДТВ — [cloudflare.com/terms](https://www.cloudflare.com/terms/), прочитано 06.10.2026]

Рядом — пункт **(b)**, запрещающий отправлять трафик проксируемого домена на IP, **не назначенный Cloudflare** (это про «优选IP» / выбор лучшего IP Cloudflare для прокси) [ПОДТВ — там же]. Разбор изменения от **03.12.2024** с указанием именно на Workers/Pages-прокси и «优选IP»: [landian.news, 16.12.2024](https://www.landian.news/archives/107113.html) **[ПОДТВ]**. По §8 нарушение = **потеря лицензии на использование сервисов**, Cloudflare оставляет за собой право расследования [ПОДТВ — там же].

**Подтверждённые случаи блокировок.** Строго «первоисточника с скриншотом бана» я не нашёл — это честная оговорка. Что есть:
- жалоба владельца Worker-VLESS на abuse-разбирательство: [nodeloc.com/t/topic/73948](https://www.nodeloc.com/t/topic/73948) **[ЗАЯВЛ]**;
- публичный отчёт о бане edgetunnel **на Render** (не CF): [zizifn/edgetunnel#116](https://github.com/zizifn/edgetunnel/issues/116) **[ЗАЯВЛ]**;
- обсуждение «Cloudflare: Free-план — это вы так используете?» с предупреждениями о бане: [linux.do/t/topic/1410841](https://linux.do/t/topic/1410841) **[ЗАЯВЛ]**;
- массовые сообщения о том, что при превышении лимитов прилетает 1102/троттлинг, а при abuse — снос деплоя **[ЗАЯВЛ]**.

**Оценка риска.** Для **бесплатного** аккаунта Cloudflare цена бана нулевая, поэтому схема «Workers как вход» технически воспроизводима, но: (1) это осознанное нарушение ToS; (2) abuse-жалоба сносит вход вместе с аккаунтом; (3) **для РФ это ещё и бесполезно на мобильных** — ASN Cloudflare в измеренных белых списках отсутствует (см. смежный отчёт `.research/entry-points-ru.md`), а с августа 2026 РКН давит на Cloudflare DNS/DoH [ЗАЯВЛ — [cisoclub](https://cisoclub.ru/roskomnadzor-vzjalsja-za-zashifrovannyj-dns-google-i-cloudflare/)]. **Не рекомендуется как основной вход для платного сервиса.**

---

## 4. Бесплатные туннели с публичным HTTPS

| Сервис | Стабильный URL? | TLS | WebSocket | Лимиты free | Вердикт |
|---|---|---|---|---|---|
| **cloudflared quick tunnel** (`*.trycloudflare.com`) | **Нет** — «The hostname changes each time you create a Quick Tunnel», аккаунт/домен не нужен | Да (CF) | Работает на практике [ЗАЯВЛ]; в докладе не заявлен явно | «no uptime guarantee», **до 200 in-flight запросов** (иначе 429), **нет SSE**, для продакшена не предназначен [ПОДТВ — [Quick Tunnels, ред. 30.09.2026](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)] | **Только тесты/аварийный доступ.** Плюс ToS-запрет прокси (§3.3) |
| **ngrok free** | **ДА — один «dev domain» на аккаунт**, `your-assigned-name.ngrok-free.app`, до 3 эндпоинтов [ПОДТВ — [free-plan-limits](https://ngrok.com/docs/pricing-limits/free-plan-limits/)] | Да, авто-TLS; **raw TLS-эндпоинты на free недоступны** | Да | **1 ГБ/мес** трафика наружу, 20 000 HTTP-запросов/мес, 5 000 TCP-соединений/мес, 4 000 req/мин, 100 TCP-conn/мин; **эндпоинты без таймаута** — могут жить бесконечно [ПОДТВ — там же] | **Лучший из туннелей, но 1 ГБ/мес убивает идею платного сервиса.** Интерстишл только для HTML-браузерного трафика и не мешает API-клиентам (обход — заголовок `ngrok-skip-browser-warning` или нестандартный User-Agent) [ПОДТВ] |
| **localtunnel / loca.lt** | Нет (поддомен по запросу, но не гарантирован) | Да | Да | — | **Плохо**: `bar.loca.lt` не разрешился в моём замере [ИЗМ]; interstitial «click to continue» ломает не-браузерных клиентов [ЗАЯВЛ]. Только тесты |
| **serveo.net** | Нет надёжно | Да | Да | — | Живой (HTTP 200, IP 37.27.37.254 — Hetzner) [ИЗМ]; история падений; только тесты |
| **bore.pub** | Нет — случайный порт | **Нет** (сырой TCP, TLS приносите свой) | Да (это TCP) | best-effort, без SLA | **Хорош как приватный TCP-релей** (или self-hosted `bore`); для публичного сервиса не годится |
| **pinggy.io** | Нет (free — 60-мин туннели) | Да | [НЕПРОВЕРЕНО] | 60 мин на туннель [ЗАЯВЛ — [freetier.co](https://freetier.co/directory/products/pinggy)] | Только тесты |
| **zrok.io** | Да (reserved public share на free) [НЕПРОВЕРЕНО по актуальным лимитам] | Да | Да (TCP/HTTP) | Лимиты в [docs/limits.md](https://github.com/openziti/zrok/blob/main/website/docs/self-hosting/metrics-and-limits/limits.md); проект **open-source и self-hostable** | Единственный из туннелей, у которого есть **легальный self-hosted путь** — упомянуть как вариант |
| **localhost.run** | Нет (кастомный поддомен — платно) | Да | Да | — | Только тесты |

**Ключевой вывод по §4.** Бесплатные туннели годятся **только для отладки и аварийного доступа**: у всех либо меняющийся URL, либо лимит трафика (1 ГБ/мес у ngrok), либо отсутствие TLS (bore). Ни один нельзя положить в основу платного сервиса. Плюс все они — зарубежные ASN, которых нет в белых списках РФ.

---

## 5. Поддомены от хостеров VPS + выпуск LE-сертификата

| Хостер | Дефолтный хостнейм | В PSL? | LE-сертификат | Комментарий |
|---|---|---|---|---|
| **Hetzner** | **Да**: PTR по умолчанию `static.<ip-через-дефис>.clients.your-server.de` — проверено мной: `5.9.0.1 → static.1.0.9.5.clients.your-server.de`, `88.198.0.1 → static.88-198-0-1.clients.your-server.de` [ИЗМ] | **НЕТ** (`your-server.de`, `clients.your-server.de` отсутствуют) [ИЗМ] | Только HTTP-01 (DNS-01 невозможен — зоны у вас нет). **Лимит 50 сертификатов/нед делится между ВСЕМИ клиентами Hetzner** → на практике вероятен `too many certificates already issued for your-server.de` [НЕПРОВЕРЕНО фактически, но следует из PSL+лимитов] | Не полагаться |
| **Aeza (aeza.net)** | PTR/хостнейм в панели настраивается; массового дефолтного `*.aeza.net` не подтверждено [НЕПРОВЕРЕНО] | aeza.net не в PSL [ИЗМ] | Теоретически HTTP-01, если PTR = ваш IP и порт 80 открыт | Требует проверки на живом VPS |
| **VDSina (vdsina.ru)** | Есть публичный API ([docs](https://vdsina.ru/files/docs/public_api.pdf)); дефолтный хостнейм не подтверждён [НЕПРОВЕРЕНО] | не в PSL [ИЗМ] | То же | То же |
| **Timeweb / Selectel / прочие РФ** | Аналогично [НЕПРОВЕРЕНО] | не в PSL [ИЗМ] | То же | То же |
| **reg.ru** | — | — | — | **Физлицо обязано пройти идентификацию**; после 01.09.2026 — ЕСИА/Госуслуги (§0.1) [ПОДТВ]. Как «способ без паспорта» не рассматривается |

**Практический вывод по §5.** Схема «взять хостнейм хостера и выпустить на него LE» **технически возможна только через HTTP-01** (DNS-01 закрыт, т.к. зона хостера вам недоступна), и почти всегда упирается в **общий лимит registered domain** (все хостерские домены вне PSL). **Если у вас есть VPS с публичным IP — LE IP-сертификат (§0.2) строго лучше: он даёт личный лимит 50/нед и не зависит от чужой зоны.** См. §8, рекомендация №2.

### 5.3. Можно ли купить домен без паспорта вообще
- В зонах **.ru/.рф/.su** — нет: ЕСИА-идентификация с 01.09.2026 [ПОДТВ].
- Внешние зоны формально без ЕСИА, но российские регистраторы всё равно запрашивают паспортные данные у физлиц; приватные регистраторы (Njalla и подобные) и оплата криптой — **[НЕПРОВЕРЕНО в этом исследовании]**, отдельная проверка не проводилась. Практический риск: такой домен всё равно палится как «домен VPN-сервиса» и требует KYC/оплаты.

---

## 6. Протоколы, которым домен не нужен вообще

| Протокол | Домен? | Сертификат? | Транспорт | Статус в РФ 2026 | Источник |
|---|---|---|---|---|---|
| **VLESS + XTLS-Reality** | **НЕТ** (используется чужой SNI через `dest`/`serverNames`) | **НЕТ** (украденный handshake) | TCP:443 | **Основной рабочий вариант**: «VLESS+Reality (vision) — ✅ десктоп/телефон, mux не нужен» [ЗАЯВЛ — [amir-shaman/vless-vpn-russia, 2026](https://github.com/amir-shaman/vless-vpn-russia/blob/main/docs/findings.md)]. Но: с февраля 2026 массовые сбои, пик — июнь 2026; ТСПУ перешёл на **поведенческую фильтрацию** (отпечатки TLS, число параллельных коннектов, подсети) [ПОДТВ по публикации — [Anti-Malware.ru, 15.06.2026](https://www.anti-malware.ru/news/2026-06-15-111332/50358)]. Уточнение из профильного форума: это **частичная блокировка TLS**, а не прицельно Reality [ЗАЯВЛ — [NTC](https://evgen-dev.ddns.net/t/%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-vless-xtls-rprx-vision-reality-%D0%B2-%D1%80%D0%BE%D1%81%D1%81%D0%B8%D0%B8-%D0%BD%D0%B5%D1%82-%D1%87%D0%B0%D1%81%D1%82%D0%B8%D1%87%D0%BD%D0%B0%D1%8F-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-tls/16061)] |
| Reality: уязвимость с утечкой `dest` | — | — | — | Обсуждается деанонимизация IP входа через форвардинг неавторизованных соединений на `dest` [ЗАЯВЛ — [NTC-тред «Reality dest IP leak»](https://ntc.rkn.quest/t/reality-dest-ip-leak-%D1%82%D0%B5%D0%BE%D1%80%D0%B8%D1%8F-%D0%B8-%D0%BF%D0%BE%D0%B4%D1%82%D0%B2%D0%B5%D1%80%D0%B6%D0%B4%D0%B5%D0%BD%D0%B8%D0%B5/24486/3); отчёт [VLESS-cracker, 11.05.2026](https://github.com/Anonymous376c1d0cf28/VLESS-cracker/blob/main/2026-05-11-%E6%BC%8F%E6%B4%9E%E6%8A%A5%E5%91%8A%E6%AD%A3%E6%96%87.md)]. **Исправлено ли и в какой версии Xray — [НЕПРОВЕРЕНО]** |
| **Shadowsocks-2022** | НЕТ | НЕТ | TCP/UDP | Есть сообщения «не работает Shadowsocks через мобильный интернет» [ЗАЯВЛ — [NTC-тред](https://ntc.rkn.quest/t/%D0%BD%D0%B5-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D0%B5%D1%82-shadowsocks-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-%D0%BC%D0%BE%D0%B1%D0%B8%D0%BB%D1%8C%D0%BD%D1%8B%D0%B9-%D0%B8%D0%BD%D1%82%D0%B5%D1%80%D0%BD%D0%B5%D1%82/16170)]; устойчивость к активному зондированию сохраняется [НЕПРОВЕРЕНО] |
| **mieru** | НЕТ | НЕТ | TCP+UDP с паддингом | Активно развивается (v3.33) [ПОДТВ — [pkg.go.dev](https://pkg.go.dev/github.com/enfein/mieru/v3@v3.33.0)]; поведение в РФ [НЕПРОВЕРЕНО] |
| **Hysteria2 / TUIC (QUIC/UDP)** | НЕТ | self-signed + `insecure` или masquerade | UDP | **Под белыми списками бесполезен**: UDP фильтруется по порту, «Hysteria2 и любой UDP-транспорт не поднимается ни с каким релеем» [ИЗМ/ПОДТВ — смежный отчёт `.research/entry-points-ru.md`] |
| **WireGuard / AmneziaWG** | НЕТ | НЕТ (ключи) | UDP | WG давно фингерпринтится; AmneziaWG-обфускация — статус в 2026 [НЕПРОВЕРЕНО в этом исследовании] |
| **SSH-туннели** | НЕТ | НЕТ | TCP:22/любой | Пригодно как аварийный канал; DPI-статус SSH в 2026 [НЕПРОВЕРЕНО] |
| **Trojan / NaiveProxy / AnyTLS** | **ДА, реальный домен** | **ДА** | TCP | Не подходят под задачу |
| **ShadowTLS v3** | Чужой домен для handshake (свой не нужен) | НЕТ для сервера | TCP | Доступность и статус в РФ [НЕПРОВЕРЕНО] |
| **XHTTP (Xray)** | НЕТ (работает поверх HTTP/HTTPS) | Нужен только если используется TLS-терминация у вас; за CDN — сертификат CDN | TCP | Новый транспорт 2025–2026; статус в РФ [НЕПРОВЕРЕНО] |

**Ключевой вывод по §6.** Два класса решений требуют **ноль доменов**: (1) **Reality** — маскировка под чужой сайт, лучшая стойкость, но в 2026 под поведенческим DPI деградирует; (2) **LE IP-сертификат + обычный TLS** (§0.2) — «честный» TLS без домена, но с отличимым признаком «SNI пуст/IP», что для активного зондирования хуже Reality. Оптимум — держать **оба** на одном сервере на разных портах.

---

## 7. Порты Cloudflare в 2026 и применимость в РФ

Официальная документация, **ред. 20.04.2026** [ПОДТВ — [Network ports](https://developers.cloudflare.com/fundamentals/reference/network-ports/)]:

- **HTTP:** 80, **8080, 8880**, 2052, 2082, 2086, 2095
- **HTTPS:** 443, **2053, 2083, 2087, 2096, 8443**
- Кэширование отключено на: 2052, 2053, 2082, 2083, 2086, 2087, 2095, 2096, 8880, 8443 (на Enterprise включается cache rule)
- Порты **80 и 443 — единственные**, совместимые с China Network (для нас неактуально)
- **Важно:** WAF Managed Ruleset содержит правило, блокирующее HTTP/HTTPS-запросы **на нестандартных портах** на L7 — то есть при включённом managed ruleset схема «VLESS-WS на 2053/2083/8443» может резаться самим Cloudflare [ПОДТВ — там же]. **Практический вывод: фронт держать на 443.**

**Применимость в РФ 2026 [НЕПРОВЕРЕНО/частично]:** список портов Cloudflare не имеет отношения к тому, пропускает ли их ТСПУ. По измерениям смежного отчёта фильтрация идёт **по IP назначения** (гранулярность /24), SNI — не whitelisted-признак, порт 80 проходит наравне с 443; **ASN Cloudflare в измеренном белом пространстве отсутствует**, то есть на мобильных в режиме белых списков вход через Cloudflare (любой порт) **не работает**, на домашнем Wi-Fi — работает [ИЗМ — `.research/entry-points-ru.md`]. Отдельно: с 17–23.08.2026 в РФ фиксировались ограничения DoH/DNS (Google, Cloudflare) [ЗАЯВЛ — [cisoclub](https://cisoclub.ru/roskomnadzor-vzjalsja-za-zashifrovannyj-dns-google-i-cloudflare/)]. **Даты/актуальность по портам: подтверждено на 20.04.2026; отдельных подтверждений блокировки именно портов 2053/2083/2087/2096/8443 в РФ на 06.10.2026 не найдено [НЕПРОВЕРЕНО].**

---

## 8. Итоговая таблица «вариант → что даёт → срок → риски»

| Вариант | Что даёт | Паспорт/домен | Срок | Стабильность | Риски |
|---|---|---|---|---|---|
| **LE IP-сертификат на VPS** | Публично доверенный TLS на IP, wildcard не нужен, личный лимит 50/нед | Нет / нет | 10–30 мин настройки + авто-renew каждые ~5 дней | Высокая (зависит только от LE и вашего IP) | Только профиль `shortlived` (160 ч); SNI пустой → отличимый признак; nginx/apache-плагины Certbot IP не умеют |
| **VLESS+Reality (`dest` чужого домена)** | Ноль доменов, ноль сертификатов, лучшая маскировка | Нет / нет | 15–30 мин | Средняя: с февраля 2026 массовые сбои, пик июнь 2026 | Поведенческий DPI; заявленная утечка `dest` [ЗАЯВЛ]; нужен «чистый» IP |
| **Reality на узле внутри РФ (фолбэк)** | Связь на слабом мобильном | Нет / нет | 15 мин | Средняя | Зарубежные сайты не открывает (YouTube), только мессенджеры |
| **deSEC/dedyn.io + DNS-01** | Настоящий DNS-API + wildcard-сертификат, свой лимит LE | Нет / нет | 5–15 мин | Высокая | Домен на чужой зоне; ToS про VPN [НЕПРОВЕРЕНО]; палится как бесплатный поддомен |
| **DuckDNS + DNS-01** | То же, проще | Нет / нет | 5 мин | Высокая | То же; DDNS-паттерн в имени |
| **sslip.io/nip.io/traefik.me + HTTP-01** | Хостнейм из IP, TLS за минуты | Нет / нет | 10 мин | Средняя | **Лимит LE общий на весь домен** (не в PSL); wildcard невозможен; сервис может закрыться |
| **afraid.org shared-домен** | Хостнейм + DNS-API (TXT) | Нет / нет | минуты | Средняя | Shared-домен не в PSL → лимит на всех; риск бана за абуз |
| **eu.org** | Полноценный домен бесплатно | Нет / нет | **месяцы** | Высокая после одобрения | Долгая модерация, могут отказать |
| **Cloudflare Workers (VLESS-WS)** | Фронт + релей в одном, TLS от CF, 100k req/сутки | Нет / нет | 15 мин | Технически высокая | **ToS §2.2.1(j) запрещает VPN/прокси**; abuse → снос; CPU 10 мс → 1102; ASN CF нет в белых списках РФ |
| **cloudflared quick tunnel** | HTTPS-URL за секунды | Нет / нет | 1 мин | Низкая | URL меняется при каждом старте; 200 in-flight; нет SSE; ToS-запрет прокси |
| **ngrok free** | **Стабильный** `*.ngrok-free.app`, TLS, WS, без таймаута | Нет / нет | 5 мин | Средняя | **1 ГБ/мес**, 20k запросов/мес; raw TLS недоступен; домен широко известен/блокируем |
| **bore.pub / self-hosted zrok** | Сырой TCP-релей | Нет / нет | 5 мин / часы | Средняя | У bore нет TLS и случайный порт; zrok требует своего хоста |
| **Хостнейм хостера (Hetzner PTR и др.)** | Бесплатное имя под ваш IP | Нет / нет | 0 мин | Низкая для LE | Домен хостера не в PSL → общий лимит; DNS-01 недоступен; нужен HTTP-01 |
| **AmneziaWG / mieru (UDP)** | Ноль доменов, ноль сертификатов | Нет / нет | 20 мин | Низкая в РФ | UDP режется под белыми списками; WG фингерпринтится |

---

## 9. Топ-3 рекомендации для нас (вход без покупки домена)

### №1. Основной вход: VLESS + XTLS-Reality на своём VPS, домен не покупаем вообще
Чужой `serverNames` (`dl.google.com`/`www.microsoft.com`), `flow=xtls-rprx-vision`, **только порт 443**, 4+ SNI на inbound для ротации, «чистый» IP (не из засвеченных диапазонов), BBR. Домена и сертификата не требуется. Резервы: второй inbound с TLS на нестандартном порту + client-side mux 8 (по замерам сообщества это то, что вытягивает, когда давят по числу TLS-коннектов), и узел внутри РФ как «спасательный круг» для мобильных. **Почему:** это единственная схема, которая в 2026 даёт ноль доменов, ноль сертификатов и максимальную стойкость, и она подтверждена живыми тестами сообщества.

### №2. Легальный TLS без домена: Let's Encrypt IP-сертификат (`--preferred-profile shortlived --ip-address`)
Certbot 5.3+, режим `webroot`/`standalone`, `--deploy-hook` для перезагрузки Xray/sing-box/nginx, авто-renew (срок 160 ч — обновлять ежедневно кроном). Даёт **публично доверенный TLS на голом IP** с **личным лимитом 50 сертификатов/неделю** — то есть не зависит ни от чужой зоны, ни от PSL, ни от хостера. Использовать как: (а) второй/третий inbound для клиентов, которые не умеют Reality, (б) страховку на случай, если Reality начнут душить сильнее. Учесть: SNI пустой → признак для активного зондирования, поэтому только как второй контур.

### №3. Для тестов и аварийного доступа: ngrok free (стабильный dev-домен) + быстрый `cloudflared` — и ничего продакшн-критичного на бесплатных PaaS
ngrok free в 2026 даёт **один постоянный `*.ngrok-free.app`** с авто-TLS, WebSocket и без таймаута эндпоинта — это лучший бесплатный «валидный TLS+WS URL» для пилота и демо. Но 1 ГБ/мес и 20k запросов/мес означают, что платный сервис на нём не построить, а **Cloudflare Workers/Pages мы не используем как вход**: ToS §2.2.1(j) прямо запрещает VPN/прокси (действует с 03.12.2024), есть abuse-кейсы, CPU-лимит 10 мс даёт 1102, и — главное для РФ — **ASN Cloudflare отсутствует в белых списках, то есть на мобильных вход всё равно не работает**. Если очень нужен бесплатный фронт для демо — держать его вне критического пути и не смешивать с боевым аккаунтом.

**Чего НЕ делать:** не строить вход на `1.2.3.4.sslip.io` как на основном имени (лимит LE общий на весь sslip.io, домен вне PSL, PR в PSL закрыт без мержа); не использовать хостнейм хостера (`*.your-server.de` и аналоги) как имя для сертификата (общий лимит на всех клиентов хостера, DNS-01 недоступен); не рассчитывать на бесплатные PaaS (Render уже банит, Glitch мёртв, Fly/Railway/Koyeb — платные или закрыты для новых).

---

## 10. Что я проверил лично [ИЗМ] и что осталось непроверенным

**Проверено мной 06.10.2026:**
1. Скачан и проанализирован Public Suffix List (16 501 строка) — вхождение/отсутствие 40+ доменов (§0.3).
2. Живые DNS-запросы: sslip.io/nip.io/traefik.me (`1.2.3.4 → 1.2.3.4`), duckdns/pp.ua/us.kg/eu.org/dedyn.io, туннели (serveo, bore.pub, localhost.run, pinggy, zrok, loca.lt).
3. CAA-записи для 11 доменов — все пустые (нет запрета на выпуск).
4. PTR-записи Hetzner (`static.*.clients.your-server.de`) — фактическая проверка.
5. HTTP-доступность сайтов провайдеров (sslip.io, nip.io, traefik.me, serveo.net, pinggy.io, zrok.io, localhost.run, niс.eu.org, pp.ua, desec.io).
6. Документация вендоров, прочитанная напрямую: LE profiles/rate-limits/challenge-types (IP-сертификаты, профиль `shortlived`), LE ACME directory (профили `classic`/`shortlived`/`tlsserver` реально анонсируются), Cloudflare Network ports (ред. 20.04.2026), Cloudflare Workers limits и `cloudflare:sockets`, Cloudflare ToS §2.2.1, Cloudflare Quick Tunnels (ред. 30.09.2026), ngrok free-plan limits, Vercel WebSockets (ред. 10.08.2026), Render AUP (ред. 22.08.2025), afraid.org FAQ, DuckDNS HTTP API spec.
7. GitHub API: sslip.io issue #57 (2024, rate limit «too many certificates already issued for sslip.io», заявка на оверрайд 3–5k → 5–10k/нед), PSL PR #2206 (закрыт, не смержен).

**Не проверено (явные пробелы):**
- Действующий размер оверрайда LE для sslip.io в 2026 и жив ли он.
- Статус `nic.us.kg` (лендинг не отдал контент) и текущие правила pp.ua/eu.org (сроки модерации).
- Точные лимиты Deno Deploy 2026 и его AUP в части прокси (страница не отдалась).
- Наличие дефолтных хостнеймов и практика выпуска LE у Aeza/VDSina/Timeweb (нужен живой VPS).
- Исправлена ли уязвимость Reality с форвардингом на `dest` и в какой версии Xray.
- Блокируются ли порты 2053/2083/2087/2096/8443 в РФ именно как порты (отдельных подтверждений нет).
- Статус AmneziaWG/mieru/SSH в РФ на октябрь 2026.
- Возможность покупки домена без KYC у приватных регистраторов (Njalla и др.).

---

## 11. Источники

**Let's Encrypt:** [IP- и 6-дневные сертификаты GA, 15.01.2026](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) · [Certbot и IP-сертификаты, 11.03.2026](https://letsencrypt.org/2026/03/11/shorter-certs-certbot) · [Profiles, ред. 04.10.2026](https://letsencrypt.org/docs/profiles/) · [Rate Limits](https://letsencrypt.org/docs/rate-limits/) · [Challenge Types](https://letsencrypt.org/docs/challenge-types/) · ACME directory `acme-v02.api.letsencrypt.org/directory`
**PSL/sslip.io:** [public_suffix_list.dat](https://publicsuffix.org/list/public_suffix_list.dat) · [sslip.io issue #57](https://github.com/cunnie/sslip.io/issues/57) · [PSL PR #2206](https://github.com/publicsuffix/list/pulls/2206) · [PSL issue #88 «Add FreeDNS domains»](https://github.com/publicsuffix/list/issues/88) · [sslip.io docs/wildcard.md](https://github.com/tigefa4u/sslip.io/blob/main/docs/wildcard.md)
**Домен и РФ:** [ТЦИ: идентификация администраторов .ru/.рф/.su через ЕСИА с 01.09.2026, 24.07.2026](https://tcinet.ru/press-centre/news/7862/) · [cctld.ru: 100 000 администраторов прошли идентификацию](https://cctld.ru/en/media/news/kc/40134/)
**Cloudflare:** [Network ports, ред. 20.04.2026](https://developers.cloudflare.com/fundamentals/reference/network-ports/) · [Self-Serve Subscription Agreement §2.2.1](https://www.cloudflare.com/terms/) · [Workers limits](https://developers.cloudflare.com/workers/platform/limits/) · [Workers TCP sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/) · [Quick Tunnels, ред. 30.09.2026](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) · [landian.news о смене ToS, 16.12.2024](https://www.landian.news/archives/107113.html) · [nodeloc: abuse на Worker-VLESS](https://www.nodeloc.com/t/topic/73948)
**Туннели/хостинги:** [ngrok free plan limits](https://ngrok.com/docs/pricing-limits/free-plan-limits/) · [Render AUP, ред. 22.08.2025](https://render.com/acceptable-use) · [zizifn/edgetunnel #116 (бан на Render)](https://github.com/zizifn/edgetunnel/issues/116) · [zrok limits](https://github.com/openziti/zrok/blob/main/website/docs/self-hosting/metrics-and-limits/limits.md) · [Glitch: закрытие](https://blog.glitch.com/post/goodbye-glitch) · [Fly.io: Discontinued Plans](https://flyio-landing.fly.dev/docs/about/discontinued-plans/) · [Railway Free Trial](https://docs.railway.com/pricing/free-trial) · [Koyeb Starter закрыт для новых](https://dev.to/build996/koyebs-starter-plan-closed-to-new-signups-kept-for-existing-orgs-30mh) · [Vercel WebSockets, ред. 10.08.2026](https://vercel.com/docs/functions/websockets) · [Koyeb TCP Proxy](https://www.koyeb.com/blog/tcp-proxy-expose-tcp-publicly)
**Reality и РФ-2026:** [Anti-Malware.ru: «VLESS+REALITY больше не магия: ТСПУ бьёт по поведению», 15.06.2026](https://www.anti-malware.ru/news/2026-06-15-111332/50358) · [NTC: Reality dest IP leak](https://ntc.rkn.quest/t/reality-dest-ip-leak-%D1%82%D0%B5%D0%BE%D1%80%D0%B8%D1%8F-%D0%B8-%D0%BF%D0%BE%D0%B4%D1%82%D0%B2%D0%B5%D1%80%D0%B6%D0%B4%D0%B5%D0%BD%D0%B8%D0%B5/24486/3) · [NTC: блокировка Reality? (частичная блокировка TLS)](https://evgen-dev.ddns.net/t/%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-vless-xtls-rprx-vision-reality-%D0%B2-%D1%80%D0%BE%D1%81%D1%81%D0%B8%D0%B8-%D0%BD%D0%B5%D1%82-%D1%87%D0%B0%D1%81%D1%82%D0%B8%D1%87%D0%BD%D0%B0%D1%8F-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-tls/16061) · [amir-shaman/vless-vpn-russia: findings.md (2026)](https://github.com/amir-shaman/vless-vpn-russia/blob/main/docs/findings.md) · [NTC: Shadowsocks не работает через мобильный](https://ntc.rkn.quest/t/%D0%BD%D0%B5-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D0%B5%D1%82-shadowsocks-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-%D0%BC%D0%BE%D0%B1%D0%B8%D0%BB%D1%8C%D0%BD%D1%8B%D0%B9-%D0%B8%D0%BD%D1%82%D0%B5%D1%80%D0%BD%D0%B5%D1%82/16170) · [cisoclub: РКН и шифрованный DNS Google/Cloudflare](https://cisoclub.ru/roskomnadzor-vzjalsja-za-zashifrovannyj-dns-google-i-cloudflare/)
**Внутренние:** `.research/entry-points-ru.md` (белые списки, ASN Cloudflare отсутствует, UDP под белыми списками), `.research/whitelist-mechanics.md`, `.research/market-remnawave.md`
