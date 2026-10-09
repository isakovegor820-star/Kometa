Title: Обход белых списков с помощью CDN и XHTTP · frank-underwood64/whitelists_bypass · Discussion #1

URL Source: https://github.com/frank-underwood64/whitelists_bypass/discussions/1

Markdown Content:
## Обход белых списков через CDN и XHTTP: самый актуальный способ в 2026 году

👋Привет, меня зовут Александр. Я не первый год «варюсь» в теме VPN и связанной инфраструктуры, помогаю поднимать проекты и оказываю услуги сервисам любого масштаба. Какое-то время назад был введён новый вид ограничений — белые списки.

## 🤓Что такое белые списки

В обычном режиме мобильный оператор маршрутизирует соединения ко множеству интернет-ресурсов. Во время локальных ограничений может применяться другой режим: доступ сохраняется только к определённым сайтам, платформам и инфраструктуре, а остальной трафик блокируется или перестаёт маршрутизироваться.

Для пользователя это выглядит примерно так:

*   открываются ТОЛЬКО определённые сайты, государственного значения или первой необходимости;
*   одна и та же конфигурация работает по домашнему Wi-Fi, но не работает через мобильную сеть;
*   наличие и интенсивность ограничений отличаются в разных регионах и на разных операторах.

Белый список содержит перечень ресурсов, которые доступны пользователям в моменты включения соответствующего режима.

Я давно слежу за белыми списками, помогаю как сервисам, так и обычным пользователям создать оптимальную инфраструктуру уклонения от новых ограничений. Сегодня наиболее актуальным решением считаю обход через CDN-инфраструктуру — ниже простым языком объясню, как устроена эта схема.

## ⏳Как обходили ранее

Не так давно эффективным решением считался обход через каскад с белым IP-адресом, который находился в том самом списке. Схема:

```
клиент → сервер с белым IP → сервер в Европе → интернет
```

И вроде бы всё отлично: скорость превосходная, со стабильностью тоже порядок. Но если раньше такие айпи можно было «выбить» на крупных российских хостингах, то сейчас получить их реально только покупкой у перекупов за огромные деньги. И нет гарантий, что на следующий день айпишник не заблокируют и не вычеркнут из списков. Да и продавец может оказаться мошенником.

## 🔥Новый подход: использовать CDN-инфраструктуру

CDN — это распределённая сеть серверов, которая обычно принимает запрос пользователя,  
 а затем получает контент с основного сервера — **источника**.

В стандартном сценарии CDN ускоряет сайты, изображения и видео. В нашей схеме она становится  
 промежуточным транспортным слоем:

```
Клиент
  └── HTTPS-запрос к домену раздачи
        └── CDN сеть
              └── HTTPS к домену источника
                    └── nginx на VPS
                          └── локальный Xray-core
                                └── интернет
```

Схема имеет смысл только там, где узлы выбранного CDN остаются доступны в нужной мобильной сети и корректно передают используемый HTTP-трафик.

В этой статье рассматривается конкретная связка:

```
VLESS → XHTTP → TLS → CDN → TLS → nginx → Xray
```

Это разные уровни, и каждый решает свою задачу:

*   **VLESS** — прокси-протокол между клиентом и Xray-сервером. Он идентифицирует пользователя  
 по UUID и передаёт его соединения.
*   **XHTTP** — транспорт Xray, внутри которого передаётся VLESS. Он раскладывает поток на  
 HTTP-запросы и ответы. Конкретное поведение зависит от режима: например, `packet-up`  
 использует пакетную отправку восходящего трафика и потоковый ответ для нисходящего.
*   **TLS** — защищает HTTPS-соединения и отвечает за сертификаты, SNI и согласование протокола.
*   **CDN** — принимает HTTPS от клиента и создаёт отдельное соединение с источником. Для этой  
 схемы важно, чтобы CDN не ломал методы, тело запроса, заголовки и потоковую передачу.
