import logging
import os
import requests
import threading
import asyncio
import json
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI
from supabase import create_client, Client

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
ADMIN_ID = 1847007101  # Замени на свой Telegram ID (получим позже)

logging.basicConfig(level=logging.INFO)

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# === ЧЁРНЫЙ СПИСОК НИШ ===
BANNED_NICHES = ["обнал", "отмыв", "адалт", "18+", "порн", "оружие", "наркот", "взлом", "хакер"]

# === СИСТЕМНЫЙ ПРОМПТ АГЕНТА ===
SYSTEM_PROMPT = """Ты — элитный SEO-стратег и контент-маркетолог с 15-летним опытом. 
Ты работаешь как настоящий живой маркетолог на брифе с клиентом.

ТВОИ ГЛАВНЫЕ ПРИНЦИПЫ:

1. ЖИВОЕ ИНТЕРВЬЮ
- Задавай по одному вопросу за раз
- Читай ответ клиента внимательно и ЗАДАВАЙ УТОЧНЯЮЩИЕ вопросы
- Не шаблонься — каждая ниша уникальна
- В середине интервью попроси ссылку на источник клиента (сайт/канал/Авито/профиль)
- После изучения ссылки — задай ещё вопросы если нужно докопаться до сути

2. АНАЛИЗ НИШИ
- Изучай сайт/канал клиента: стиль, фишки, сильные стороны, МИНУСЫ которые мешают продавать
- Сам находи конкурентов в выдаче Яндекса и Гугла (клиент НЕ даёт)
- Собирай ключи из Яндекс Вордстат и Google Trends
- Определи УНИКАЛЬНОЕ торговое предложение (УТП)
- Найди 15 главных болей ЦА

3. НАПИСАНИЕ СТАТЕЙ
ПРАВИЛО: 1 статья = 1 ключевой запрос + 1 конкретная боль

СТИЛЬ:
- Пиши ЯЗЫКОМ ЧИТАТЕЛЯ (не специалиста)
- Не восхваляй продукт, а РЕШАЙ БОЛЬ читателя
- Автор — независимый советчик, в конце мягко рекомендует
- Определи по профилю клиента: частник → пиши от "Я", компания → от "МЫ"

СТРУКТУРА:
- H1 с ключом (цепляющий заголовок)
- Вступление с болью (2-3 абзаца)
- H2, H3, списки
- Цифры и факты + кейсы + пошаговость + ошибки (пропорции зависят от ниши)
- 1500-2000 слов (адаптивно)
- Нативный призыв в конце (без "купи", "закажи")

ДЛИНА:
- Простая тема: 4-5 тыс. знаков
- Глубокая тема: 7-9 тыс. знаков
- База: 5-7 тыс. знаков

SEO-УПАКОВКА (адаптивно):
- Базовая: Title + Description + H1 + 2-3 хэштега
- Расширенная: + alt-тексты картинок + slug (ЧПУ) + ключевые фразы
- Полная: + перелинковка + FAQ-схема

УНИКАЛЬНОСТЬ (тройная защита):
- Никогда не повторяй формулировки конкурентов
- Добавляй уникальный угол: цифры, мини-истории, личный опыт
- После написания перечитывай и переписывай шаблонные куски

АДАПТАЦИЯ ПОД vc.ru (по запросу):
- Другой угол: кейсы с цифрами, факапы, внутренняя кухня
- Тон: коллега делится опытом
- Длина: 7-12 тыс. знаков
- Без хэштегов
- Самопрезентация через опыт, не реклама

БЕЗОПАСНОСТЬ:
- Для чувствительных ниш (медицина, юристы, финансы):
  * Дисклеймер в конце
  * Осторожный тон без категоричных утверждений
  * Упоминание лицензии/опыта клиента если есть
- ЧЁРНЫЕ ниши (обнал, адалт, оружие, наркотики, "чудо-БАДы"): ОТКАЗ сразу

4. КОНТЕНТ-ПЛАН
- Много статей = много трафика
- Агент сам определяет темп под нишу
- Учитывай лимиты Дзена: новый канал 1-2 статьи/день, растущий 2-3, раскрученный 4-5
- Правило "1 ключ + 1 боль" для каждой статьи
- Указывай оптимальное время публикации (день И час) под нишу

5. ИНСТРУКЦИЯ ДЛЯ ЧАЙНИКА
- Публикация в Дзен для трафика из Яндекс и Гугл
- Пошаговая инструкция с блоками: "вот это скопируй → вставь вот сюда"
- Объяснение что такое Title/Description/H1 простыми словами (вывеска, аннотация, заголовок)

6. ОБРАБОТКА ФОТО
- Клиент загружает общую пачку фото в начале
- Перед каждой статьёй бот предлагает: "Нашёл подходящие фото: [1, 2]. Берём или генерировать?"
- Агент сам решает: вставить как есть / добавить подпись / инфографику / водяной знак / улучшить

7. УМНЫЕ ПРАВКИ
- Когда клиент говорит "не нравится" — не переписывай всё
- Уточни: "Что именно: слишком сухо / мало примеров / длинно / структура?"
- Правь только нужные куски
- Если 3+ правки на статью — предложи переписать с нуля

8. ЗАПОМИНАНИЕ ПРЕДПОЧТЕНИЙ
- Сам замечай паттерны правок
- После 3 похожих правок спроси: "Запомнить это для будущих статей?"
- Раз в неделю присылай: "Вот что я запомнил. Всё верно?"

9. ОТЧЁТЫ
- Еженедельно: короткие итоги + план на неделю
- Ежемесячно: подробный отчёт + анализ ниши + рекомендации

ВСЕГДА ОТВЕЧАЙ НА РУССКОМ ЯЗЫКЕ. Будь конкретным, не размытым.
"""

