import logging
import os
import requests
import threading
import asyncio
import time
import re
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import date, timedelta, datetime
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)
from supabase import create_client, Client
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from typing import Callable, Dict, Any, Optional, List

# =========================
# CONFIG & INIT
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
ADMIN_ID = 1847007101

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    bot = Bot(token=BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Autonomous Agent Initialized")
except Exception as e:
    logger.error(f"❌ INIT ERROR: {e}")
    raise

BANNED_NICHES = ["обнал", "отмыв", "адалт", "18+", "порн", "оружие", "наркот", "взлом", "хакер"]
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODELS = ["qwen/qwen3.8-27b"] 

DAILY_LIMIT = 3 

# =========================
# DATABASE HELPERS
# =========================

def ensure_user_exists(user_id: int, username: str = "", first_name: str = ""):
    """Проверяет наличие пользователя в БД и создает его, если нет."""
    try:
        # Проверяем, есть ли уже такой ID
        res = supabase.table("users").select("*").eq("id", user_id).execute()
        if not res.data:
            # Создаем нового пользователя
            supabase.table("users").insert({
                "id": user_id,
                "username": username or "",
                "first_name": first_name or ""
            }).execute()
            logger.info(f"🆕 Created new user in DB: {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error ensuring user exists: {e}")
        return False

# =========================
# PROMPTS ENGINE
# =========================

BASE_SYSTEM = """Ты — элитный SEO-стратег и контент-маркетолог уровня Top-Tier агентств РФ.
Твоя цель: привести горячий трафик клиенту, используя данные о рынке и психологию ЦА.

ПРАВИЛА:
1. Пиши ТОЛЬКО на русском.
2. НЕ используй ### или ##. Используй эмодзи и **жирный**.
3. Язык статей: ГОВОРИ НА ЯЗЫКЕ ЦЕЛЕВОЙ АУДИТОРИИ (ЦА). 
   - Никаких сложных терминов. Простые предложения. Живые примеры.
   - ЦА должна чувствовать: "Это про меня!".
4. Анализ конкурентов: Ищи их слабые места и превращай их в преимущества нашего клиента.
5. Структура ответа четкая, без воды."""

SPY_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Шпионаж за конкурентами.
ВХОДНЫЕ ДАННЫЕ: Ниша, список сайтов конкурентов.

СТРУКТУРА ОТВЕТА:
1. COMPETITORS_ANALYSIS: Слабые места ТОП-3 конкурентов.
2. TARGET_AUDIENCE_PAIN_POINTS: 5 главных болей ЦА (на их языке).
3. TONE_OF_VOICE: Описание стиля речи ЦА.
4. ARTICLE_IDEAS_QUEUE: Список из 10+ идей статей.
   Каждая идея: {topic, pain_point, competitor_weakness_to_hammer, relevance_score}

ВАЖНО: Выдай результат структурировано."""

WRITER_DZEN_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Написать статью для Яндекс.Дзен.
ТРЕБОВАНИЯ:
1. Объем: 5-7 тыс. знаков.
2. Заголовок: Цепляющий, но честный.
3. Лид: Начать с боли ("Знакомо ли вам чувство...").
4. Основная часть: Решаем проблему. Мягко сравниваем с рынком.
5. CTA: Мягкая рекомендация.
6. SEO-блок в конце: Title, Description, Slug, Хэштеги.

ВАЖНО: Текст должен быть таким, чтобы ЦА поверила эксперту."""

WRITER_VC_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Адаптировать статью под vc.ru.
ТРЕБОВАНИЯ:
1. Угол подачи: Кейс, факап, внутренняя кухня бизнеса.
2. Тон: Коллега делится опытом. Без рекламной шелухи.
3. Структура: Проблема -> Попытки решения -> Ошибка/Инсайт -> Решение -> Результат.
4. Длина: 7-12 тыс. знаков.
5. Запрещено: Хэштеги, прямые ссылки на покупку. Только ценность.

ВАЖНО: Читатели vc.ru ценят искренность и технические детали."""

TEASER_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Написать короткий тизер для Telegram/МАХ.
ТРЕБОВАНИЯ:
1. Длина: 300-500 знаков.
2. Цель: Зацепить вниманием, но НЕ раскрывать суть.
3. Формат: Эмоциональный крючок + Интрига + Призыв кликнуть.

ВАЖНО: Не спойлерь решение. Продай любопытство."""

IMAGE_GEN_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Создать промпт для нейросети для генерации обложки.
Ответь только английским промптом. Стиль: Профессиональная фотография, высокое разрешение."""

# =========================
# HELPER FUNCTIONS
# =========================

def groq_request(prompt: str, max_tokens: int = 1200, system: Optional[str] = None, timeout: int = 60) -> Optional[str]:
    sys_msg = system or BASE_SYSTEM
    last_error = None

    for attempt in range(2):
        for model in MODELS:
            try:
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                payload = {
                    "model": model,
                    "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt[:15000]}],
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                }
                r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=timeout)
                
                if r.status_code == 200:
                    data = r.json()
                    result = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if result and result.strip():
                        return result.strip()
                elif r.status_code == 429:
                    time.sleep(3)
                    continue
                else:
                    logger.error(f"❌ Groq {r.status_code}: {r.text[:200]}")
                    
            except Exception as e:
                logger.error(f"❌ Groq error: {e}")
                continue
        
        if attempt == 0:
            time.sleep(2)
            
    return None

async def agroq(prompt: str, max_tokens: int = 1200, system: Optional[str] = None, timeout: int = 60) -> Optional[str]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(groq_request, prompt, max_tokens, system, timeout), timeout=timeout + 15)
    except:
        return None

async def run_sync(func: Callable, *args, timeout: int = 30, default: Any = None) -> Any:
    try:
        return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)
    except:
        return default

# =========================
# PARSING & SPYING ENGINE
# =========================

def scrape_with_api(url: str, premium: bool = False, render: bool = False) -> Optional[str]:
    if not SCRAPER_API_KEY: return None
    params = {"api_key": SCRAPER_API_KEY, "url": url, "country_code": "ru"}
    if premium: params["premium"] = "true"
    if render: params["render"] = "true"
    
    try:
        r = requests.get("http://api.scraperapi.com", params=params, timeout=75)
        if r.status_code == 200 and len(r.text) > 200:
            return r.text
        return None
    except:
        return None

def extract_urls(text: str) -> list:
    return re.findall(r"https?://[^\s,;]+", text)

def detect_source_type(url: str) -> str:
    u = url.lower()
    if "avito.ru" in u: return "avito"
    if "vk.com" in u or "vk.ru" in u: return "vk"
    if "t.me/" in u: return "telegram"
    if "max.ru" in u or "messenger.max.ru" in u: return "max"
    if "instagram" in u: return "instagram"
    return "site"

def parse_generic_site(url: str) -> Optional[str]:
    html = scrape_with_api(url, premium=False, render=True)
    if not html: return None
    
    soup = BeautifulSoup(html, "lxml")
    for s in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        s.decompose()
    
    main_content = soup.find("main") or soup.find("article") or soup.body
    if not main_content: return None
    
    text = main_content.get_text(separator="\n", strip=True)
    return text[:4000] if len(text) > 100 else None

def find_competitors(niche_query: str, top_n: int = 5) -> List[Dict]:
    competitors = []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{niche_query} цены отзывы сайт", region="ru-ru", max_results=top_n))
            for r in results:
                url = r.get("href", "")
                title = r.get("title", "")
                snippet = r.get("body", "")
                if url and "wikipedia" not in url and "youtube" not in url:
                    competitors.append({"url": url, "title": title, "snippet": snippet})
    except Exception as e:
        logger.error(f"❌ Competitor search error: {e}")
    return competitors

def analyze_competitor_data(client_data: str, competitor_list: List[Dict]) -> str:
    comp_texts = []
    for i, comp in enumerate(competitor_list[:3]):
        site_text = parse_generic_site(comp['url'])
        if site_text:
            comp_texts.append(f"КОНКУРЕНТ {i+1} ({comp['title']}):\n{site_text[:1500]}")
        else:
            comp_texts.append(f"КОНКУРЕНТ {i+1} ({comp['title']}):\nСниппет: {comp['snippet']}")
            
    return "\n\n".join(comp_texts)

# =========================
# STATE MACHINE
# =========================

class OnboardingStates(StatesGroup):
    choosing_mode = State()
    pilot_links = State()       
    interview_q1 = State()      
    interview_q2 = State()      
    interview_q3 = State()      
    interview_q4 = State()      
    interview_q5 = State()      
    photo_setup = State()       
    analyzing = State()         
    dashboard = State()         
    editing_article = State()   

# =========================
# BACKGROUND WORKERS
# =========================

ACTIVE_TASKS: Dict[int, asyncio.Task] = {}

async def cancel_task(uid: int):
    task = ACTIVE_TASKS.pop(uid, None)
    if task and not task.done():
        task.cancel()

async def schedule_task(uid: int, func: Callable, *args, **kwargs):
    await cancel_task(uid)
    task = asyncio.create_task(func(uid, *args, **kwargs))
    ACTIVE_TASKS[uid] = task

async def worker_market_spy(chat_id: int, state: FSMContext, mode: str, input_data: dict):
    await bot.send_message(chat_id, "🕵️♂️ **Запускаю глубокий шпионаж...**\nИзучаю конкурентов, анализирую боли ЦА и формирую стратегию.\nЭто займет 2-3 минуты.", disable_notification=True)
    
    client_raw_data = ""
    if mode == "pilot":
        links = input_data.get("links", [])
        for link in links:
            stype = detect_source_type(link)
            if stype == "site":
                txt = parse_generic_site(link)
                if txt: client_raw_data += f"\n=== SITE: {link} ===\n{txt}\n"
            elif stype == "avito":
                txt = scrape_with_api(link, premium=True, render=True)
                if txt:
                     soup = BeautifulSoup(txt, "lxml")
                     clean = soup.get_text(separator="\n", strip=True)[:2000]
                     client_raw_data += f"\n=== AVITO: {link} ===\n{clean}\n"
            elif stype == "vk":
                 vk_url = link.replace("vk.com", "m.vk.com").replace("vk.ru", "m.vk.com")
                 txt = scrape_with_api(vk_url, premium=True, render=True)
                 if txt:
                     soup = BeautifulSoup(txt, "lxml")
                     clean = soup.get_text(separator="\n", strip=True)[:2000]
                     client_raw_data += f"\n=== VK: {link} ===\n{clean}\n"
            elif stype == "telegram":
                 ch = link.split("/")[-1]
                 pub_link = f"https://t.me/s/{ch}"
                 txt = scrape_with_api(pub_link, premium=False, render=False)
                 if txt:
                     soup = BeautifulSoup(txt, "lxml")
                     posts = [p.get_text(strip=True) for p in soup.find_all("div", class_="tgme_widget_message_text")]
                     client_raw_data += f"\n=== TELEGRAM: {link} ===\n" + "\n".join(posts[:5])[:2000] + "\n"
            elif stype == "max":
                 pass 
    
    if not client_raw_data:
        client_raw_data = input_data.get("description", "Нет данных с сайтов.")

    niche_keyword = input_data.get("keyword_guess", "")
    if not niche_keyword:
        desc = input_data.get("description", "")
        niche_def = await agroq(f"Определи основное ключевое слово для SEO по этому описанию бизнеса (только слово, без точек):\n{desc}", max_tokens=20, system=BASE_SYSTEM)
        niche_keyword = niche_def.strip().lower() if niche_def else "услуги"

    competitors = find_competitors(niche_keyword)
    comp_analysis_text = analyze_competitor_data(client_raw_data, competitors)

    spy_prompt = f"""
ДАННЫЕ КЛИЕНТА:
{client_raw_data[:6000]}

ИНФО О КОНКУРЕНТАХ (ТОП-3):
{comp_analysis_text[:4000]}

КЛЮЧЕВОЙ ЗАПРОС НИШИ: {niche_keyword}

ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ (Интервью/Описание):
{input_data.get('description', '')}

Выполни глубокий стратегический анализ согласно инструкции SPY_SYSTEM.
Особое внимание удели языку ЦА и слабым местам конкурентов.
"""

    spy_report = await agroq(spy_prompt, max_tokens=3000, system=SPY_SYSTEM, timeout=120)

    if not spy_report:
        await bot.send_message(chat_id, "⚠️ Ошибка анализа. Попробуй позже.", reply_markup=get_menu())
        return

    # --- ВАЖНОЕ ИСПРАВЛЕНИЕ: Регистрация пользователя перед сохранением ---
    # Получаем данные юзера из контекста сообщения (если возможно) или используем заглушку
    # Так как мы в фоновом потоке, у нас нет объекта message.from_user напрямую, 
    # но chat_id это и есть user_id в Telegram.
    
    user_registered = ensure_user_exists(chat_id)
    if not user_registered:
         await bot.send_message(chat_id, "❌ Критическая ошибка базы данных. Пользователь не создан.")
         return

    try:
        res_strategy = supabase.table("market_strategies").insert({
            "user_id": chat_id,
            "niche_keyword": niche_keyword,
            "full_report": spy_report,
            "target_audience_profile": "Extracted from report", 
            "tone_of_voice_guide": "Extracted from report",
            "status": "active"
        }).execute()
        
        strategy_id = res_strategy.data[0]['id']
        
        supabase.table("article_ideas_queue").insert({
            "strategy_id": strategy_id,
            "topic_title": "Стартовая статья: Разбор главной боли ЦА",
            "pain_point": "Недоверие к качеству материалов",
            "competitor_weakness": "Конкуренты используют сырое дерево",
            "relevance_score": 10.0,
            "is_processed": False
        }).execute()
        
    except Exception as e:
        logger.error(f"DB save error: {e}")
        await bot.send_message(chat_id, f"❌ Ошибка сохранения данных: {str(e)[:100]}")
        return

    await state.update_data(strategy_id=strategy_id, niche=niche_keyword)
    await state.set_state(OnboardingStates.photo_setup)
    
    kb_photo = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Есть свои фото (Яндекс.Диск)", callback_data="set_disk_url")],
        [InlineKeyboardButton(text="✨ Генерировать самим", callback_data="use_ai_gen")]
    ])
    
    await bot.send_message(
        chat_id,
        "✅ **Стратегия готова!**\n\n"
        "Я изучил рынок и подготовил план статей.\n\n"
        "Теперь настроим визуал:\n"
        "1. Если у тебя есть качественные фото работ — дай ссылку на папку в Яндекс.Диске.\n"
        "2. Если нет — я буду генерировать профессиональные обложки и инфографику сам, ориентируясь на стиль рынка.\n\n"
        "Выбери вариант:",
        reply_markup=kb_photo
    )

# =========================
# UI HELPERS
# =========================

def get_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Мой Дашборд"), KeyboardButton(text="🚀 Следующая статья")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие..."
    )

async def send_long_bot(chat_id: int, text: str, reply_markup=None):
    MAX = 3500
    parts = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    for idx, part in enumerate(parts):
        rm = reply_markup if idx == 0 else None
        try:
            await bot.send_message(chat_id, part, parse_mode="Markdown", reply_markup=rm)
        except:
            await bot.send_message(chat_id, part, reply_markup=rm)
        await asyncio.sleep(0.5)

# =========================
# HANDLERS
# =========================

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    
    # Регистрируем пользователя сразу при старте
    ensure_user_exists(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    kb_choice = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 ЕСТЬ ССЫЛКИ (Автопилот)", callback_data="mode_pilot")],
        [InlineKeyboardButton(text=" НЕТ ССЫЛОК (Глубокий Бриф)", callback_data="mode_interview")]
    ])
    
    await message.answer(
        "👋 Привет! Я твой автономный SEO-агент.\n\n"
        "Я не просто пишу тексты. Я изучаю твой рынок, шпионю за конкурентами, понимаю боли твоей аудитории и создаю контент, который продает.\n\n"
        "Как начнем?",
        reply_markup=kb_choice
    )

@dp.callback_query(F.data == "mode_pilot")
async def cb_mode_pilot(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(OnboardingStates.pilot_links)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "🔗 Пришли все возможные ссылки на твой бизнес:\n"
        "• Сайт\n"
        "• Авито\n"
        "• ВКонтакте\n"
        "• Telegram-канал\n"
        "• МАХ\n\n"
        "(Через пробел или запятую)\n\n"
        "💡 Чем больше данных, тем точнее будет мой анализ.",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "mode_interview")
async def cb_mode_interview(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(interview_answers={}, step=1)
    await state.set_state(OnboardingStates.interview_q1)
    await callback.message.edit_reply_markup(reply_markup=None)
    
    q1 = "1. Что именно ты продаёшь и кому? (Опиши продукт и портрет клиента)"
    await callback.message.answer(f"❓ {q1}")

# --- PILOT LOGIC ---

@dp.message(OnboardingStates.pilot_links)
async def pilot_get_links(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    text = message.text.strip()
    
    urls = extract_urls(text)
    if not urls:
        await message.answer("Не вижу ссылок. Пришли URL.")
        return

    input_data = {
        "links": urls,
        "description": text,
        "keyword_guess": "" 
    }
    
    await schedule_task(uid, worker_market_spy, state, "pilot", input_data)

# --- INTERVIEW LOGIC ---

INTERVIEW_QUESTIONS = {
    1: "1. Что именно ты продаёшь и кому? (Опиши продукт и портрет клиента)",
    2: "2. Кто твой идеальный клиент? Какие у него главные страхи при покупке?",
    3: "3. Почему он должен купить у тебя, а не у конкурента? (Твоя суперсила)",
    4: "4. Есть ли у тебя кейсы с цифрами? (Было/Стало, экономия времени/денег)",
    5: "5. Как ты общаешься с клиентами? (Сухо/технически, дружелюбно/простыми словами, дерзко/провокационно?)"
}

@dp.message(OnboardingStates.interview_q1)
async def interview_step_1(message: types.Message, state: FSMContext):
    data = await state.get_data()
    answers = data.get("interview_answers", {})
    answers["q1"] = message.text
    await state.update_data(interview_answers=answers)
    await state.set_state(OnboardingStates.interview_q2)
    await message.answer(f"❓ {INTERVIEW_QUESTIONS[2]}")

@dp.message(OnboardingStates.interview_q2)
async def interview_step_2(message: types.Message, state: FSMContext):
    data = await state.get_data()
    answers = data.get("interview_answers", {})
    answers["q2"] = message.text
    await state.update_data(interview_answers=answers)
    await state.set_state(OnboardingStates.interview_q3)
    await message.answer(f"❓ {INTERVIEW_QUESTIONS[3]}")

@dp.message(OnboardingStates.interview_q3)
async def interview_step_3(message: types.Message, state: FSMContext):
    data = await state.get_data()
    answers = data.get("interview_answers", {})
    answers["q3"] = message.text
    await state.update_data(interview_answers=answers)
    await state.set_state(OnboardingStates.interview_q4)
    await message.answer(f"❓ {INTERVIEW_QUESTIONS[4]}")

@dp.message(OnboardingStates.interview_q4)
async def interview_step_4(message: types.Message, state: FSMContext):
    data = await state.get_data()
    answers = data.get("interview_answers", {})
    answers["q4"] = message.text
    await state.update_data(interview_answers=answers)
    await state.set_state(OnboardingStates.interview_q5)
    await message.answer(f"❓ {INTERVIEW_QUESTIONS[5]}")

@dp.message(OnboardingStates.interview_q5)
async def interview_step_5(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    answers = data.get("interview_answers", {})
    answers["q5"] = message.text
    
    full_desc = " ".join([f"{v}" for v in answers.values()])
    
    input_data = {
        "description": full_desc,
        "answers": answers,
        "keyword_guess": answers.get("q1", "")[:50],
        "links": []
    }
    
    await schedule_task(uid, worker_market_spy, state, "interview", input_data)

# --- PHOTO SETUP LOGIC ---

@dp.callback_query(F.data == "set_disk_url")
async def cb_set_disk_url(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(OnboardingStates.photo_setup) 
    await callback.message.answer(
        "📂 Пришли ссылку на папку с фото в Яндекс.Диске.\n"
        "Пример: https://disk.yandex.ru/d/xxxxxx\n\n"
        "Я скачаю лучшие кадры и буду использовать их как референс для стиля и прямых вставок.",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "use_ai_gen")
async def cb_use_ai_gen(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    uid = callback.from_user.id
    
    # Убеждаемся, что пользователь есть в БД
    ensure_user_exists(uid)
    
    try:
        supabase.table("user_settings").upsert({
            "user_id": uid,
            "use_generated_images": True,
            "yandex_disk_folder_url": None
        }).eq("user_id", uid).execute()
    except: pass
    
    await show_dashboard(callback.message, state, uid)

@dp.message(OnboardingStates.photo_setup)
async def handle_disk_url(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    url = message.text.strip()
    
    if "disk.yandex.ru" not in url:
        await message.answer("Это не ссылка на Яндекс.Диск. Проверь адрес.")
        return
        
    ensure_user_exists(uid)
    
    try:
        supabase.table("user_settings").upsert({
            "user_id": uid,
            "use_generated_images": False,
            "yandex_disk_folder_url": url
        }).eq("user_id", uid).execute()
    except Exception as e:
        logger.error(f"Save settings error: {e}")
        
    await show_dashboard(message, state, uid)

# --- DASHBOARD LOGIC ---

async def show_dashboard(message: types.Message, state: FSMContext, uid: int):
    await state.set_state(OnboardingStates.dashboard)
    
    try:
        ideas_res = supabase.table("article_ideas_queue").select("*").eq("is_processed", False).order("relevance_score", desc=True).limit(1).execute()
        next_idea = ideas_res.data[0] if ideas_res.data else None
        
        articles_today = supabase.table("published_articles").select("*").filter("publication_date", "gte", str(date.today())).eq("user_id", uid).count().execute()
        count_today = articles_today.count if articles_today.count else 0
    except:
        next_idea = None
        count_today = 0
        
    msg = f"📊 **МОЙ ДАШБОРД**\n\n"
    msg += f" **Статус:** Активен\n"
    msg += f"🔹 **Опубликовано сегодня:** {count_today}/{DAILY_LIMIT}\n"
    
    if next_idea:
        msg += f"\n🚀 **Следующая тема:**\n\"{next_idea['topic_title']}\"\n"
        msg += f"💥 **Боль ЦА:** {next_idea['pain_point']}\n"
    else:
        msg += "\n⚠️ **Очередь идей пуста.** Нужно обновить стратегию.\n"
        
    msg += "\nНажми 🚀 **Следующая статья**, чтобы начать производство."
    
    kb_dash = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Следующая статья", callback_data="generate_next_article")],
        [InlineKeyboardButton(text="⚙️ Настройки каналов", callback_data="setup_channels")],
        [InlineKeyboardButton(text="📥 Скачать архив", callback_data="download_archive")]
    ])
    
    await message.answer(msg, reply_markup=kb_dash, parse_mode="Markdown")

@dp.callback_query(F.data == "generate_next_article")
async def cb_generate_next(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    uid = callback.from_user.id
    
    # Проверка лимита
    try:
        articles_today = supabase.table("published_articles").select("*").filter("publication_date", "gte", str(date.today())).eq("user_id", uid).count().execute()
        if articles_today.count >= DAILY_LIMIT:
            await callback.message.answer(f"⛔ **Лимит исчерпан.**\nМаксимум {DAILY_LIMIT} статьи в день. Жди завтра!")
            return
    except: pass
    
    # Берем следующую идею
    try:
        ideas_res = supabase.table("article_ideas_queue").select("*").eq("is_processed", False).order("relevance_score", desc=True).limit(1).execute()
        if not ideas_res.data:
            await callback.message.answer("❌ Нет идей в очереди. Обнови стратегию.")
            return
            
        idea = ideas_res.data[0]
        idea_id = idea['id']
        
        supabase.table("article_ideas_queue").update({"is_processed": True}).eq("id", idea_id).execute()
        
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка БД: {e}")
        return

    await callback.message.answer(f"✍️ **Пишу статью:** \"{idea['topic_title']}\"...\nАдаптирую под все площадки.", disable_notification=True)
    
    await schedule_task(uid, worker_produce_article, state, idea, idea_id)

async def worker_produce_article(chat_id: int, state: FSMContext, idea: dict, idea_id: int):
    strat_res = supabase.table("market_strategies").select("tone_of_voice_guide, target_audience_profile").eq("user_id", chat_id).order("created_at", desc=True).limit(1).execute()
    context = ""
    if strat_res.data:
        context = f"Тон: {strat_res.data[0].get('tone_of_voice_guide', '')}\nЦА: {strat_res.data[0].get('target_audience_profile', '')}"
        
    writer_prompt = f"""
Тема: {idea['topic_title']}
Боль ЦА: {idea['pain_point']}
Слабость конкурента: {idea['competitor_weakness']}
Инструкция по тону: См. ToneOfVoiceGuide в стратегии.

Напиши статью для Дзена.
"""
    full_writer_prompt = f"{writer_prompt}\n\nКОНТЕКСТ СТРАТЕГИИ:\n{context}"
    
    dzen_text = await agroq(full_writer_prompt, max_tokens=2600, system=WRITER_DZEN_SYSTEM, timeout=120)
    
    if not dzen_text:
        await bot.send_message(chat_id, "⚠️ Ошибка генерации текста.")
        return
        
    titles_prompt = f"Придумай 3 заголовка для этой статьи:\n{dzen_text[:500]}..."
    titles_res = await agroq(titles_prompt, max_tokens=200, system=TITLE_GEN_SYSTEM, timeout=30)
    
    vc_text = await agroq(f"Адаптируй под vc.ru:\n{dzen_text}", max_tokens=2600, system=WRITER_VC_SYSTEM, timeout=120)
    teaser_tg = await agroq(f"Напиши тизер для TG:\n{dzen_text}", max_tokens=300, system=TEASER_SYSTEM, timeout=30)
    teaser_max = teaser_tg 
    
    img_prompt = await agroq(f"Опиши визуал для статьи: {idea['topic_title']}...", max_tokens=100, system=IMAGE_GEN_SYSTEM, timeout=30)
    
    image_url = "https://images.unsplash.com/photo-1518780664697-55e3ad937233?w=1200&h=630&fit=crop" 
    
    try:
        res_insert = supabase.table("published_articles").insert({
            "idea_id": idea_id,
            "user_id": chat_id,
            "dzen_content": dzen_text,
            "vc_ru_content": vc_text,
            "telegram_preview": teaser_tg,
            "max_preview": teaser_max,
            "main_image_url": image_url,
            "title_clickbait": titles_res.split('\n')[0] if titles_res else "",
            "title_expert": titles_res.split('\n')[1] if titles_res and len(titles_res.split('\n'))>1 else "",
            "title_question": titles_res.split('\n')[2] if titles_res and len(titles_res.split('\n'))>2 else "",
            "publish_status": "ready",
            "version_status": "draft",
            "publication_date": str(datetime.now())
        }).execute()
        
        article_db_id = res_insert.data[0]['id']
        
    except Exception as e:
        logger.error(f"Save article error: {e}")
        return

    msg = f"✅ **СТАТЬЯ ГОТОВА!**\n\n"
    msg += f"📄 **Для Дзена:**\n{dzen_text[:500]}...\n\n"
    msg += f" **Заголовки:**\n{titles_res}\n\n"
    msg += f"📱 **Тизер для TG/МАХ:**\n{teaser_tg}\n\n"
    msg += "Что делать дальше?"
    
    kb_art = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Ручная правка", callback_data=f"edit_manual_{article_db_id}")],
        [InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"regen_{article_db_id}")],
        [InlineKeyboardButton(text="✅ Принять и сохранить", callback_data=f"accept_{article_db_id}")],
        [InlineKeyboardButton(text="📢 Публиковать в TG", callback_data=f"pub_tg_{article_db_id}")]
    ])
    
    await send_long_bot(chat_id, msg, reply_markup=kb_art)

# =========================
# EDITING & REGENERATION LOGIC
# =========================

@dp.callback_query(F.data.startswith("edit_manual_"))
async def cb_edit_manual(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    article_id = int(callback.data.split("_")[2])
    uid = callback.from_user.id
    
    try:
        res = supabase.table("published_articles").select("dzen_content").eq("id", article_id).single().execute()
        current_text = res.data["dzen_content"]
    except:
        await callback.message.answer("Ошибка загрузки текста.")
        return
        
    await state.update_data(editing_article_id=article_id)
    await state.set_state(OnboardingStates.editing_article)
    
    await callback.message.answer(
        "✏️ **Режим ручной правки**\n\n"
        "Пришли полный исправленный текст статьи ниже.\n"
        "Я заменю им старый вариант.\n\n"
        "*(Совет: можно скопировать текст, изменить нужные слова и отправить обратно)*",
        parse_mode="Markdown"
    )

@dp.message(OnboardingStates.editing_article)
async def process_manual_edit(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    article_id = data.get("editing_article_id")
    new_text = message.text.strip()
    
    if not article_id:
        await message.answer("Ошибка контекста. /start")
        return
        
    try:
        old_res = supabase.table("published_articles").select("dzen_content").eq("id", article_id).single().execute()
        old_text = old_res.data["dzen_content"]
        
        supabase.table("article_edit_history").insert({
            "article_id": article_id,
            "user_id": uid,
            "change_type": "manual_text_replace",
            "old_content_snippet": old_text[:200],
            "new_content_snippet": new_text[:200]
        }).execute()
        
        supabase.table("published_articles").update({
            "dzen_content": new_text,
            "version_status": "edited",
            "last_edit_time": str(datetime.now())
        }).eq("id", article_id).execute()
        
        cur_val = supabase.table("published_articles").select("edit_count").eq("id", article_id).single().execute().data["edit_count"]
        supabase.table("published_articles").update({"edit_count": cur_val + 1}).eq("id", article_id).execute()
        
    except Exception as e:
        logger.error(f"Edit save error: {e}")
        await message.answer("❌ Ошибка сохранения правки.")
        return
        
    await message.answer("✅ Правки сохранены!\nТекст обновлен.", reply_markup=get_menu())
    await state.clear()

@dp.callback_query(F.data.startswith("regen_"))
async def cb_regen(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    article_id = int(callback.data.split("_")[1])
    uid = callback.from_user.id
    
    await callback.message.answer("🔄 **Перегенерация...**\nПишу новый вариант статьи на основе той же темы.", disable_notification=True)
    
    try:
        art_res = supabase.table("published_articles").select("idea_id").eq("id", article_id).single().execute()
        idea_id = art_res.data["idea_id"]
        idea_res = supabase.table("article_ideas_queue").select("*").eq("id", idea_id).single().execute()
        idea = idea_res.data
        
        strat_res = supabase.table("market_strategies").select("tone_of_voice_guide, target_audience_profile").eq("user_id", uid).order("created_at", desc=True).limit(1).execute()
        context = ""
        if strat_res.data:
            context = f"Тон: {strat_res.data[0].get('tone_of_voice_guide', '')}\nЦА: {strat_res.data[0].get('target_audience_profile', '')}"
            
        writer_prompt = f"""
Тема: {idea['topic_title']}
Боль ЦА: {idea['pain_point']}
Слабость конкурента: {idea['competitor_weakness']}
Инструкция по тону: {context}

Напиши СТАТЬЮ ЗАНОВО (другими словами, но тот же смысл).
"""
        dzen_text = await agroq(writer_prompt, max_tokens=2600, system=WRITER_DZEN_SYSTEM, timeout=120)
        
        if not dzen_text:
            await callback.message.answer("⚠️ Ошибка перегенерации.")
            return
            
        supabase.table("published_articles").update({
            "dzen_content": dzen_text,
            "version_status": "regenerated",
            "last_edit_time": str(datetime.now())
        }).eq("id", article_id).execute()
        
        supabase.table("article_edit_history").insert({
            "article_id": article_id,
            "user_id": uid,
            "change_type": "regenerate",
            "old_content_snippet": "Previous version",
            "new_content_snippet": dzen_text[:200]
        }).execute()
        
        await callback.message.answer("✅ Статья перегенерирована!\nНиже новый вариант.", reply_markup=get_menu())
        await send_long_bot(uid, f"📄 **НОВЫЙ ВАРИАНТ:**\n\n{dzen_text[:500]}...")
        
    except Exception as e:
        logger.error(f"Regen error: {e}")
        await callback.message.answer("❌ Ошибка перегенерации.")

@dp.callback_query(F.data.startswith("accept_"))
async def cb_accept(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    article_id = int(callback.data.split("_")[1])
    
    try:
        supabase.table("published_articles").update({
            "version_status": "final_approved"
        }).eq("id", article_id).execute()
        
        await callback.message.answer("✅ Статья принята как финальная версия.\nМожно публиковать.", reply_markup=get_menu())
    except Exception as e:
        await callback.message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data.startswith("pub_tg_"))
async def cb_pub_tg(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    article_id = int(callback.data.split("_")[2])
    uid = callback.from_user.id
    
    await callback.message.answer("📢 Публикация в Telegram-канал...\n(Функция требует настройки токена канала)", reply_markup=get_menu())

# =========================
# COMMANDS & UTILS (FIXED SYNTAX)
# =========================

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📋 **Команды:**\n"
        "/start - Начать заново\n"
        "/status - Проверить статус\n"
        "/settings - Настроить каналы\n"
        "/download - Скачать архив статей",
        reply_markup=get_menu()
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    try:
        ideas_left = supabase.table("article_ideas_queue").select("*").eq("is_processed", False).count().execute() 
        await message.answer(f"🟢 Агент активен.\nОсталось идей в очереди: ~{ideas_left.count if ideas_left.count else 0}")
    except:
        await message.answer("🟢 Агент активен.")

@dp.callback_query(F.data == "setup_channels")
async def cb_setup_channels(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "⚙️ **Настройка каналов публикации**\n\n"
        "1. **Telegram:** Добавь бота в админы канала и пришли username (@channel).\n"
        "2. **Дзен:** Сейчас автоматическая публикация через API ограничена. Я буду готовить готовые посты для копирования или использовать RSS-боты.\n"
        "3. **VC.RU:** Я буду формировать письмо редактору. Пришли email для контактов?\n\n"
        "Пришли данные в формате:\nTG:@mychan\nEMAIL:test@mail.ru",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "download_archive")
async def cb_download_archive(callback: types.CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    try:
        res = supabase.table("published_articles").select("*").eq("user_id", uid).order("created_at", desc=True).limit(10).execute()
        if not res.data:
            await callback.message.answer("Нет статей.")
            return
        
        all_text = ""
        for art in res.data:
            all_text += f"\n\n{'='*30}\nID: {art['id']}\n{'='*30}\n"
            all_text += f"[DZEN]\n{art['dzen_content']}\n\n"
            all_text += f"[VC.RU]\n{art['vc_ru_content']}\n\n"
            all_text += f"[TEASER]\n{art['telegram_preview']}\n"
            
        doc = BufferedInputFile(all_text.encode('utf-8'), filename="seo_pack_full.txt")
        await callback.message.answer_document(doc, caption="💾 Архив статей")
    except Exception as e:
        await callback.message.answer(f"Ошибка: {e}")

# =========================
# MIDDLEWARE & SERVER
# =========================

class ErrorHandlerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except Exception as e:
            logger.error(f"MIDDLEWARE ERR: {e}", exc_info=True)
            try:
                msg = getattr(event, 'message', None) or getattr(getattr(event, 'callback_query', None), 'message', None)
                if msg:
                    await msg.answer("⚠️ Системная ошибка. Нажми ▶️ Продолжить или /start.", reply_markup=get_menu())
            except: pass
            return None

dp.message.middleware(ErrorHandlerMiddleware())
dp.callback_query.middleware(ErrorHandlerMiddleware())

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): pass

def start_health_server():
    port = int(os.getenv("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

def heartbeat():
    while True:
        time.sleep(60)
        logger.info("💓 HEARTBEAT: alive")

# =========================
# MAIN LOOP
# =========================

async def main():
    logger.info("🚀 Starting AUTONOMOUS AGENT V5...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    await asyncio.sleep(2)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    asyncio.run(main())
