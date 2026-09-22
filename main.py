import logging
import os
import requests
import threading
import asyncio
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
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

# === ЧЁРНЫЙ СПИСОК ===
BANNED_NICHES = ["обнал", "отмыв", "адалт", "18+", "порн", "оружие", "наркот", "взлом", "хакер"]

# === СИСТЕМНЫЙ ПРОМПТ ===
SYSTEM_PROMPT = """Ты — элитный SEO-стратег и контент-маркетолог с 15-летним опытом.
Работаешь как настоящий живой маркетолог на брифе.

ГЛАВНЫЕ ПРИНЦИПЫ:

1. ЖИВОЕ ИНТЕРВЬЮ
- Задавай по одному вопросу за раз
- Внимательно читай ответы и задавай уточняющие
- Не шаблонься — каждая ниша уникальна
- Не выдумывай факты о клиенте

2. ЛОГИКА НЕСКОЛЬКИХ УСЛУГ
Если у клиента несколько разных услуг (например: лазерная эпиляция + массаж + чистка лица) — это РАЗНЫЕ ниши.
Обязательно:
- Спроси: "По какой услуге в первую очередь делаем анализ?"
- Проведи анализ только по приоритетной услуге
- После выдачи статей по первой услуге — переходи к следующей

3. АНАЛИЗ ИСТОЧНИКА
ВАЖНО: Тебе присылают РЕАЛЬНЫЕ данные источника. 
- Анализируй ТОЛЬКО их
- Если источник не прислали или данных нет — честно скажи "источник не изучен"
- НИКОГДА не выдумывай что ты изучил сайт/группу, если данных нет
- Если данных мало — задай уточняющие вопросы

4. НАПИСАНИЕ СТАТЕЙ
ПРАВИЛО: 1 статья = 1 ключевой запрос + 1 конкретная боль

СТИЛЬ:
- Языком читателя (не специалиста)
- Не восхваляй продукт — решай боль читателя
- Автор — независимый советчик, в конце мягко рекомендует
- Частник → от "Я", компания → от "МЫ"

СТРУКТУРА:
- Цепляющий заголовок с ключом
- Вступление с болью (2-3 абзаца)
- Подзаголовки, списки
- Цифры, кейсы, шаги, ошибки (пропорции зависят от ниши)
- Нативный призыв в конце (без "купи", "закажи")

ДЛИНА: 5-7 тыс. знаков (адаптивно)

УНИКАЛЬНОСТЬ:
- Не повторяй формулировки конкурентов
- Добавляй уникальный угол: цифры, мини-истории
- Переписывай шаблонные куски

5. ФОРМАТИРОВАНИЕ ОТВЕТА
- Используй эмодзи для структуры (🎯, 📊, ⚡, ✅, ❌)
- Выделяй важное **жирным**
- Разбивай на абзацы и блоки
- Используй списки с тире или цифрами
- Делай визуальные разделители (---)

6. КОНТЕНТ-ПЛАН
- Учитывай лимиты Дзена: новый канал 1-2/день, растущий 2-3
- Указывай оптимальный день и ЧАС публикации под нишу

7. ИНСТРУКЦИЯ ДЛЯ ЧАЙНИКА
- Публикация в Дзен для трафика из Яндекса и Гугла
- Объясняй простыми словами через аналогию с магазином

8. АДАПТАЦИЯ ПОД vc.ru
- Угол: кейсы, факапы, внутренняя кухня
- Тон: коллега делится опытом
- Длина: 7-12 тыс. знаков

9. БЕЗОПАСНОСТЬ
- Чувствительные ниши: дисклеймер
- Чёрные ниши: отказ сразу

ВСЕГДА ОТВЕЧАЙ НА РУССКОМ. Будь конкретным."""

# === СОСТОЯНИЯ ===
class Onboarding(StatesGroup):
    gathering = State()
    service_priority = State()  # Выбор приоритетной услуги
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

# === ПАРСИНГ ИСТОЧНИКОВ ===
def extract_site_text(url):
    try:
        if not url.startswith("http"):
            url = "https://" + url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "lxml")
        for script in soup(["script", "style", "nav", "footer", "header"]):
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