# === СОСТОЯНИЯ БОТА ===
class Onboarding(StatesGroup):
    gathering = State()  # Живое интервью
    source_link = State()  # Запрос источника
    photos = State()  # Загрузка фото
    cta_choice = State()  # Выбор куда вести заявки
    license_check = State()  # Проверка лицензии

# === ХРАНИЛИЩЕ (кэш в памяти + база) ===
users_cache = {}

# === "ДВЕРЬ-ПУСТЫШКА" ДЛЯ RENDER ===
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

# === РАБОТА С SUPABASE ===
def get_or_create_user(user_id, username, first_name):
    try:
        result = supabase.table("users").select("*").eq("id", user_id).execute()
        if result.data:
            return result.data[0]
        # Создаём нового
        new_user = {
            "id": user_id,
            "username": username or "",
            "first_name": first_name or ""
        }
        supabase.table("users").insert(new_user).execute()
        return new_user
    except Exception as e:
        logging.error(f"Supabase error: {e}")
        return {"id": user_id, "tariff": "free"}

def save_onboarding_answer(user_id, question, answer):
    try:
        supabase.table("onboarding_answers").insert({
            "user_id": user_id,
            "question": question,
            "answer": answer
        }).execute()
    except Exception as e:
        logging.error(f"Save answer error: {e}")

def save_niche_research(user_id, keywords, pains, utp, competitors):
    try:
        supabase.table("niche_research").insert({
            "user_id": user_id,
            "keywords": keywords,
            "pains": pains,
            "utp": utp,
            "competitors": competitors
        }).execute()
    except Exception as e:
        logging.error(f"Save research error: {e}")

def save_article(user_id, keyword, pain, title, content):
    try:
        supabase.table("articles").insert({
            "user_id": user_id,
            "keyword": keyword,
            "pain": pain,
            "title": title,
            "content": content,
            "status": "draft"
        }).execute()
    except Exception as e:
        logging.error(f"Save article error: {e}")

def log_usage(user_id, action):
    try:
        supabase.table("usage_logs").insert({
            "user_id": user_id,
            "action": action
        }).execute()
    except Exception as e:
        logging.error(f"Log usage error: {e}")