*   **nginx** — принимает запрос CDN на источнике и передаёт транспортный путь в локальный  
 VLESS-inbound Xray.
*   **Xray-core** — завершает XHTTP-транспорт, проверяет пользователя VLESS, восстанавливает  
 поток и отправляет его дальше через настроенный outbound.
*   **Remnawave** — необязательный управляющий слой над Xray. Можно использовать любую другую панель или голое ядро.

XHTTP поддерживает не только VLESS, однако далее речь идёт именно о наиболее распространённой для этой схемы комбинации **VLESS over XHTTP**.

## Почему XHTTP через CDN особенно актуален сейчас

В первую очередь — цена. Сам ресурс некоторые хостинги предоставляют бесплатно, на остальных демократичные цены: ~0,6 рубля за ГБ исходящего трафика (есть отличия между хостингами). VPS в Европе тоже обойдётся недорого — для начала буквально за $5/мес. За такие деньги получаем отличную альтернативу каскаду, на обслуживание которого могут уходить сотни долларов в месяц.

Далее, у CDN не одна точка входа, и это важная часть российского интернета. Такую инфраструктуру сложнее ограничить белыми списками, а значит, подобный обход может оказаться долговечнее каскада.

## 🤝Что понадобится для своей схемы

Минимальный набор выглядит так:

1.   VPS с публичным IPv4 и достаточной пропускной способностью.
2.   Два доменных имени: одно для источника, второе для CDN-раздачи.
3.   TLS-сертификат для соединения CDN с источником.
4.   TLS-сертификат для пользовательского домена раздачи.
5.   CDN-ресурс на подходящем хостинге.
6.   nginx или другой обратный прокси на источнике.
7.   Xray-core, при необходимости — панель управления Remnawave.
8.   Клиент с поддержкой VLESS и нужных полей XHTTP (Xray новее 26.6.27; Happ и INCY уже на 2.7.11 — с ними точно заработает).

Два домена нужны потому, что это два разных участка соединения:

```
Клиент ──TLS──> CDN_HOST
CDN    ──TLS──> SOURCE_DOMAIN
```

## 🧐Как происходит настройка

### Шаг 1. Подготавливается источник

На VPS запускается Xray-core напрямую или через Remnawave Node, а перед ним — обратный прокси.  
 В Xray создаётся VLESS-inbound с транспортом XHTTP, который слушает локальный порт.

nginx принимает HTTPS на внешнем порту и передаёт только заданный путь на локальный inbound:

```
SOURCE_DOMAIN:443
  ├── /обычный-путь/     → сайт или заглушка
  └── /transport-path/   → 127.0.0.1:XHTTP_PORT
```

### Шаг 2. Настраиваются DNS и сертификаты

Домен источника указывает непосредственно на VPS. Домен раздачи связывается с техническим доменом CDN согласно инструкции провайдера.

Сертификат источника должен соответствовать имени, по которому CDN подключается к серверу.

### Шаг 3. Создаётся CDN-ресурс

Какой вообще хостинг рассматривать? Можно выделить несколько подходящих: например, Yandex, VK, Beeline, Selectel, Timeweb, Beget и т. д. Стоит сказать, что почти каждый из них требует индивидуальных настроек — как минимум в XHTTP-конфиге.

В качестве источника указывается его домен. Вид раздачи — статика.

Встречается много различий между провайдерами. Где-то параметр называется «Хост источника», где-то — `Host header`; один сервис принимает загруженный сертификат,  
 другой выпускает его самостоятельно; часть настроек может находиться не в очевидном разделе  
 панели.

### Шаг 4. Настраивается хост или клиент

В хосте указываются параметры подключения к домену раздачи. Путь, режим и остальные согласуемые параметры XHTTP должны соответствовать серверной конфигурации.

### Цепочка проверяется по слоям

