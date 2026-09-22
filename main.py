import logging
import os
import requests
import threading
import asyncio
import time
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from openai import OpenAI
from supabase import create_client, Client
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from typing import Callable, Dict, Any

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
    groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ All services initialized")
except Exception as e:
    logger.error(f"❌ INIT ERROR: {e}")
    raise

BANNED_NICHES = ["обнал", "отмыв", "адалт", "18+", "порн", "оружие", "наркот", "взлом", "хакер"]

# === ПРОМПТЫ ===
BASE_SYSTEM = """Ты — элитный SEO-стратег и контент-маркетолог с 15-летним опытом.
СТРОГИЕ ПРАВИЛА:
1. Пиши ТОЛЬКО на русском
2. НЕ используй ### или ##
3. Используй эмодзи для заголовков
4. Выделяй важное **жирным**
5. ОТВЕТ ДОЛЖЕН БЫТЬ ПОЛНЫМ
6. НИКОГДА не задавай вопросов если не просят"""

RECON_SYSTEM = BASE_SYSTEM + """
ЗАДАЧА: КРАТКАЯ разведка (максимум 800 слов). НЕ ЗАДАВАЙ ВОПРОСОВ."""

FINAL_ANALYSIS_SYSTEM = BASE_SYSTEM + """
ЗАДАЧА: ПОЛНЫЙ глубокий анализ. НЕ ЗАДАВАЙ ВОПРОСОВ."""

QUESTION_SYSTEM = BASE_SYSTEM + """
ЗАДАЧА: ОДИН вопрос для интервью.
- Только ОДИН вопрос, открытый
- Без нумерации, без эмодзи
- Максимум 150 слов"""

# === ГЛАВНОЕ МЕНЮ ===
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Продолжить"), KeyboardButton(text="📝 Новая статья")],
            [KeyboardButton(text="🔄 Начать заново"), KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Нажми кнопку или напиши сообщение..."
    )

MENU_CONTINUE = "▶️ Продолжить"
MENU_ARTICLE = "📝 Новая статья"
MENU_RESTART = "🔄 Начать заново"
MENU_HELP = "❓ Помощь"

class Onboarding(StatesGroup):
    gathering = State()
    service_priority = State()
    source_link = State()
    waiting_source_parsing = State()
    photos = State()
    cta_choice = State()
    cta_value = State()

class ErrorHandlerMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable, event: types.Update, data: Dict[str, Any]) -> Any:
        try:
            return await handler(event, data)
        except Exception as e:
            logger.error(f"❌ MIDDLEWARE ERROR: {type(e).__name__}: {e}", exc_info=True)
            try:
                msg = None
                if hasattr(event, 'message') and event.message:
                    msg = event.message
                elif hasattr(event, 'callback_query') and event.callback_query:
                    msg = event.callback_query.message
                if msg:
                    await msg.answer(
                        f"⚠️ Что-то пошло не так. Нажми **▶️ Продолжить** в меню.",
                        parse_mode="Markdown",
                        reply_markup=get_main_menu()
                    )
            except Exception:
                pass
            return None

dp.message.middleware(ErrorHandlerMiddleware())
dp.callback_query.middleware(ErrorHandlerMiddleware())

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

def heartbeat():
    while True:
        time.sleep(60)
        logger.info("💓 HEARTBEAT: alive")

# === SCRAPERAPI ===
def scrape_with_api(url, premium=False, render=False, country_code="ru"):
    if not SCRAPER_API_KEY:
        return None
    params = {"api_key": SCRAPER_API_KEY, "url": url, "country_code": country_code}
    if premium: params["premium"] = "true"
    if render: params["render"] = "true"
    try:
        logger.info(f"🌐 Scraping {url[:60]}...")
        r = requests.get("http://api.scraperapi.com", params=params, timeout=90)
        if r.status_code == 200 and len(r.text) > 200:
            if "captcha" in r.text.lower() and len(r.text) < 1000:
                return None
            return r.text
        return None
    except Exception as e:
        logger.error(f"❌ Scrape error: {e}")
        return None

