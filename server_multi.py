# --- imports ---
import os
import uuid
import hashlib
import json
from pathlib import Path
from collections import defaultdict
from typing import Dict, Any

import requests
from requests.adapters import HTTPAdapter, Retry
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
import telebot
from telebot import types


# =============================
# Config helpers
# =============================

def env_json(name: str, default: Dict[str, Any] | None = None) -> Dict[str, Any]:
    raw = os.getenv(name)
    if not raw:
        return default or {}
    try:
        return json.loads(raw)
    except Exception:
        return default or {}


PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
BOT_KEYS_CSV = os.getenv("BOT_KEYS", "").strip()
BOT_KEYS = [k.strip() for k in BOT_KEYS_CSV.split(",") if k.strip()]

TELEGRAM_TOKENS = env_json("TELEGRAM_TOKENS")        # {bot_key: token}
TG_WEBHOOK_SECRETS = env_json("TG_WEBHOOK_SECRETS")  # {bot_key: secret} (можно пустым)
MERCHANT_IDS = env_json("MERCHANT_IDS")              # {bot_key: merchant_id}
SECRET_KEYS = env_json("SECRET_KEYS")                # {bot_key: nicepay_secret}

# Базовый whitelist общий для всех ботов
BASE_WHITELIST = {958579430, 8051914154, 2095741832, 7167283179}

if not BOT_KEYS:
    raise RuntimeError("BOT_KEYS пуст. Укажи список ключей через запятую (например: 'bot1,bot2').")

for k in BOT_KEYS:
    if k not in TELEGRAM_TOKENS:
        raise RuntimeError(f"TELEGRAM_TOKENS не содержит токен для '{k}'")
    if k not in MERCHANT_IDS:
        raise RuntimeError(f"MERCHANT_IDS не содержит merchant_id для '{k}'")
    if k not in SECRET_KEYS:
        raise RuntimeError(f"SECRET_KEYS не содержит nicepay secret для '{k}'")
    # если нет индивидуального секрета вебхука — используем глобальный, если задан
    if k not in TG_WEBHOOK_SECRETS:
        TG_WEBHOOK_SECRETS[k] = os.getenv("TG_WEBHOOK_SECRET", "")


# =============================
# App & State
# =============================
app = FastAPI()

# bot_key -> TeleBot
bots: Dict[str, telebot.TeleBot] = {}

# last link messages: {bot_key: {chat_id: {...}}}
last_link_msg: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)

# order_id -> (bot_key, chat_id, message_id)
order_map: Dict[str, Dict[str, Any]] = {}

# Dynamic whitelists per bot
WHITELIST_DIR = Path("whitelists")
WHITELIST_DIR.mkdir(exist_ok=True)

def wl_file(bot_key: str) -> Path:
    return WHITELIST_DIR / f"whitelist_{bot_key}.json"

