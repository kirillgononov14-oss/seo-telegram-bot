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

# Импорты для поиска
try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        logger.error("❌ Neither 'ddgs' nor 'duckduckgo-search' found.")
        raise

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
MODELS = ["llama-3.3-70b-versatile"] # Более надежная модель чем qwen для сложных задач

DAILY_LIMIT = 3 
GLOBAL_TIMEOUT_SEC = 300 

# Глобальный словарь статусов для быстрого доступа командой /status
ACTIVE_TASKS: Dict[int, asyncio.Task] = {}
TASK_STATUS: Dict[int, str] = {} 

# =========================
# DATABASE HELPERS
# =========================

def register_user_safe(user_id: int, username: str = "", first_name: str = "") -> bool:
    try:
        data = {"id": user_id, "username": username or "", "first_name": first_name or ""}
        res = supabase.table("users").upsert(data).execute()
        return True if res.data else False
    except Exception as e:
        logger.error(f"DB Register Error: {e}")
        return False

# =========================
# PROMPTS ENGINE
# =========================

BASE_SYSTEM = """Ты — элитный SEO-стратег. Пиши на русском. Без Markdown заголовков (#). Используй эмодзи.
Говори языком ЦА (простые слова, живые примеры)."""

SPY_SYSTEM = BASE_SYSTEM + """
ЗАДАЧА: Анализ рынка.
ВЫВЕДИ JSON:
{
  "niche_keyword": "...",
  "competitors_weaknesses": ["...", "..."],
  "audience_pains": ["...", "..."],
  "article_ideas": [
    {"topic": "...", "pain_point": "...", "weakness_to_hammer": "...", "score": 10.0}
  ]
}"""

WRITER_DZEN_SYSTEM = BASE_SYSTEM + """
ЗАДАЧА: Статья для Дзен. 5-7к знаков. Структура: Боль -> Решение -> Сравнение с рынком -> CTA. SEO блок в конце."""

WRITER_VC_SYSTEM = BASE_SYSTEM + """
ЗАДАЧА: Адаптация под VC.RU. Стиль кейса/факапа. Без рекламы. Только инсайды."""

TEASER_SYSTEM = BASE_SYSTEM + """
ЗАДАЧА: Тизер для TG/МАХ. 300-500 знаков. Интрига, но без спойлеров."""

TITLE_GEN_SYSTEM = BASE_SYSTEM + """
ЗАДАЧА: 3 заголовка.
A: Кликбейт.
B: Экспертный.
C: Вопросительный.
Формат:
A: ...
B: ...
C: ..."""

IMAGE_GEN_SYSTEM = BASE_SYSTEM + """
ЗАДАЧА: Промпт для Midjourney/Stable Diffusion на английском. Realistic photography."""

# =========================
# NETWORK & PARSING HELPERS
# =========================

def normalize_vk_url(url: str) -> str:
    """Приводит vk.ru и другие варианты к m.vk.com для лучшего парсинга"""
    u = url.lower()
    if "vk.com" in u:
        return u.replace("vk.com", "m.vk.com")
    if "vk.ru" in u:
        return u.replace("vk.ru", "m.vk.com")
    return u

def scrape_with_api(url: str, premium: bool = False, render: bool = False) -> Optional[str]:
    if not SCRAPER_API_KEY: 
        logger.warning("No Scraper API Key")
        return None
    
    # Нормализация URL специально для VK
    final_url = url
    if "vk." in url:
        final_url = normalize_vk_url(url)

    params = {"api_key": SCRAPER_API_KEY, "url": final_url, "country_code": "ru"}
    if premium: params["premium"] = "true"
    if render: params["render"] = "true"
    
    try:
        r = requests.get("http://api.scraperapi.com", params=params, timeout=60)
        if r.status_code == 200 and len(r.text) > 200:
            return r.text
        return None
    except Exception as e:
        logger.error(f"Scrape error {url}: {e}")
        return None

def extract_urls(text: str) -> list:
    return re.findall(r"https?://[^\s,;]+", text)