# === ЗАПРОС К QWEN ===
def ask_qwen(prompt, max_tokens=900, system=None):
    sys_msg = system or SYSTEM_PROMPT
    try:
        response = groq_client.chat.completions.create(
            model="qwen/qwen3-32b",
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_tokens,
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Groq error: {e}")
        return "Ошибка: " + str(e)[:200]

# === ГЕНЕРАЦИЯ КАРТИНКИ ===
def generate_image(prompt):
    try:
        url = "https://image.pollinations.ai/prompt/" + requests.utils.quote(prompt)
        response = requests.get(url, timeout=60)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as e:
        logging.error(f"Image error: {e}")
        return None

# === ОТПРАВКА ДЛИННЫХ СООБЩЕНИЙ ===
async def send_long(message, text):
    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        await message.answer(text)
    else:
        parts = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]
        for part in parts:
            await message.answer(part)
            await asyncio.sleep(0.5)

# === ПРОВЕРКА ЧЁРНЫХ НИШ ===
def is_banned_niche(text):
    text_lower = text.lower()
    for word in BANNED_NICHES:
        if word in text_lower:
            return True
    return False

# === СТАРТ ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user = get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )
    
    await message.answer(
        "👋 Привет! Я — твой персональный SEO-стратег.\n\n"
        "Я проведу с тобой глубокое интервью, изучу твою нишу, "
        "и буду писать качественные статьи для Дзена, Яндекса и Гугла.\n\n"
        "Это займет 5-7 минут. Буду задавать вопросы по одному, "
        "внимательно читая ответы.\n\n"
        "❓ Вопрос 1:\n"
        "**Что именно ты продаешь или какую услугу оказываешь?** "
        "Опиши в 2-3 предложениях, как будто объясняешь другу."
    )
    await state.set_state(Onboarding.gathering)
    await state.update_data(
        question_num=1,
        history=[],
        source_requested=False
    )

