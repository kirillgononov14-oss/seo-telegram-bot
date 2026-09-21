import logging
import os
import requests
import threading
import asyncio
import re
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from openai import OpenAI
from supabase import create_client, Client
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
ADMIN_ID = 1847007101  # Твой Telegram ID

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
- Читай ответ клиента внимательно и задавай уточняющие вопросы
- Не шаблонься — каждая ниша уникальна
- Не выдумывай факты о клиенте — используй только то что он сам написал

2. АНАЛИЗ НИШИ
ВАЖНО: Тебе присылают РЕАЛЬНЫЕ данные источника (текст сайта/канала/объявления).
Анализируй ТОЛЬКО их. НИЧЕГО не выдумывай и не додумывай.
Если данных мало — скажи об этом и задай уточняющие вопросы.

3. НАПИСАНИЕ СТАТЕЙ
ПРАВИЛО: 1 статья = 1 ключевой запрос + 1 конкретная боль

СТИЛЬ:
- Пиши языком читателя (не специалиста)
- Не восхваляй продукт, а решай боль читателя
- Автор — независимый советчик, в конце мягко рекомендует
- Частник — пиши от "Я", компания — от "МЫ"

СТРУКТУРА:
- Цепляющий заголовок с ключом
- Вступление с болью
- Подзаголовки, списки
- Цифры и факты, кейсы, пошаговость, ошибки (пропорции зависят от ниши)
- Нативный призыв в конце (без слов "купи", "закажи")

ДЛИНА:
- Простая тема: 4-5 тыс. знаков
- Глубокая тема: 7-9 тыс. знаков
- База: 5-7 тыс. знаков

УНИКАЛЬНОСТЬ:
- Никогда не повторяй формулировки конкурентов
- Добавляй уникальный угол: цифры, мини-истории, личный опыт
- После написания переписывай шаблонные куски

4. КОНТЕНТ-ПЛАН
- Много статей = много трафика
- Учитывай лимиты Дзена: новый канал 1-2 статьи/день, растущий 2-3
- Правило "1 ключ + 1 боль" для каждой статьи
- Указывай оптимальный день и час публикации под нишу

5. ИНСТРУКЦИЯ ДЛЯ ЧАЙНИКА
- Публикация в Дзен для трафика из Яндекса и Гугла
- Пошагово с блоками: "вот это скопируй — вставь вот сюда"
- Объясняй простыми словами через аналогию с магазином (вывеска, аннотация, заголовок)

6. АДАПТАЦИЯ ПОД vc.ru (по запросу)
- Угол: кейсы с цифрами, факапы, внутренняя кухня
- Тон: коллега делится опытом
- Длина: 7-12 тыс. знаков
- Без хэштегов, без прямой рекламы

7. БЕЗОПАСНОСТЬ
- Чувствительные ниши (медицина, юристы, финансы): дисклеймер + осторожный тон
- Чёрные ниши: отказ сразу

8. УМНЫЕ ПРАВКИ
- Когда клиент говорит "не нравится" — не переписывай всё
- Уточни что именно: тон / структура / мало примеров / слишком длинно
- Правь только нужные куски