1.   Исключить проблемы и несоответствия с доменами, сертификатами и т. д.
2.   Источник отвечает по HTTPS.
3.   CDN получает ответ от источника.
4.   Запрос к транспортному пути появляется в логах nginx.
5.   nginx соединяется с локальным портом Xray.
6.   Клиент получил свежую конфигурацию.

Только после этого имеет смысл менять параметры самого XHTTP.

## ⚒️С чего начинать диагностику

Код ответа указывает на класс ошибки, но редко называет её точную причину. Кроме того,  
 ответ может сформировать CDN, nginx или приложение на источнике. Поэтому сначала стоит  
 определить слой, на котором он появился:

1.   повторить эквивалентный запрос напрямую к источнику и через CDN;
2.   сохранить точное время, код, заголовки и тело ответа;
3.   сопоставить запрос с журналами CDN, nginx и Xray;
4.   проверить документацию конкретного CDN: нестандартные коды и ограничения различаются.

Ниже — не готовые диагнозы, а направления, с которых разумно начинать проверку.

### `502 Bad Gateway`

По стандарту HTTP этот код означает, что сервер, работавший как шлюз или прокси, получил  
 некорректный ответ от следующего сервера. На практике разные CDN могут использовать `502`  
 для более широкого набора сбоев.

Сначала нужно выяснить, кто вернул код. Если это CDN, имеет смысл проверить разрешение домена  
 источника, соединение с его портом, TLS/SNI, сертификат, заголовок `Host` и ответ источника  
 на тот же запрос без CDN. Если `502` сформировал nginx, смотреть следует его `error.log`,  
 доступность локального порта Xray и протокол, по которому nginx обращается к нему.

### `504 Gateway Timeout`

Стандартное значение `504` — шлюз не получил ответ от следующего узла за отведённое время.  
 Сам код не показывает, на каком именно этапе возникла задержка.

Стоит сравнить время ответа источника напрямую и через CDN, затем проверить ограничения провайдера, журналы источника и таймауты прокси. У некоторых CDN окно ожидания фиксировано: например, Yandex требует, чтобы источник ответил в течение пяти секунд. Увеличение таймаута только в nginx такое ограничение не изменит.

### Ответы `4xx`

Ответ класса `4xx` не обязательно означает проблему на клиентском устройстве: его может  
 сформировать CDN, WAF, nginx или обработчик на источнике. В качестве первых ориентиров:

*   `403` — проверить правила доступа, WAF и авторизацию;
*   `404` — проверить `Host`, путь и то, в какой обработчик nginx попал запрос;
*   `405` — проверить, разрешён ли используемый HTTP-метод;
*   `413` — проверить ограничения размера тела запроса на каждом слое;
*   `429` — проверить лимиты частоты запросов и соединений.

Для XHTTP особенно важен `405`: некоторые CDN по умолчанию пропускают только `GET`, `HEAD` и `OPTIONS`, тогда как типичный `packet-up` использует несколько запросов `POST` для восходящего потока и отдельный потоковый `GET` для нисходящего. Лечится это переводом восходящего потока на `GET` (данные уезжают в теле или заголовке запроса) — но конкретная настройка под каждый CDN тянет на отдельный разбор.

### Соединение работает, затем замирает

Нужно сопоставить точное время обрыва на клиенте с журналами CDN, nginx и Xray. Смотреть следует в сторону максимальной длительности и времени простоя соединения у CDN, таймаутов чтения и отправки nginx, ограничений запросов, исчерпания ресурсов и ошибок самого Xray.

Важно учитывать семантику параметров: например, `proxy_read_timeout` в nginx задаёт интервал  
 между операциями чтения от проксируемого сервера, а не максимальную длительность всего ответа.

### По Wi-Fi работает, через мобильную сеть при ограничениях — нет

Это подтверждает работоспособность хотя бы одного сетевого маршрута, но не доказывает доступность того же узла через конкретного оператора. Ситуации, когда какой-то CDN не работает у некоторых операторов в отдельных регионах, вполне возможны.

### 😍Тесты моих настроенных конфигураций