def detect_source_type(url: str) -> str:
    u = url.lower()
    if "avito.ru" in u: return "avito"
    if "vk.com" in u or "vk.ru" in u: return "vk"
    if "t.me/" in u: return "telegram"
    if "max.ru" in u or "messenger.max.ru" in u: return "max"
    return "site"

def parse_content(html: str) -> str:
    if not html: return ""
    soup = BeautifulSoup(html, "lxml")
    for s in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        s.decompose()
    main_content = soup.find("main") or soup.find("article") or soup.body
    if not main_content: return ""
    text = main_content.get_text(separator="\n", strip=True)
    return text[:4000] if len(text) > 100 else ""

async def agroq(prompt: str, max_tokens: int = 1200, system: Optional[str] = None, timeout: int = 60) -> Optional[str]:
    sys_msg = system or BASE_SYSTEM
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODELS[0],
        "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt[:15000]}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    try:
        r = await asyncio.to_thread(requests.post, GROQ_URL, headers=headers, json=payload, timeout=timeout)
        if r.status_code == 200:
            return r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.error(f"Groq Async Error: {e}")
    return None

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
    dashboard = State()         
    editing_article = State()   

# =========================
# WORKERS (BACKGROUND JOBS)
# =========================

async def worker_market_spy(chat_id: int, state: FSMContext, mode: str, input_data: dict):
    TASK_STATUS[chat_id] = "🕵️♂️ Изучаю источники..."
    
    try:
        # 1. Парсинг данных клиента
        client_raw_data = ""
        parsing_report_lines = [] 
        
        if mode == "pilot":
            links = input_data.get("links", [])
            for link in links:
                stype = detect_source_type(link)
                content = ""
                
                if stype == "site":
                    html = await asyncio.to_thread(scrape_with_api, link, False, True)
                    content = parse_content(html)
                    status = "✅ Сайт изучен" if content else "⚠️ Пусто/Ошибка"
                    parsing_report_lines.append(f"🌐 Сайт: {status} ({len(content)} зн.)")
                    
                elif stype == "avito":
                    html = await asyncio.to_thread(scrape_with_api, link, True, True)
                    content = parse_content(html)
                    status = "✅ Объявление прочитано" if content else "⚠️ Мало данных"
                    parsing_report_lines.append(f"✈️ Авито: {status} ({len(content)} зн.)")
                    
                elif stype == "vk":
                    # Специальная обработка VK через нормализацию
                    html = await asyncio.to_thread(scrape_with_api, link, True, True)
                    content = parse_content(html)
                    status = "✅ Группа изучена" if content else "❌ Блокировка/VK требует вход"
                    parsing_report_lines.append(f"💬 ВК: {status} ({len(content)} зн.)")
                    
                elif stype == "telegram":
                    ch = link.split("/")[-1]
                    pub_link = f"https://t.me/s/{ch}"
                    html = await asyncio.to_thread(scrape_with_api, pub_link, False, False)
                    soup = BeautifulSoup(html or "", "lxml")
                    posts = [p.get_text(strip=True) for p in soup.find_all("div", class_="tgme_widget_message_text")]
                    content = "\n".join(posts[:5])
                    status = "✅ Канал прочитан" if content else "⚠️ Публичная версия недоступна"
                    parsing_report_lines.append(f"📢 TG: {status} ({len(content)} зн.)")
                
                if content:
                    client_raw_data += f"\n=== SOURCE [{stype.upper()}]: {link} ===\n{content}\n"

        # Отправляем отчет пользователю сразу, чтобы он видел прогресс
        if parsing_report_lines:
            report_text = "📊 **Отчет по твоим источникам:**\n\n" + "\n".join(parsing_report_lines)
            await bot.send_message(chat_id, report_text)
        
        if not client_raw_data:
            client_raw_data = input_data.get("description", "Нет данных.")

        TASK_STATUS[chat_id] = "🧠 Анализирую рынок и конкурентов..."
        
        # 2. Поиск конкурентов
        niche_guess = input_data.get("keyword_guess", "")
        if not niche_guess:
            # Быстрый запрос на определение ниши
            niche_def = await agroq(f"Определи одно ключевое слово для SEO бизнеса:\n{client_raw_data[:1000]}", max_tokens=20, system=BASE_SYSTEM)
            niche_guess = niche_def.strip().lower() if niche_def else "услуги"

        competitors = []
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(f"{niche_guess} цены отзывы сайт", region="ru-ru", max_results=3))
                for r in results:
                    if r.get('href'):
                        competitors.append({"url": r['href'], "title": r.get('title', ''), "snippet": r.get('body', '')})
        except: pass

        comp_analysis_text = ""
        for i, comp in enumerate(competitors[:2]): # Берем топ-2 для скорости
            c_html = await asyncio.to_thread(scrape_with_api, comp['url'], False, True)
            c_content = parse_content(c_html)
            comp_analysis_text += f"\nКОНКУРЕНТ {i+1} ({comp['title']}):\n{c_content[:1000]}"

        TASK_STATUS[chat_id] = "📝 Генерирую стратегию статей..."

        spy_prompt = f"""
ДАННЫЕ КЛИЕНТА:
{client_raw_data[:5000]}

КОНКУРЕНТЫ:
{comp_analysis_text[:3000]}

НИША: {niche_guess}

Выполни анализ согласно SPY_SYSTEM. Верни строго JSON.
"""
        raw_json_response = await agroq(spy_prompt, max_tokens=2000, system=SPY_SYSTEM, timeout=90)
        
        if not raw_json_response:
            raise Exception("LLM failed to generate strategy")

        # Парсим JSON (очищаем от markdown блоков если есть)
        clean_json = raw_json_response.replace("```json", "").replace("```", "").strip()
        try:
            strategy_data = json.loads(clean_json)
        except:
            # Если JSON битый, сохраняем как текст fallback
            strategy_data = {"raw_text": raw_json_response, "article_ideas": []}

        # 3. Сохранение в БД
        res_strategy = supabase.table("market_strategies").insert({
            "user_id": chat_id,
            "niche_keyword": niche_guess,
            "full_report": raw_json_response,
            "target_audience_profile": ", ".join(strategy_data.get("audience_pains", [])),
            "tone_of_voice_guide": "Живой язык ЦА",
            "status": "active"
        }).execute()
        
        strategy_id = res_strategy.data[0]['id']
        
        ideas = strategy_data.get("article_ideas", [])
        if not ideas:
            ideas = [{"topic": "Стартовая статья", "pain_point": "Боль клиента", "weakness_to_hammer": "Слабость конкурента", "score": 10.0}]

        for idea in ideas[:5]: # Создаем первые 5 идей
            supabase.table("article_ideas_queue").insert({
                "strategy_id": strategy_id,
                "topic_title": idea.get("topic", "Новая тема"),
                "pain_point": idea.get("pain_point", ""),
                "competitor_weakness": idea.get("weakness_to_hammer", ""),
                "relevance_score": float(idea.get("score", 5.0)),
                "is_processed": False
            }).execute()

        TASK_STATUS[chat_id] = "✅ Стратегия готова!"
        
        # Переход к настройке фото
        await state.update_data(strategy_id=strategy_id, niche=niche_guess)
        await state.set_state(OnboardingStates.photo_setup)
        
        kb_photo = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📸 Свои фото (Яндекс.Диск)", callback_data="set_disk_url")],
            [InlineKeyboardButton(text="✨ Генерировать самим", callback_data="use_ai_gen")]
        ])
        
        await bot.send_message(
            chat_id,
            "✅ **Стратегия готова!**\n\n"
            "Я изучил рынок и подготовил план статей.\n\n"
            "Настрой визуал:",
            reply_markup=kb_photo
        )

    except Exception as e:
        logger.exception(f"Spy Worker Error: {e}")
        TASK_STATUS[chat_id] = f"❌ Ошибка: {str(e)[:50]}"
        await bot.send_message(chat_id, f"⚠️ Произошла ошибка при анализе: {str(e)[:100]}. Попробуй снова.", reply_markup=get_menu())