def load_dynamic_whitelist(bot_key: str) -> set[int]:
    p = wl_file(bot_key)
    if p.exists():
        try:
            return set(int(x) for x in json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_dynamic_whitelist(bot_key: str, ids: set[int]) -> None:
    wl_file(bot_key).write_text(json.dumps(list(ids)), encoding="utf-8")

DYNAMIC_WHITELISTS: Dict[str, set[int]] = {k: load_dynamic_whitelist(k) for k in BOT_KEYS}

def has_access(bot_key: str, chat_id: int) -> bool:
    return (chat_id in BASE_WHITELIST) or (chat_id in DYNAMIC_WHITELISTS.get(bot_key, set()))

def fmt_rub(amount_int: int) -> str:
    return f"{amount_int:,}".replace(",", " ")


# =============================
# Bot handlers per bot
# =============================
def attach_handlers(bot_key: str, bot: telebot.TeleBot):
    """Регистрирует хендлеры команд и колбеков для конкретного бота."""

    @bot.message_handler(commands=['getid'])
    def getid(message):
        bot.send_message(message.chat.id, f"Твой chat_id: {message.chat.id}")

    @bot.message_handler(commands=['info'])
    def info(message):
        if not has_access(bot_key, message.chat.id):
            bot.send_message(message.chat.id, "⛔ У вас нет доступа")
            return
        if message.chat.id not in last_link_msg[bot_key]:
            bot.send_message(message.chat.id, "⚠️ Нет последнего платежа для редактирования")
            return
        try:
            raw = message.text[len("/info"):].strip()
            # Свободный текст без форматов; если пусто — ничего не делаем
            base = last_link_msg[bot_key][message.chat.id].get("base_text", "")
            new_text = base + ("\n" + raw if raw else "")
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=last_link_msg[bot_key][message.chat.id]["message_id"],
                text=new_text,
                disable_web_page_preview=True
            )
            last_link_msg[bot_key][message.chat.id]["base_text"] = new_text
        except Exception as e:
            bot.send_message(message.chat.id, f"⚠️ Ошибка при редактировании: {e}")

    @bot.message_handler(commands=['add'])
    def add_user(message):
        if message.chat.id not in BASE_WHITELIST:
            bot.send_message(message.chat.id, "⛔ У тебя нет прав")
            return
        parts = message.text.strip().split()
        if len(parts) != 2 or not parts[1].isdigit():
            bot.send_message(message.chat.id, "⚠️ Используй: /add <chat_id>")
            return
        new_id = int(parts[1])
        DYNAMIC_WHITELISTS[bot_key].add(new_id)
        save_dynamic_whitelist(bot_key, DYNAMIC_WHITELISTS[bot_key])
        bot.send_message(message.chat.id, f"✅ Пользователь {new_id} добавлен")

    @bot.message_handler(commands=['delete'])
    def delete_user(message):
        if message.chat.id not in BASE_WHITELIST:
            bot.send_message(message.chat.id, "⛔ У тебя нет прав")
            return
        parts = message.text.strip().split()
        if len(parts) != 2 or not parts[1].isdigit():
            bot.send_message(message.chat.id, "⚠️ Используй: /delete <chat_id>")
            return
        del_id = int(parts[1])
        if del_id in DYNAMIC_WHITELISTS[bot_key]:
            DYNAMIC_WHITELISTS[bot_key].remove(del_id)
            save_dynamic_whitelist(bot_key, DYNAMIC_WHITELISTS[bot_key])
            bot.send_message(message.chat.id, f"🚫 Пользователь {del_id} удалён")
        else:
            bot.send_message(message.chat.id, "⚠️ Такого chat_id нет среди добавленных")

    @bot.message_handler(commands=['start'])
    def start(message):
        if not has_access(bot_key, message.chat.id):
            bot.send_message(message.chat.id, "⛔ У вас нет доступа")
            return
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Оплатить", callback_data="pay_custom"))
        kb.add(types.InlineKeyboardButton("Проснись", callback_data="wake_up"))
        bot.send_message(message.chat.id, "Нажми «Оплатить», затем введи сумму (200–85000 ₽).", reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: True)
    def callback(call):
        # Быстро подтверждаем callback, чтобы Telegram не ругался "query is too old"
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

        if not has_access(bot_key, call.message.chat.id):
            return

        if call.data == "wake_up":
            return

        if call.data == "pay_custom":
            msg = bot.send_message(call.message.chat.id, "Введи сумму в рублях (200–85000):")
            bot.register_next_step_handler(msg, handle_custom_amount)
            return

    def handle_custom_amount(message):
        if not has_access(bot_key, message.chat.id):
            bot.send_message(message.chat.id, "⛔ У вас нет доступа")
            return
        try:
            amt = int(message.text.strip())
            if amt < 200 or amt > 85000:
                bot.send_message(message.chat.id, "Сумма вне лимитов Nicepay (200–85000 ₽).")
                return

            result = create_payment_core(bot_key, amt, message.chat.id, "RUB")
            link = result.get("payment_link")
            oid = result.get("order_id")

            text = f"💳 Ссылка на оплату ({fmt_rub(amt)} ₽):\n{link}"
            msg = bot.send_message(message.chat.id, text, disable_web_page_preview=True)

            last_link_msg[bot_key][message.chat.id] = {
                "message_id": msg.message_id,
                "order_id": oid,
                "base_text": text
            }
            order_map[oid] = {
                "bot_key": bot_key,
                "chat_id": message.chat.id,
                "message_id": msg.message_id
            }
        except ValueError:
            bot.send_message(message.chat.id, "Введите целое число без копеек.")
        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка при создании платежа ❌\n{e}")


# =============================
# HTTP session with retries (Nicepay)
# =============================
_session = requests.Session()
_retries = Retry(
    total=5,
    connect=5,
    read=5,
    backoff_factor=0.8,              # ~0.8s, 1.6s, 3.2s, 6.4s, 12.8s
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retries, pool_maxsize=20))