def scrape_google_cache(url):
    try:
        r = requests.get(f"https://webcache.googleusercontent.com/search?q=cache:{url}", headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        return r.text if r.status_code == 200 and len(r.text) > 500 else None
    except Exception:
        return None

def scrape_web_archive(url):
    try:
        r = requests.get(f"https://web.archive.org/web/2024/{url}", headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        return r.text if r.status_code == 200 and len(r.text) > 500 else None
    except Exception:
        return None

def extract_urls(text):
    return re.findall(r'https?://[^\s,;]+', text)

def detect_source_type(url):
    u = url.lower()
    if "avito.ru" in u: return "avito"
    elif "vk.com" in u or "vk.ru" in u: return "vk"
    elif "t.me/" in u: return "telegram"
    else: return "site"

def parse_source(url):
    st = detect_source_type(url)
    if st == "avito": return extract_avito(url), "Авито"
    elif st == "vk": return extract_vk_group(url), "ВКонтакте"
    elif st == "telegram": return extract_telegram_channel(url), "Telegram"
    else: return extract_site_text(url), "Сайт"

def extract_site_text(url):
    try:
        if not url.startswith("http"): url = "https://" + url
        html = scrape_with_api(url, premium=False, render=False)
        if not html: html = scrape_google_cache(url)
        if not html: html = scrape_web_archive(url)
        if not html: return None
        soup = BeautifulSoup(html, "lxml")
        for s in soup(["script", "style", "nav", "footer", "header", "noscript"]): s.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:5000] if len(text) > 100 else None
    except Exception as e:
        logger.error(f"❌ Site: {e}")
        return None

def extract_telegram_channel(url):
    try:
        ch = url.replace("https://t.me/", "").replace("t.me/", "").strip("/").split("/")[0]
        html = scrape_with_api(f"https://t.me/s/{ch}", premium=False, render=False)
        if not html: html = scrape_google_cache(f"https://t.me/s/{ch}")
        if not html: return None
        soup = BeautifulSoup(html, "lxml")
        posts = [p.get_text(strip=True) for p in soup.find_all("div", class_="tgme_widget_message_text") if p.get_text(strip=True)]
        return "\n\n".join(posts[:10])[:5000] if posts else None
    except Exception:
        return None

def extract_avito(url):
    try:
        if not url.startswith("http"): url = "https://" + url
        variants = [
            (url, True, True),
            (url.replace("www.avito.ru", "m.avito.ru"), True, True),
            (url, True, False),
            (url, False, False),
        ]
        for v_url, prem, rend in variants:
            html = scrape_with_api(v_url, premium=prem, render=rend)
            if not html: continue
            soup = BeautifulSoup(html, "lxml")
            parts = []
            for tag in soup.find_all(["h1", "h2", "h3"]):
                t = tag.get_text(strip=True)
                if t and len(t) > 5: parts.append(t)
            for tag in soup.find_all(["div", "p", "span"]):
                t = tag.get_text(strip=True)
                if len(t) > 20 and any(w in t.lower() for w in ["руб", "₽", "дом", "м²", "участок"]):
                    if t not in parts: parts.append(t)
            result = "\n".join(parts)
            if len(result) > 200: return result[:5000]
            for s in soup(["script", "style"]): s.decompose()
            full = soup.get_text(separator="\n", strip=True)
            if len(full) > 500: return full[:5000]
        html = scrape_google_cache(url)
        if html:
            soup = BeautifulSoup(html, "lxml")
            for s in soup(["script", "style"]): s.decompose()
            text = soup.get_text(separator="\n", strip=True)
            if len(text) > 500: return text[:5000]
        return None
    except Exception:
        return None

def extract_vk_group(url):
    try:
        vk = url.replace("https://vk.com/", "").replace("https://vk.ru/", "")
        vk = vk.replace("vk.com/", "").replace("vk.ru/", "").strip("/").split("/")[0]
        variants = [
            (f"https://m.vk.com/{vk}", True, True),
            (f"https://vk.com/{vk}", True, True),
            (f"https://m.vk.com/{vk}", True, False),
            (f"https://m.vk.com/{vk}?act=info", True, False),
            (f"https://vk.com/{vk}", False, False),
        ]
        for v_url, prem, rend in variants:
            html = scrape_with_api(v_url, premium=prem, render=rend)
            if not html: continue
            soup = BeautifulSoup(html, "lxml")
            posts = []
            for p in soup.find_all(["div", "p"]):
                t = p.get_text(strip=True)
                if 40 < len(t) < 2000 and not any(s in t.lower() for s in ["cookie", "войти", "зарегистрироваться"]):
                    posts.append(t)
            unique = list(dict.fromkeys(posts))[:15]
            desc = ""
            for d in soup.find_all(["div", "p"], class_=["group_info", "page_info", "group_description", "info"]):
                desc += d.get_text(strip=True) + "\n"
            if unique or desc:
                result = ""
                if desc: result += "ОПИСАНИЕ:\n" + desc + "\n\n"
                if unique: result += "ПОСТЫ:\n" + "\n---\n".join(unique[:10])
                if len(result) > 200: return result[:5000]
            for s in soup(["script", "style"]): s.decompose()
            full = soup.get_text(separator="\n", strip=True)
            if len(full) > 500: return full[:5000]
        for fallback_url in [f"https://vk.com/{vk}"]:
            html = scrape_google_cache(fallback_url)
            if html:
                soup = BeautifulSoup(html, "lxml")
                for s in soup(["script", "style"]): s.decompose()
                full = soup.get_text(separator="\n", strip=True)
                if len(full) > 500: return full[:5000]
            html = scrape_web_archive(fallback_url)
            if html:
                soup = BeautifulSoup(html, "lxml")
                for s in soup(["script", "style"]): s.decompose()
                full = soup.get_text(separator="\n", strip=True)
                if len(full) > 500: return full[:5000]
        return None
    except Exception:
        return None

def search_competitors(q, max_results=5):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{q} цены отзывы", region="ru-ru", max_results=max_results))
            return [{"title": r.get("title", ""), "snippet": r.get("body", "")[:200]} for r in results]
    except Exception:
        return []

def get_yandex_suggestions(kw):
    try:
        r = requests.get("https://suggest.yandex.net/suggest-ff.cgi", params={"part": kw, "lang": "ru", "v": "3"}, timeout=10)
        data = r.json()
        return data[1] if isinstance(data, list) and len(data) > 1 else []
    except Exception:
        return []

# === SUPABASE ===
def get_or_create_user(uid, username, fname):
    try:
        result = supabase.table("users").select("*").eq("id", uid).execute()
        if result.data: return result.data[0]
        u = {"id": uid, "username": username or "", "first_name": fname or ""}
        supabase.table("users").insert(u).execute()
        return u
    except Exception:
        return {"id": uid}

def save_answer(uid, q, a):
    try:
        supabase.table("onboarding_answers").insert({"user_id": uid, "question": q, "answer": str(a)[:1500]}).execute()
    except Exception:
        pass

def save_research(uid, kw, pains, utp, comp):
    try:
        supabase.table("niche_research").insert({"user_id": uid, "keywords": str(kw)[:8000], "pains": str(pains)[:5000], "utp": str(utp)[:2000], "competitors": str(comp)[:3000]}).execute()
    except Exception:
        pass

def save_article(uid, kw, pain, title, content):
    try:
        supabase.table("articles").insert({"user_id": uid, "keyword": str(kw)[:500], "pain": str(pain)[:500], "title": str(title)[:500], "content": str(content)[:15000], "status": "draft"}).execute()
    except Exception:
        pass

def log_usage(uid, action):
    try:
        supabase.table("usage_logs").insert({"user_id": uid, "action": action}).execute()
    except Exception:
        pass

# === QWEN ===
def ask_qwen(prompt, max_tokens=1200, system=None):
    sys_msg = system or BASE_SYSTEM
    for attempt in range(3):
        try:
            r = groq_client.chat.completions.create(
                model="qwen/qwen3-32b",
                messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt[:15000]}],
                max_tokens=max_tokens, temperature=0.7, timeout=120
            )
            result = r.choices[0].message.content
            if r.choices[0].finish_reason == "length":
                result += "\n\n[Ответ обрезан]"
            return result
        except Exception as e:
            logger.error(f"❌ Qwen fail {attempt+1}: {e}")
            if attempt < 2: time.sleep(2 ** attempt)
    return "⚠️ Нейросеть недоступна. Нажми ▶️ Продолжить."

