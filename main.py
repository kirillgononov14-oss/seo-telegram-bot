import logging
import os
import requests
import threading
import time
import asyncio
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile
from openai import OpenAI

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

class Onboarding(StatesGroup):
    product = State()
    region = State()
    site = State()
    competitors = State()
    price = State()

users_data = {}

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

def ask_qwen(prompt, max_tokens=900):
    try:
        response = groq_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[
                {"role": "system", "content": "You are an elite SEO copywriter and business analyst. Answer in Russian language only. Be concise and structured."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_tokens,
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error("Groq error: " + str(e))
        return "Ошибка: " + str(e)[:200]

def generate_image(prompt):
    try:
        url = "https://image.pollinations.ai/prompt/" + requests.utils.quote(prompt)
        response = requests.get(url, timeout=60)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as e:
        logging.error("Image error: " + str(e))
        return None

async def send_long_message(message, text):
    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        await message.answer(text)
    else:
        parts = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]
        for part in parts:
            await message.answer(part)
            await asyncio.sleep(0.5)

@dp.message(Command("start"))
async def cmd_start(message, state: FSMContext):
    await message.answer(
        "👋 Привет! Я SEO-фабрика трафика.\n\n"
        "Я сам проанализирую твой бизнес, найду ключевые слова и боли ЦА, "
        "и буду писать готовые статьи с картинками.\n\n"
        "❓ Вопрос 1 из 5:\n"
        "Что именно ты продаешь? Опиши 2-3 словами."
    )
    await state.set_state(Onboarding.product)

@dp.message(Onboarding.product)
async def get_product(message, state: FSMContext):
    users_data[message.from_user.id] = {"product": message.text}
    await message.answer("✅ Принял!\n\n❓ Вопрос 2 из 5:\nВ каком регионе работаешь?")
    await state.set_state(Onboarding.region)

@dp.message(Onboarding.region)
async def get_region(message, state: FSMContext):
    users_data[message.from_user.id]["region"] = message.text
    await message.answer("✅ Принял!\n\n❓ Вопрос 3 из 5:\nПришли ссылку на свой сайт (если нет - напиши нет).")
    await state.set_state(Onboarding.site)

@dp.message(Onboarding.site)
async def get_site(message, state: FSMContext):
    users_data[message.from_user.id]["site"] = message.text
    await message.answer("✅ Принял!\n\n❓ Вопрос 4 из 5:\nПришли ссылки на 3-5 конкурентов.")
    await state.set_state(Onboarding.competitors)

@dp.message(Onboarding.competitors)
async def get_competitors(message, state: FSMContext):
    users_data[message.from_user.id]["competitors"] = message.text
    await message.answer("✅ Принял!\n\n❓ Вопрос 5 из 5:\nКакой ценовой сегмент? Напиши: эконом / средний / премиум.")
    await state.set_state(Onboarding.price)

@dp.message(Onboarding.price)
async def get_price(message, state: FSMContext):
    user_id = message.from_user.id
    users_data[user_id]["price"] = message.text
    await message.answer("⏳ Анализирую нишу... Это займет 1-2 минуты.")
    data = users_data[user_id]

    analysis_text = "Продукт: " + data['product'] + "\n"
    analysis_text += "Регион: " + data['region'] + "\n"
    analysis_text += "Конкуренты: " + data['competitors'] + "\n"
    analysis_text += "Сегмент: " + data['price'] + "\n\n"
    analysis_text += "Сделай кратко:\n"
    analysis_text += "1. 15 ключевых запросов (самые важные)\n"
    analysis_text += "2. 10 главных болей ЦА\n"
    analysis_text += "3. Короткое УТП (2-3 предложения)\n"
    analysis_text += "4. Контент-план на 7 дней\n\n"
    analysis_text += "Оформи так:\n"
    analysis_text += "=== КЛЮЧИ ===\n"
    analysis_text += "=== БОЛИ ЦА ===\n"
    analysis_text += "=== УТП ===\n"
    analysis_text += "=== КОНТЕНТ-ПЛАН ===\n"

    analysis = ask_qwen(analysis_text, max_tokens=900)
    users_data[user_id]["analysis"] = analysis
    
    await asyncio.sleep(1)

    guide_text = "Напиши КРАТКУЮ пошаговую инструкцию для чайника: как опубликовать SEO-статью на сайте. "
    guide_text += "5 простых шагов без терминов."

    guide = ask_qwen(guide_text, max_tokens=500)

    await send_long_message(message, "🎯 АНАЛИЗ ГОТОВ!\n\n" + analysis)
    await send_long_message(message, "📚 ИНСТРУКЦИЯ ПО ПУБЛИКАЦИИ:\n\n" + guide)
    await message.answer("Готов получить первую статью - напиши: /article")
    await state.clear()

@dp.message(Command("article"))
async def generate_article(message):
    user_id = message.from_user.id
    if user_id not in users_data or "analysis" not in users_data[user_id]:
        await message.answer("Сначала пройди онбординг: /start")
        return
    await message.answer("✍️ Пишу статью... 1-2 минуты.")
    data = users_data[user_id]

    article_text = "На основе анализа:\n" + data['analysis'] + "\n\n"
    article_text += "Напиши SEO-статью:\n"
    article_text += "1. H1 с ключом\n"
    article_text += "2. Вступление с болью ЦА\n"
    article_text += "3. H2, H3, списки\n"
    article_text += "4. 1500-2000 слов\n"
    article_text += "5. В конце нативный призыв\n"
    article_text += "6. Title (до 60 симв) и Description (до 160 симв)\n\n"
    article_text += "Оформи:\n"
    article_text += "=== TITLE ===\n"
    article_text += "=== DESCRIPTION ===\n"
    article_text += "=== СТАТЬЯ ===\n"

    article = ask_qwen(article_text, max_tokens=900)
    
    await send_long_message(message, "📄 СТАТЬЯ ГОТОВА:\n\n" + article)

    await message.answer("🎨 Генерирую обложку...")
    img_prompt = "professional photo, " + data['product'] + ", high quality"
    image_bytes = generate_image(img_prompt)
    if image_bytes:
        photo = BufferedInputFile(image_bytes, filename="article_image.jpg")
        await message.answer_photo(photo, caption="🖼 Обложка готова!")
    else:
        await message.answer("⚠️ Картинку не сгенерировал, но статья готова.")

async def main():
    logging.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    import asyncio
    asyncio.run(main())
