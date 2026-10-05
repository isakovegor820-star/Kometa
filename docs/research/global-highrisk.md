# Международные посредники для VPN-подписки: MoR и high-risk эквайринг

**Дата проверки: 2026-10-05.** Контекст: Telegram-бот, чеки 199–1590 ₽, аудитория РФ, юрлица нет, основателю 15 лет.
Документ — «на будущее», когда появится юрлицо за рубежом. Все проценты, которых нет в официальном источнике, помечены «по запросу»: у high-risk ставки почти никогда не публикуются.
Где живая страница недоступна (антибот/404), цитата взята из архива web.archive.org — ссылка на архив указана явно.

## Сводная таблица

| Сервис | Тип | VPN/анонимайзеры | Юрлицо / РФ | Комиссии (публично) | Рекуррент + интеграция | Сложн. |
|---|---|---|---|---|---|---|
| Paddle | MoR | **Серая зона**: «VPN and Proxies (Restricted Category)» | РФ в unsupported countries → нет | 5% + $0.50 (сайт pricing), остальное по запросу | Да; hosted checkout, API, вебхуки | 2 |
| Lemon Squeezy | MoR | Не подтверждено; прямой формулировки про VPN нет, но «Services of any kind» запрещены | РФ нет (и как продавец, и как покупатель) | 5% + $0.50; +1.5% int'l; +1.5% PayPal; +0.5% подписки | Да; hosted checkout/Lemon.js, API, вебхуки | 1 |
| FastSpring | MoR | Не подтверждено (публичного списка с VPN нет) | РФ не подтверждено; юрлицо нужно | По запросу; «no minimum transaction volume» | Да; hosted/embedded checkout, API, вебхуки | 2 |
| PayPro Global | MoR | **Нет** (VPN назван прямо) | РФ не подтверждено; нужна регистрация компании | По запросу; выплаты на 15-е число | Да; checkout pages, API, вебхуки | 2 |
| Cleverbridge | MoR | Не подтверждено (в публичных политиках VPN не упомянут) | US/DE; РФ не подтверждено | По запросу | Да; enterprise-интеграция | 4 |
| 2Checkout / Verifone | MoR+PSP | Не подтверждено (в PPL VPN не указан; есть Remote Access Services) | РФ не подтверждено | По запросу | Да; hosted checkout, API | 3 |
| SegPay | High-risk эквайринг | Не подтверждено (публичный список недоступен, сайт под JS-защитой) | США/ЕС юрлицо; РФ не подтверждено | По запросу | Да; hosted-страница + API | 3 |
| CCBill | High-risk эквайринг | **Серая зона**: VPN в AUP не запрещён, но запрещены «shell accounts» и поддержка ботов | Юрлицо обязательно; РФ не подтверждено | По запросу (публично — только структура fee types) | Да; рекуррент — ядро продукта, hosted + API | 3 |
| Verotel | High-risk эквайринг | Не подтверждено (в AUP/договоре VPN не упомянут) | Любое юрлицо; заявка открыта всем | **15,5%** (BASIC, non-recurring) + EUR 500/год | Да; FlexPay API, hosted | 3 |
| Payop | PSP | **Нет** (плюс РФ в списке запрещённых стран) | РФ прямо запрещена | По запросу | Да; API | 3 |
| Unlimit | PSP/EMI | Не подтверждено (публичного restricted-списка не нашли) | UK/Cyprus юрлицо; РФ не подтверждено | По запросу | Да; API | 3 |
| TailoredPay | Брокер | **Да, заявляет**: «VPN Merchant Account» | Подбирает банк/эквайер под мерчанта | По запросу (контент за Cloudflare не читается) | Зависит от подобранного эквайера | 4 |

---

## MoR (Merchant of Record)