ВСЕГДА ОТВЕЧАЙ НА РУССКОМ. Будь конкретным, не размытым.
Не используй в ответах символы ** и решётки — пиши обычным текстом."""

# === СОСТОЯНИЯ ===
class Onboarding(StatesGroup):
    gathering = State()
    source_link = State()
    photos = State()
    cta_choice = State()
    cta_value = State()

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

# === ПАРСИНГ ИСТОЧНИКОВ (бот изучает сам) ===
def extract_site_text(url):
    try:
        if not url.startswith("http"):
            url = "https://" + url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "lxml")
        for script in soup(["script", "style", "nav", "footer"]):
            script.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:5000]
    except Exception as e:
        logging.error(f"Site parse error: {e}")
        return None

def extract_telegram_channel(channel_url):
    try:
        channel_name = channel_url.replace("https://t.me/", "").replace("t.me/", "").strip("/")
        channel_name = channel_name.split("/")[0]
        public_url = f"https://t.me/s/{channel_name}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(public_url, headers=headers, timeout=15)
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "lxml")
        posts = []
        for post in soup.find_all("div", class_="tgme_widget_message_text"):
            text = post.get_text(strip=True)
            if text:
                posts.append(text)
        if not posts:
            return None
        return "\n\n".join(posts[:10])[:5000]
    except Exception as e:
        logging.error(f"TG parse error: {e}")
        return None

def extract_avito(avito_id_or_url):
    try:
        # Если это просто цифры — считаем что это ID профиля
        if str(avito_id_or_url).isdigit():
            url = f"https://www.avito.ru/user/{avito_id_or_url}/shop"
        elif "avito.ru" in str(avito_id_or_url):
            url = avito_id_or_url if str(avito_id_or_url).startswith("http") else "https://" + str(avito_id_or_url)
        else:
            return None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = "utf-8"
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        if len(text) < 200:
            return None
        return text[:5000]
    except Exception as e:
        logging.error(f"Avito parse error: {e}")
        return None

def search_competitors(niche_query, max_results=5):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{niche_query} цены отзывы", region="ru-ru", max_results=max_results))
            competitors = []
            for r in results:
                competitors.append({
                    "title": r.get("title", ""),
                    "snippet": r.get("body", "")[:200]
                })
            return competitors
    except Exception as e:
        logging.error(f"Search error: {e}")
        return []

def get_yandex_suggestions(keyword):
    try:
        url = "https://suggest.yandex.net/suggest-ff.cgi"
        params = {"part": keyword, "lang": "ru", "v": "3"}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if isinstance(data, list) and len(data) > 1:
            return data[1]
        return []
    except Exception as e:
        logging.error(f"Yandex suggest error: {e}")
        return []

# === РАБОТА С SUPABASE ===
def get_or_create_user(user_id, username, first_name):
    try:
        result = supabase.table("users").select("*").eq("id", user_id).execute()
        if result.data:
            return result.data[0]
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
            "answer": str(answer)[:1000]
        }).execute()
    except Exception as e:
        logging.error(f"Save answer error: {e}")

def save_niche_research(user_id, keywords, pains, utp, competitors):
    try:
        supabase.table("niche_research").insert({
            "user_id": user_id,
            "keywords": str(keywords)[:5000],
            "pains": str(pains)[:3000],
            "utp": str(utp)[:1000],
            "competitors": str(competitors)[:3000]
        }).execute()
    except Exception as e:
        logging.error(f"Save research error: {e}")

def save_article(user_id, keyword, pain, title, content):
    try:
        supabase.table("articles").insert({
            "user_id": user_id,
            "keyword": str(keyword)[:500],
            "pain": str(pain)[:500],
            "title": str(title)[:500],
            "content": str(content)[:10000],
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
            model="qwen/qwen3.8-27b",
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_tokens,
            temperature=0.7
        )
        text = response.choices[0].message.content
        # Очищаем от markdown, который ломает Telegram
        text = text.replace("**", "")
        return text
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
    get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )
    await message.answer(
        "Привет! Я — твой персональный SEO-стратег.\n\n"
        "Я проведу с тобой глубокое интервью, изучу твою нишу "
        "и буду писать качественные статьи для Дзена, Яндекса и Гугла.\n\n"
        "Это займёт 5-7 минут. Буду задавать вопросы по одному, внимательно читая ответы.\n\n"
        "Вопрос 1:\n"
        "Что именно ты продаёшь или какую услугу оказываешь? Опиши в 2-3 предложениях, как будто объясняешь другу."
    )
    await state.set_state(Onboarding.gathering)
    await state.update_data(question_num=1, history=[], source_requested=False)

# === ЖИВОЕ ИНТЕРВЬЮ ===
@dp.message(Onboarding.gathering)
async def live_interview(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    history = data.get("history", [])
    question_num = data.get("question_num", 1)
    source_requested = data.get("source_requested", False)

    # Чёрная ниша (первый ответ)
    if question_num == 1 and is_banned_niche(message.text):
        await message.answer(
            "Извини, я не работаю с этой темой.\n\n"
            "Я специализируюсь на белом бизнесе: услуги, товары, экспертные ниши, B2B.\n\n"
            "Если я ошибся — опиши свою нишу иначе и начнём сначала: /start"
        )
        await state.clear()
        return

    history.append({"q": question_num, "a": message.text})
    save_onboarding_answer(user_id, f"Q{question_num}", message.text)

    # После 4-го вопроса просим источник
    if question_num >= 4 and not source_requested:
        await state.update_data(history=history, question_num=question_num + 1, source_requested=True)
        await message.answer(
            "Отлично, уже многое понял!\n\n"
            "Чтобы я мог сам изучить твой бизнес, пришли ссылку на любой твой источник:\n"
            "- Сайт (например: mysite.ru)\n"
            "- Telegram-канал (например: t.me/mychannel)\n"
            "- Профиль на Авито (ID или ссылку)\n\n"
            "Я сам открою и изучу его. Если ничего нет — напиши «нет»."
        )
        await state.set_state(Onboarding.source_link)
        await state.update_data(waiting_manual=False)
        return

    if question_num >= 8:
        await finish_interview(message, state, history)
        return

    # Следующий вопрос
    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    next_q_prompt = f"""Ты уже задал клиенту {question_num} вопросов. История:

{history_text}

Задай следующий конкретный уточняющий вопрос, который поможет глубже понять боли клиентов и конкурентные преимущества.
Вопрос должен быть открытым (не да/нет).
Напиши ТОЛЬКО вопрос. Без markdown, без вступлений."""

    next_question = ask_qwen(next_q_prompt, max_tokens=150)
    await state.update_data(history=history, question_num=question_num + 1)
    await message.answer(f"Вопрос {question_num + 1}:\n{next_question}")

# === ОБРАБОТКА ИСТОЧНИКА ===
@dp.message(Onboarding.source_link)
async def get_source(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    history = data.get("history", [])
    waiting_manual = data.get("waiting_manual", False)

    # Если бот ждёт текст вручную (запасной вариант)
    if waiting_manual:
        await message.answer("Изучаю присланный текст...")
        source_text = message.text
        source_type = "Источник"
    else:
        answer = message.text.strip()
        save_onboarding_answer(user_id, "source_link", answer)

        if answer.lower() in ["нет", "нету", "нет источника", "-", "0"]:
            await message.answer("Понял, работаю без источника. Продолжаем интервью...")
            await state.update_data(source_requested=True, waiting_manual=False)
            await state.set_state(Onboarding.gathering)
            await continue_interview(message, state, history)
            return

        await message.answer("Изучаю твой источник сам... 10-20 секунд.")

        answer_lower = answer.lower()
        source_text = None
        source_type = ""

        if "авито" in answer_lower or "avito" in answer_lower or answer.isdigit():
            source_type = "Авито"
            source_text = extract_avito(answer)
            if not source_text:
                await message.answer(
                    "Авито защитил объявление от роботов (так бывает часто).\n"
                    "Не страшно — просто скопируй и пришли мне:\n"
                    "1. Текст объявления\n"
                    "2. Цены\n"
                    "3. Что входит в услугу"
                )
                await state.update_data(waiting_manual=True)
                return

        elif "t.me/" in answer_lower or "телеграм" in answer_lower:
            source_type = "Telegram-канал"
            source_text = extract_telegram_channel(answer)
            if not source_text:
                await message.answer(
                    "Канал закрытый или не публичный.\n"
                    "Пришли мне тексты 3-5 постов (скопируй) — я их изучу."
                )
                await state.update_data(waiting_manual=True)
                return

        elif "http" in answer_lower or ".ru" in answer_lower or ".com" in answer_lower:
            source_type = "Сайт"
            source_text = extract_site_text(answer)
            if not source_text:
                await message.answer(
                    "Не смог открыть сайт.\n"
                    "Пришли мне текст со страницы «О нас» или описание услуг."
                )
                await state.update_data(waiting_manual=True)
                return
        else:
            source_text = extract_site_text(answer)
            source_type = "Источник"
            if not source_text:
                await message.answer(
                    "Не смог изучить этот источник автоматически.\n"
                    "Пришли текстовое описание: кто ты, что делаешь, цены."
                )
                await state.update_data(waiting_manual=True)
                return

    if not source_text:
        await message.answer(
            "Не получилось изучить. Пришли текстовое описание: кто ты, что делаешь, цены."
        )
        await state.update_data(waiting_manual=True)
        return

    # Анализируем найденные данные
    save_onboarding_answer(user_id, "source_data", source_text[:2000])
    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])

    analysis_prompt = f"""Ты изучил {source_type} клиента. Вот реальные данные:

