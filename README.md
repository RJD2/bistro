# Бот Быстротест

## Локальная установка

### Для установки выполните команды

`pip install -r requirements.txt`

`python migrations/init_db.py`

### Для запуска бота выполните

`python bot.py`

## Запуск в Docker

### Предварительные требования

- Docker
- Docker Compose
- Переменная окружения `TOKEN` с токеном Telegram бота

### Запуск

1. Создайте файл `.env` в корне проекта с вашим токеном:
   ```
   TOKEN=your_telegram_bot_token_here
   ```

2. Запустите сервисы:
   ```bash
   docker-compose up --build
   ```

   Это запустит PostgreSQL базу данных и бота.

3. Для остановки:
   ```bash
   docker-compose down
   ```

### Миграции базы данных

После первого запуска, чтобы инициализировать базу данных и добавить вопросы, выполните:

```bash
docker-compose exec bot python -m migrations.init_db
docker-compose exec bot python -m migrations.add_questions
```

Или добавьте эти команды в Dockerfile или entrypoint, если нужно автоматизировать.
