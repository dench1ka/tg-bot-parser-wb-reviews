# WB Reviews Bot

Telegram-бот для сбора отзывов и вопросов с Wildberries.

**Что умеет:**
- Принимает ссылку или артикул товара
- Фильтрует отзывы по звёздам **до сбора** (не тратит трафик на лишнее)
- Выгружает результат в два Excel-файла + ZIP-архив
- Хранит историю последних 10 сборов — можно перескачать файлы без повторного парсинга
- Ограничен белым списком Telegram ID

---

## Быстрый старт на VPS (рекомендуемый способ)

### 1. Подготовка VPS

Нужен любой VPS с Linux (Ubuntu 22.04+, Debian 11+). Минимум: 1 CPU, 512 МБ RAM, 2 ГБ диска.

Поставьте Docker и Docker Compose:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# Перелогиньтесь, чтобы обновились права группы
```

Проверить:
```bash
docker --version
docker compose version
```

### 2. Получить токен и свой ID

- **BOT_TOKEN:** напишите [@BotFather](https://t.me/BotFather) → `/newbot` → следуйте подсказкам. Получите строку вида `7123456789:AAEdefGhi...`
- **Свой Telegram ID:** напишите [@userinfobot](https://t.me/userinfobot) → `/start` → он пришлёт ваш ID (число).

### 3. Развернуть бота

```bash
# Склонировать / распаковать проект в любую папку
cd /opt
sudo mkdir wb-bot && sudo chown $USER wb-bot
cd wb-bot
# (сюда копируется содержимое этой папки)

# Создать конфиг из шаблона
cp .env.example .env

# Открыть .env и заполнить BOT_TOKEN и ALLOWED_USER_IDS
nano .env
```

Минимум, что должно быть в `.env`:

```ini
BOT_TOKEN=7123456789:AAEdefGhiJKlmNoPQrsTUVwxyz
ALLOWED_USER_IDS=123456789
```

Запуск:

```bash
docker compose up -d --build
```

Проверить, что бот живой:

```bash
docker compose logs -f
# в логах должно быть: "Бот запущен"
# Ctrl+C чтобы выйти из просмотра (контейнер продолжит работать)
```

Открыть своего бота в Telegram → `/start`.

### 4. Управление

```bash
# Статус
docker compose ps

# Логи (последние 100 строк)
docker compose logs --tail=100

# Перезапуск (например, после правок .env)
docker compose restart

# Остановить
docker compose down

# Обновить образ после изменений в коде
docker compose up -d --build
```

Данные (БД истории + Excel-файлы + логи) хранятся на хосте в `./data/` и переживают пересборку.

---

## Запуск локально (без Docker)

Подойдёт для отладки на Mac/Linux/Windows.

```bash
# Нужен Python 3.12+
python3 --version

# Виртуальное окружение
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# Зависимости
pip install -r requirements.txt

# Конфиг
cp .env.example .env
# отредактируй .env (BOT_TOKEN, ALLOWED_USER_IDS)

# Запуск
python bot.py
```

---

## Использование

1. Отправь боту ссылку на товар WB или артикул (число).
2. Выбери фильтр по звёздам:
   - Все / 1 / 1–2 / 1–3 / 4–5
   - «Выбрать вручную» — отметь любые комбинации галочками
3. Жди прогресса. Можно отменить кнопкой под сообщением или командой `/cancel`.
4. Получи: `Отзывы_[артикул]_[фильтр]_[timestamp].xlsx`, `Вопросы_[артикул]_[timestamp].xlsx` и ZIP-архив.

**Команды:**
- `/start` — начало
- `/help` — справка
- `/history` — последние 10 сборов, повторное скачивание без пересбора
- `/cancel` — отменить текущий сбор

---

## Настройки в `.env`

| Параметр | Значение по умолчанию | Описание |
|---|---|---|
| `BOT_TOKEN` | — | Токен от @BotFather (**обязательно**) |
| `ALLOWED_USER_IDS` | пусто | Telegram ID через запятую. Пусто = доступ всем |
| `DATABASE_PATH` | `data/history.db` | Путь к SQLite |
| `FILES_DIR` | `data/files` | Папка с Excel-файлами |
| `WB_REQUEST_DELAY` | `0.3` | Пауза между запросами к WB (сек). Подними до `0.7–1.0` при 429 |
| `COLLECTION_TIMEOUT` | `1800` | Макс. время одного сбора (сек) |
| `PROGRESS_EDIT_INTERVAL` | `3.0` | Как часто редактировать прогресс-сообщение |

---

## Устранение проблем

**Бот не отвечает на `/start`**
- Проверь `docker compose logs` — там будут ошибки
- Убедись, что твой ID добавлен в `ALLOWED_USER_IDS`
- Проверь, что токен скопирован полностью, без пробелов

**В логах `feedbacks API вернул 429`**
- WB просит замедлиться. Увеличь в `.env`:
  ```
  WB_REQUEST_DELAY=1.0
  ```
  и перезапусти: `docker compose restart`

**Собирается меньше отзывов, чем видно на сайте**
- WB отдаёт максимум ~1000 отзывов на один `nmId`. Бот автоматически обходит все цвета/размеры товара, но если карточка большая — часть старых отзывов может быть недоступна через API.

**Диск забивается файлами**
- Старые Excel-файлы накапливаются в `data/files/`. Можно безопасно чистить файлы, созданные раньше нужного срока — на работу бота это не повлияет (только не получится перескачать очень старые сборы из `/history`).

---

## Структура проекта

```
.
├── bot.py              # точка входа
├── config.py           # загрузка .env
├── handlers.py         # обработчики команд и callback'ов
├── keyboards.py        # inline-клавиатуры
├── wb_api.py           # клиент к API Wildberries
├── excel_export.py     # выгрузка в .xlsx и ZIP
├── database.py         # SQLite (история)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── data/               # создаётся автоматически
    ├── history.db
    ├── files/          # выгруженные Excel и архивы
    └── logs/           # ротируемые логи
```

---

## Безопасность

- **Никогда не коммить `.env`** — он в `.gitignore`
- Если токен утёк — перевыпусти через `@BotFather` → `/revoke`
- Белый список `ALLOWED_USER_IDS` — единственная защита бота от чужих запусков
