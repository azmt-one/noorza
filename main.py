import os
import time
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


load_dotenv()

UZUM_TOKEN = os.getenv("UZUM_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
UZUM_SHOP_ID = os.getenv("UZUM_SHOP_ID", "113982").strip()

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "2"))

UZUM_URL = "https://api-seller.uzum.uz/api/seller-openapi/v1/finance/orders"
DB_PATH = os.getenv("DB_PATH", "uzum_sales.db")
TZ = ZoneInfo("Asia/Tashkent")


def init_db() -> None:
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


def get_sales() -> list[dict]:
    now = datetime.now(tz=TZ)
    date_from = int((now - timedelta(days=LOOKBACK_DAYS)).timestamp())
    date_to = int((now + timedelta(hours=1)).timestamp() * 1000)

    params = {
        "page": 0,
        "size": 50,
        "group": "false",
        "dateFrom": date_from,
        "dateTo": date_to,
        "shopIds": UZUM_SHOP_ID,
    }

    headers = {
        # В Swagger написано: Authorization без префикса Bearer
        "Authorization": UZUM_TOKEN,
        "Accept": "application/json",
    }

    response = requests.get(UZUM_URL, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data.get("orderItems", [])


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()


def build_message(item: dict) -> str:
    title = item.get("productTitle") or "-"
    sku = item.get("skuTitle") or "-"
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
<b>Статус:</b> {item.get("status", "-")}
<b>Дата:</b> {format_date(item.get("date"))}"""


def check_required_env() -> None:
    missing = []
    if not UZUM_TOKEN:
        missing.append("UZUM_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not UZUM_SHOP_ID:
        missing.append("UZUM_SHOP_ID")

    if missing:
        raise RuntimeError("Не заполнены переменные в .env: " + ", ".join(missing))


def main() -> None:
    check_required_env()
    init_db()

    print("Uzum Telegram bot запущен.")
    print(f"Shop ID: {UZUM_SHOP_ID}")
    print(f"Интервал проверки: {CHECK_INTERVAL_SECONDS} секунд")

    while True:
        try:
            sales = get_sales()
            sales = sorted(sales, key=lambda x: x.get("date", 0))

            new_count = 0

            for item in sales:
                sale_id = item.get("id")
                if not sale_id:
                    continue

                sale_id = int(sale_id)

                if not is_seen(sale_id):
                    send_telegram_message(build_message(item))
                    mark_seen(sale_id, item.get("orderId"))
                    new_count += 1

            print(f"{datetime.now(tz=TZ).strftime('%d.%m.%Y %H:%M:%S')} | Проверено: {len(sales)} | Новых: {new_count}")

        except Exception as e:
            print("Ошибка:", repr(e))

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