{source_text}

Из интервью известно:
{history_text}

Проанализируй ЧЕСТНО, только на основе присланных данных, ничего не выдумывай:
1. Сильные стороны позиционирования (3-4 пункта)
2. Слабые места, мешающие продажам (2-3 пункта)
3. Стиль общения
4. Что использовать в статьях

Задай 2-3 глубоких уточняющих вопроса.
Кратко. Без markdown."""

    analysis = ask_qwen(analysis_prompt, max_tokens=500)
    await send_long(message, f"Изучил твой {source_type}!\n\n{analysis}")

    await state.update_data(
        history=history,
        question_num=data.get("question_num", 4),
        source_requested=True,
        waiting_manual=False
    )
    await state.set_state(Onboarding.gathering)

# === ПРОДОЛЖЕНИЕ ИНТЕРВЬЮ ПОСЛЕ ИСТОЧНИКА ===
async def continue_interview(message: types.Message, state: FSMContext, history):
    data = await state.get_data()
    question_num = data.get("question_num", 5)

    if question_num >= 8:
        await finish_interview(message, state, history)
        return

    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    next_q_prompt = f"""История интервью:

{history_text}

Задай следующий конкретный вопрос, который поможет понять боли клиентов и конкурентные преимущества.
Открытый вопрос (не да/нет). Напиши ТОЛЬКО вопрос. Без markdown."""

    next_question = ask_qwen(next_q_prompt, max_tokens=200)
    await state.update_data(history=history, question_num=question_num + 1)
    await message.answer(f"Вопрос {question_num + 1}:\n{next_question}")

# === ЗАВЕРШЕНИЕ ИНТЕРВЬЮ ===
async def finish_interview(message: types.Message, state: FSMContext, history):
    user_id = message.from_user.id
    await message.answer("Отлично, интервью завершено! Анализирую нишу... 1-2 минуты.")

    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    niche_query = history[0]["a"] if history else ""

    # Ищем конкурентов и подсказки Яндекса
    competitors = search_competitors(niche_query[:80], max_results=5)
    suggestions = get_yandex_suggestions(niche_query[:30])

    competitors_text = ""
    if competitors:
        for c in competitors[:5]:
            competitors_text += f"- {c['title']}: {c['snippet']}\n"

    suggestions_text = ", ".join(suggestions[:10]) if suggestions else ""

    analysis_prompt = f"""На основе интервью с клиентом:

{history_text}

Найденные конкуренты (из поиска):
{competitors_text}

Подсказки Яндекса (что реально ищут люди):
{suggestions_text}

Проведи глубокий анализ ниши:

1. 15 ключевых запросов для Яндекса и Гугла — используй подсказки Яндекса как основу
2. 10 главных болей ЦА — на основе ответов клиента
3. УТП (3-4 предложения) — чем клиент лучше конкурентов
4. Минусы позиционирования (если видишь)
5. Контент-план на 7 дней: ключ + боль + тип статьи + день и час публикации + длина