def extract_vk_group(vk_url):
    """Пытается парсить группу ВК"""
    try:
        # Извлекаем имя группы
        vk_name = vk_url.replace("https://vk.com/", "").replace("https://vk.ru/", "").replace("vk.com/", "").replace("vk.ru/", "").strip("/")
        vk_name = vk_name.split("/")[0]
        
        # Пробуем мобильную версию (проще парсится)
        url = f"https://m.vk.com/{vk_name}"
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept": "text/html,application/xhtml+xml"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = "utf-8"
        
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, "lxml")
        
        # Ищем посты
        posts = []
        # Мобильная версия ВК
        for post in soup.find_all("div", class_="wall_post_text"):
            text = post.get_text(strip=True)
            if text and len(text) > 20:
                posts.append(text)
        
        # Обычная версия
        if not posts:
            for post in soup.find_all("div", class_="Post__copy"):
                text = post.get_text(strip=True)
                if text and len(text) > 20:
                    posts.append(text)
        
        # Описание группы
        description = ""
        for desc in soup.find_all(["div", "p"], class_=["group_info", "page_info", "group_description"]):
            description += desc.get_text(strip=True) + "\n"
        
        if not posts and not description:
            return None
        
        result = ""
        if description:
            result += "ОПИСАНИЕ ГРУППЫ:\n" + description + "\n\n"
        if posts:
            result += "ПОСЛЕДНИЕ ПОСТЫ:\n" + "\n---\n".join(posts[:10])
        
        return result[:5000]
    except Exception as e:
        logging.error(f"VK parse error: {e}")
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
            "answer": str(answer)[:1500]
        }).execute()
    except Exception as e:
        logging.error(f"Save answer error: {e}")