async def send_long(message, text):
    MAX = 4000
    try:
        if len(text) <= MAX:
            try: await message.answer(text, parse_mode="Markdown")
            except Exception: await message.answer(text)
        else:
            for part in [text[i:i+MAX] for i in range(0, len(text), MAX)]:
                try: await message.answer(part, parse_mode="Markdown")
                except Exception: await message.answer(part)
                await asyncio.sleep(0.5)
        return True
    except Exception as e:
        logger.error(f"❌ Send: {e}")
        return False

def is_banned(text):
    return any(w in text.lower() for w in BANNED_NICHES)

# === ГЛАВНЫЕ КОМАНДЫ ===
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    logger.info(f"🚀 /start from {message.from_user.id}")
    await state.clear()
    get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "👋 **Привет! Я — твой SEO-стратег.**\n\n"
        "Проведу интервью, изучу нишу и буду писать статьи.\n"
        "⏱ 5-7 минут. 📊 Всего 8 вопросов.\n\n"
        "❓ **Вопрос 1 из 8:**\nЧто продаёшь? Опиши в 2-3 предложениях.\n\n"
        "💡 Используй кнопки в меню внизу.",
        parse_mode="Markdown",
        reply_markup=get_main_menu()
    )
    await state.set_state(Onboarding.gathering)
    await state.update_data(question_num=1, history=[], source_requested=False)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📋 **Что я умею:**\n\n"
        "• **▶️ Продолжить** — продолжить с последнего места\n"
        "• **📝 Новая статья** — сгенерировать статью\n"
        "• **🔄 Начать заново** — начать с нуля\n"
        "• **❓ Помощь** — эта справка\n"
        "• **/ping** — проверить что бот жив\n\n"
        "💡 Просто нажимай кнопки в меню!",
        parse_mode="Markdown",
        reply_markup=get_main_menu()
    )