Оформи по разделам:
=== КЛЮЧИ ===
=== БОЛИ ===
=== УТП ===
=== МИНУСЫ ===
=== КОНТЕНТ-ПЛАН НА 7 ДНЕЙ ===

Не выдумывай конкурентов. Используй только тех что я нашёл."""

    analysis = ask_qwen(analysis_prompt, max_tokens=900)
    save_niche_research(user_id, analysis, "", "", competitors_text)

    guide_prompt = """Напиши пошаговую инструкцию для чайника: как опубликовать SEO-статью в Дзен, чтобы получать трафик из Яндекса и Гугла. 5 простых шагов.
Для каждого шага объясни простыми словами что такое Title, Description, H1, alt-текст.
Приведи аналогию с магазином (вывеска, аннотация, заголовок).
Без markdown."""

    guide = ask_qwen(guide_prompt, max_tokens=600)

    await send_long(message, f"АНАЛИЗ ТВОЕЙ НИШИ ГОТОВ!\n\n{analysis}")
    await asyncio.sleep(1)
    await send_long(message, f"КАК ПУБЛИКОВАТЬ В ДЗЕН (для чайника):\n\n{guide}")
    await asyncio.sleep(1)

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📸 Загрузить фото")],
            [KeyboardButton(text="⏭ Пропустить")]
        ],
        resize_keyboard=True
    )
    await message.answer(
        "Хочешь загрузить фото своих работ/товара/команды?\n"
        "Я буду использовать их в статьях. Можно сейчас или позже.",
        reply_markup=keyboard
    )
    await state.set_state(Onboarding.photos)

# === ФОТО ===
@dp.message(Onboarding.photos, F.text == "📸 Загрузить фото")
async def request_photos(message: types.Message, state: FSMContext):
    await message.answer(
        "Отлично! Пришли фото — по одному или пачкой.\n"
        "Когда закончишь — напиши слово «готово»."
    )

@dp.message(Onboarding.photos, F.text == "⏭ Пропустить")
async def skip_photos(message: types.Message, state: FSMContext):
    await message.answer("Ок, буду генерировать картинки сам.")
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.text.lower() == "готово")
async def photos_done(message: types.Message, state: FSMContext):
    await message.answer("Фото сохранены!")
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.photo)
async def receive_photo(message: types.Message, state: FSMContext):
    await message.answer("Фото принял. Загружай ещё или напиши «готово».")

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
        "Куда тебе удобнее принимать заявки?\n"
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

    await state.update_data(cta_type=cta_type)
    await state.set_state(Onboarding.cta_value)
    await callback.message.answer(f"Выбрано: {cta_type}\n\n{prompts.get(cta_type, '')}")

@dp.message(Onboarding.cta_value)
async def cta_value_received(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    cta_type = data.get("cta_type", "")

    try:
        supabase.table("users").update({
            "cta_value": message.text.strip()
        }).eq("id", user_id).execute()
    except Exception as e:
        logging.error(f"Save CTA value error: {e}")

    await message.answer(
        "Готово! Ты прошёл онбординг.\n\n"
        "Теперь напиши /article чтобы получить первую статью.\n"
        "Или /help чтобы увидеть все команды.\n\n"
        "После первой бесплатной статьи ты сможешь выбрать тариф."
    )
    await state.clear()

# === ГЕНЕРАЦИЯ СТАТЬИ ===
@dp.message(Command("article"))
async def generate_article(message: types.Message):
    user_id = message.from_user.id

    try:
        research = supabase.table("niche_research").select("*").eq("user_id", user_id).order("id", desc=True).limit(1).execute()
        if not research.data:
            await message.answer("Сначала пройди онбординг: /start")
            return
        analysis = research.data[0]["keywords"]
    except Exception:
        await message.answer("Сначала пройди онбординг: /start")
        return

    await message.answer("Пишу статью... 1-2 минуты.")

    article_prompt = f"""На основе анализа ниши:

