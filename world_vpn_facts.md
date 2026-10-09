# Мировые эталонные VPN: функции и политики (проверяемые факты)

Сбор 2026-10-08, официальные сайты вендоров. M = Mullvad, P = Proton VPN, S = Surfshark.

| Функция | M | P | S | URL |
|---|---|---|---|---|
| Лимит устройств | **5**, больше нельзя — второй аккаунт или VPN на роутере | **Free — 1**; **Plus/Unlimited/Duo/Family — до 10** | **Без лимита** (Starter, One, One+) | [M FAQ](https://mullvad.net/en/help/faq) · [P](https://protonvpn.com/support/proton-vpn-plans) · [S](https://support.surfshark.com/hc/en-us/articles/360003069434) |
| Контроль лимита и превышение | Привязка к **устройству в аккаунте**; экран «**Too many devices**»: снять одно → «Continue with login» | По одновременным сессиям аккаунта; HWID нет | Не контролируется; оговорка против перепродажи | [M app](https://mullvad.net/en/help/using-mullvad-vpn-app) · [S](https://support.surfshark.com/hc/en-us/articles/360003069434) |
| Конфиги на устройство | **WireGuard-конфиги**, имена устройств в app → Account, [account/devices](https://mullvad.net/account/devices) | Ручные конфиги для роутеров и CLI | Manual setup: OpenVPN/WireGuard/IKEv2 | [M FAQ](https://mullvad.net/en/help/faq) · [P](https://protonvpn.com/support/download-and-installation/routers) |
| Автопродление | **Нет**: предоплаченный баланс, срок истекает | Есть (подписка / PayPal billing agreement) | Есть; отмена: Subscription → Payments → Cancel renewal | [M ToS](https://mullvad.net/en/help/terms-service) · [P](https://protonvpn.com/support/payment-options) · [S](https://support.surfshark.com/hc/en-us/articles/17673853278226) |
| Возврат | **14 дней**; не возвращают cash, крипту, App Store, ваучеры | **30 дней** money-back на платных | **30 дней** с первой покупки, не после продления | [M](https://mullvad.net/en/help/refunds) · [P](https://protonvpn.com/support/proton-vpn-plans) · [S](https://support.surfshark.com/hc/en-us/articles/360003103653) |
| Пробный период | **Нет** | **Free-план**: 10 стран, 1 устройство | **7 дней** на 12/24-мес. планах, в триале **до 3 устройств** | [M](https://mullvad.net/en/pricing) · [P](https://protonvpn.com/support/proton-vpn-plans) · [S](https://support.surfshark.com/hc/en-us/articles/360026123554) |
| Протоколы | **WireGuard** (+UDP-over-TCP), OpenVPN, **Bridge mode = Shadowsocks**, quantum-resistant tunnel | **WireGuard (UDP/TCP), OpenVPN (Linux GUI), IKEv2 (macOS), Smart Protocol, Stealth** | **WireGuard, OpenVPN, IKEv2, Dausos** (свой; только macOS) | [M](https://mullvad.net/en/help/faq) · [P](https://protonvpn.com/support/how-to-change-vpn-protocols) · [S](https://support.surfshark.com/hc/en-us/articles/360010324739) |
| Обход DPI | **DAITA** — защита от AI-guided traffic analysis (может идти через DAITA-сервер мультихопом) | **Stealth** — «bypass internet blocks by hiding your VPN connection» | **NoBorders** — работа при геоблокировках и госцензуре | [M](https://mullvad.net/en/vpn/daita) · [P](https://protonvpn.com/support/how-to-change-vpn-protocols) · [S](https://support.surfshark.com/hc/en-us/articles/10448039999122) |
| Локальные сервисы / split tunneling | **Split tunneling** + **Local network sharing** | **Split Tunneling** + **Allow LAN connections** (вкл. по умолчанию, платный) | **Bypasser**; на iOS/macOS **Invisible on LAN** блокирует локальную сеть | [M](https://mullvad.net/en/help/split-tunneling-with-the-mullvad-app) · [P](https://protonvpn.com/support/lan-connections) · [S](https://support.surfshark.com/hc/en-us/articles/10448039999122) · [LAN](https://support.surfshark.com/hc/en-us/articles/36162255549074) |
| Kill switch / DNS | Kill switch **всегда включён, отключить нельзя**; **Lockdown mode**; DNS-защита всегда включена | Kill switch / always-on VPN (Free и платные); DNS-защита заявлена | **Kill Switch** — отключаемый; DNS-защита заявлена; RAM-only серверы | [M](https://mullvad.net/en/help/faq) · [P](https://protonvpn.com/support/proton-vpn-plans) · [S](https://support.surfshark.com/hc/en-us/articles/360016978413) |
| Логирование | «Не храним логи активности никакого рода»; EU Data Retention Directive не применяется | Не логируют сайты, трафик, IP, длительность сессий, гео; Швейцария; в 2019 суд требовал логи — их не было | Не собирают IP, историю, сессии, bandwidth; сервер удаляет user ID/IP/время подключения **за 15 минут** | [M](https://mullvad.net/en/help/faq) · [P](https://protonvpn.com/support/no-logs-vpn) · [S](https://support.surfshark.com/hc/en-us/articles/360003068834) |
| Аудиты | App 2024/2026 (MASA), **X41 D-Sec** (API платежей, янв. 2026), **Assured** (web app окт. 2025; GotaTun март 2026), **Cure53** (июнь 2024) | **Securitum** — 5-й ежегодный аудит no-logs (2022–2026), отчёты публикуются | **SecuRing** — инфраструктура, дек. 2025; **Deloitte** — no-logs в 2023 и 2025 | [M](https://mullvad.net/en/blog/tag/audits) · [P](https://protonvpn.com/blog/no-logs-audit) · [S](https://surfshark.com/trust-center) |
| Локации / автовыбор / пинг | Страна/город/сервер, поиск, фильтр по владельцу; **загрузку серверов не показывают** | **20 000+ серверов, 140+ стран** (Plus) | **4500+ серверов, 100 стран**; «**Recommended for you**» | [M app](https://mullvad.net/en/help/using-mullvad-vpn-app) · [P](https://protonvpn.com/support/proton-vpn-plans) · [S](https://support.surfshark.com/hc/en-us/articles/360003069614) |
| Языки, поддержка, статус | 21 язык (русский есть); email, GPG, onion; статус-страницы нет | **35 языков** (русский, украинский); **24/7 live chat** (платные); status.proton.me | 13 языков; **24/7 live chat и email**; статус-страницы нет | [M](https://mullvad.net/en/help) · [P](https://protonvpn.com/support/live-chat-support) · [S](https://support.surfshark.com/hc/en-us/articles/360003069114) |
| Оплата / анонимность | **Наличные по почте** (9 валют), **BTC/BCH/Monero** (−10%), карта, SEPA, PayPal, Swish, ваучеры; аккаунт без email | Карта, PayPal, Apple/Google Pay, **Bitcoin**, **cash (CHF/USD/EUR)**, перевод; есть инструкция для РФ | Через сайт; партнёры **Paddle, Google Play, Cleverbridge** | [M](https://mullvad.net/en/pricing) · [P](https://protonvpn.com/support/payment-options) · [S](https://support.surfshark.com/hc/en-us/articles/360003103653) |
| «Удержание» | Семейных тарифов нет; **подписка на 10 лет** по той же цене; реселлерская программа | Планы **Duo/Family**, Unlimited, Business; партнёрская программа | **Refer & Earn**: друг держится ≥31 день → 1 месяц (месячный план) или **3 месяца** обоим, либо выплата на PayPal | [M](https://mullvad.net/en/pricing) · [P](https://protonvpn.com/support/proton-vpn-plans) · [S](https://support.surfshark.com/hc/en-us/articles/360013512279) |

## A) Клиент не передаёт идентификатор / не подключается

- **M:** идентичность — «устройство в аккаунте», не железо; слоты кончились → «**Too many devices**» → снять лишнее → **Continue with login**. Фолбэки: переключение методов доступа к API («API reachable / unreachable», кастомный через Shadowsocks/SOCKS5), **Server IP override**, **UDP-over-TCP**, MTU 1280, TCP/443. Диагностика: **Report a Problem → View app logs**. ([app](https://mullvad.net/en/help/using-mullvad-vpn-app), [FAQ](https://mullvad.net/en/help/faq))
- **P:** фолбэки — **Smart Protocol** (автовыбор по умолчанию) и **Stealth** при блокировках; есть статьи по ошибкам подключения. Диагностика — логи, **packet capture**, Report a bug. ([protocols](https://protonvpn.com/support/how-to-change-vpn-protocols))
- **S:** фолбэк — смена протокола + **NoBorders**. Диагностика: **Get Help → Troubleshooting tools → Collect diagnostics** (Windows), **Report a bug** (Android/macOS/iOS). ([diagnostics](https://support.surfshark.com/hc/en-us/articles/13412100532242))
- **Паттерн:** HWID не используется; при сбое — освободить слот, сменить протокол, собрать диагностику в приложении.

## B) Публичные SLA и компенсации за простой

- **P** — единственный с публичным SLA: ToS §5, доступность **99.95%**; при недоступности >0.05% в месяц кредит **по запросу**: 10% месячной стоимости (uptime 99.0–99.95%) или **30%** (<99.0%). ([terms](https://proton.me/legal/terms))
- **M** — SLA и компенсаций в ToS нет; после 14 дней «остаток времени может быть возвращён при некоторых условиях» по обращению в поддержку. ([ToS](https://mullvad.net/en/help/terms-service))
- **S** — в ToS нет ни SLA, ни uptime-гарантий, ни service credit (проверено по тексту). ([ToS](https://surfshark.com/terms-of-service))

## C) Раздельный роутинг локальных сервисов (банки/госуслуги)

Отдельной функции нет; используют два механизма:

1. **Исключение приложений из VPN:** M — *Split tunneling*, P — *Split Tunneling*, S — ***Bypasser***; исключённое приложение ходит с реальным IP. ([M](https://mullvad.net/en/help/split-tunneling-with-the-mullvad-app), [P](https://protonvpn.com/support/protonvpn-features/split-tunneling), [S](https://support.surfshark.com/hc/en-us/articles/10448039999122))
2. **Разрешение LAN-трафика:** M — *Local network sharing* (+ статические маршруты); P — *Allow LAN connections* (вкл. по умолчанию, платный) и *LAN devices by name*; S на iOS/macOS применяет *Invisible on LAN* и **блокирует** локальную сеть (защита от TunnelCrack/TunnelVision). ([M](https://mullvad.net/en/help/faq), [P](https://protonvpn.com/support/lan-connections), [S](https://support.surfshark.com/hc/en-us/articles/36162255549074))

## НЕ ПРОВЕРЕНО

- Поведение Proton при превышении лимита одновременных подключений (ошибка или вытеснение сессии).
- HWID / аппаратная привязка у любого из трёх.
- SLA и время ответа поддержки у Mullvad и Surfshark; их статус-страницы (status.mullvad.net, status.surfshark.com не отвечают).
- Gift-подписки, «заморозка» и «перенос устройства»; показ ping у Proton и Surfshark; семейные тарифы у Mullvad и Surfshark; автосписание у Proton по умолчанию; детали Dausos.