@dp.message(F.text == MENU_RESTART)
async def btn_restart_kb(message: types.Message, state: FSMContext):
    logger.info("🔄 Restart via menu")
    await cmd_start(message, state)

@dp.message(F.text == MENU_HELP)
async def btn_help_kb(message: types.Message):
    await cmd_help(message)

@dp.message(F.text == MENU_CONTINUE)
async def btn_continue_kb(message: types.Message, state: FSMContext):
    logger.info("▶️ Continue via menu")
    await cmd_continue_logic(message, state)

@dp.message(F.text == MENU_ARTICLE)
async def btn_article_kb(message: types.Message, state: FSMContext):
    logger.info("📝 Article via menu")
    await gen_article_logic(message)

@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    await message.answer(f"✅ Бот жив! {int(time.time())}", reply_markup=get_main_menu())

# === ЛОГИКА /continue ===
async def cmd_continue_logic(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    data = await state.get_data()
    qn = data.get("question_num", 1)
    history = data.get("history", [])
    
    logger.info(f"📊 Continue: state={current_state}, qn={qn}, hist={len(history)}")
    
    if not current_state or (qn == 1 and not history):
        await message.answer(
            "🤔 У тебя нет активного диалога.\nНажми **🔄 Начать заново**.",
            parse_mode="Markdown",
            reply_markup=get_main_menu()
        )
        return
    
    if current_state == "Onboarding:gathering":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Да, продолжить", callback_data="continue_action")],
            [InlineKeyboardButton(text="🔄 Начать заново", callback_data="restart_action")]
        ])
        await message.answer(
            f"👋 **С возвращением!**\n\n"
            f"📊 Мы на **вопросе {qn} из 8**.\n"
            f"Уже ответили на {len(history)} вопросов.\n\nПродолжаем?",
            reply_markup=kb, parse_mode="Markdown"
        )
    elif current_state == "Onboarding:source_link":
        await message.answer(
            "🔗 Мы ждали ссылки на источники.\nПришли их или напиши **«нет»**.",
            parse_mode="Markdown"
        )
    elif current_state == "Onboarding:waiting_source_parsing":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить парсинг", callback_data="retry_parsing")]
        ])
        await message.answer("⏳ Мы изучали источники. Повторить?", reply_markup=kb, parse_mode="Markdown")
    elif current_state == "Onboarding:photos":
        kb = ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📸 Фото")], [KeyboardButton(text="⏭ Пропустить")]
        ], resize_keyboard=True)
        await message.answer("📸 Мы загружали фото.", reply_markup=kb, parse_mode="Markdown")
    elif current_state == "Onboarding:cta_choice":
        await message.answer("📍 Выбирали куда вести заявки.", parse_mode="Markdown")
        await ask_cta(message, state)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить", callback_data="continue_action")],
            [InlineKeyboardButton(text="🔄 Начать заново", callback_data="restart_action")]
        ])
        await message.answer("🤔 Не могу определить где мы.", reply_markup=kb, parse_mode="Markdown")

@dp.message(Command("continue"))
async def cmd_continue(message: types.Message, state: FSMContext):
    await cmd_continue_logic(message, state)