{analysis}

Напиши первую SEO-статью по правилу "1 ключ + 1 боль".
Выбери самую сильную связку ключ+боль из анализа.

ТРЕБОВАНИЯ:
1. Цепляющий заголовок с ключом
2. Вступление с болью (2-3 абзаца)
3. Подзаголовки, списки
4. Стиль "эксперт-друг" простыми словами
5. 5-7 тыс. знаков
6. Цифры, кейсы, шаги, ошибки
7. Мягкий призыв в конце
8. В самом конце укажи:
   - Title (до 60 символов)
   - Description (до 160 символов)
   - 2-3 хэштега
   - Slug (ЧПУ-ссылка латиницей)

Оформи:
=== СТАТЬЯ ===
[текст статьи]
=== SEO-ПАКЕТ ===
Title: ...
Description: ...
Хэштеги: ...
Slug: ..."""

    article = ask_qwen(article_prompt, max_tokens=900)
    save_article(user_id, "первая статья", "первая боль", "первая статья", article)
    log_usage(user_id, "article_generated")

    await send_long(message, f"ТВОЯ ПЕРВАЯ СТАТЬЯ ГОТОВА:\n\n{article}")
    await asyncio.sleep(1)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Адаптировать под vc.ru", callback_data="vc_yes"),
            InlineKeyboardButton(text="❌ Только Дзен", callback_data="vc_no")
        ]
    ])
    await message.answer(
        "Адаптировать эту статью под vc.ru?\n"
        "Это удвоит трафик с одной темы.",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "vc_yes")
async def adapt_vc(callback: types.CallbackQuery):
    await callback.message.answer("Адаптирую под vc.ru... 1-2 минуты.")

    try:
        articles = supabase.table("articles").select("*").eq("user_id", callback.from_user.id).order("id", desc=True).limit(1).execute()
        if not articles.data:
            await callback.message.answer("Ошибка: статья не найдена")
            return
        original = articles.data[0]["content"]
        article_id = articles.data[0]["id"]
    except Exception:
        await callback.message.answer("Ошибка базы данных")
        return

    vc_prompt = f"""Адаптируй эту статью под vc.ru:

{original}

ТРЕБОВАНИЯ ДЛЯ vc.ru:
- Угол: кейс с цифрами / факап с уроками / внутренняя кухня
- Тон: коллега делится опытом
- Длина: 7-12 тыс. знаков
- Без хэштегов
- Без прямой рекламы
- Самопрезентация через опыт
- Живой заголовок с интригой/цифрами

Верни адаптированную версию."""

    vc_article = ask_qwen(vc_prompt, max_tokens=900)

    try:
        supabase.table("articles").update({"vc_version": vc_article}).eq("id", article_id).execute()
    except Exception:
        pass

    await send_long(callback.message, f"ВЕРСИЯ ДЛЯ vc.ru ГОТОВА:\n\n{vc_article}")

@dp.callback_query(F.data == "vc_no")
async def skip_vc(callback: types.CallbackQuery):
    await callback.message.answer("Ок, оставляем только версию для Дзена.")

# === HELP ===
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "Команды бота:\n\n"
        "/start — пройти онбординг заново\n"
        "/article — сгенерировать новую статью\n"
        "/history — мои прошлые статьи (скоро)\n"
        "/plan — контент-план на неделю (скоро)\n"
        "/tariff — мой тариф и лимиты (скоро)\n"
        "/help — эта справка"
    )

# === АДМИН (только для Кирилла) ===
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        users_count = supabase.table("users").select("id").execute()
        articles_count = supabase.table("articles").select("id").execute()
        await message.answer(
            f"Статистика:\n"
            f"Клиентов: {len(users_count.data)}\n"
            f"Статей создано: {len(articles_count.data)}"
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

# === ЗАПУСК ===
async def main():
    logging.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    asyncio.run(main())