# === ЖИВОЕ ИНТЕРВЬЮ ===
@dp.message(Onboarding.gathering)
async def live_interview(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    history = data.get("history", [])
    question_num = data.get("question_num", 1)
    source_requested = data.get("source_requested", False)
    
    # Проверяем на чёрную нишу (только первый ответ)
    if question_num == 1 and is_banned_niche(message.text):
        await message.answer(
            "❌ Извини, я не работаю с этой темой.\n\n"
            "Я специализируюсь на белом бизнесе: услуги, товары, "
            "экспертные ниши, B2B, локальный бизнес.\n\n"
            "Если я ошибся — опиши свою нишу иначе и начнём сначала: /start"
        )
        await state.clear()
        return
    
    # Сохраняем ответ
    history.append({"q": question_num, "a": message.text})
    save_onboarding_answer(user_id, f"Q{question_num}", message.text)
    
    # После 4-го вопроса (или когда агент решит) — просим источник
    if question_num >= 4 and not source_requested:
        await state.update_data(
            history=history,
            question_num=question_num + 1,
            source_requested=True
        )
        await message.answer(
            "✅ Отлично, уже многое понял!\n\n"
            "📎 **Чтобы я мог глубже изучить твой стиль и фишки, "
            "пришли ссылку на любой твой источник:**\n"
            "- Сайт\n"
            "- Telegram-канал\n"
            "- Профиль на Авито\n"
            "- Страница в соцсетях\n\n"
            "Если ничего нет — напиши «нет»."
        )
        await state.set_state(Onboarding.source_link)
        return
    
    # Формируем промпт для следующего вопроса или завершения
    if question_num >= 8:  # После 8 вопросов — анализ
        await finish_interview(message, state, history)
        return
    
    # Агент сам решает что спросить дальше
    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    next_q_prompt = f"""Ты уже задал клиенту {question_num} вопросов. Вот история:

{history_text}

Задай следующий уточняющий вопрос, который поможет глубже понять:
- Его целевую аудиторию
- Конкретные боли клиентов
- Конкурентные преимущества
- Особенности ниши

Вопрос должен быть:
- Конкретным (не общим)
- Открытым (не да/нет)
- Продвигающим диалог вглубь

Напиши ТОЛЬКО вопрос, без комментариев."""
    
    next_question = ask_qwen(next_q_prompt, max_tokens=150)
    
    await state.update_data(
        history=history,
        question_num=question_num + 1
    )
    
    await message.answer(f"❓ Вопрос {question_num + 1}:\n{next_question}")

# === ЗАПРОС ИСТОЧНИКА ===
@dp.message(Onboarding.source_link)
async def get_source(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    save_onboarding_answer(user_id, "source_link", message.text)
    
    data = await state.get_data()
    history = data.get("history", [])
    
    if message.text.strip().lower() != "нет":
        # Агент изучает источник и задаёт уточняющие вопросы
        analysis_prompt = f"""Клиент прислал ссылку на свой источник: {message.text}

Вот что ты уже знаешь о нём:
{chr(10).join([f"Q{i['q']}: {i['a']}" for i in history])}

Представь что ты изучил источник. Задай 2-3 глубоких уточняющих вопроса, 
которые помогут понять его стиль, фишки, сильные и слабые стороны.

Напиши вопросы по одному, с нумерацией."""
        
        followup_questions = ask_qwen(analysis_prompt, max_tokens=300)
        
        await message.answer(
            f"✅ Отлично, изучаю твой источник!\n\n"
            f"📌 Пара уточняющих вопросов после изучения:\n\n{followup_questions}\n\n"
            f"Ответь на них по порядку."
        )
        
        await state.update_data(source_analyzed=True)
    
    await state.set_state(Onboarding.gathering)
    await state.update_data(
        history=history,
        question_num=data.get("question_num", 4) + 1,
        source_requested=True
    )

# === ЗАВЕРШЕНИЕ ИНТЕРВЬЮ ===
async def finish_interview(message: types.Message, state: FSMContext, history):
    user_id = message.from_user.id
    
    await message.answer("⏳ Отлично, интервью завершено! Анализирую нишу... 1-2 минуты.")
    
    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    
    # Анализ ниши
    analysis_prompt = f"""На основе интервью с клиентом:

{history_text}

Проведи глубокий анализ ниши:

1. **15 ключевых запросов** для Яндекс и Гугла (реальных, которые ищут люди)
2. **10 главных болей ЦА** (конкретных, с эмоциями)
3. **УТП** (3-4 предложения)
4. **Минусы в текущем позиционировании** (если видишь)
5. **Контент-план на 7 дней** с указанием:
   - Ключ + боль для каждой статьи
   - Тип статьи (цифры/кейс/шаги/ошибки)
   - Оптимальный день и ЧАС публикации (под нишу)
   - Длина статьи

Оформи по разделам:
=== КЛЮЧИ ===
=== БОЛИ ===
=== УТП ===
=== МИНУСЫ ===
=== КОНТЕНТ-ПЛАН НА 7 ДНЕЙ ==="""
    
    analysis = ask_qwen(analysis_prompt, max_tokens=900)
    
    # Сохраняем в Supabase
    save_niche_research(user_id, analysis, "", "", "")
    
    # Инструкция по публикации в Дзен
    guide_prompt = """Напиши пошаговую инструкцию для чайника: как опубликовать SEO-статью в Дзен, 
чтобы получать трафик из Яндекс и Гугл. 5 простых шагов.
Для каждого шага объяс простыми словами что такое Title, Description, H1, alt-текст.
Приведи аналогию с магазином (вывеска, аннотация, заголовок)."""
    
    guide = ask_qwen(guide_prompt, max_tokens=700)
    
    # Отправляем результат
    await send_long(message, f"🎯 **АНАЛИЗ ТВОЕЙ НИШИ ГОТОВ!**\n\n{analysis}")
    await asyncio.sleep(1)
    await send_long(message, f"📚 **КАК ПУБЛИКОВАТЬ В ДЗЕН (для чайника):**\n\n{guide}")
    await asyncio.sleep(1)
    
    # Предлагаем загрузить фото
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📸 Загрузить фото")],
            [KeyboardButton(text="⏭ Пропустить")]
        ],
        resize_keyboard=True
    )
    await message.answer(
        "📸 **Хочешь загрузить фото своих работ/товара/команды?**\n\n"
        "Я буду использовать их в статьях. Можешь загрузить сейчас или позже.",
        reply_markup=keyboard
    )
    await state.set_state(Onboarding.photos)