@dp.callback_query(F.data == "continue_action")
async def cb_continue_action(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    qn = data.get("question_num", 1)
    history = data.get("history", [])
    if qn >= 8:
        await finish_interview(callback.message, state, history)
    else:
        await ask_next_interview_question(callback.message, state)

@dp.callback_query(F.data == "restart_action")
async def cb_restart_action(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await cmd_start(callback.message, state)

@dp.callback_query(F.data == "retry_parsing")
async def cb_retry_parsing(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Onboarding.source_link)
    await state.update_data(waiting_manual=False)
    await callback.message.answer(
        "🔗 Пришли ссылки ещё раз. Или напиши **«нет»**.",
        parse_mode="Markdown"
    )

# === ГЕНЕРАЦИЯ ВОПРОСА ===
async def ask_next_interview_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    history = data.get("history", [])
    qn = data.get("question_num", 1)
    pri = data.get("priority_service", "")
    
    if qn >= 8:
        await finish_interview(message, state, history)
        return
    
    hist = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    pq = f"""Ты задал {qn} вопросов. История:
{hist}
"""
    if pri: pq += f"\nПриоритет: {pri}\n"
    pq += "\nЗадай ОДИН следующий вопрос. ТОЛЬКО вопрос."
    
    nq = ask_qwen(pq, max_tokens=150, system=QUESTION_SYSTEM)
    
    if not nq or nq.startswith("⚠️"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="continue_action")]
        ])
        await message.answer("⚠️ Не удалось сгенерировать вопрос.", reply_markup=kb, reply_markup_fallback=get_main_menu())
        return
    
    nq = nq.strip()
    for prefix in ["Вопрос:", "Q:", "q:", "В:", "**", "❓", "?\n", "\n"]:
        nq = nq.replace(prefix, "").strip()
    
    await state.update_data(question_num=qn + 1)
    
    await message.answer(
        f"📊 **Вопрос {qn} из 8**\n\n❓ {nq}\n\n"
        "💡 Не знаешь — напиши **«не знаю»** или **«пропустить»**.",
        parse_mode="Markdown"
    )

# === ИНТЕРВЬЮ ===
@dp.message(Onboarding.gathering)
async def live_interview(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    
    if message.text.strip().lower() in ["не знаю", "пропустить", "незнаю", "пропусти", "дальше", "skip"]:
        await message.answer("👌 Пропускаю.", parse_mode="Markdown")
        await ask_next_interview_question(message, state)
        return
    
    data = await state.get_data()
    history = data.get("history", [])
    qn = data.get("question_num", 1)
    src_req = data.get("source_requested", False)

    if qn == 1 and is_banned(message.text):
        await message.answer("❌ Не работаю с этой темой.", reply_markup=get_main_menu())
        await state.clear()
        return

    history.append({"q": qn, "a": message.text})
    save_answer(uid, f"Q{qn}", message.text)

    if qn == 1:
        check = ask_qwen(f'Клиент: "{message.text}"\nНесколько РАЗНЫХ услуг? Ответь "YES|у1|у2" или "NO"', max_tokens=100, system=QUESTION_SYSTEM)
        if check.startswith("YES|"):
            services = [s.strip() for s in check.split("|")[1:] if s.strip()]
            if services:
                await state.update_data(history=history, question_num=2, multiple_services=services)
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=s, callback_data=f"svc_{i}")] for i, s in enumerate(services)])
                await message.answer(
                    "🎯 **Вижу несколько направлений:**\n" + "\n".join([f"• {s}" for s in services]) +
                    "\n\nПо какой делаем анализ первым?",
                    reply_markup=kb, parse_mode="Markdown"
                )
                await state.set_state(Onboarding.service_priority)
                return

    if qn >= 4 and not src_req:
        await state.update_data(history=history, source_requested=True)
        await message.answer(
            "✅ **Уже многое понял!**\n\n"
            "🔗 Пришли ссылки на источники (можно несколько):\n"
            "• 🌐 Сайт\n• ✈️ Telegram-канал\n• 📱 Авито\n• 💬 ВКонтакте\n\n"
            "Изучу каждый (30-90 сек). Нет — напиши **«нет»**.",
            parse_mode="Markdown"
        )
        await state.set_state(Onboarding.source_link)
        await state.update_data(waiting_manual=False)
        return

    await ask_next_interview_question(message, state)

