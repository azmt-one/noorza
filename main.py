import os
import time
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


load_dotenv()

UZUM_TOKEN = os.getenv("UZUM_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Можно указать один ID:
# TELEGRAM_CHAT_ID=445354240
# Или несколько ID через запятую:
# TELEGRAM_CHAT_ID=445354240,938965878
TELEGRAM_CHAT_IDS = [
    chat_id.strip()
    for chat_id in os.getenv("TELEGRAM_CHAT_ID", "").split(",")
    if chat_id.strip()
]
TELEGRAM_CHAT_ID = TELEGRAM_CHAT_IDS[0] if TELEGRAM_CHAT_IDS else ""

UZUM_SHOP_ID = os.getenv("UZUM_SHOP_ID", "113982").strip()

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "2"))

# За сколько дней считать команду /balance
BALANCE_LOOKBACK_DAYS = int(os.getenv("BALANCE_LOOKBACK_DAYS", "30"))

# Как часто бот проверяет команды Telegram
COMMAND_POLL_INTERVAL_SECONDS = int(os.getenv("COMMAND_POLL_INTERVAL_SECONDS", "5"))

# Остаток, ниже или равно которому товар считается заканчивающимся
LOW_STOCK_THRESHOLD = int(os.getenv("LOW_STOCK_THRESHOLD", "5"))

UZUM_FINANCE_URL = "https://api-seller.uzum.uz/api/seller-openapi/v1/finance/orders"
UZUM_PRODUCTS_URL = f"https://api-seller.uzum.uz/api/seller-openapi/v1/product/shop/{UZUM_SHOP_ID}"

# Для Bothost лучше указать DB_PATH=/app/data/uzum_sales.db
DB_PATH = os.getenv("DB_PATH", "uzum_sales.db")

try:
    TZ = ZoneInfo("Asia/Tashkent") if ZoneInfo else timezone(timedelta(hours=5))
except Exception:
    TZ = timezone(timedelta(hours=5))


MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "💰 Баланс"}, {"text": "📊 Сегодня"}],
        [{"text": "📦 Остатки"}, {"text": "⚠️ Заканчивается"}],
        [{"text": "🧭 Потерянные"}, {"text": "ℹ️ Помощь"}],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False,
}


