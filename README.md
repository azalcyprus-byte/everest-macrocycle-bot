# Everest Macrocycle Bot

Закрытый Telegram-бот для системы Macrocycle #3.

## Команды

- `/start` — проверка Telegram-бота и авторизации.
- `/sheet` — проверка доступа к Google Sheets.
- `/notifytest ЧЧ:ММ` — одно тестовое уведомление на сегодня по московскому времени.

Пример:

```text
/notifytest 11:50
```

Повторная постановка уведомления на то же время блокируется в рамках текущего запуска сервиса.

## Переменные окружения

- `BOT_TOKEN`
- `TELEGRAM_USER_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `GOOGLE_SHEET_ID`
- `PORT` — необязательно, по умолчанию `8000`.