@dp.callback_query(F.data.startswith("svc_"))
async def service_chosen(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.replace("svc_", ""))
    data = await state.get_data()
    services = data.get("multiple_services", [])
    history = data.get("history", [])
    pri = services[idx].strip() if idx < len(services) else ""
    save_answer(callback.from_user.id, "priority", pri)
    await callback.message.answer(f"✅ **Акцент на:** {pri}", parse_mode="Markdown")
    await state.update_data(priority_service=pri, history=history, question_num=2)
    await state.set_state(Onboarding.gathering)
    await ask_next_interview_question(callback.message, state)
    await callback.answer()

# === ИСТОЧНИКИ ===
@dp.message(Onboarding.source_link)
async def get_source(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    history = data.get("history", [])
    waiting_manual = data.get("waiting_manual", False)

    if waiting_manual:
        await message.answer("⏳ Изучаю текст (30-60 сек)...")
        src = message.text
        save_answer(uid, "source_data", src[:2000])
        ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
        pri = data.get("priority_service", "")
        analysis = ask_qwen(f"""Клиент прислал: {src}
Интервью: {ht}
Приоритет: {pri or 'не указан'}
КРАТКАЯ разведка (800 слов):
📌 Сильные стороны (3 пункта)
📌 Слабые места (2-3 пункта)
📌 Стиль общения
📌 Идеи для статей (2-3)
НЕ ЗАДАВАЙ ВОПРОСОВ.""", max_tokens=1500, system=RECON_SYSTEM)
        if not analysis.startswith("⚠️"):
            await send_long(message, f"📊 **Разведка:**\n{analysis}")
        await state.update_data(history=history, source_requested=True, source_data=src[:2000], waiting_manual=False)
        await state.set_state(Onboarding.gathering)
        await ask_next_interview_question(message, state)
        return

    answer = message.text.strip()
    save_answer(uid, "source_link", answer)

    if answer.lower() in ["нет", "нету", "-", "0", "нет источника"]:
        await message.answer("👌 Работаем без источника.", parse_mode="Markdown")
        await state.update_data(source_requested=True, source_data=None, waiting_manual=False)
        await state.set_state(Onboarding.gathering)
        await ask_next_interview_question(message, state)
        return

    urls = extract_urls(answer)
    if not urls:
        if answer.isdigit(): urls = [f"https://www.avito.ru/user/{answer}/shop"]
        else: urls = [answer]

    await state.set_state(Onboarding.waiting_source_parsing)
    await state.update_data(urls_to_parse=urls)
    await message.answer(
        f"⏳ **Нашёл {len(urls)} источник(ов). Изучаю...** (30-90 сек)\n"
        f"💡 Если долго — нажми **▶️ Продолжить**.",
        parse_mode="Markdown"
    )

    all_data = ""
    results = []
    for url in urls:
        src_text, src_type = parse_source(url)
        if src_text:
            results.append(f"✅ **{src_type}** — изучен!")
            all_data += f"\n=== {src_type}: {url} ===\n{src_text}\n"
        else:
            results.append(f"⚠️ **{src_type}** ({url}) — не открылся")

    results_text = "\n".join(results)

    if all_data:
        save_answer(uid, "source_data", all_data[:3000])
        ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
        pri = data.get("priority_service", "")
        analysis = ask_qwen(f"""Данные источников:
{all_data[:6000]}
Интервью: {ht}
Приоритет: {pri or 'не указан'}
КРАТКАЯ разведка (800 слов):
📌 Сильные стороны (3)
📌 Слабые места (2-3)
📌 Стиль общения
📌 Идеи для статей (2-3)
НЕ ЗАДАВАЙ ВОПРОСОВ.""", max_tokens=1500, system=RECON_SYSTEM)
        if not analysis.startswith("⚠️"):
            await send_long(message, f"📊 **Результаты:**\n{results_text}\n\n**Разведка:**\n{analysis}")
        else:
            await send_long(message, f"📊 **Результаты:**\n{results_text}\n\n{analysis}")
        await state.update_data(history=history, source_requested=True, source_data=all_data[:3000], waiting_manual=False)
        await state.set_state(Onboarding.gathering)
        await message.answer("✅ Готово! Продолжаем.", parse_mode="Markdown")
        await ask_next_interview_question(message, state)
    else:
        await message.answer(
            f"📊 **Результаты:**\n{results_text}\n\n"
            f"Не смог открыть. Пришли **текстом**: описание, цены.",
            parse_mode="Markdown"
        )
        await state.update_data(waiting_manual=True)
        await state.set_state(Onboarding.source_link)

# === ЗАВЕРШЕНИЕ ===
async def finish_interview(message, state, history):
    uid = message.from_user.id
    data = await state.get_data()
    pri = data.get("priority_service", "")
    src = data.get("source_data", "")
    await message.answer(
        "✅ **Интервью завершено!**\n⏳ Делаю **полный анализ** (2-3 мин).\n"
        "💡 Если долго — нажми **▶️ Продолжить**.",
        parse_mode="Markdown"
    )

    ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    nq = pri if pri else (history[0]["a"] if history else "")
    competitors = search_competitors(nq[:80])
    suggestions = get_yandex_suggestions(nq[:30])
    comp_text = "\n".join([f"- {c['title']}: {c['snippet']}" for c in competitors[:5]]) if competitors else "нет"
    sugg_text = ", ".join(suggestions[:10]) if suggestions else "пусто"
    src_sec = f"\n=== ИСТОЧНИКИ ===\n{src}\n" if src else ""

    analysis = ask_qwen(f"""Интервью: {ht}{src_sec}
Приоритет: {pri or 'нет'}
Конкуренты: {comp_text}
Яндекс: {sugg_text}
ПОЛНЫЙ АНАЛИЗ:
🎯 КЛЮЧИ (15)
💥 БОЛИ (10)
⭐ УТП (3-4 предложения)
⚠️ МИНУСЫ (2-3)
НЕ ЗАДАВАЙ ВОПРОСОВ.""", max_tokens=2500, system=FINAL_ANALYSIS_SYSTEM)

    if analysis.startswith("⚠️"):
        await message.answer(f"⚠️ {analysis}", reply_markup=get_main_menu())
        return

    plan = ask_qwen(f"""Анализ: {analysis[:3000]}
Контент-план на 7 дней. 1 статья = 1 ключ + 1 боль.
📅 ПЛАН НА 7 ДНЕЙ
НЕ ЗАДАВАЙ ВОПРОСОВ.""", max_tokens=1500, system=FINAL_ANALYSIS_SYSTEM)

    guide = ask_qwen("""Инструкция: публикация в Дзен. 5 шагов.
📚 ПУБЛИКАЦИЯ В ДЗЕН
НЕ ЗАДАВАЙ ВОПРОСОВ.""", max_tokens=1200, system=FINAL_ANALYSIS_SYSTEM)

    save_research(uid, analysis + "\n\n" + plan, "", "", comp_text)

    await send_long(message, f"🎯 **ПОЛНЫЙ АНАЛИЗ:**\n{analysis}")
    await asyncio.sleep(1.5)
    if not plan.startswith("⚠️"):
        await send_long(message, f"📅 **ПЛАН:**\n{plan}")
    await asyncio.sleep(1.5)
    if not guide.startswith("⚠️"):
        await send_long(message, f"📚 **ДЗЕН:**\n{guide}")
    await asyncio.sleep(1)

    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📸 Фото")], [KeyboardButton(text="⏭ Пропустить")]], resize_keyboard=True)
    await message.answer("📸 **Загрузить фото работ?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(Onboarding.photos)

# === ФОТО ===
@dp.message(Onboarding.photos, F.text.in_(["⏭ Пропустить", "Позже", "позже", "нет", "Нет"]))
async def skip_photos(message, state: FSMContext):
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.text == "📸 Фото")
async def req_photos(message, state: FSMContext):
    await message.answer("📸 Пришли фото. Закончишь — **«готово»**.", parse_mode="Markdown")

@dp.message(Onboarding.photos, F.text.lower() == "готово")
async def photos_done(message, state: FSMContext):
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.photo)
async def recv_photo(message, state: FSMContext):
    await message.answer("📸 Принял. Ещё или **«готово»**.", parse_mode="Markdown")