def save_niche_research(user_id, keywords, pains, utp, competitors):
    try:
        supabase.table("niche_research").insert({
            "user_id": user_id,
            "keywords": str(keywords)[:8000],
            "pains": str(pains)[:5000],
            "utp": str(utp)[:2000],
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
            "content": str(content)[:15000],
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
def ask_qwen(prompt, max_tokens=1200, system=None):
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
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Groq error: {e}")
        return "⚠️ Ошибка: " + str(e)[:200]

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
        await message.answer(text, parse_mode="Markdown")
    else:
        parts = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]
        for part in parts:
            try:
                await message.answer(part, parse_mode="Markdown")
            except Exception:
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
    # Полный сброс состояния
    await state.clear()
    
    get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )
    await message.answer(
        "👋 **Привет! Я — твой персональный SEO-стратег.**\n\n"
        "Я проведу с тобой глубокое интервью, изучу твою нишу "
        "и буду писать качественные статьи для **Дзена, Яндекса и Гугла**.\n\n"
        "⏱ Это займёт 5-7 минут. Буду задавать вопросы по одному, внимательно читая ответы.\n\n"
        "❓ **Вопрос 1:**\n"
        "Что именно ты продаёшь или какую услугу оказываешь? "
        "Опиши в 2-3 предложениях, как будто объясняешь другу.",
        parse_mode="Markdown"
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

    if question_num == 1 and is_banned_niche(message.text):
        await message.answer(
            "❌ Извини, я не работаю с этой темой.\n\n"
            "Я специализируюсь на белом бизнесе: услуги, товары, экспертные ниши, B2B.\n\n"
            "Если я ошибся — опиши свою нишу иначе и начнём сначала: /start",
            parse_mode="Markdown"
        )
        await state.clear()
        return

    history.append({"q": question_num, "a": message.text})
    save_onboarding_answer(user_id, f"Q{question_num}", message.text)

    # После 1-го вопроса проверяем — нет ли нескольких услуг
    if question_num == 1:
        history_text = message.text
        check_prompt = f"""Клиент описал свой бизнес:
"{history_text}"

Проанализируй: предлагает ли клиент НЕСКОЛЬКО РАЗНЫХ услуг/направлений? 
Например: эпиляция + массаж + чистка лица = 3 разные услуги.

Ответь СТРОГО в формате:
- Если несколько услуг: "YES|услуга1|услуга2|услуга3"
- Если одна услуга или одно направление: "NO"
"""
        check_result = ask_qwen(check_prompt, max_tokens=100)
        
        if check_result.startswith("YES|"):
            services = check_result.split("|")[1:]
            services_text = "\n".join([f"• {s.strip()}" for s in services if s.strip()])
            
            await state.update_data(
                history=history,
                question_num=question_num + 1,
                multiple_services=services
            )
            
            # Переходим к выбору приоритета
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=s.strip(), callback_data=f"svc_{i}")] 
                for i, s in enumerate(services) if s.strip()
            ])
            
            await message.answer(
                f"🎯 **Вижу, что у тебя несколько направлений:**\n\n"
                f"{services_text}\n\n"
                f"Это **разные ниши** с разными клиентами и болями. "
                f"Давай начнём с одной — по какой услуге в первую очередь "
                f"делаем глубокий анализ и пишем статьи?\n\n"
                f"**Выбери приоритетную:**",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            await state.set_state(Onboarding.service_priority)
            return

    # После 4-го вопроса просим источник
    if question_num >= 4 and not source_requested:
        await state.update_data(history=history, question_num=question_num + 1, source_requested=True)
        await message.answer(
            "✅ **Отлично, уже многое понял!**\n\n"
            "🔗 Чтобы я мог **сам изучить** твой бизнес, пришли ссылку на любой источник:\n\n"
            "• 🌐 **Сайт** (например: mysite.ru)\n"
            "• ✈️ **Telegram-канал** (например: t.me/mychannel)\n"
            "• 📱 **Профиль на Авито** (ID или ссылку)\n"
            "• 💬 **Группа ВКонтакте** (например: vk.com/mygroup)\n\n"
            "Я сам открою и изучу. Если ничего нет — напиши **«нет»**.",
            parse_mode="Markdown"
        )
        await state.set_state(Onboarding.source_link)
        await state.update_data(waiting_manual=False)
        return

    if question_num >= 8:
        await finish_interview(message, state, history)
        return

    # Следующий вопрос
    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    priority = data.get("priority_service", "")
    priority_note = f"\n\nВажно: клиент хочет делать акцент на услуге: {priority}" if priority else ""
    
    next_q_prompt = f"""Ты задал клиенту {question_num} вопросов. История:

{history_text}{priority_note}

Задай следующий конкретный уточняющий вопрос для глубокого понимания:
- Болей клиентов
- Конкурентных преимуществ
- Реальных кейсов

Вопрос должен быть открытым (не да/нет).
Напиши ТОЛЬКО вопрос. Без вступлений. Без markdown-разметки (**, #)."""

    next_question = ask_qwen(next_q_prompt, max_tokens=200)
    await state.update_data(history=history, question_num=question_num + 1)
    await message.answer(f"❓ **Вопрос {question_num + 1}:**\n{next_question}", parse_mode="Markdown")

# === ВЫБОР ПРИОРИТЕТНОЙ УСЛУГИ ===
@dp.callback_query(F.data.startswith("svc_"), Onboarding.service_priority)
async def service_chosen(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.replace("svc_", ""))
    data = await state.get_data()
    services = data.get("multiple_services", [])
    history = data.get("history", [])
    
    if idx < len(services):
        priority = services[idx].strip()
    else:
        priority = services[0].strip() if services else ""
    
    save_onboarding_answer(callback.from_user.id, "priority_service", priority)
    
    await callback.message.answer(
        f"✅ **Отлично, делаем акцент на:** {priority}\n\n"
        f"Продолжаем интервью по этой услуге...",
        parse_mode="Markdown"
    )
    
    await state.update_data(
        priority_service=priority,
        history=history,
        question_num=2
    )
    await state.set_state(Onboarding.gathering)
    
    # Сразу задаём следующий вопрос
    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    next_q_prompt = f"""Клиент выбрал приоритетную услугу: {priority}

Общее описание бизнеса:
{history_text}

Задай 2-й конкретный вопрос по этой услуге: о страхах/возражениях клиентов перед этой услугой.
Напиши ТОЛЬКО вопрос. Без вступлений. Без markdown-разметки."""

    next_question = ask_qwen(next_q_prompt, max_tokens=200)
    await callback.message.answer(f"❓ **Вопрос 2:**\n{next_question}", parse_mode="Markdown")
    
    await callback.answer()

# === ОБРАБОТКА ИСТОЧНИКА ===
@dp.message(Onboarding.source_link)
async def get_source(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    history = data.get("history", [])
    waiting_manual = data.get("waiting_manual", False)

    if waiting_manual:
        await message.answer("⏳ Изучаю присланный текст...")
        source_text = message.text
        source_type = "Присланный текст"
    else:
        answer = message.text.strip()
        save_onboarding_answer(user_id, "source_link", answer)

        if answer.lower() in ["нет", "нету", "нет источника", "-", "0", "отсутствует"]:
            await message.answer(
                "👌 Понял, работаем без источника. "
                "Буду опираться только на твои ответы.\n\nПродолжаем...",
                parse_mode="Markdown"
            )
            await state.update_data(source_requested=True, source_data=None, waiting_manual=False)
            await state.set_state(Onboarding.gathering)
            await continue_interview(message, state, history)
            return

        await message.answer("⏳ **Изучаю твой источник сам...** 10-20 секунд.", parse_mode="Markdown")

        answer_lower = answer.lower()
        source_text = None
        source_type = ""

        if "авито" in answer_lower or "avito" in answer_lower or answer.isdigit():
            source_type = "Авито"
            source_text = extract_avito(answer)
            if not source_text:
                await message.answer(
                    "⚠️ Авито защитил объявление от роботов.\n\n"
                    "Не страшно — просто **скопируй и пришли мне:**\n"
                    "1. Текст объявления\n"
                    "2. Цены\n"
                    "3. Что входит в услугу",
                    parse_mode="Markdown"
                )
                await state.update_data(waiting_manual=True)
                return

        elif "vk.com" in answer_lower or "vk.ru" in answer_lower or "вконтакт" in answer_lower:
            source_type = "Группа ВКонтакте"
            source_text = extract_vk_group(answer)
            if not source_text:
                await message.answer(
                    "⚠️ ВКонтакте защитил группу от парсинга.\n\n"
                    "Пришли мне **текстом:**\n"
                    "• Описание группы\n"
                    "• Тексты 3-5 последних постов\n\n"
                    "Я их изучу и учту в статьях.",
                    parse_mode="Markdown"
                )
                await state.update_data(waiting_manual=True)
                return

        elif "t.me/" in answer_lower or "телеграм" in answer_lower:
            source_type = "Telegram-канал"
            source_text = extract_telegram_channel(answer)
            if not source_text:
                await message.answer(
                    "⚠️ Канал закрытый или не публичный.\n"
                    "Пришли тексты 3-5 постов (скопируй) — я их изучу.",
                    parse_mode="Markdown"
                )
                await state.update_data(waiting_manual=True)
                return

        elif "http" in answer_lower or ".ru" in answer_lower or ".com" in answer_lower:
            source_type = "Сайт"
            source_text = extract_site_text(answer)
            if not source_text:
                await message.answer(
                    "⚠️ Не смог открыть сайт.\n"
                    "Пришли мне текст со страницы «О нас» или описание услуг.",
                    parse_mode="Markdown"
                )
                await state.update_data(waiting_manual=True)
                return
        else:
            source_text = extract_site_text(answer)
            source_type = "Источник"
            if not source_text:
                await message.answer(
                    "⚠️ Не смог изучить этот источник автоматически.\n"
                    "Пришли текстовое описание: кто ты, что делаешь, цены.",
                    parse_mode="Markdown"
                )
                await state.update_data(waiting_manual=True)
                return

    if not source_text:
        await message.answer(
            "⚠️ Не получилось изучить. Пришли текстовое описание: кто ты, что делаешь, цены.",
            parse_mode="Markdown"
        )
        await state.update_data(waiting_manual=True)
        return

    # Анализируем реальные данные
    save_onboarding_answer(user_id, "source_data", source_text[:2000])
    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    priority = data.get("priority_service", "")
    priority_note = f"\n\nПриоритетная услуга клиента: {priority}" if priority else ""

    analysis_prompt = f"""Ты изучил {source_type} клиента. Вот РЕАЛЬНЫЕ данные:

---
{source_text}
---

Из интервью известно:
{history_text}{priority_note}

Проанализируй ЧЕСТНО, только на основе этих данных (ничего не выдумывай!):

📌 **Сильные стороны** (3-4 пункта)
📌 **Слабые места**, мешающие продажам (2-3 пункта)
📌 **Стиль общения**
📌 **Что использовать в статьях**

В конце задай **2-3 глубоких уточняющих вопроса**.

Используй эмодзи, выделяй важное **жирным**."""

    analysis = ask_qwen(analysis_prompt, max_tokens=700)
    await send_long(message, f"✅ **Изучил твой {source_type}!**\n\n{analysis}")

    await state.update_data(
        history=history,
        question_num=data.get("question_num", 4),
        source_requested=True,
        source_data=source_text[:2000],
        waiting_manual=False
    )
    await state.set_state(Onboarding.gathering)

# === ПРОДОЛЖЕНИЕ ИНТЕРВЬЮ ===
async def continue_interview(message: types.Message, state: FSMContext, history):
    data = await state.get_data()
    question_num = data.get("question_num", 5)

    if question_num >= 8:
        await finish_interview(message, state, history)
        return

    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    next_q_prompt = f"""История интервью:
{history_text}

Задай следующий конкретный вопрос (открытый, не да/нет).
Напиши ТОЛЬКО вопрос. Без вступлений. Без markdown-разметки."""

    next_question = ask_qwen(next_q_prompt, max_tokens=200)
    await state.update_data(history=history, question_num=question_num + 1)
    await message.answer(f"❓ **Вопрос {question_num + 1}:**\n{next_question}", parse_mode="Markdown")

# === ЗАВЕРШЕНИЕ ИНТЕРВЬЮ ===
async def finish_interview(message: types.Message, state: FSMContext, history):
    user_id = message.from_user.id
    data = await state.get_data()
    priority = data.get("priority_service", "")
    source_data = data.get("source_data", "")
    
    await message.answer(
        "✅ **Интервью завершено!**\n\n"
        "⏳ Анализирую нишу, ищу конкурентов и собираю ключи... "
        "**1-2 минуты**.",
        parse_mode="Markdown"
    )

    history_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    niche_query = priority if priority else (history[0]["a"] if history else "")

    # Ищем конкурентов и подсказки Яндекса
    competitors = search_competitors(niche_query[:80], max_results=5)
    suggestions = get_yandex_suggestions(niche_query[:30])

    competitors_text = ""
    if competitors:
        for c in competitors[:5]:
            competitors_text += f"- {c['title']}: {c['snippet']}\n"

    suggestions_text = ", ".join(suggestions[:10]) if suggestions else "пусто"

    source_section = ""
    if source_data:
        source_section = f"\n\n=== ДАННЫЕ ИЗ ИСТОЧНИКА КЛИЕНТА ===\n{source_data}\n"

    # ЧАСТЬ 1: Анализ (ключи, боли, УТП)
    analysis_prompt = f"""На основе интервью с клиентом:

=== ОТВЕТЫ КЛИЕНТА ===
{history_text}{source_section}

Приоритетная услуга: {priority or 'не указана'}

Найденные конкуренты:
{competitors_text or 'не найдены'}

Подсказки Яндекса (что реально ищут люди):
{suggestions_text}

Проведи глубокий анализ:

1. **15 ключевых запросов** для Яндекса и Гугла — используй подсказки Яндекса как основу
2. **10 главных болей ЦА** — на основе ответов
3. **УТП** (3-4 предложения) — чем клиент лучше конкурентов
4. **Минусы позиционирования** (если видишь)

Оформи:
=== 🎯 КЛЮЧИ ===
=== 💥 БОЛИ ===
=== ⭐ УТП ===
=== ⚠️ МИНУСЫ ===

Используй эмодзи и **жирный** шрифт для структуры."""

    analysis = ask_qwen(analysis_prompt, max_tokens=1200)

    # ЧАСТЬ 2: Контент-план (отдельный запрос)
    plan_prompt = f"""На основе анализа:

{analysis}

Приоритетная услуга: {priority or 'не указана'}

Составь **контент-план на 7 дней** для Дзен:
- Каждая статья = 1 ключ + 1 боль
- Укажи: ключ, боль, тип статьи (кейс/советы/разбор/ошибки), день недели, ЧАС публикации, длина
- Учитывай что Дзен любит: новые каналы 1-2 статьи/день, растущие 2-3
- Час публикации должен соответствовать нише (например: ремонт — вечер, B2B — будни день)

Оформи таблицей или списком:
=== 📅 КОНТЕНТ-ПЛАН НА 7 ДНЕЙ ===

Используй эмодзи и структуру."""

    plan = ask_qwen(plan_prompt, max_tokens=900)
    
    save_niche_research(user_id, analysis + "\n\n" + plan, "", "", competitors_text)

    # Инструкция по Дзену
    guide_prompt = """Напиши пошаговую инструкцию для чайника: как опубликовать SEO-статью в Дзен, чтобы получать трафик из Яндекса и Гугла.

5 простых шагов. Для каждого шага:
- Объясни простыми словами что такое Title, Description, H1, alt-текст
- Приведи аналогию с магазином (вывеска, аннотация, заголовок)
- Дай пример для темы "лазерная эпиляция" или "ремонт квартир"

Используй эмодзи, **жирный**, списки. Без технического жаргона."""

    guide = ask_qwen(guide_prompt, max_tokens=800)

    await send_long(message, f"🎯 **АНАЛИЗ ТВОЕЙ НИШИ ГОТОВ!**\n\n{analysis}")
    await asyncio.sleep(1)
    await send_long(message, f"📅 **КОНТЕНТ-ПЛАН:**\n\n{plan}")
    await asyncio.sleep(1)
    await send_long(message, f"📚 **КАК ПУБЛИКОВАТЬ В ДЗЕН (для чайника):**\n\n{guide}")
    await asyncio.sleep(1)

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📸 Загрузить фото")],
            [KeyboardButton(text="⏭ Пропустить")]
        ],
        resize_keyboard=True
    )
    await message.answer(
        "📸 **Хочешь загрузить фото своих работ/товара/команды?**\n\n"
        "Я буду использовать их в статьях. Можно сейчас или позже.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(Onboarding.photos)

# === ФОТО (теперь обрабатываем все варианты) ===
@dp.message(Onboarding.photos, F.text.in_(["⏭ Пропустить", "Позже", "позже", "Не сейчас", "не сейчас", "Нет", "нет"]))
async def skip_photos(message: types.Message, state: FSMContext):
    await message.answer("👌 Ок, буду генерировать картинки сам.")
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.text == "📸 Загрузить фото")
async def request_photos(message: types.Message, state: FSMContext):
    await message.answer(
        "📸 **Отлично! Пришли фото** — по одному или пачкой.\n\n"
        "Когда закончишь — напиши слово **«готово»** или **«позже»**.",
        parse_mode="Markdown"
    )

