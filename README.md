# Uzum FBO Telegram Bot

Бот проверяет продажи Uzum FBO через метод:

GET https://api-seller.uzum.uz/api/seller-openapi/v1/finance/orders

и отправляет новые продажи в Telegram.

## 1. Установка

```bash
pip install -r requirements.txt
```

## 2. Настройка

Скопируйте файл `.env.example` в `.env`:

```bash
cp .env.example .env
```

Откройте `.env` и заполните:

- `UZUM_TOKEN` — новый API-ключ Uzum, без слова Bearer
- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота от BotFather
- `TELEGRAM_CHAT_ID` — ваш chat_id
- `UZUM_SHOP_ID` — ваш shopId, сейчас указан 113982

## 3. Как получить TELEGRAM_CHAT_ID

1. Напишите своему боту любое сообщение, например `/start`.
2. Откройте в браузере:

```text
https://api.telegram.org/botВАШ_TELEGRAM_BOT_TOKEN/getUpdates
```

3. Найдите в ответе:

```json
"chat": {
  "id": 123456789
}
```

Это и есть `TELEGRAM_CHAT_ID`.

## 4. Запуск

```bash
python uzum_bot.py
```

## 5. Важно

Не отправляйте никому:
- `UZUM_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- screenshots, где виден Authorization