def ensure_db_dir() -> None:
    db_file = Path(DB_PATH)
    if db_file.parent and str(db_file.parent) not in ("", "."):
        db_file.parent.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS seen_sales (
                sale_id INTEGER PRIMARY KEY,
                order_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def get_state(key: str, default: str | None = None) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def set_state(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def is_seen(sale_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM seen_sales WHERE sale_id = ?", (sale_id,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def mark_seen(sale_id: int, order_id: int | None = None) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO seen_sales (sale_id, order_id) VALUES (?, ?)",
            (sale_id, order_id),
        )
        conn.commit()
    finally:
        conn.close()


def money(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{int(value):,}".replace(",", " ") + " сум"
    except (TypeError, ValueError):
        return str(value)


def format_date(ms) -> str:
    if not ms:
        return "-"
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=TZ)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(ms)


def check_required_env() -> None:
    missing = []
    if not UZUM_TOKEN:
        missing.append("UZUM_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_IDS:
        missing.append("TELEGRAM_CHAT_ID")
    if not UZUM_SHOP_ID:
        missing.append("UZUM_SHOP_ID")

    if missing:
        raise RuntimeError("Не заполнены переменные окружения: " + ", ".join(missing))


def uzum_headers() -> dict:
    return {
        # В Swagger написано: Authorization без префикса Bearer
        "Authorization": UZUM_TOKEN,
        "Accept": "application/json",
    }


def get_sales_page(date_from: int, date_to: int, page: int = 0, size: int = 100) -> dict:
    params = {
        "page": page,
        "size": size,
        "group": "false",
        # По вашему Swagger сработал вариант: dateFrom в секундах, dateTo в миллисекундах.
        "dateFrom": date_from,
        "dateTo": date_to,
        "shopIds": UZUM_SHOP_ID,
    }

    response = requests.get(UZUM_FINANCE_URL, headers=uzum_headers(), params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def get_sales_for_period(start_dt: datetime, end_dt: datetime, max_pages: int = 30) -> list[dict]:
    date_from = int(start_dt.timestamp())          # секунды
    date_to = int(end_dt.timestamp() * 1000)       # миллисекунды

    all_items: list[dict] = []
    page = 0
    size = 100

    while page < max_pages:
        data = get_sales_page(date_from, date_to, page=page, size=size)
        items = data.get("orderItems", [])

        if not items:
            break

        all_items.extend(items)

        total = data.get("totalElements")
        if total is not None and len(all_items) >= int(total):
            break

        if len(items) < size:
            break

        page += 1

    return all_items


def get_recent_sales() -> list[dict]:
    now = datetime.now(tz=TZ)
    start = now - timedelta(days=LOOKBACK_DAYS)
    end = now + timedelta(hours=1)
    return get_sales_for_period(start, end, max_pages=5)


def get_products_page(page: int = 0, size: int = 100) -> dict:
    params = {
        "sortBy": "DEFAULT",
        "order": "ASC",
        "size": size,
        "page": page,
        "filter": "ALL",
    }
    response = requests.get(UZUM_PRODUCTS_URL, headers=uzum_headers(), params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def get_all_products(max_pages: int = 50) -> list[dict]:
    products: list[dict] = []
    page = 0
    size = 100

    while page < max_pages:
        data = get_products_page(page=page, size=size)
        items = data.get("productList", [])

        if not items:
            break

        products.extend(items)

        total = data.get("totalElements") or data.get("total")
        if total is not None and len(products) >= int(total):
            break

        if len(items) < size:
            break

        page += 1

    return products


def send_telegram_message(
    text: str,
    chat_id: str | int | None = None,
    reply_markup: dict | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Если chat_id указан — отвечаем только ему.
    # Если не указан — отправляем всем владельцам из TELEGRAM_CHAT_ID.
    target_chat_ids = [str(chat_id)] if chat_id else TELEGRAM_CHAT_IDS

    for target_chat_id in target_chat_ids:
        payload = {
            "chat_id": target_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()


def build_sale_message(item: dict) -> str:
    title = escape(str(item.get("productTitle") or "-"))
    sku = escape(str(item.get("skuTitle") or "-"))
    amount = item.get("amount") or 0

    return f"""🛒 <b>Новая продажа Uzum FBO</b>

<b>Товар:</b> {title}
<b>SKU:</b> {sku}
<b>Кол-во:</b> {amount} шт.

<b>Цена продажи:</b> {money(item.get("sellPrice"))}
<b>Комиссия:</b> {money(item.get("commission"))}
<b>Логистика:</b> {money(item.get("logisticDeliveryFee"))}
<b>К выплате:</b> {money(item.get("sellerProfit"))}

<b>ID заказа:</b> {item.get("orderId", "-")}
<b>ID продажи:</b> {item.get("id", "-")}
<b>Статус:</b> {escape(str(item.get("status", "-")))}
<b>Дата:</b> {format_date(item.get("date"))}"""


def summarize_sales(items: list[dict], title: str) -> str:
    active_items = [i for i in items if i.get("status") != "CANCELED"]

    positions = len(active_items)
    units = sum(int(i.get("amount") or 0) for i in active_items)

    # sellPrice обычно цена за единицу; если amount > 1, умножаем.
    revenue = sum(int(i.get("sellPrice") or 0) * int(i.get("amount") or 0) for i in active_items)

    commission = sum(int(i.get("commission") or 0) for i in active_items)
    logistics = sum(int(i.get("logisticDeliveryFee") or 0) for i in active_items)
    seller_profit = sum(int(i.get("sellerProfit") or 0) for i in active_items)
    withdrawn_profit = sum(int(i.get("withdrawnProfit") or 0) for i in active_items)
    to_withdraw = seller_profit - withdrawn_profit

    processing = sum(1 for i in active_items if i.get("status") == "PROCESSING")
    to_withdraw_count = sum(1 for i in active_items if i.get("status") == "TO_WITHDRAW")
    returned_units = sum(int(i.get("amountReturns") or 0) for i in active_items)

    return f"""💰 <b>{escape(title)}</b>

<b>Позиций продаж:</b> {positions}
<b>Кол-во товаров:</b> {units} шт.
<b>Возвраты:</b> {returned_units} шт.

<b>Выручка:</b> {money(revenue)}
<b>Комиссия Uzum:</b> {money(commission)}
<b>Логистика:</b> {money(logistics)}

<b>К выплате всего:</b> {money(seller_profit)}
<b>Уже выведено:</b> {money(withdrawn_profit)}
<b>Остаток к выплате:</b> {money(to_withdraw)}

<b>Статусы:</b>
PROCESSING: {processing}
TO_WITHDRAW: {to_withdraw_count}

<i>Это расчёт по данным /v1/finance/orders. Если в кабинете Uzum есть корректировки/расходы, итог может отличаться.</i>"""


def build_balance_message(days: int = BALANCE_LOOKBACK_DAYS) -> str:
    now = datetime.now(tz=TZ)
    start = now - timedelta(days=days)
    items = get_sales_for_period(start, now + timedelta(hours=1), max_pages=30)
    return summarize_sales(items, f"Баланс Uzum FBO за {days} дней")


def build_today_message() -> str:
    now = datetime.now(tz=TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    items = get_sales_for_period(start, now + timedelta(hours=1), max_pages=10)
    return summarize_sales(items, "Продажи Uzum FBO за сегодня")


def product_status_text(product: dict) -> str:
    status = product.get("status") or {}
    if isinstance(status, dict):
        return status.get("title") or status.get("value") or "-"
    return str(status or "-")


def product_available_qty(product: dict) -> int:
    # По вашему ответу Swagger:
    # quantityAvailable — доступный остаток
    # quantityActive — активный остаток
    # Для команды /stock берём quantityAvailable.
    return int(product.get("quantityAvailable") or 0)



def product_missing_qty(product: dict) -> int:
    # По вашему ответу Swagger:
    # quantityMissing — потерянный товар
    return int(product.get("quantityMissing") or 0)


def short_product_name(name: str, limit: int = 60) -> str:
    name = " ".join(str(name or "-").split())
    if len(name) <= limit:
        return name
    return name[: limit - 1] + "…"


def format_stock_line(product: dict, idx: int) -> str:
    qty = product_available_qty(product)
    title = short_product_name(product.get("productTitle") or "-")
    sku = product.get("skuFullTitle") or product.get("skuTitle") or "-"
    status = product_status_text(product)
    price = product.get("price")

    return (
        f"{idx}. <b>{escape(title)}</b>\n"
        f"SKU: {escape(str(sku))}\n"
        f"Остаток: <b>{qty} шт.</b> | Статус: {escape(str(status))}"
        + (f" | Цена: {money(price)}" if price is not None else "")
    )


def split_long_message(text: str, limit: int = 3900) -> list[str]:
    parts = []
    current = ""

    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                parts.append(current)
            current = block

    if current:
        parts.append(current)

    return parts


def build_stock_messages(low_only: bool = False) -> list[str]:
    products = get_all_products()

    # Показываем неархивные товары. Если нужно, можно убрать этот фильтр.
    products = [p for p in products if not p.get("archived")]

    products = sorted(products, key=lambda p: (product_available_qty(p), str(p.get("skuFullTitle") or p.get("skuTitle") or "")))

    if low_only:
        products = [p for p in products if product_available_qty(p) <= LOW_STOCK_THRESHOLD]
        title = f"⚠️ <b>Товары с остатком ≤ {LOW_STOCK_THRESHOLD} шт.</b>"
    else:
        title = "📦 <b>Остатки Uzum FBO</b>"

    if not products:
        return [title + "\n\nНет товаров для отображения."]

    total_units = sum(product_available_qty(p) for p in products)

    lines = [
        title,
        f"Всего SKU: {len(products)}",
        f"Общий доступный остаток: <b>{total_units} шт.</b>",
    ]

    for idx, product in enumerate(products[:80], start=1):
        lines.append(format_stock_line(product, idx))

    if len(products) > 80:
        lines.append(f"Показаны первые 80 SKU из {len(products)}.")

    return split_long_message("\n\n".join(lines))



def format_missing_line(product: dict, idx: int) -> str:
    missing = product_missing_qty(product)
    available = product_available_qty(product)
    title = short_product_name(product.get("productTitle") or "-")
    sku = product.get("skuFullTitle") or product.get("skuTitle") or "-"
    status = product_status_text(product)
    price = product.get("price")

    return (
        f"{idx}. <b>{escape(title)}</b>\n"
        f"SKU: {escape(str(sku))}\n"
        f"Потеряно: <b>{missing} шт.</b> | Остаток: {available} шт. | Статус: {escape(str(status))}"
        + (f" | Цена: {money(price)}" if price is not None else "")
    )


def build_missing_messages() -> list[str]:
    products = get_all_products()
    products = [p for p in products if not p.get("archived") and product_missing_qty(p) > 0]
    products = sorted(products, key=lambda p: product_missing_qty(p), reverse=True)

    title = "🧭 <b>Потерянные товары Uzum FBO</b>"

    if not products:
        return [title + "\n\nПотерянных товаров не найдено."]

    total_missing = sum(product_missing_qty(p) for p in products)
    approx_value = sum(product_missing_qty(p) * int(p.get("price") or 0) for p in products)

    lines = [
        title,
        f"SKU с потерями: {len(products)}",
        f"Всего потеряно: <b>{total_missing} шт.</b>",
        f"Примерная сумма по текущей цене: <b>{money(approx_value)}</b>",
    ]

    for idx, product in enumerate(products[:80], start=1):
        lines.append(format_missing_line(product, idx))

    if len(products) > 80:
        lines.append(f"Показаны первые 80 SKU из {len(products)}.")

    lines.append("<i>Команда использует поле quantityMissing из списка товаров Uzum.</i>")

    return split_long_message("\n\n".join(lines))


def normalize_command(text: str) -> str:
    stripped = text.strip()
    mapping = {
        "💰 Баланс": "/balance",
        "📊 Сегодня": "/today",
        "📦 Остатки": "/stock",
        "⚠️ Заканчивается": "/lowstock",
        "🧭 Потерянные": "/missing",
        "ℹ️ Помощь": "/help",
    }
    return mapping.get(stripped, stripped)


def parse_days_from_command(text: str, default_days: int) -> int:
    parts = text.strip().split()
    if len(parts) >= 2:
        try:
            days = int(parts[1])
            return max(1, min(days, 365))
        except ValueError:
            return default_days
    return default_days


def handle_command(text: str, chat_id: int) -> None:
    # Защита: отвечаем только владельцам, чьи chat_id указаны в переменной TELEGRAM_CHAT_ID.
    if str(chat_id) not in TELEGRAM_CHAT_IDS:
        return

    text = normalize_command(text)
    cmd = text.strip().split()[0].lower()

    if cmd in ("/start", "/help"):
        send_telegram_message(
            """🤖 <b>Uzum FBO bot работает</b>

Команды:
💰 /balance — баланс за последние 30 дней
📆 /balance 7 — баланс за 7 дней
📊 /today — продажи за сегодня
📦 /stock — остатки товаров
⚠️ /lowstock — товары, которые заканчиваются
🧭 /missing — потерянные товары
ℹ️ /help — список команд""",
            chat_id,
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if cmd == "/balance":
        days = parse_days_from_command(text, BALANCE_LOOKBACK_DAYS)
        send_telegram_message("⏳ Считаю баланс...", chat_id)
        send_telegram_message(build_balance_message(days), chat_id)
        return

    if cmd == "/today":
        send_telegram_message("⏳ Считаю продажи за сегодня...", chat_id)
        send_telegram_message(build_today_message(), chat_id)
        return

    if cmd == "/stock":
        send_telegram_message("⏳ Получаю остатки...", chat_id)
        for msg in build_stock_messages(low_only=False):
            send_telegram_message(msg, chat_id)
        return

    if cmd == "/lowstock":
        send_telegram_message("⏳ Проверяю товары, которые заканчиваются...", chat_id)
        for msg in build_stock_messages(low_only=True):
            send_telegram_message(msg, chat_id)
        return


    if cmd in ("/missing", "/lost"):
        send_telegram_message("⏳ Проверяю потерянные товары...", chat_id)
        for msg in build_missing_messages():
            send_telegram_message(msg, chat_id)
        return


def check_telegram_commands() -> None:
    offset = get_state("telegram_update_offset", "0")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "offset": int(offset),
        "timeout": 1,
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        return

    updates = data.get("result", [])
    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None:
            set_state("telegram_update_offset", str(int(update_id) + 1))

        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        text = message.get("text", "")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        if text and chat_id:
            handle_command(text, int(chat_id))


def check_new_sales() -> None:
    sales = get_recent_sales()
    sales = sorted(sales, key=lambda x: x.get("date", 0))

    new_count = 0

    for item in sales:
        sale_id = item.get("id")
        if not sale_id:
            continue

        sale_id = int(sale_id)

        if not is_seen(sale_id):
            send_telegram_message(build_sale_message(item))
            mark_seen(sale_id, item.get("orderId"))
            new_count += 1

    print(
        f"{datetime.now(tz=TZ).strftime('%d.%m.%Y %H:%M:%S')} | "
        f"Проверено: {len(sales)} | Новых: {new_count}"
    )


def main() -> None:
    check_required_env()
    init_db()

    print("Uzum Telegram bot запущен.")
    print(f"Shop ID: {UZUM_SHOP_ID}")
    print(f"Chat IDs: {', '.join(TELEGRAM_CHAT_IDS)}")
    print(f"Интервал проверки продаж: {CHECK_INTERVAL_SECONDS} секунд")
    print(f"Интервал проверки команд: {COMMAND_POLL_INTERVAL_SECONDS} секунд")

    next_sales_check = 0.0

    while True:
        try:
            check_telegram_commands()

            now_ts = time.time()
            if now_ts >= next_sales_check:
                check_new_sales()
                next_sales_check = now_ts + CHECK_INTERVAL_SECONDS

        except Exception as e:
            print("Ошибка:", repr(e))

        time.sleep(COMMAND_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