@dp.message(Onboarding.photos, F.text.lower() == "готово")
async def photos_done(message: types.Message, state: FSMContext):
    await message.answer("✅ Фото сохранены!")
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.photo)
async def receive_photo(message: types.Message, state: FSMContext):
    await message.answer("📸 Фото принял. Загружай ещё или напиши **«готово»** / **«позже»**.", parse_mode="Markdown")

# === CTA ===
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
        reply_markup=keyboard,
        parse_mode="Markdown"
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
        "site": "🌐 Пришли ссылку на страницу с формой заявки или калькулятором",
        "tg": "✈️ Пришли свой username через @ или ссылку на Telegram",
        "wa": "📱 Пришли номер в формате +7...",
        "max": "📣 Пришли ссылку на канал/чат в МАХ",
        "phone": "📞 Пришли номер телефона"
    }

    await state.update_data(cta_type=cta_type)
    await state.set_state(Onboarding.cta_value)
    await callback.message.answer(f"✅ Выбрано: {cta_type}\n\n{prompts.get(cta_type, '')}", parse_mode="Markdown")
    await callback.answer()

@dp.message(Onboarding.cta_value)
async def cta_value_received(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    try:
        supabase.table("users").update({
            "cta_value": message.text.strip()
        }).eq("id", user_id).execute()
    except Exception as e:
        logging.error(f"Save CTA value error: {e}")

    await message.answer(
        "🎉 **Готово! Ты прошёл онбординг.**\n\n"
        "Теперь напиши:\n"
        "• `/article` — получить первую статью\n"
        "• `/help` — все команды\n\n"
        "После первой бесплатной статьи ты сможешь выбрать тариф.",
        parse_mode="Markdown"
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

    await message.answer("✍️ **Пишу статью...** 1-2 минуты.", parse_mode="Markdown")

    article_prompt = f"""На основе анализа ниши:

{analysis}

Напиши первую SEO-статью по правилу "1 ключ + 1 боль".
Выбери самую сильную связку ключ+боль из анализа.

ТРЕБОВАНИЯ:
1. **Цепляющий заголовок** с ключом
2. **Вступление с болью** (2-3 абзаца)
3. Подзаголовки, списки
4. Стиль "эксперт-друг" простыми словами
5. 5-7 тыс. знаков
6. Цифры, кейсы, шаги, ошибки
7. **Мягкий призыв в конце** (без "купи")
8. В самом конце:
   - Title (до 60 символов)
   - Description (до 160 символов)
   - 2-3 хэштега
   - Slug (ЧПУ-ссылка латиницей)

Используй эмодзи, **жирный** шрифт, списки.

Оформи:
=== 📄 СТАТЬЯ ===
[текст статьи]
=== 🏷 SEO-ПАКЕТ ===
**Title:** ...
**Description:** ...
**Хэштеги:** ...
**Slug:** ..."""

    article = ask_qwen(article_prompt, max_tokens=1400)
    save_article(user_id, "первая статья", "первая боль", "первая статья", article)
    log_usage(user_id, "article_generated")

    await send_long(message, f"📄 **ТВОЯ ПЕРВАЯ СТАТЬЯ ГОТОВА:**\n\n{article}")
    await asyncio.sleep(1)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Адаптировать под vc.ru", callback_data="vc_yes"),
            InlineKeyboardButton(text="❌ Только Дзен", callback_data="vc_no")
        ]
    ])
    await message.answer(
        "📰 **Адаптировать эту статью под vc.ru?**\n\n"
        "Это удвоит трафик с одной темы.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "vc_yes")
async def adapt_vc(callback: types.CallbackQuery):
    await callback.message.answer("⏳ Адаптирую под vc.ru... 1-2 минуты.")

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

Используй **жирный** и эмодзи."""

    vc_article = ask_qwen(vc_prompt, max_tokens=1400)

    try:
        supabase.table("articles").update({"vc_version": vc_article}).eq("id", article_id).execute()
    except Exception:
        pass

    await send_long(callback.message, f"📰 **ВЕРСИЯ ДЛЯ vc.ru ГОТОВА:**\n\n{vc_article}")
    await callback.answer()

@dp.callback_query(F.data == "vc_no")
async def skip_vc(callback: types.CallbackQuery):
    await callback.message.answer("👌 Ок, оставляем только версию для Дзена.", parse_mode="Markdown")
    await callback.answer()

# === HELP ===
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📋 **Команды бота:**\n\n"
        "• `/start` — пройти онбординг заново\n"
        "• `/article` — сгенерировать новую статью\n"
        "• `/history` — мои прошлые статьи (скоро)\n"
        "• `/plan` — контент-план на неделю (скоро)\n"
        "• `/tariff` — мой тариф и лимиты (скоро)\n"
        "• `/help` — эта справка",
        parse_mode="Markdown"
    )

# === АДМИН ===
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
            f"📝 Статей создано: {len(articles_count.data)}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")

# === ЗАПУСК ===
async def main():
    logging.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    asyncio.run(main())