async def worker_produce_article(chat_id: int, state: FSMContext, idea: dict, idea_id: int):
    start_time = time.time()
    TASK_STATUS[chat_id] = "✍️ Пишу статью..."
    
    try:
        strat_res = supabase.table("market_strategies").select("*").eq("user_id", chat_id).order("created_at", desc=True).limit(1).execute()
        context = ""
        if strat_res.data:
            s = strat_res.data[0]
            context = f"Тон: {s.get('tone_of_voice_guide')} | ЦА: {s.get('target_audience_profile')}"
            
        writer_prompt = f"""
Тема: {idea['topic_title']}
Боль: {idea['pain_point']}
Слабость конкурента: {idea['competitor_weakness']}
Контекст: {context}

Напиши статью для Дзен.
"""
        
        dzen_text = await agroq(writer_prompt, max_tokens=2500, system=WRITER_DZEN_SYSTEM, timeout=120)
        if not dzen_text: raise Exception("Text generation failed")
        
        titles_res = await agroq(f"Заголовки для:\n{dzen_text[:300]}...", max_tokens=200, system=TITLE_GEN_SYSTEM, timeout=30)
        vc_text = await agroq(f"Адаптируй под VC:\n{dzen_text}", max_tokens=2500, system=WRITER_VC_SYSTEM, timeout=120)
        teaser_tg = await agroq(f"Тизер для TG:\n{dzen_text}", max_tokens=300, system=TEASER_SYSTEM, timeout=30)
        
        image_url = "https://images.unsplash.com/photo-1518780664697-55e3ad937233?w=1200&h=630&fit=crop" 
        
        res_insert = supabase.table("published_articles").insert({
            "idea_id": idea_id,
            "user_id": chat_id,
            "dzen_content": dzen_text,
            "vc_ru_content": vc_text,
            "telegram_preview": teaser_tg,
            "max_preview": teaser_tg,
            "main_image_url": image_url,
            "title_clickbait": titles_res.split('\n')[0] if titles_res else "",
            "title_expert": titles_res.split('\n')[1] if titles_res and len(titles_res.split('\n'))>1 else "",
            "title_question": titles_res.split('\n')[2] if titles_res and len(titles_res.split('\n'))>2 else "",
            "publish_status": "ready",
            "version_status": "draft",
            "publication_date": str(datetime.now())
        }).execute()
        
        article_db_id = res_insert.data[0]['id']
        duration = int(time.time() - start_time)
        
        msg = f"✅ **СТАТЬЯ ГОТОВА!** ({duration} сек.)\n\n"
        msg += f"📄 **Начало текста:**\n{dzen_text[:300]}...\n\n"
        msg += f"🔖 **Заголовки:**\n{titles_res}\n\n"
        msg += f"📱 **Тизер:**\n{teaser_tg}\n\n"
        msg += "Что делать?"
        
        kb_art = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Править вручную", callback_data=f"edit_manual_{article_db_id}")],
            [InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"regen_{article_db_id}")],
            [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{article_db_id}")],
            [InlineKeyboardButton(text="📥 Скачать", callback_data="download_archive")]
        ])
        
        await send_long_bot(chat_id, msg, reply_markup=kb_art)
        TASK_STATUS[chat_id] = "Done"
        
    except Exception as e:
        logger.exception(f"Article Worker Error: {e}")
        TASK_STATUS[chat_id] = f"Error: {str(e)[:50]}"
        await bot.send_message(chat_id, f"⚠️ Ошибка генерации: {str(e)[:100]}", reply_markup=get_menu())