# =============================
# Payment core (per-bot merchant)
# =============================
def create_payment_core(bot_key: str, amount: int, chat_id: int, currency: str = "RUB") -> Dict[str, Any]:
    if currency == "RUB":
        if amount < 200 or amount > 85000:
            raise HTTPException(400, "Amount must be between 200 and 85000 RUB")
        amount_minor = amount * 100
    elif currency == "USD":
        if amount < 10 or amount > 990:
            raise HTTPException(400, "Amount must be between 10 and 990 USD")
        amount_minor = amount * 100
    else:
        raise HTTPException(400, "Unsupported currency")

    order_id = f"{bot_key}-{chat_id}-{uuid.uuid4().hex[:8]}"
    uniq = uuid.uuid4().hex[:4]
    customer_id = f"u{chat_id}{uniq}"

    payload = {
        "merchant_id": MERCHANT_IDS[bot_key],
        "secret":      SECRET_KEYS[bot_key],
        "order_id":    order_id,
        "customer":    customer_id,
        "account":     customer_id,
        "amount":      amount_minor,
        "currency":    currency,
        "description": f"Top up from Telegram bot ({bot_key})",
    }
    try:
        # Раздельные таймауты: 12s на connect, 60s на read
        r = _session.post(
            "https://nicepay.io/public/api/payment",
            json=payload,
            timeout=(12, 60),
        )
        data = r.json()
    except Exception as e:
        raise HTTPException(502, f"Nicepay request failed: {e}")

    if data.get("status") == "success":
        link = (data.get("data") or {}).get("link")
        if not link:
            raise HTTPException(502, "Nicepay success without link")
        return {"payment_link": link, "order_id": order_id}
    else:
        msg = (data.get("data") or {}).get("message", "Unknown Nicepay error")
        raise HTTPException(400, f"Nicepay error: {msg}")


# =============================
# HTTP endpoints
# =============================
@app.get("/health")
def health():
    return {"ok": True, "bots": BOT_KEYS}

@app.post("/tg-webhook/{bot_key}")
async def tg_webhook(bot_key: str, request: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if bot_key not in bots:
        return JSONResponse({"ok": True}, status_code=200)

    expected = TG_WEBHOOK_SECRETS.get(bot_key) or os.getenv("TG_WEBHOOK_SECRET", "")
    if expected and x_telegram_bot_api_secret_token != expected:
        # молча игнорируем, но 200 OK, чтобы TG не ретраил
        return {"ok": True}

    try:
        payload = await request.body()
        update = telebot.types.Update.de_json(payload.decode("utf-8"))
        bots[bot_key].process_new_updates([update])
    except Exception as e:
        print(f"TG webhook error ({bot_key}):", e)
    return {"ok": True}

@app.get("/webhook")
async def nicepay_webhook(request: Request):
    params = dict(request.query_params)
    received_hash = params.pop("hash", None)
    if not received_hash:
        raise HTTPException(400, "hash missing")

    order_id = params.get("order_id", "")
    bot_key = order_id.split("-", 1)[0] if "-" in order_id else None
    if not bot_key or bot_key not in SECRET_KEYS:
        raise HTTPException(400, "unknown bot_key in order_id")

    # Nicepay hash: отсортированные значения + SECRET_KEY[bot_key]
    base = "{np}".join([v for _, v in sorted(params.items(), key=lambda x: x[0])] + [SECRET_KEYS[bot_key]])
    calc_hash = hashlib.sha256(base.encode()).hexdigest()
    if calc_hash != received_hash:
        raise HTTPException(400, "bad hash")

    result     = params.get("result")
    amount_str = params.get("amount", "0")
    amount_cur = params.get("amount_currency", "")
    profit_str = params.get("profit")
    profit_cur = params.get("profit_currency")

    def minor_to_human(x: str, cur: str) -> str:
        try:
            val = int(x)
        except Exception:
            return x
        if cur in ("RUB", "USD", "USDT"):
            return f"{val/100:.2f}"
        return str(val)

    amount_human = minor_to_human(amount_str, amount_cur)
    profit_human = minor_to_human(profit_str, profit_cur) if profit_str is not None else None

    chat_id = None
    try:
        parts = order_id.split("-")
        if len(parts) >= 3:
            chat_id = int(parts[1])
    except Exception:
        pass

    if result == "success" and chat_id is not None:
        try:
            b = bots.get(bot_key)
            if b:
                if profit_human and profit_cur:
                    text = f"✅ Оплата подтверждена. Сумма: {amount_human} {amount_cur} (на счёт: {profit_human} {profit_cur})"
                else:
                    text = f"✅ Оплата подтверждена. Сумма: {amount_human} {amount_cur}"
                b.send_message(chat_id, text)
        except Exception as e:
            print(f"send_message error ({bot_key}):", e)

    return {"ok": True}

@app.get("/create_payment")
def create_payment(amount: int, chat_id: int, currency: str = "RUB", bot_key: str = ""):
    if not bot_key or bot_key not in bots:
        raise HTTPException(400, "unknown or missing bot_key")
    return create_payment_core(bot_key, amount, chat_id, currency)


# =============================
# Bootstrap: init all bots
# =============================
for k in BOT_KEYS:
    token = TELEGRAM_TOKENS[k]
    b = telebot.TeleBot(token, threaded=False)
    attach_handlers(k, b)
    bots[k] = b

# Вебхуки нужно ставить так (для справки):
# https://api.telegram.org/bot<TOKEN_botX>/setWebhook?url=<PUBLIC_BASE_URL>/tg-webhook/<bot_key>&secret_token=<SECRET_for_bot_key>