# === CTA ===
async def ask_cta(message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Сайт", callback_data="cta_site"), InlineKeyboardButton(text="✈️ TG", callback_data="cta_tg")],
        [InlineKeyboardButton(text="📱 WA", callback_data="cta_wa"), InlineKeyboardButton(text="📣 МАХ", callback_data="cta_max")],
        [InlineKeyboardButton(text="📞 Телефон", callback_data="cta_phone")]
    ])
    await message.answer("📍 **Куда вести заявки?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(Onboarding.cta_choice)

@dp.callback_query(F.data.startswith("cta_"))
async def cta_chosen(callback, state: FSMContext):
    ct = callback.data.replace("cta_", "")
    try: supabase.table("users").update({"cta_type": ct}).eq("id", callback.from_user.id).execute()
    except Exception: pass
    prompts = {"site": "🌐 Ссылка на форму", "tg": "✈️ @username", "wa": "📱 Номер +7...", "max": "📣 Ссылка МАХ", "phone": "📞 Номер"}
    await state.update_data(cta_type=ct)
    await state.set_state(Onboarding.cta_value)
    await callback.message.answer(f"✅ {ct}\n{prompts.get(ct, '')}", parse_mode="Markdown")
    await callback.answer()

@dp.message(Onboarding.cta_value)
async def cta_value(message, state: FSMContext):
    try: supabase.table("users").update({"cta_value": message.text.strip()}).eq("id", message.from_user.id).execute()
    except Exception: pass
    await message.answer(
        "🎉 **Готово!**\nНажми **📝 Новая статья** чтобы получить первую статью.",
        parse_mode="Markdown",
        reply_markup=get_main_menu()
    )
    await state.clear()

# === СТАТЬЯ ===
async def gen_article_logic(message):
    uid = message.from_user.id
    try:
        r = supabase.table("niche_research").select("*").eq("user_id", uid).order("id", desc=True).limit(1).execute()
        if not r.data:
            await message.answer("Сначала пройди онбординг: нажми **🔄 Начать заново**", parse_mode="Markdown", reply_markup=get_main_menu())
            return
        analysis = r.data[0]["keywords"]
    except Exception:
        await message.answer("Сначала пройди онбординг.", reply_markup=get_main_menu())
        return

    await message.answer(
        "✍️ **Пишу статью...** 1-2 мин.\n💡 Если долго — нажми **▶️ Продолжить**.",
        parse_mode="Markdown"
    )
    article = ask_qwen(f"""Анализ: {analysis}
SEO-статья: 1 ключ + 1 боль. Заголовок, вступление с болью, подзаголовки, 5-7 тыс знаков, мягкий призыв.
📄 СТАТЬЯ
🏷 SEO (Title, Description, хэштеги, Slug)
НЕ ЗАДАВАЙ ВОПРОСОВ.""", max_tokens=2500, system=FINAL_ANALYSIS_SYSTEM)

    if article.startswith("⚠️"):
        await message.answer(f"⚠️ {article}", reply_markup=get_main_menu())
        return
    save_article(uid, "статья", "боль", "статья", article)
    log_usage(uid, "article")
    await send_long(message, f"📄 **СТАТЬЯ:**\n{article}")
    await asyncio.sleep(1)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ vc.ru", callback_data="vc_yes"), InlineKeyboardButton(text="❌ Только Дзен", callback_data="vc_no")]
    ])
    await message.answer("📰 **Адаптировать под vc.ru?**", reply_markup=kb, parse_mode="Markdown")