[![Image 1: image](https://private-user-images.githubusercontent.com/309089613/627074709-da304a09-1754-4d53-8c21-f6e58265bac4.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc0NzA5LWRhMzA0YTA5LTE3NTQtNGQ1My04YzIxLWY2ZTU4MjY1YmFjNC5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT05ZTQ2YjEwNWEzYjNiNzdhNWEwYTVmMWQ5YmQ5ZDQ2YmE1ZDVjYjRkNTlhZDM1ZDIzNzhhMmJmYzQ2OGU5MjU1JlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.pvXYeGATZ7i2g2uXq8vNB1G_6Y-cR6XS34mptE1-5TY)](https://private-user-images.githubusercontent.com/309089613/627074709-da304a09-1754-4d53-8c21-f6e58265bac4.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc0NzA5LWRhMzA0YTA5LTE3NTQtNGQ1My04YzIxLWY2ZTU4MjY1YmFjNC5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT05ZTQ2YjEwNWEzYjNiNzdhNWEwYTVmMWQ5YmQ5ZDQ2YmE1ZDVjYjRkNTlhZDM1ZDIzNzhhMmJmYzQ2OGU5MjU1JlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.pvXYeGATZ7i2g2uXq8vNB1G_6Y-cR6XS34mptE1-5TY)[![Image 2: image](https://private-user-images.githubusercontent.com/309089613/627075533-c65fe07e-e7ce-444e-9e50-150e4760a092.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc1NTMzLWM2NWZlMDdlLWU3Y2UtNDQ0ZS05ZTUwLTE1MGU0NzYwYTA5Mi5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT0wM2FjMDI1Mzk3MTQyMjFhY2U1ZTUxZGE3Y2FlNjIxOWJjZGY5ZTRmMzkzODk1NzVkYzZjNjhiYzViMTNkNTVlJlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.F8NUr5io5DGRkK0ZTBt0RmYmuuPLGpJ4_tsdvwUpsXQ)](https://private-user-images.githubusercontent.com/309089613/627075533-c65fe07e-e7ce-444e-9e50-150e4760a092.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc1NTMzLWM2NWZlMDdlLWU3Y2UtNDQ0ZS05ZTUwLTE1MGU0NzYwYTA5Mi5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT0wM2FjMDI1Mzk3MTQyMjFhY2U1ZTUxZGE3Y2FlNjIxOWJjZGY5ZTRmMzkzODk1NzVkYzZjNjhiYzViMTNkNTVlJlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.F8NUr5io5DGRkK0ZTBt0RmYmuuPLGpJ4_tsdvwUpsXQ)[![Image 3: image](https://private-user-images.githubusercontent.com/309089613/627075766-2c3ddc7f-3b66-487a-ba3d-6ac4c6ef45d0.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc1NzY2LTJjM2RkYzdmLTNiNjYtNDg3YS1iYTNkLTZhYzRjNmVmNDVkMC5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT01OGMzOGRiYmNhYjJjNWIzOTE2MjdlMDk2OTE3ZTE1MDU0NzUyYWIwZmVkMzZhMDU0ODY4NjBhZjAyYTA4YTdiJlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.5LwjxVQT8LAveb_4usCyFeYUW79ThTONvJea4qmwlWg)](https://private-user-images.githubusercontent.com/309089613/627075766-2c3ddc7f-3b66-487a-ba3d-6ac4c6ef45d0.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc1NzY2LTJjM2RkYzdmLTNiNjYtNDg3YS1iYTNkLTZhYzRjNmVmNDVkMC5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT01OGMzOGRiYmNhYjJjNWIzOTE2MjdlMDk2OTE3ZTE1MDU0NzUyYWIwZmVkMzZhMDU0ODY4NjBhZjAyYTA4YTdiJlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.5LwjxVQT8LAveb_4usCyFeYUW79ThTONvJea4qmwlWg)  
[![Image 4: image](https://private-user-images.githubusercontent.com/309089613/627075975-dcab1177-9414-4333-806d-b310e16ad561.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc1OTc1LWRjYWIxMTc3LTk0MTQtNDMzMy04MDZkLWIzMTBlMTZhZDU2MS5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT1lNGM1OGFmM2Q3MzlmNjFhZGNhNTMxNGRjNzUwMTFlMGUzYmVmNmFmZTI2NDNmYTIwY2E5OWQ0ZGJhZGZlMmZlJlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.EnCPeeWDnRXwaSIQKSFcZUuSbhdHUeL3VQyqcZHMhCo)](https://private-user-images.githubusercontent.com/309089613/627075975-dcab1177-9414-4333-806d-b310e16ad561.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc1OTc1LWRjYWIxMTc3LTk0MTQtNDMzMy04MDZkLWIzMTBlMTZhZDU2MS5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT1lNGM1OGFmM2Q3MzlmNjFhZGNhNTMxNGRjNzUwMTFlMGUzYmVmNmFmZTI2NDNmYTIwY2E5OWQ0ZGJhZGZlMmZlJlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.EnCPeeWDnRXwaSIQKSFcZUuSbhdHUeL3VQyqcZHMhCo)[![Image 5: image](https://private-user-images.githubusercontent.com/309089613/627076087-fdaec381-e37b-4919-af37-67a1c210f3ea.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc2MDg3LWZkYWVjMzgxLWUzN2ItNDkxOS1hZjM3LTY3YTFjMjEwZjNlYS5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT03OTI0MDc5MGVhZjY0MWUzNzUyOTIzNDFlNWY2ZGQ4Y2ZmZDNlYmE3MmRjMzJjZWRlZDVkMDY5OTA5OWI4Zjk3JlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.jIqMEszTFHu4ts1PFy_mWroIgQSZ0qUtUw3hgeK2FgE)](https://private-user-images.githubusercontent.com/309089613/627076087-fdaec381-e37b-4919-af37-67a1c210f3ea.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc2MDg3LWZkYWVjMzgxLWUzN2ItNDkxOS1hZjM3LTY3YTFjMjEwZjNlYS5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT03OTI0MDc5MGVhZjY0MWUzNzUyOTIzNDFlNWY2ZGQ4Y2ZmZDNlYmE3MmRjMzJjZWRlZDVkMDY5OTA5OWI4Zjk3JlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.jIqMEszTFHu4ts1PFy_mWroIgQSZ0qUtUw3hgeK2FgE)[![Image 6: image](https://private-user-images.githubusercontent.com/309089613/627076208-ecdaae3c-09dd-40c9-983f-e836701449f0.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc2MjA4LWVjZGFhZTNjLTA5ZGQtNDBjOS05ODNmLWU4MzY3MDE0NDlmMC5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT0yNjk3ZGExYTYwYThiMjRmMWNjZDdhMmEyMjQ4YzY1YzM1ODRjN2FlZDg4ZGE1MjI1ZWViNmYzMWYzMGYwZWU1JlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.C0mLoU6MGY6Z8-iwJKp5zotAuiJGK4bAH4rIJht_XnE)](https://private-user-images.githubusercontent.com/309089613/627076208-ecdaae3c-09dd-40c9-983f-e836701449f0.png?jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJnaXRodWIuY29tIiwiYXVkIjoicmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbSIsImtleSI6ImtleTUiLCJleHAiOjE3OTE1Mjg0NzUsIm5iZiI6MTc5MTUyODE3NSwicGF0aCI6Ii8zMDkwODk2MTMvNjI3MDc2MjA4LWVjZGFhZTNjLTA5ZGQtNDBjOS05ODNmLWU4MzY3MDE0NDlmMC5wbmc_WC1BbXotQWxnb3JpdGhtPUFXUzQtSE1BQy1TSEEyNTYmWC1BbXotQ3JlZGVudGlhbD1BS0lBVkNPRFlMU0E1M1BRSzRaQSUyRjIwMjYxMDA5JTJGdXMtZWFzdC0xJTJGczMlMkZhd3M0X3JlcXVlc3QmWC1BbXotRGF0ZT0yMDI2MTAwOVQwNjQyNTVaJlgtQW16LUV4cGlyZXM9MzAwJlgtQW16LVNpZ25hdHVyZT0yNjk3ZGExYTYwYThiMjRmMWNjZDdhMmEyMjQ4YzY1YzM1ODRjN2FlZDg4ZGE1MjI1ZWViNmYzMWYzMGYwZWU1JlgtQW16LVNpZ25lZEhlYWRlcnM9aG9zdCZyZXNwb25zZS1jb250ZW50LXR5cGU9aW1hZ2UlMkZwbmcifQ.C0mLoU6MGY6Z8-iwJKp5zotAuiJGK4bAH4rIJht_XnE)

## 👨‍💻Порог вхождения для полной самостоятельной настройки

Самостоятельная установка имеет смысл, если вы уверенно работаете с Linux, DNS, TLS, nginx и логами, разбираетесь в устройстве сети, протоколе VLESS и транспорте XHTTP. Нужно всегда оставаться на плаву: проверять всю цепочку и обновлять её после изменений у CDN или Xray. Запасайтесь огромным количеством нервов и времени, друзья)

Для личной ноды это хороший способ разобраться в технологии. Для коммерческого VPN-сервиса  
 к настройке добавляются другие задачи:

*   расчёт нагрузки и стоимости запросов;
*   лимиты соединений и файловых дескрипторов;
*   мониторинг источника и CDN;
*   тестирование нескольких операторов и регионов;
*   план замены CDN или профиля транспорта.

В продакшене ценность имеет не конфигурация сама по себе, а воспроизводимость, диагностика  
 и возможность быстро восстановить работу после изменения внешних условий.

К чему я это — путь полностью рабочий, но требовательный: к времени, к нервам и к постоянному вниманию.

## 📣Вывод

Каскад с купленным белым айпи может работать, но остаётся дорогой и плохо контролируемой ставкой на один адрес. XHTTP через CDN требует более трепетной первоначальной настройки, зато даёт архитектуру, которая станет отличным и, на удивление, экономичным обходом для вас или вашего сервиса.

## ✅Готовые решения

Если возиться самому не хочется, а нужен результат — я уже прошёл этот путь на множестве CDN и упаковал его в два формата:

*   **Мануалы под конкретный CDN** — пошаговые гайды по Beeline, Yandex, VK Cloud, Timeweb/Beget: рабочие конфиги, значения параметров и грабли с обходами, которые порой непосильны нейронкам.
*   **Настройка под ключ** — разворачиваю и отдаю рабочую схему целиком: сервер, CDN, домены, сертификаты, клиентский конфиг, проверенные под реальной нагрузкой. Подходит и для личной ноды, и для коммерческого сервиса. Для тех, кому нужен результат, а не процесс.
*   Для обоих вариантов оказываю долгосрочную поддержку.

🤝Детали можем разобрать в личке тг. Отвечу на любые вопросы и предложения. **[https://t.me/underwood642](https://t.me/underwood642)**

* * *

⛔️Материал носит исключительно информационно-ознакомительный и образовательный характер. Он посвящён сетевой архитектуре, транспортным протоколам и отказоустойчивости — темам, которые важны для системных администраторов, сетевых инженеров и специалистов по информационной безопасности.

Я никого ни к чему не призываю и не принуждаю, не побуждаю нарушать закон и не гарантирую какой-либо результат — всё изложенное приведено для изучения того, как устроены современные сети, и для повышения собственной цифровой безопасности. Каждый читатель самостоятельно принимает решения и сам отвечает за их последствия: прежде чем что-либо настраивать, учитывайте законодательство своей юрисдикции, правила вашего оператора связи и условия использования выбранного CDN. За возможные последствия использования этой информации ответственности не несу.