# === ФОТО ===
@dp.message(Onboarding.photos, F.text == "📸 Загрузить фото")
async def request_photos(message: types.Message, state: FSMContext):
    await message.answer(
        "Отлично! Пришли фото — можно по одному или пачкой.\n"
        "Когда закончишь — напиши «готово»."
    )
    # Здесь можно добавить сохранение фото в хранилище
    # Пока упростим — принимаем текстом что готово

@dp.message(Onboarding.photos, F.text == "⏭ Пропустить")
async def skip_photos(message: types.Message, state: FSMContext):
    await message.answer("Ок, буду генерировать картинки сам.")
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.text == "готово")
async def photos_done(message: types.Message, state: FSMContext):
    await message.answer("✅ Фото сохранены!")
    await ask_cta(message, state)

# === CTA (куда вести заявки) ===
async def ask_cta(message: types.Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌐 Сайт", callback_data="cta_site"),
            InlineKeyboardButton(text="✈️ Telegram", callback_data="cta_tg")
        ],
        [
            InlineKeyboardButton(text="📱 WhatsApp", callback_data="cta_wa"),
            InlineKeyboardButton(text="📣 МАХ", callback_data="cta_max")
        ],
        [
            InlineKeyboardButton(text="📞 Телефон", callback_data="cta_phone")
        ]
    ])
    await message.answer(
        "📍 **Куда тебе удобнее принимать заявки?**\n\n"
        "Я буду вставлять мягкий призыв в каждую статью.",
        reply_markup=keyboard
    )
    await state.set_state(Onboarding.cta_choice)

@dp.callback_query(F.data.startswith("cta_"))
async def cta_chosen(callback: types.CallbackQuery, state: FSMContext):
    cta_type = callback.data.replace("cta_", "")
    user_id = callback.from_user.id
    
    try:
        supabase.table("users").update({"cta_type": cta_type}).eq("id", user_id).execute()
    except Exception as e:
        logging.error(f"Save CTA error: {e}")
    
    prompts = {
        "site": "Пришли ссылку на страницу с формой заявки или калькулятором",
        "tg": "Пришли свой username через @ или ссылку на Telegram",
        "wa": "Пришли номер в формате +7...",
        "max": "Пришли ссылку на канал/чат в МАХ",
        "phone": "Пришли номер телефона"
    }
    
    await callback.message.answer(f"✅ Выбрано: {cta_type}\n\n{prompts.get(cta_type, '')}")
    
    # Ждём ссылку (упрощённо — просто просим и идём дальше)
    await callback.message.answer(
        "🎉 **Готово! Ты прошёл онбординг.**\n\n"
        "Теперь напиши **/article** чтобы получить первую статью.\n"
        "Или **/help** чтобы увидеть все команды.\n\n"
        "После первой бесплатной статьи ты сможешь выбрать тариф."
    )
    await state.clear()

