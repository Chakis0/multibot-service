import os
import telebot
from telebot import types
import requests

BASE_URL = "https://alexabot-kg4y.onrender.com"  # Твой Render URL

# Токен бота — лучше читать из переменной окружения, но пока можно так:
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8327166939:AAHkKaYzsob_B8bKyH2n25gvURpaEMsMLtY")  # <-- подставлен твой текущий

bot = telebot.TeleBot(TOKEN)

WHITELIST = [958579430]  # твой chat_id

def has_access(chat_id): return chat_id in WHITELIST

@bot.message_handler(commands=['start'])
def start(message):
    if not has_access(message.chat.id):
        bot.send_message(message.chat.id, "⛔ У вас нет доступа")
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("Проснись", callback_data="wake_up"),
        types.InlineKeyboardButton("Оплатить", callback_data="pay"),
    )
    bot.send_message(message.chat.id, "Привет! Используй кнопки ниже:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    if not has_access(call.message.chat.id):
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа")
        return

    if call.data == "wake_up":
        try:
            r = requests.get(f"{BASE_URL}/health", timeout=8)
            if r.ok:
                bot.answer_callback_query(call.id, "Сервер проснулся ✅")
            else:
                bot.answer_callback_query(call.id, f"Ответ сервера: {r.status_code}")
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ {e}")

    elif call.data == "pay":
        try:
            response = requests.get(
                "https://alexabot-kg4y.onrender.com/create_payment",
                params={
                    "amount": 500,  # сумма в RUB (можешь поменять)
                    "chat_id": call.message.chat.id
                },
                timeout=20
            )
            response.raise_for_status()
            data = response.json()
            link = data.get("payment_link")
            order_id = data.get("order_id")
            if link:
                bot.send_message(
                    call.message.chat.id,
                    f"Ссылка на оплату:\n{link}\n\nOrder ID: {order_id}"
                )
            else:
                bot.send_message(call.message.chat.id, f"Ответ сервера: {data}")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"Ошибка при создании платежа ❌\n{e}")

@bot.message_handler(commands=['getid'])
def get_id(message):
    bot.send_message(message.chat.id, f"Твой chat_id: {message.chat.id}")
    
bot.infinity_polling()