# =========================
# UI HELPERS
# =========================

def get_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="🚀 Новая статья")],
            [KeyboardButton(text="⏳ Где мой текст?"), KeyboardButton(text="❓ Помощь")]
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
        await asyncio.sleep(0.3)

# =========================
# HANDLERS
# =========================

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    register_user_safe(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    kb_choice = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 ЕСТЬ ССЫЛКИ (Автопилот)", callback_data="mode_pilot")],
        [InlineKeyboardButton(text=" 📝 НЕТ ССЫЛОК (Бриф)", callback_data="mode_interview")]
    ])
    
    await message.answer(
        "👋 Привет! Я твой автономный SEO-агент.\n\n"
        "Как начнем?",
        reply_markup=kb_choice
    )

@dp.callback_query(F.data == "mode_pilot")
async def cb_mode_pilot(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(OnboardingStates.pilot_links)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "🔗 Пришли ссылки (сайт, авито, вк, тг):\n"
        "(Через пробел или запятую)"
    )

@dp.callback_query(F.data == "mode_interview")
async def cb_mode_interview(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(interview_answers={}, step=1)
    await state.set_state(OnboardingStates.interview_q1)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("1. Что продаёшь и кому?")

# --- PILOT LOGIC ---

@dp.message(OnboardingStates.pilot_links)
async def pilot_get_links(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    urls = extract_urls(message.text)
    if not urls:
        await message.answer("Не вижу ссылок.")
        return

    input_data = {"links": urls, "description": message.text, "keyword_guess": ""}
    
    # Запускаем фон
    task = asyncio.create_task(worker_market_spy(uid, state, "pilot", input_data))
    ACTIVE_TASKS[uid] = task
    TASK_STATUS[uid] = "Started"

# --- INTERVIEW LOGIC ---

INTERVIEW_QS = {
    1: "1. Что продаёшь и кому?",
    2: "2. Кто идеальный клиент? Его страхи?",
    3: "3. Почему купить у тебя, а не у конкурента?",
    4: "4. Есть кейсы с цифрами?",
    5: "5. Как общаешься с клиентами?"
}

@dp.message(OnboardingStates.interview_q1)
async def iq1(m: types.Message, s: FSMContext):
    d = await s.get_data(); ans = d.get("interview_answers", {}); ans["q1"]=m.text
    await s.update_data(interview_answers=ans); await s.set_state(OnboardingStates.interview_q2)
    await m.answer(INTERVIEW_QS[2])

@dp.message(OnboardingStates.interview_q2)
async def iq2(m: types.Message, s: FSMContext):
    d = await s.get_data(); ans = d.get("interview_answers", {}); ans["q2"]=m.text
    await s.update_data(interview_answers=ans); await s.set_state(OnboardingStates.interview_q3)
    await m.answer(INTERVIEW_QS[3])

@dp.message(OnboardingStates.interview_q3)
async def iq3(m: types.Message, s: FSMContext):
    d = await s.get_data(); ans = d.get("interview_answers", {}); ans["q3"]=m.text
    await s.update_data(interview_answers=ans); await s.set_state(OnboardingStates.interview_q4)
    await m.answer(INTERVIEW_QS[4])

@dp.message(OnboardingStates.interview_q4)
async def iq4(m: types.Message, s: FSMContext):
    d = await s.get_data(); ans = d.get("interview_answers", {}); ans["q4"]=m.text
    await s.update_data(interview_answers=ans); await s.set_state(OnboardingStates.interview_q5)
    await m.answer(INTERVIEW_QS[5])

@dp.message(OnboardingStates.interview_q5)
async def iq5(m: types.Message, s: FSMContext):
    uid = m.from_user.id
    d = await s.get_data(); ans = d.get("interview_answers", {}); ans["q5"]=m.text
    full_desc = " ".join(ans.values())
    input_data = {"description": full_desc, "answers": ans, "keyword_guess": ans.get("q1","")[:50], "links":[]}
    
    task = asyncio.create_task(worker_market_spy(uid, s, "interview", input_data))
    ACTIVE_TASKS[uid] = task
    TASK_STATUS[uid] = "Started"

# --- PHOTO SETUP ---

@dp.callback_query(F.data == "set_disk_url")
async def cb_set_disk(cb: types.CallbackQuery, s: FSMContext):
    await cb.answer()
    await s.set_state(OnboardingStates.photo_setup)
    await cb.message.answer("Пришли ссылку на Яндекс.Диск:")

@dp.callback_query(F.data == "use_ai_gen")
async def cb_use_ai(cb: types.CallbackQuery, s: FSMContext):
    await cb.answer()
    uid = cb.from_user.id
    try:
        upd = supabase.table("user_settings").update({"use_generated_images": True}).eq("user_id", uid).execute()
        if not upd.data:
            supabase.table("user_settings").insert({"user_id": uid, "use_generated_images": True}).execute()
    except: pass
    await show_dashboard(cb.message, s, uid)

@dp.message(OnboardingStates.photo_setup)
async def handle_disk(m: types.Message, s: FSMContext):
    uid = m.from_user.id
    url = m.text.strip()
    if "disk.yandex.ru" not in url:
        await m.answer("Не та ссылка."); return
    try:
        upd = supabase.table("user_settings").update({"yandex_disk_folder_url": url, "use_generated_images": False}).eq("user_id", uid).execute()
        if not upd.data:
            supabase.table("user_settings").insert({"user_id": uid, "yandex_disk_folder_url": url, "use_generated_images": False}).execute()
    except: pass
    await show_dashboard(m, s, uid)

# --- DASHBOARD ---

async def show_dashboard(message: types.Message, state: FSMContext, uid: int):
    await state.set_state(OnboardingStates.dashboard)
    try:
        ideas_res = supabase.table("article_ideas_queue").select("*").eq("is_processed", False).order("relevance_score", desc=True).limit(1).execute()
        next_idea = ideas_res.data[0] if ideas_res.data else None
        
        today_str = str(date.today())
        arts_res = supabase.table("published_articles").select("*", count="exact").filter("publication_date", "gte", today_str).eq("user_id", uid).execute()
        count_today = arts_res.count if arts_res.count else 0
    except:
        next_idea = None; count_today = 0
        
    msg = f"📋 **МОИ ЗАДАЧИ**\n\n"
    msg += f"🔹 **Написано сегодня:** {count_today}/{DAILY_LIMIT}\n"
    if next_idea:
        msg += f"\n🚀 **Следующая тема:** \"{next_idea['topic_title']}\"\n"
    else:
        msg += "\n⚠️ Нет идей. Нажми /start для новой стратегии.\n"
        
    kb_dash = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Новая статья", callback_data="generate_next_article")],
        [InlineKeyboardButton(text="📥 Скачать архив", callback_data="download_archive")]
    ])
    await message.answer(msg, reply_markup=kb_dash, parse_mode="Markdown")

