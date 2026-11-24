# 🚀 Revolution — High-Performance Verification & Stress-Testing Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Framework: aiogram 3](https://img.shields.io/badge/framework-aiogram%203-2b82c9.svg)](https://github.com/aiogram/aiogram)
[![Engine: aiohttp](https://img.shields.io/badge/engine-aiohttp-2ca44f.svg)](https://github.com/aio-libs/aiohttp)
[![Browser: Patchright](https://img.shields.io/badge/browser-patchright-9b59b6.svg)](https://github.com/Kaliiiiiiiiii-tools/patchright)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**Revolution** — мощный, полностью асинхронный Telegram-бот и фреймворк на Python для автоматизированного стресс-тестирования шлюзов верификации, SMS/Call-уведомлений и инфраструктуры авторизации сервисов.

Проект разработан для инженеров по безопасности, разработчиков и QA-специалистов для проверки устойчивости собственных шлюзов авторизации к высоким нагрузкам (SMS-flood resilience), корректности работы рейт-лимитеров и надежности защиты от ботов.

---

## ⚡ Ключевые возможности

- 🏎 **Высокопроизводительное асинхронное ядро (`attack.py`):**
  - Неблокирующий I/O на базе `aiohttp` и `asyncio`.
  - Пул постоянных соединений (`TCPConnector`), тонкая настройка concurrency и таймаутов.
  - Точный rate-limiting (ограничение запросов в секунду) раздельно для Free и VIP пользователей.
  - Поддержка цепочек запросов (multi-step registration flows) и извлечения CSRF/Session токенов.

- 🤖 **Интерактивный интерфейс Telegram (`main.py`, `ui.py`):**
  - Построен на современном `aiogram 3.x` с использованием FSM.
  - Удобное Inline-меню, интерактивные статусы и прогресс выполнения тестов в реальном времени.
  - Парсинг и строгая валидация телефонных номеров международных стандартов через `phonenumbers`.
  - Админ-панель: мониторинг активных процессов, статистика, управление пользователями, горячая перезагрузка базы сервисов без даунтайма (`/reload`).

- 🛡 **Интегрированный обход капч и бот-детекции:**
  - **reCAPTCHA v2 / v3:** кэширование токенов, решение аудио-челленджей через распознавание речи, эмуляция токенов.
  - **Yandex SmartCaptcha (`yandexcaptcha/`):** нативная автономная реализация криптографии, Proof-of-Work (PoW) и генерации фингерпринтов.
  - **Cloudflare Turnstile (`turnstile_solver.py`):** автоматическое извлечение и прохождение проверок.
  - **Patchright (Playwright):** скрытая браузерная автоматизация для сложных динамических сервисов с обходом отпечатков (fingerprint spoofing).

- 🌐 **Пул прокси с ротацией и проверкой здоровья (`proxies.py`):**
  - Поддержка SOCKS4, SOCKS5, HTTP/HTTPS прокси с авторизацией.
  - Автоматическая валидация доступности (health checks) и изоляция деградировавших узлов.
  - Гео-фильтрация трафика (раздельные пулы для RU, UA и других регионов).

- 💎 **Монетизация и VIP-система (`xrocket.py`, `db.py`):**
  - Поддержка Telegram Stars (оплата внутри приложения).
  - Интеграция с крипто-платежной системой **xRocket** (USDT, TON, GRAM).
  - Персональные одноразовые инвайт-ссылки в закрытое VIP-сообщество при покупке привилегии.
  - Индивидуальные лимиты параллельных потоков и длительности сессий.

---

## 📂 Структура проекта

```text
revolution/
├── attack.py              # Основной асинхронный движок отправки запросов и цепочек
├── browser.py             # Автоматизация браузера на Patchright для JS/капча-сервисов
├── config.py              # Загрузка и валидация конфигурации (.env)
├── db.py                  # Асинхронный слой базы данных (SQLite / aiosqlite)
├── main.py                # Главная точка входа: Telegram-бот и диспетчер
├── proxies.py             # Пул прокси, ротация, health check и парсинг
├── recaptcha.py           # Кэш и логика работы с Google reCAPTCHA v3
├── recaptcha_v2.py        # Решатель reCAPTCHA v2 (audio/token)
├── turnstile_solver.py    # Решатель Cloudflare Turnstile
├── ui.py                  # Компоненты интерфейса, стили и инлайн-клавиатуры
├── xrocket.py             # Клиент API платежного шлюза xRocket
├── yandex_captcha.py      # Интеграция с Yandex SmartCaptcha
├── yandexcaptcha/         # Автономный пакет криптографии и PoW для SmartCaptcha
├── configs/               # Конфигурационные файлы и шаблоны
│   ├── .env.example       # Пример настроек окружения
│   ├── proxies.json.example # Пример конфигурации прокси
│   ├── proxies.txt.example  # Пример текстового списка прокси
│   └── services.json      # База шаблонов сервисов (Free / VIP)
├── docs/                  # Документация по сервисам и интеграциям
│   ├── SERVICES.md        # Руководство по структуре сервисов и SmartCaptcha
│   └── service.md         # Описание проверенных рабочих эндпоинтов
├── pic/                   # Дизайн-ассеты и баннеры интерфейса
├── tests/                 # Модульные и интеграционные тесты (pytest)
├── tools/                 # Утилиты верификации, чистки и добавления сервисов
│   ├── add_service.py     # Интерактивное добавление и тест нового сервиса
│   ├── prune_services.py  # Автоматическая очистка нерабочих сервисов
│   └── verify_services.py # Массовая проверка актуальности базы сервисов
├── restore.sh             # Скрипт развертывания окружения в 1 команду
├── restart.sh             # Скрипт безопасного перезапуска (systemd / container)
├── requirements.txt       # Зависимости проекта
└── pytest.ini             # Конфигурация тестов
```

---

## 🛠 Быстрый старт

### Требования
- **ОС:** Linux (Ubuntu 20.04+, Debian 11+ рекомендуется)
- **Python:** 3.10 или новее
- **Дополнительно:** Chromium / Patchright зависимости (устанавливаются автоматически)

### 1. Клонирование репозитория
```bash
git clone https://github.com/factorydeath/revolution.git
cd revolution
```

### 2. Автоматическая установка (в 1 команду)
В проект встроен скрипт `restore.sh`, который создает виртуальное окружение, устанавливает все зависимости Python и браузеры Patchright:

```bash
chmod +x restore.sh restart.sh
./restore.sh
```

---

### 3. Ручная установка (альтернативный способ)

```bash
# Создание и активация виртуального окружения
python3 -m venv .venv
source .venv/bin/activate

# Установка зависимостей
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Установка браузеров Patchright
patchright install chromium ffmpeg
```

---

## ⚙️ Конфигурация

### Переменные окружения (`.env`)

Скопируйте пример файла конфигурации:
```bash
cp configs/.env.example .env
```

Отредактируйте параметры в файле `.env`:

| Переменная | Обязательная | Описание |
|---|---|---|
| `REVOLUTION_BOT_TOKEN` | **Да** | API токен Telegram-бота от [@BotFather](https://t.me/BotFather) |
| `REVOLUTION_ADMIN_IDS` | **Да** | Telegram ID администраторов через запятую (например: `123456789,987654321`) |
| `XROCKET_API_KEY` | Нет | API-ключ шлюза xRocket Pay для приема криптоплатежей |
| `VIP_PRICE` | Нет | Стоимость VIP в Telegram Stars (по умолчанию `100`) |
| `XROCKET_VIP_PRICE_USDT` | Нет | Стоимость VIP в USDT через xRocket (по умолчанию `2`) |
| `REVOLUTION_VIP_COMMUNITY_CHAT_ID` | Нет | ID закрытого канала/чата для автоматической выдачи инвайтов VIP-клиентам |
| `ACTUAL_DEFAULT_RATE_LIMIT` | Нет | Рейт-лимит запросов/сек для обычных пользователей (по умолчанию `15.0`) |
| `ACTUAL_VIP_RATE_LIMIT` | Нет | Рейт-лимит запросов/сек для VIP пользователей (по умолчанию `35.0`) |
| `HTTP_WORKERS_VIP` | Нет | Количество параллельных HTTP-воркеров для VIP (по умолчанию `6`) |

### Настройка прокси (`configs/proxies.json`)

Для работы модулей ротации прокси скопируйте шаблон:
```bash
cp configs/proxies.json.example configs/proxies.json
```

Формат:
```json
{
  "RU": [
    "socks5://user:password@194.67.197.89:9885",
    "http://user:password@31.129.21.113:9631"
  ],
  "UA": [
    "socks5://user:password@193.32.155.91:9943"
  ]
}
```

---

## 🚀 Запуск

### Прямой запуск:
```bash
source .venv/bin/activate
python3 main.py
```

### Фоновый запуск / перезапуск через скрипт:
```bash
./restart.sh
```
Скрипт автоматически проверит наличие systemd-юнита `revolution-bot`, перезапустит сервис и дождется успешного ответа эндпоинта `/healthz`.

---

## 🧪 Тестирование

Запуск полного набора тестов:
```bash
source .venv/bin/activate
pytest
```

Запуск конкретного модуля тестов:
```bash
pytest tests/test_engine.py
pytest tests/test_db.py
pytest tests/test_proxies.py
```

---

## 🔧 Утилиты для работы с сервисами

В директории `tools/` находятся служебные скрипты для управления базой сервисов:

- **Добавление нового сервиса с автотестом капчи:**
  ```bash
  python3 tools/add_service.py --name "ExampleService" --country RU \
    --url "https://api.example.com/auth/code" \
    --body '{"phone":"{{RAW}}"}' \
    --content-type application/json --probe --write
  ```

- **Массовая проверка доступности сервисов:**
  ```bash
  python3 tools/verify_services.py
  ```

- **Удаление устаревших/битых эндпоинтов:**
  ```bash
  python3 tools/prune_services.py
  ```

---

## ⚠️ Дисклеймер (Disclaimer)

Данное программное обеспечение создано исключительно в образовательных целях, а также для авторизованного аудита безопасности и стресс-тестирования собственных сервисов и инфраструктуры. Авторы не несут ответственности за любое неправомерное использование или ущерб, причиненный в результате применения этого ПО.