@dp.message(Command("article"))
async def gen_article(message):
    await gen_article_logic(message)

@dp.callback_query(F.data == "vc_yes")
async def adapt_vc(callback):
    await callback.message.answer("⏳ Адаптирую...")
    try:
        a = supabase.table("articles").select("*").eq("user_id", callback.from_user.id).order("id", desc=True).limit(1).execute()
        if not a.data: return
        orig = a.data[0]["content"]
        aid = a.data[0]["id"]
    except Exception: return
    vc = ask_qwen(f"Адаптируй под vc.ru: {orig}\nКейс/факап. Тон: коллега. 7-12 тыс. НЕ ЗАДАВАЙ ВОПРОСОВ.", max_tokens=2500, system=FINAL_ANALYSIS_SYSTEM)
    try: supabase.table("articles").update({"vc_version": vc}).eq("id", aid).execute()
    except Exception: pass
    if not vc.startswith("⚠️"):
        await send_long(callback.message, f"📰 **vc.ru:**\n{vc}")
    await callback.answer()

@dp.callback_query(F.data == "vc_no")
async def skip_vc(callback):
    await callback.message.answer("👌 Только Дзен.", reply_markup=get_main_menu())
    await callback.answer()

# === ADMIN ===
@dp.message(Command("admin"))
async def cmd_admin(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        u = supabase.table("users").select("id").execute()
        a = supabase.table("articles").select("id").execute()
        await message.answer(f"📊 Клиентов: {len(u.data)} | Статей: {len(a.data)}")
    except Exception as e:
        await message.answer(f"⚠️ {e}")

# === ЗАПУСК ===
async def main():
    logger.info("🚀 Starting bot...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    logger.info("✅ Threads started, running main...")
    asyncio.run(main())