### 1. Paddle
1. **VPN:** серая зона — это разрешённая, но «усиленная» категория. В AUP: «System Health Products, including but not limited to: Device Cleaners, Antivirus, **VPN and Proxies (Restricted Category)**, Captcha Solving»; там же: «Due to the increased risk on certain offerings, Paddle must do enhanced due diligence». То есть не бан, но доп. проверка и право отказать. ([paddle.com AUP](https://www.paddle.com/help/start/intro-to-paddle/what-am-i-not-allowed-to-sell-on-paddle), Last Updated 13 April 2026)
2. **Комиссии:** публично на сайте — «5% + 50¢ per Checkout transaction» и «No monthly fees, migration fees, or hidden extras». Rolling reserve / chargeback fee не раскрыты → **по запросу**; setup fee отсутствует. ([paddle.com/pricing](https://www.paddle.com/pricing))
3. **Требования:** Paddle — MoR, работает с «software businesses anywhere in the world» **кроме** списка unsupported, где прямо указана **Russia** («Paddle is unable to support suppliers operating from the below countries», в списке: Russia, Belarus, Crimea, Donetsk, Luhansk, Kherson, Zaporizhzhia и др.). Значит РФ-резидент и РФ-юрлицо отпадают; нужна компания в поддерживаемой юрисдикции. VAT-номер не требуется — налоги Paddle берёт на себя. ([paddle.com supported countries](https://www.paddle.com/help/start/intro-to-paddle/which-countries-are-supported-by-paddle))
4. **Выплаты:** по расписанию (net-30/ежемесячно) на банковский счёт, валюта — USD/EUR/GBP и др.; минимальный порог публично не на странице AUP → **по запросу**. ([paddle.com/pricing](https://www.paddle.com/pricing))
5. **Рекуррент/интеграция:** да, подписки — основной сценарий; hosted checkout (overlay/inline), REST API, вебхуки. Сложность **2/5**. ([developer.paddle.com](https://developer.paddle.com/))

### 2. Lemon Squeezy
1. **VPN:** не подтверждено. Прямого запрета VPN в списке нет, но есть широкие формулировки: «Services of any kind (including marketing, design, web development, consulting or other related services)» и «Any products restricted by our payment processing partners». ([docs.lemonsqueezy.com prohibited products](https://docs.lemonsqueezy.com/help/getting-started/prohibited-products))
2. **Комиссии:** «$0.50 + 5% of total» + «+1.5% for international (outside of the US) transactions» + «+1.5% for PayPal transactions» + «+0.5% for subscription payments». Setup fee и rolling reserve не публикуются → **по запросу**. ([docs.lemonsqueezy.com fees](https://docs.lemonsqueezy.com/help/getting-started/fees))
3. **Требования:** РФ недоступна: в unsupported countries для покупок указана «Russian Federation», а выплаты возможны только в страны из списка банковских/ PayPal- payout-стран (РФ там нет; Армения и Казахстан — есть, Грузии и Кыргызстана — нет). VAT-номер не нужен — налоги на стороне MoR. ([docs.lemonsqueezy.com supported countries](https://docs.lemonsqueezy.com/help/getting-started/supported-countries))
4. **Выплаты:** Stripe-выплата бесплатна для US-счетов и «1% per payout for bank accounts outside the US»; PayPal — «$0.50 per payout» в США и «3% capped at $30 per payout» вне США. Сроки/минимум — в разделе Getting paid → **по запросу/смотри кабинет**. ([docs.lemonsqueezy.com fees](https://docs.lemonsqueezy.com/help/getting-started/fees))
5. **Рекуррент/интеграция:** да; hosted checkout, overlay, Lemon.js, REST API, вебхуки. Сложность **1/5**. ([docs.lemonsqueezy.com](https://docs.lemonsqueezy.com/))

### 3. FastSpring
1. **VPN:** не подтверждено. Публичной страницы с VPN-запретом найти не удалось (/acceptable-use-policy → 404, /legal/ без перечня категорий); FastSpring позиционирует себя как MoR для «SaaS, software, digital products, games, and AI companies». ([fastspring.com/llms.txt](https://fastspring.com/llms.txt), [fastspring.com/legal/](https://fastspring.com/legal/))
2. **Комиссии:** «FastSpring operates on a revenue-sharing model… charges a small commission from that transaction»; ставка обсуждается «during a meeting with an Account Executive» → **по запросу**. ([fastspring.com/pricing](https://fastspring.com/pricing))
3. **Требования:** нужен продавец с юрлицом; заявленный юридический контур — Bright Market, LLC (US), FastSpring Limited (UK), FastSpring B.V. (NL), SalesRight ULC (CA). Про РФ и VAT-номер публично не заявлено → **не подтверждено**. ([fastspring.com/legal/](https://fastspring.com/legal/))
4. **Выплаты:** комиссия вычитается из транзакции, выплата — на счёт продавца; частота/минимум/валюты публично не указаны → **по запросу**. ([fastspring.com/pricing](https://fastspring.com/pricing))
5. **Рекуррент/интеграция:** да; hosted checkout, Store Builder Library, API, вебхуки. Сложность **2/5**. ([developer.fastspring.com](https://developer.fastspring.com/))

### 4. PayPro Global
1. **VPN: нет.** В официальном Prohibited Product List: «Any product or service enabling consumers to **circumvent locks, programming codes or security features, or geographic or IP-based restrictions, including through usage of VPN, proxy or anonymous user facilities**». Это прямой запрет. ([PayPro Global Prohibited Product List, PDF](https://docs.payproglobal.com/documents/legal/prohibitedProducts.pdf))
2. **Комиссии:** ставка MoR публично не раскрыта → **по запросу**. Публично раскрыты только payout-комиссии: Payoneer $2, WebMoney 3.5% + $5, Wire Transfer $21, ACH $3, PayPal 2% (max $20). ([payproglobal.com/faq](https://payproglobal.com/faq/))
3. **Требования:** нужна зарегистрированная компания (в FAQ есть отдельный вопрос про регистрацию в Chamber of Commerce у партнёра PayPro.nl); про РФ прямо не сказано → **не подтверждено**. Налоги (VAT/GST) PayPro берёт на себя. ([payproglobal.com/faq](https://payproglobal.com/faq/))
4. **Выплаты:** «Payouts are processed on the 15th of every month. The amount of the payout matches the previous month's closing balance, minus the payout fees». ([payproglobal.com/faq](https://payproglobal.com/faq/))
5. **Рекуррент/интеграция:** да; checkout pages с шаблонизатором, API, вебхуки, поддержка подписок и лицензий. Сложность **2/5**. ([payproglobal.com/faq](https://payproglobal.com/faq/))

### 5. Cleverbridge
1. **VPN:** не подтверждено. В публичных документах — Acceptable Conduct Policy и Master Subscription Agreement — слова VPN/proxy/circumvent не встречаются вообще; перечня «запрещённых продуктов» в открытом виде нет. ([Acceptable Conduct Policy, PDF](https://grow.cleverbridge.com/hubfs/Acceptable%20Conduct%20Policy%20as%20of%20Sept%202022.pdf), [Master Subscription Agreement, PDF](https://grow.cleverbridge.com/hubfs/Cleverbridge%20Master%20Subscription%20Agreement.pdf))
2. **Комиссии:** публичного прайса нет → **по запросу**. ([cleverbridge.com pricing](https://www.cleverbridge.com/corporate/pricing/))
3. **Требования:** ориентирован на крупных software/SaaS-вендоров; юридический контур US (Cook County, Illinois) и Германия (Cologne) — по применимому праву в MSA. Про РФ и VAT-номер публично не заявлено → **не подтверждено**. ([Master Subscription Agreement, PDF](https://grow.cleverbridge.com/hubfs/Cleverbridge%20Master%20Subscription%20Agreement.pdf))
4. **Выплаты:** публично не раскрыты → **по запросу**.
5. **Рекуррент/интеграция:** да, но интеграция и онбординг ближе к enterprise-процессу (менеджер, согласование договора). Сложность **4/5**. ([cleverbridge.com](https://www.cleverbridge.com/))

### 6. 2Checkout / Verifone
1. **VPN:** не подтверждено. Их Prohibited Product List (PPL) прямо не называет VPN; ближайшие пункты — «Remote Access Services, Technical Support Services» и общая оговорка «your business practices, products and services may not be approved by 2Checkout regardless of whether they appear below». ([2Checkout Acceptance Policy / PPL (архив 2024)](https://web.archive.org/web/20240910215446/https://www.2checkout.com/legal/acceptance/))
2. **Комиссии:** публичного прайса нет → **по запросу**.
3. **Требования:** юрлицо, KYC; для high-risk вертикалей — согласование с risk-командой Verifone. Про РФ и VAT-номер публично не заявлено → **не подтверждено**. ([2Checkout Acceptance Policy (архив)](https://web.archive.org/web/20240910215446/https://www.2checkout.com/legal/acceptance/))
4. **Выплаты:** публично не раскрыты → **по запросу**.
5. **Рекуррент/интеграция:** да; hosted checkout, API, поддержка подписок и «2Subscribe». Сложность **3/5**. ([2checkout.com](https://www.2checkout.com/))

---

## High-risk эквайринг и PSP

### 7. SegPay
1. **VPN:** не подтверждено. Публичные страницы закрыты антибот-защитой (403 «Please enable JS and disable any ad blocker»), поэтому проверить AUP/restricted-список по первоисточнику не удалось. Косвенно: SegPay — профильный high-risk эквайер (adult, subscriptions) и публикует материалы про VPN-платежи. ([segpay.com](https://segpay.com/))
2. **Комиссии:** публично не раскрыты → **по запросу** (стандарт для high-risk).
3. **Требования:** юрлицо в US/EU-контуре, KYC/KYB; про РФ публично не заявлено → **не подтверждено**. ([gethelp.segpay.com](https://gethelp.segpay.com/))
4. **Выплаты:** публично не раскрыты → **по запросу**.
5. **Рекуррент/интеграция:** да, рекуррент — профильная функция; hosted-страница оплаты + API. Сложность **3/5**. ([segpay.com](https://segpay.com/))

### 8. CCBill
1. **VPN:** серая зона. В Merchant AUP VPN/анонимайзеры не запрещены; запрещены, в частности, «shell accounts»: «CCBill will not process orders for websites offering shell accounts. CCBill may cancel any accounts whose primary use can be determined as supporting the use of bots or any other programs executed on a server». Плюс общий запрет «Any website that is in violation of the card associations rules». ([CCBill Merchant AUP](https://ccbill.com/cs/client/policies/ccbill/acceptable_use.html), v.15; July 2026)
2. **Комиссии:** публичного прайса нет — на сайте опубликована только структура издержек (Interchange, Card Brand Assessments, Processor Markup, где markup зависит от «Business vertical (MCC), Acquiring bank region, Monthly transaction volume»). Rolling reserve/chargeback fee → **по запросу**. ([ccbill.com/pricing](https://ccbill.com/pricing))
3. **Требования:** юрлицо, KYB, сайт с понятным рекуррентом и раскрытием условий (AUP требует «clear disclosure of trial periods and/or recurring charges conspicuously on the website»). Про РФ публично не заявлено → **не подтверждено**. ([CCBill Merchant AUP](https://ccbill.com/cs/client/policies/ccbill/acceptable_use.html))
4. **Выплаты:** публично не раскрыты → **по запросу**; для high-risk типичны weekly/2-weekly settlement и holdback. ([ccbill.com](https://ccbill.com/))
5. **Рекуррент/интеграция:** да, «Subscription Processing» — отдельное ядро продукта; hosted payment page + REST API + вебхуки. Сложность **3/5**. ([ccbill.com](https://ccbill.com/))

### 9. Verotel (Yoursafe B.V., Амстердам)
1. **VPN:** не подтверждено. В Merchant Services Agreement и на публичных страницах VPN/proxy/circumvention не упоминаются; список restricted-категорий публично не выложен. ([Verotel Merchant Services Agreement](https://www.verotel.com/en/merchantagreement.html))
2. **Комиссии — единственный из списка с публичным прайсом:** BASIC — «Pricing non-recurring transactions **15,5%**» + «Annual registration fee EUR 500.00» (продление списывается, если объём ≥ EUR 100/нед.). PREMIUM — «Rates depending on volume», плюс «Weekly fee … EUR 25 / week (only if Weekly Volume is less than EUR 1000)». Rolling reserve/chargeback fee публично не раскрыты → **по запросу**. ([verotel.com products and pricing](https://www.verotel.com/en/productchoice.html))
3. **Требования:** заявка открыта для любого юрлица («Apply for an account»), PREMIUM требует истории: «No Premium merchants should provide 6 months of processing statements to open an account». Про РФ публично не заявлено → **не подтверждено**. ([verotel.com products and pricing](https://www.verotel.com/en/productchoice.html))
4. **Выплаты:** BASIC — без требования по обороту, PREMIUM — «Weekly processing requirement EUR 100 / week»; валюта EUR, счёт в Нидерландах. Минимум выплаты публично не указан → **по запросу**. ([verotel.com products and pricing](https://www.verotel.com/en/productchoice.html))
5. **Рекуррент/интеграция:** да; FlexPay, hosted-страница, API (есть публичная спецификация FlexPay API). Сложность **3/5**. ([verotel.com](https://www.verotel.com/))

### 10. Payop
1. **VPN: нет, и РФ тоже запрещена.** В Acceptable Use Policy в списке Prohibited Industries нет VPN прямым словом, но есть «Satellite and cable TV descramblers» и «Items which encourage or facilitate illegal activities»; отдельно прямо запрещена **Russia** в «Prohibited Countries and jurisdictions» («You may not use our services in case you or your payers are situated in certain countries», в списке: Russia, Belarus, USA, Taiwan и др.). ([payop.com acceptable use policy](https://payop.com/acceptable-use-policy/))
2. **Комиссии:** публично не раскрыты → **по запросу**.
3. **Требования:** юрлицо; оператор — Transferop Payment Gateway Ltd (Канада, FINTRAC MSB M22769088). РФ-резидент не подходит — юрисдикция в запрещённом списке. ([payop.com acceptable use policy](https://payop.com/acceptable-use-policy/))
4. **Выплаты:** публично не раскрыты → **по запросу**.
5. **Рекуррент/интеграция:** да; API-первый провайдер, документация на GitHub. Сложность **3/5**. ([Payop API docs](https://github.com/Payop/payop-api-doc))

### 11. Unlimit (ex-Cardpay)
1. **VPN:** не подтверждено. Публичные страницы `/acceptable-use-policy/` и `/prohibited-businesses/` отдают 404; в доступных Terms of Use и Legal-хабе VPN/анонимайзеры не упоминаются — restricted-список выдают только на этапе онбординга. ([unlimit.com/legal](https://www.unlimit.com/legal/))
2. **Комиссии:** публичного прайса нет → **по запросу**.
3. **Требования:** работают с UK/EU-юрлицами (Terms of Use governed by laws of the Republic of Cyprus), в ряде вертикалей требуют лицензию/регистрацию. Про РФ публично не заявлено → **не подтверждено**. ([unlimit.com/legal](https://www.unlimit.com/legal/))
4. **Выплаты:** публично не раскрыты → **по запросу**.
5. **Рекуррент/интеграция:** да, есть подписочный продукт и «Subscription and recurring payments»; REST API, hosted checkout. Сложность **3/5**. ([unlimit.com/payment-processing](https://www.unlimit.com/payment-processing/))

### 12. TailoredPay (брокер, не прямой эквайер)
1. **VPN: да, заявляют прямо.** Профильная страница называется «Get a VPN Merchant Account with TailoredPay». Важно: это **брокер** — он подбирает мерчанта под эквайера/банк, а не процессит сам. ([tailoredpay.com/vpn-merchant-account](https://tailoredpay.com/vpn-merchant-account/))
2. **Комиссии:** публично не раскрыты → **по запросу**; итоговая ставка/reserve зависят от подобранного банка.
3. **Требования:** определяет принимающий банк; типично нужно юрлицо (US/EU/offshore) и KYC. Про РФ публично не заявлено → **не подтверждено**.
4. **Выплаты:** определяет эквайер → **по запросу**.
5. **Рекуррент/интеграция:** зависит от выбранного эквайера, отдельного «продукта» у брокера нет. Сложность **4/5** (посредничество + согласование).
   ⚠️ Содержимое страницы не читается автоматически (Cloudflare «Sorry, you have been blocked») — факт существования страницы подтверждён поисковой выдачей и заголовком, но цитаты из тела страницы получить не удалось. ([tailoredpay.com/vpn-merchant-account](https://tailoredpay.com/vpn-merchant-account/))

---

## Ответы на 3 вопроса

**1. Есть ли хоть один MoR/PSP, который подключит физлицо или самозанятого из РФ?**
Нет — ни одного в этом списке. Все рассмотренные схемы требуют юрлица и KYB (CCBill AUP, Verotel «Apply for an account», 2Checkout PPL, PayPro Global FAQ), а РФ-присутствие либо прямо запрещено, либо невозможно технически: Paddle — «unable to support suppliers operating from the below countries… Russia» ([paddle.com](https://www.paddle.com/help/start/intro-to-paddle/which-countries-are-supported-by-paddle)); Lemon Squeezy — «Russian Federation» в unsupported countries для покупок ([lemonsqueezy](https://docs.lemonsqueezy.com/help/getting-started/supported-countries)); Payop — «Russia (the Russian Federation)» в Prohibited Countries ([payop.com](https://payop.com/acceptable-use-policy/)). Ни один не публикует путь «физлицо/самозанятый из РФ → подключение». Подтверждено: **нет.**

**2. Кто пускает микробизнес без истории оборота и без setup fee?**
- **FastSpring** — прямая цитата: «we have no minimum transaction volume»; setup fee не публикуется ([fastspring.com/pricing](https://fastspring.com/pricing)).
- **Lemon Squeezy** — setup fee нет, порог входа минимальный: «$0.50 + 5% of total» без ежемесячной платы ([fees](https://docs.lemonsqueezy.com/help/getting-started/fees)).
- **Verotel BASIC** — нет требования по обороту («Weekly processing requirement: None») и нет недельного fee, но есть «Annual registration fee EUR 500.00» — фактически это плата за вход, а не setup fee ([verotel.com](https://www.verotel.com/en/productchoice.html)).
- Остальные (Paddle, PayPro Global, Cleverbridge, 2Checkout, SegPay, CCBill, Payop, Unlimit, TailoredPay) либо требуют историю/объём, либо не раскрывают условия → **по запросу / не подтверждено**.

**3. Работает ли кто-то с юрлицом в Казахстане / Армении / Грузии / Кыргызстане для русскоязычного VPN?**
Прямых подтверждений нет — ни один из 12 не публикует политику «принимаем юрлицо в KZ/AM/GE/KG под VPN». Что проверяемо: Lemon Squeezy принимает банковские выплаты в **Армению и Казахстан**, но **Грузии и Кыргызстана в списке нет** ([supported countries](https://docs.lemonsqueezy.com/help/getting-started/supported-countries)); Paddle работает по принципу «любая юрисдикция, кроме unsupported-списка», где KZ/AM/GE/KG не перечислены ([paddle.com](https://www.paddle.com/help/start/intro-to-paddle/which-countries-are-supported-by-paddle)). Для high-risk (SegPay, CCBill, Verotel, Unlimit, TailoredPay) юрисдикция KZ/AM/GE/KG в публичных политиках не упоминается вообще. Вывод: **вопрос решается только индивидуальным запросом к risk-команде**, публичных гарантий нет.

---

## Не подтверждено (требует прямого запроса)

- **Комиссии, setup fee, rolling reserve, chargeback fee** у: Paddle (кроме 5% + $0.50), PayPro Global, Cleverbridge, 2Checkout/Verifone, SegPay, CCBill, Payop, Unlimit, TailoredPay, FastSpring. У high-risk публичных прайсов практически не бывает — ожидаемо «по запросу».
- **Допуск VPN** у: FastSpring, Cleverbridge, 2Checkout/Verifone, SegPay, Unlimit, TailoredPay (TailoredPay заявляет VPN в заголовке, но тело страницы недоступно). Формально это «нет запрета в публичном документе» ≠ «одобрят онбординг».
- **Отношение к РФ-резидентам** у FastSpring, Cleverbridge, 2Checkout, Verotel, CCBill, SegPay, Unlimit, TailoredPay — публичных заявлений не найдено.
- **Payout-графики и минимумы** у FastSpring, Cleverbridge, 2Checkout, SegPay, CCBill, Payop, Unlimit, TailoredPay — публично не раскрыты.
- **Юрлицо в KZ/AM/GE/KG под VPN** — публичных подтверждений нет ни у одного из 12.
- Технические ограничения проверки на 2026-10-05: segpay.com отдаёт 403 «Please enable JS and disable any ad blocker»; 2checkout.com — «Request unsuccessful. Incapsula incident ID» (использован архив 2024 года); tailoredpay.com — Cloudflare block; unlimit.com отдаёт 404 на страницы AUP/restricted.