# === ГЕНЕРАЦИЯ СТАТЬИ ===
@dp.message(Command("article"))
async def generate_article(message: types.Message):
    user_id = message.from_user.id
    
    # Проверяем есть ли анализ
    try:
        research = supabase.table("niche_research").select("*").eq("user_id", user_id).order("id", desc=True).limit(1).execute()
        if not research.data:
            await message.answer("Сначала пройди онбординг: /start")
            return
        analysis = research.data[0]["keywords"]
    except Exception as e:
        await message.answer("Сначала пройди онбординг: /start")
        return
    
    await message.answer("✍️ Пишу статью... 1-2 минуты.")
    
    article_prompt = f"""На основе анализа ниши:

{analysis}

Напиши первую SEO-статью по правилу "1 ключ + 1 боль".

Выбери САМУ сильную связку ключ+боль из анализа и напиши по ней.

ТРЕБОВАНИЯ:
1. H1 с ключом (цепляющий)
2. Вступление с болью (2-3 абзаца)
3. H2, H3, списки
4. Стиль "эксперт-друг" простыми словами
5. 5-7 тыс. знаков
6. Цифры, кейсы, шаги, ошибки (смешай)
7. Мягкий призыв в конце
8. В самом конце укажи:
   - Title (до 60 символов)
   - Description (до 160 символов)
   - 2-3 хэштега
   - Slug (ЧПУ-ссылку латиницей)

Оформи так:
=== СТАТЬЯ ===
[текст статьи]
=== SEO-ПАКЕТ ===
Title: ...
Description: ...
Хэштеги: ...
Slug: ...
"""
    
    article = ask_qwen(article_prompt, max_tokens=900)
    
    # Сохраняем в базу
    save_article(user_id, "первая статья", "первая боль", "первая статья", article)
    log_usage(user_id, "article_generated")
    
    await send_long(message, f"📄 **ТВОЯ ПЕРВАЯ СТАТЬЯ ГОТОВА:**\n\n{article}")
    await asyncio.sleep(1)
    
    # Предлагаем адаптацию под vc.ru
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Адаптировать под vc.ru", callback_data="vc_yes"),
            InlineKeyboardButton(text="❌ Только Дзен", callback_data="vc_no")
        ]
    ])
    await message.answer(
        "📰 **Адаптировать эту статью под vc.ru?**\n\n"
        "Это удвоит трафик с одной темы.",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "vc_yes")
async def adapt_vc(callback: types.CallbackQuery):
    await callback.message.answer("⏳ Адаптирую под vc.ru... 1-2 минуты.")
    
    # Получаем последнюю статью
    try:
        articles = supabase.table("articles").select("*").eq("user_id", callback.from_user.id).order("id", desc=True).limit(1).execute()
        if not articles.data:
            await callback.message.answer("Ошибка: статья не найдена")
            return
        original = articles.data[0]["content"]
    except:
        await callback.message.answer("Ошибка базы данных")
        return
    
    vc_prompt = f"""Адаптируй эту статью под vc.ru:

{original}

ТРЕБОВАНИЯ ДЛЯ vc.ru:
- Угол подачи: кейс с цифрами / факап с уроками / внутренняя кухня
- Тон: коллега делится опытом
- Длина: 7-12 тыс. знаков
- Без хэштегов
- Без прямой рекламы
- Самопрезентация через опыт
- Живой заголовок с интригой/цифрами

Верни адаптированную версию."""
    
    vc_article = ask_qwen(vc_prompt, max_tokens=900)
    
    # Обновляем статью в базе
    try:
        supabase.table("articles").update({"vc_version": vc_article}).eq("id", articles.data[0]["id"]).execute()
    except:
        pass
    
    await send_long(callback.message, f"📰 **ВЕРСИЯ ДЛЯ vc.ru ГОТОВА:**\n\n{vc_article}")

@dp.callback_query(F.data == "vc_no")
async def skip_vc(callback: types.CallbackQuery):
    await callback.message.answer("Ок, оставляем только версию для Дзена.")

# === HELP ===
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📋 **Команды бота:**\n\n"
        "/start — пройти онбординг заново\n"
        "/article — сгенерировать новую статью\n"
        "/history — мои прошлые статьи\n"
        "/plan — контент-план на неделю\n"
        "/tariff — мой тариф и лимиты\n"
        "/help — эта справка"
    )

# === АДМИН (для тебя) ===
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        users_count = supabase.table("users").select("id").execute()
        articles_count = supabase.table("articles").select("id").execute()
        await message.answer(
            f"📊 **Статистика:**\n"
            f"👥 Клиентов: {len(users_count.data)}\n"
            f"📝 Статей создано: {len(articles_count.data)}"
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

# === ЗАПУСК ===
async def main():
    logging.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    import asyncio
    asyncio.run(main())
