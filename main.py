import logging
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile
from openai import OpenAI

# === НАСТРОЙКИ (сюда вставь свои ключи) ===
BOT_TOKEN = "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER"
GROQ_API_KEY = "ВСТАВЬ_СЮДА_КЛЮЧ_GROQ"

# === ЛОГИ ===
logging.basicConfig(level=logging.INFO)

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

# === СОСТОЯНИЯ БОТА ===
class Onboarding(StatesGroup):
    product = State()
    region = State()
    site = State()
    competitors = State()
    price = State()

# === ХРАНИЛИЩЕ ДАННЫХ ===
users_data = {}

# === ЗАПРОС К QWEN ===
def ask_qwen(prompt: str, max_tokens: int = 4000) -> str:
    try:
        response = groq_client.chat.completions.create(
            model="qwen/qwen3-8b",
            messages=[
                {"role": "system", "content": "Ты — элитный SEO-копирайтер и бизнес-аналитик. Отвечай на русском языке."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_tokens,
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Ошибка Groq: {e}")
        return "Ошибка при обращении к ИИ. Попробуйте позже."

# === ГЕНЕРАЦИЯ КАРТИНКИ ===
def generate_image(prompt: str):
    try:
        url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}"
        response = requests.get(url, timeout=60)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as e:
        logging.error(f"Ошибка генерации картинки: {e}")
        return None

# === ИНФОГРАФИКА ПОВЕРХ ФОТО ===
def add_infographic(image_bytes: bytes, title: str, text: str):
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle([(20, 20), (img.width - 20, 180)], fill=(0, 0, 0, 180))
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        try:
            font_title = ImageFont.truetype("DejaVuSans.ttf", 40)
            font_text = ImageFont.truetype("DejaVuSans.ttf", 24)
        except Exception:
            font_title = ImageFont.load_default()
            font_text = ImageFont.load_default()
        draw.text((40, 40), title, fill="white", font=font_title)
        draw.text((40, 100), text, fill="white", font=font_text)
        output = BytesIO()
        img.convert("RGB").save(output, format="JPEG")
        output.seek(0)
        return output.getvalue()
    except Exception as e:
        logging.error(f"Ошибка инфографики: {e}")
        return image_bytes

# === СТАРТ ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer(
        "👋 Привет! Я — SEO-фабрика трафика.\n\n"
        "Я сам проанализирую твой бизнес, найду ключевые слова и боли ЦА, "
        "и буду писать готовые статьи с картинками.\n\n"
        "❓ Вопрос 1 из 5:\n"
        "Что именно ты продаёшь? Опиши 2-3 словами."
    )
    await state.set_state(Onboarding.product)

# === ВОПРОС 1 ===
@dp.message(Onboarding.product)
async def get_product(message: types.Message, state: FSMContext):
    users_data[message.from_user.id] = {"product": message.text}
    await message.answer("✅ Принял!\n\n❓ Вопрос 2 из 5:\nВ каком регионе работаешь?")
    await state.set_state(Onboarding.region)

# === ВОПРОС 2 ===
@dp.message(Onboarding.region)
async def get_region(message: types.Message, state: FSMContext):
    users_data[message.from_user.id]["region"] = message.text
    await message.answer("✅ Принял!\n\n❓ Вопрос 3 из 5:\nПришли ссылку на свой сайт (если нет — напиши «нет»).")
    await state.set_state(Onboarding.site)

# === ВОПРОС 3 ===
@dp.message(Onboarding.site)
async def get_site(message: types.Message, state: FSMContext):
    users_data[message.from_user.id]["site"] = message.text
    await message.answer("✅ Принял!\n\n❓ Вопрос 4 из 5:\nПришли ссылки на 3-5 конкурентов.")
    await state.set_state(Onboarding.competitors)

# === ВОПРОС 4 ===
@dp.message(Onboarding.competitors)
async def get_competitors(message: types.Message, state: FSMContext):
    users_data[message.from_user.id]["competitors"] = message.text
    await message.answer("✅ Принял!\n\n❓ Вопрос 5 из 5:\nКакой ценовой сегмент? Напиши: эконом / средний / премиум.")
    await state.set_state(Onboarding.price)

# === ВОПРОС 5 + АНАЛИЗ ===
@dp.message(Onboarding.price)
async def get_price(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    users_data[user_id]["price"] = message.text
    await message.answer("⏳ Анализирую нишу... 1-2 минуты.")
    data = users_data[user_id]

    analysis_prompt = f"""
Продукт: {data['product']}
Регион: {data['region']}
Конкуренты: {data['competitors']}
Сегмент: {data['price']}

Сделай:
1. 30 ключевых запросов для ниши
2. 15 главных болей ЦА
3. УТП
4. Контент-план на 2 недели (1 статья в день)

Оформи так:
=== КЛЮЧИ ===
=== БОЛИ ЦА ===
=== УТП ===
=== КОНТЕНТ-ПЛАН ===
"""
    analysis = ask_qwen(analysis_prompt, max_tokens=3000)
    users_data[user_id]["analysis"] = analysis

    guide_prompt = """
Напиши пошаговую инструкцию для чайника: как опубликовать SEO-статью на сайте.
7 простых шагов: создать страницу, вставить текст, прописать Title и Description,
добавить картинку с alt-тегом, когда публиковать. Без терминов.
"""
    guide = ask_qwen(guide_prompt, max_tokens=1500)

    await message.answer(
        f"🎯 АНАЛИЗ ГОТОВ!\n\n{analysis}\n\n---\n\n"
        f"📚 ИНСТРУКЦИЯ ПО ПУБЛИКАЦИИ:\n\n{guide}\n\n---\n\n"
        f"Готов получить первую статью — напиши: /article"
    )
    await state.clear()

# === ГЕНЕРАЦИЯ СТАТЬИ ===
@dp.message(Command("article"))
async def generate_article(message: types.Message):
    user_id = message.from_user.id
    if user_id not in users_data or "analysis" not in users_data[user_id]:
        await message.answer("Сначала пройди онбординг: /start")
        return
    await message.answer("✍️ Пишу статью... 1-2 минуты.")
    data = users_data[user_id]

    article_prompt = f"""
На основе анализа:
{data['analysis']}

Напиши первую SEO-статью:
1. H1 с ключом
2. Вступление с болью ЦА
3. H2, H3, списки
4. 2500-3000 слов
5. В конце нативный призыв (без «купи»)
6. Title (до 60 символов) и Description (до 160 символов)

Оформи:
=== TITLE ===
=== DESCRIPTION ===
=== СТАТЬЯ ===
=== НУЖНЫЕ КАРТИНКИ ===
"""
    article = ask_qwen(article_prompt, max_tokens=4000)
    await message.answer(f"📄 СТАТЬЯ ГОТОВА:\n\n{article}")

    await message.answer("🎨 Генерирую обложку...")
    image_bytes = generate_image(f"professional photo, {data['product']}, high quality")
    if image_bytes:
        photo = BufferedInputFile(image_bytes, filename="article_image.jpg")
        await message.answer_photo(photo, caption="🖼 Обложка для статьи готова!")
    else:
        await message.answer("⚠️ Картинку сгенерировать не удалось, но статья готова.")

# === ЗАПУСК ===
async def main():
    logging.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