@dp.callback_query(F.data == "generate_next_article")
async def cb_generate(cb: types.CallbackQuery, s: FSMContext):
    await cb.answer()
    uid = cb.from_user.id
    
    # Лимит
    today_str = str(date.today())
    arts_res = supabase.table("published_articles").select("*", count="exact").filter("publication_date", "gte", today_str).eq("user_id", uid).execute()
    if arts_res.count >= DAILY_LIMIT:
        await cb.message.answer("⛔ Лимит исчерпан."); return
    
    # Взять идею
    ideas_res = supabase.table("article_ideas_queue").select("*").eq("is_processed", False).order("relevance_score", desc=True).limit(1).execute()
    if not ideas_res.data:
        await cb.message.answer("❌ Нет идей."); return
        
    idea = ideas_res.data[0]
    supabase.table("article_ideas_queue").update({"is_processed": True}).eq("id", idea['id']).execute()
    
    await cb.message.answer(f"✍️ Пишу: \"{idea['topic_title']}\"...")
    task = asyncio.create_task(worker_produce_article(uid, s, idea, idea['id']))
    ACTIVE_TASKS[uid] = task
    TASK_STATUS[uid] = "Writing Article"

# --- STATUS COMMAND (INSTANT RESPONSE) ---

@dp.message(Command("status"))
@dp.message(F.text == "⏳ Где мой текст?")
async def cmd_status(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    current_state = await state.get_state()
    task_status = TASK_STATUS.get(uid, "Idle")
    is_busy = uid in ACTIVE_TASKS and not ACTIVE_TASKS[uid].done()
    
    status_msg = f"🧠 **Процесс агента:**\n\n"
    status_msg += f"🔹 **FSM:** `{current_state or 'None'}`\n"
    status_msg += f"🔹 **Задача:** {'🟢 РАБОТАЕТ' if is_busy else '⚪ СВОБОДНА'}\n"
    status_msg += f"🔹 **Этап:** `{task_status}`\n"
    
    if is_busy:
        status_msg += "\n⏳ Жди завершения (до 5 мин)."
    else:
        status_msg += "\n✅ Агент свободен."
        
    await message.answer(status_msg, reply_markup=get_menu())

# --- EDITING & OTHERS ---

@dp.callback_query(F.data.startswith("edit_manual_"))
async def cb_edit_manual(cb: types.CallbackQuery, s: FSMContext):
    await cb.answer()
    aid = int(cb.data.split("_")[2])
    await s.update_data(editing_article_id=aid)
    await s.set_state(OnboardingStates.editing_article)
    await cb.message.answer("✏️ Пришли новый текст статьи:")

@dp.message(OnboardingStates.editing_article)
async def process_edit(m: types.Message, s: FSMContext):
    uid = m.from_user.id
    d = await s.get_data(); aid = d.get("editing_article_id"); new_txt = m.text.strip()
    if not aid: await m.answer("Ошибка контекста."); return
    
    try:
        old = supabase.table("published_articles").select("dzen_content").eq("id", aid).single().execute().data["dzen_content"]
        supabase.table("article_edit_history").insert({"article_id": aid, "user_id": uid, "change_type": "manual", "old_content_snippet": old[:200], "new_content_snippet": new_txt[:200]}).execute()
        supabase.table("published_articles").update({"dzen_content": new_txt, "version_status": "edited", "last_edit_time": str(datetime.now())}).eq("id", aid).execute()
        await m.answer("✅ Сохранено.", reply_markup=get_menu())
        await s.clear()
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

@dp.callback_query(F.data.startswith("regen_"))
async def cb_regen(cb: types.CallbackQuery, s: FSMContext):
    await cb.answer()
    aid = int(cb.data.split("_")[1]); uid = cb.from_user.id
    await cb.message.answer("🔄 Перегенерация...")
    
    art = supabase.table("published_articles").select("idea_id").eq("id", aid).single().execute().data
    iid = art["idea_id"]
    idea = supabase.table("article_ideas_queue").select("*").eq("id", iid).single().execute().data
    
    strat = supabase.table("market_strategies").select("*").eq("user_id", uid).order("created_at", desc=True).limit(1).execute().data[0]
    ctx = f"Тон: {strat.get('tone_of_voice_guide')}"
    
    wprompt = f"Тема: {idea['topic_title']}\nБоль: {idea['pain_point']}\nКонтекст: {ctx}\nНапиши заново."
    txt = await agroq(wprompt, max_tokens=2500, system=WRITER_DZEN_SYSTEM, timeout=120)
    
    if txt:
        supabase.table("published_articles").update({"dzen_content": txt, "version_status": "regenerated", "last_edit_time": str(datetime.now())}).eq("id", aid).execute()
        await cb.message.answer("✅ Обновлено!", reply_markup=get_menu())
        await send_long_bot(uid, f"📄 Новый вариант:\n{txt[:500]}...")
    else:
        await cb.message.answer("⚠️ Ошибка перегенерации.")

@dp.callback_query(F.data.startswith("accept_"))
async def cb_accept(cb: types.CallbackQuery, s: FSMContext):
    await cb.answer()
    aid = int(cb.data.split("_")[1])
    supabase.table("published_articles").update({"version_status": "final_approved"}).eq("id", aid).execute()
    await cb.message.answer("✅ Принято.", reply_markup=get_menu())

@dp.callback_query(F.data == "download_archive")
async def cb_download(cb: types.CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    res = supabase.table("published_articles").select("*").eq("user_id", uid).order("created_at", desc=True).limit(10).execute()
    if not res.data: await cb.message.answer("Нет статей."); return
    
    all_text = ""
    for a in res.data:
        all_text += f"\n{'='*30}\nID:{a['id']}\n[DZEN]\n{a['dzen_content']}\n"
    doc = BufferedInputFile(all_text.encode('utf-8'), filename="seo_pack.txt")
    await cb.message.answer_document(doc, caption="💾 Архив")

@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await m.answer("/start - Начать\n/status - Проверить процесс\n/help - Помощь", reply_markup=get_menu())

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
                    await msg.answer("⚠️ Системная ошибка. /start", reply_markup=get_menu())
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
    logger.info("🚀 Starting FINAL VERSION...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    await asyncio.sleep(5) # Пауза меньше, т.к. мы оптимизировали старт
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    asyncio.run(main())
