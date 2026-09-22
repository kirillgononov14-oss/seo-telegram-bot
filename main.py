import logging
import os
import requests
import threading
import asyncio
import time
import re
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
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
from typing import Callable, Dict, Any

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
ADMIN_ID = 1847007101

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Проверка переменных при старте
for var_name, var_value in [("BOT_TOKEN", BOT_TOKEN), ("GROQ_API_KEY", GROQ_API_KEY), ("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_KEY", SUPABASE_KEY)]:
    if not var_value:
        logger.error(f"❌ MISSING ENV VAR: {var_name}")
    else:
        logger.info(f"✅ {var_name} is set ({len(var_value)} chars)")

try:
    bot = Bot(token=BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Bot and Supabase initialized")
except Exception as e:
    logger.error(f"❌ INIT ERROR: {e}")
    raise

BANNED_NICHES = ["обнал", "отмыв", "адалт", "18+", "порн", "оружие", "наркот", "взлом", "хакер"]

# === ПРЯМЫЕ ЗАПРОСЫ К GROQ (без библиотеки!) ===
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODELS = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "qwen/qwen3-32b",
    "gemma2-9b-it",
    "llama3-8b-8192",
]

def groq_request(prompt, max_tokens=1200, system=None):
    """Прямой запрос к Groq через requests. Надёжнее чем библиотека."""
    sys_msg = system or "Ты — полезный ассистент. Отвечай на русском."
    
    for model in MODELS:
        try:
            logger.info(f"🤖 Groq: trying {model}...")
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": prompt[:15000]}
                ],
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }
            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=120)
            
            if r.status_code == 200:
                data = r.json()
                result = data["choices"][0]["message"]["content"]
                if result and len(result.strip()) > 0:
                    logger.info(f"✅ Groq OK ({model}): {len(result)} chars")
                    return result
                else:
                    logger.warning(f"⚠️ Groq empty response ({model})")
            elif r.status_code == 429:
                logger.warning(f"⚠️ Rate limit ({model}), waiting 5 sec...")
                time.sleep(5)
                continue
            else:
                logger.error(f"❌ Groq {r.status_code} ({model}): {r.text[:200]}")
        except requests.exceptions.Timeout:
            logger.error(f"⏱️ Timeout ({model})")
            continue
        except Exception as e:
            logger.error(f"❌ Groq error ({model}): {type(e).__name__}: {e}")
            continue
    
    logger.error("❌ ALL GROQ MODELS FAILED")
    return None

# === ПРОМПТЫ ===
BASE_SYSTEM = """Ты — элитный SEO-стратег и контент-маркетолог с 15-летним опытом.
СТРОГИЕ ПРАВИЛА:
1. Пиши ТОЛЬКО на русском
2. НЕ используй ### или ##
3. Используй эмодзи для заголовков
4. Выделяй важное **жирным**
5. ОТВЕТ ДОЛЖЕН БЫТЬ ПОЛНЫМ
6. НИКОГДА не задавай вопросов если не просят"""

RECON_SYSTEM = BASE_SYSTEM + "\nЗАДАЧА: КРАТКАЯ разведка (максимум 800 слов). НЕ ЗАДАВАЙ ВОПРОСОВ."
FINAL_ANALYSIS_SYSTEM = BASE_SYSTEM + "\nЗАДАЧА: ПОЛНЫЙ глубокий анализ. НЕ ЗАдавай ВОПРОСОВ."
QUESTION_SYSTEM = BASE_SYSTEM + "\nЗАДАЧА: ОДИН вопрос для интервью. Только ОДИН вопрос, открытый. Без нумерации, без эмодзи. Максимум 150 слов."

def ask_qwen(prompt, max_tokens=1200, system=None):
    result = groq_request(prompt, max_tokens, system)
    if result:
        return result
    return "⚠️ Нейросеть недоступна. Нажми ▶️ Продолжить."

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

def progress_bar(qn, total=8):
    filled = "▓" * max(0, qn - 1)
    empty = "░" * max(0, total - qn + 1)
    return f"📊 {filled}{empty} {max(0,qn-1)}/{total}"

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
                    await msg.answer("⚠️ Ошибка. Нажми ▶️ Продолжить.", reply_markup=get_main_menu())
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

# === ТЕСТ GROQ ПРИ СТАРТЕ ===
def test_groq():
    logger.info("🧪 Testing Groq connection...")
    result = groq_request("Ответь одним словом: привет", max_tokens=10)
    if result:
        logger.info(f"✅ GROQ TEST PASSED: {result[:50]}")
    else:
        logger.error("❌ GROQ TEST FAILED! Check API key and limits")
    return result is not None

# === SCRAPERAPI ===
def scrape_with_api(url, premium=False, render=False, country_code="ru"):
    if not SCRAPER_API_KEY: return None
    params = {"api_key": SCRAPER_API_KEY, "url": url, "country_code": country_code}
    if premium: params["premium"] = "true"
    if render: params["render"] = "true"
    try:
        r = requests.get("http://api.scraperapi.com", params=params, timeout=90)
        if r.status_code == 200 and len(r.text) > 200:
            if "captcha" in r.text.lower() and len(r.text) < 1000: return None
            return r.text
        return None
    except Exception:
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
        html = scrape_with_api(url) or scrape_google_cache(url) or scrape_web_archive(url)
        if not html: return None
        soup = BeautifulSoup(html, "lxml")
        for s in soup(["script", "style", "nav", "footer", "header", "noscript"]): s.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:5000] if len(text) > 100 else None
    except Exception:
        return None

def extract_telegram_channel(url):
    try:
        ch = url.replace("https://t.me/", "").replace("t.me/", "").strip("/").split("/")[0]
        html = scrape_with_api(f"https://t.me/s/{ch}") or scrape_google_cache(f"https://t.me/s/{ch}")
        if not html: return None
        soup = BeautifulSoup(html, "lxml")
        posts = [p.get_text(strip=True) for p in soup.find_all("div", class_="tgme_widget_message_text") if p.get_text(strip=True)]
        return "\n\n".join(posts[:10])[:5000] if posts else None
    except Exception:
        return None

def extract_avito(url):
    try:
        if not url.startswith("http"): url = "https://" + url
        variants = [(url, True, True), (url.replace("www.avito.ru", "m.avito.ru"), True, True), (url, True, False), (url, False, False)]
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
            posts = [p.get_text(strip=True) for p in soup.find_all(["div", "p"]) if 40 < len(p.get_text(strip=True)) < 2000 and not any(s in p.get_text(strip=True).lower() for s in ["cookie", "войти", "зарегистрироваться"])]
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
        for fallback in [f"https://vk.com/{vk}"]:
            for fn in [scrape_google_cache, scrape_web_archive]:
                html = fn(fallback)
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

# === ОТПРАВКА ===
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

# === КОМАНДЫ ===
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    logger.info(f"🚀 /start from {message.from_user.id}")
    await state.clear()
    get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "👋 **Привет! Я — твой SEO-стратег.**\n\n"
        "Проведу интервью, изучу нишу и буду писать статьи.\n"
        "⏱ 5-7 минут. 📊 Всего 8 вопросов.\n"
        f"{progress_bar(1)}\n\n"
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
        "📋 **Команды:**\n"
        "• ▶️ Продолжить — продолжить с последнего места\n"
        "• 📝 Новая статья — сгенерировать статью\n"
        "• 🔄 Начать заново — начать с нуля",
        parse_mode="Markdown",
        reply_markup=get_main_menu()
    )

@dp.message(F.text == MENU_RESTART)
async def btn_restart_kb(message: types.Message, state: FSMContext):
    await cmd_start(message, state)

@dp.message(F.text == MENU_HELP)
async def btn_help_kb(message: types.Message):
    await cmd_help(message)

@dp.message(F.text == MENU_CONTINUE)
async def btn_continue_kb(message: types.Message, state: FSMContext):
    await cmd_continue_logic(message, state)

@dp.message(F.text == MENU_ARTICLE)
async def btn_article_kb(message: types.Message):
    await gen_article_logic(message)

@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    await message.answer(f"✅ Бот жив! {int(time.time())}", reply_markup=get_main_menu())

async def cmd_continue_logic(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    data = await state.get_data()
    qn = data.get("question_num", 1)
    history = data.get("history", [])
    
    if not current_state or (qn == 1 and not history):
        await message.answer("🤔 Нет активного диалога. Нажми 🔄 Начать заново.", reply_markup=get_main_menu())
        return
    
    if current_state == "Onboarding:gathering":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить", callback_data="continue_action")],
            [InlineKeyboardButton(text="🔄 Заново", callback_data="restart_action")]
        ])
        await message.answer(f"👋 **С возвращением!**\n{progress_bar(qn)}\nМы на вопросе {qn} из 8.", reply_markup=kb, parse_mode="Markdown")
    elif current_state == "Onboarding:source_link":
        await message.answer("🔗 Ждали ссылки. Пришли их или «нет».", parse_mode="Markdown")
    elif current_state == "Onboarding:waiting_source_parsing":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Повторить", callback_data="retry_parsing")]])
        await message.answer("⏳ Изучали источники. Повторить?", reply_markup=kb)
    elif current_state == "Onboarding:photos":
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📸 Фото")], [KeyboardButton(text="⏭ Пропустить")]], resize_keyboard=True)
        await message.answer("📸 Загружали фото.", reply_markup=kb)
    elif current_state == "Onboarding:cta_choice":
        await ask_cta(message, state)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить", callback_data="continue_action")],
            [InlineKeyboardButton(text="🔄 Заново", callback_data="restart_action")]
        ])
        await message.answer("🤔 Не могу определить где мы.", reply_markup=kb)

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
    await callback.message.answer("🔗 Пришли ссылки ещё раз. Или «нет».", parse_mode="Markdown")

async def ask_next_interview_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    history = data.get("history", [])
    qn = data.get("question_num", 1)
    pri = data.get("priority_service", "")
    
    if qn >= 8:
        await finish_interview(message, state, history)
        return
    
    hist = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    pq = f"Ты задал {qn} вопросов. История:\n{hist}\n"
    if pri: pq += f"\nПриоритет: {pri}\n"
    pq += "\nЗадай ОДИН следующий вопрос. ТОЛЬКО вопрос."
    
    nq = ask_qwen(pq, max_tokens=150, system=QUESTION_SYSTEM)
    
    if not nq or nq.startswith("⚠️"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="continue_action")]])
        await message.answer("⚠️ Не удалось сгенерировать вопрос. Нажми кнопку.", reply_markup=kb)
        return
    
    nq = nq.strip()
    for prefix in ["Вопрос:", "Q:", "q:", "В:", "**", "❓", "?\n", "\n"]:
        nq = nq.replace(prefix, "").strip()
    
    await state.update_data(question_num=qn + 1)
    await message.answer(
        f"{progress_bar(qn)}\n❓ **Вопрос {qn} из 8:**\n{nq}\n\n"
        "💡 Не знаешь — напиши **«не знаю»** или **«пропустить»**.",
        parse_mode="Markdown"
    )

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
        if check and check.startswith("YES|"):
            services = [s.strip() for s in check.split("|")[1:] if s.strip()]
            if services:
                await state.update_data(history=history, question_num=2, multiple_services=services)
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=s, callback_data=f"svc_{i}")] for i, s in enumerate(services)])
                await message.answer("🎯 **Вижу несколько направлений:**\n" + "\n".join([f"• {s}" for s in services]) + "\n\nПо какой делаем анализ?", reply_markup=kb, parse_mode="Markdown")
                await state.set_state(Onboarding.service_priority)
                return

    if qn >= 4 and not src_req:
        await state.update_data(history=history, source_requested=True)
        await message.answer(
            f"{progress_bar(qn)}\n✅ Уже многое понял!\n"
            "🔗 Пришли ссылки (можно несколько):\n"
            "• 🌐 Сайт\n• ✈️ Telegram\n• 📱 Авито\n• 💬 ВКонтакте\n\n"
            "Изучу каждый (30-90 сек). Нет — напиши «нет».",
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

@dp.message(Onboarding.source_link)
async def get_source(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    history = data.get("history", [])
    waiting_manual = data.get("waiting_manual", False)

    if waiting_manual:
        await message.answer("⏳ Изучаю текст...")
        src = message.text
        save_answer(uid, "source_data", src[:2000])
        ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
        pri = data.get("priority_service", "")
        analysis = ask_qwen(f"Клиент прислал: {src}\nИнтервью: {ht}\nПриоритет: {pri or 'нет'}\nКРАТКАЯ разведка (800 слов). НЕ ЗАДАВАЙ ВОПРОСОВ.", max_tokens=1500, system=RECON_SYSTEM)
        if analysis and not analysis.startswith("⚠️"):
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
        urls = [f"https://www.avito.ru/user/{answer}/shop"] if answer.isdigit() else [answer]

    await state.set_state(Onboarding.waiting_source_parsing)
    await state.update_data(urls_to_parse=urls)
    await message.answer(f"⏳ Нашёл {len(urls)} источник(ов). Изучаю (30-90 сек)...", parse_mode="Markdown")

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
        analysis = ask_qwen(f"Данные источников:\n{all_data[:6000]}\nИнтервью: {ht}\nПриоритет: {pri or 'нет'}\nКРАТКАЯ разведка (800 слов). НЕ ЗАДАВАЙ ВОПРОСОВ.", max_tokens=1500, system=RECON_SYSTEM)
        if analysis and not analysis.startswith("⚠️"):
            await send_long(message, f"📊 **Результаты:**\n{results_text}\n\n{analysis}")
        else:
            await send_long(message, f"📊 **Результаты:**\n{results_text}")
        await state.update_data(history=history, source_requested=True, source_data=all_data[:3000], waiting_manual=False)
        await state.set_state(Onboarding.gathering)
        await message.answer("✅ Готово! Продолжаем.", parse_mode="Markdown")
        await ask_next_interview_question(message, state)
    else:
        await message.answer(f"📊 **Результаты:**\n{results_text}\n\nНе смог открыть. Пришли **текстом**.", parse_mode="Markdown")
        await state.update_data(waiting_manual=True)
        await state.set_state(Onboarding.source_link)

async def finish_interview(message, state, history):
    uid = message.from_user.id
    data = await state.get_data()
    pri = data.get("priority_service", "")
    src = data.get("source_data", "")
    await message.answer("✅ **Интервью завершено!**\n⏳ Полный анализ (2-3 мин)...", parse_mode="Markdown")

    ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    nq = pri if pri else (history[0]["a"] if history else "")
    competitors = search_competitors(nq[:80])
    suggestions = get_yandex_suggestions(nq[:30])
    comp_text = "\n".join([f"- {c['title']}: {c['snippet']}" for c in competitors[:5]]) if competitors else "нет"
    sugg_text = ", ".join(suggestions[:10]) if suggestions else "пусто"
    src_sec = f"\n=== ИСТОЧНИКИ ===\n{src}\n" if src else ""

    analysis = ask_qwen(f"Интервью: {ht}{src_sec}\nПриоритет: {pri or 'нет'}\nКонкуренты: {comp_text}\nЯндекс: {sugg_text}\nПОЛНЫЙ АНАЛИЗ:\n🎯 КЛЮЧИ (15)\n💥 БОЛИ (10)\n⭐ УТП\n⚠️ МИНУСЫ", max_tokens=2500, system=FINAL_ANALYSIS_SYSTEM)

    if not analysis or analysis.startswith("⚠️"):
        await message.answer("⚠️ Ошибка анализа. Нажми ▶️ Продолжить.", reply_markup=get_main_menu())
        return

    plan = ask_qwen(f"Анализ: {analysis[:3000]}\nКонтент-план на 7 дней.", max_tokens=1500, system=FINAL_ANALYSIS_SYSTEM)
    guide = ask_qwen("Инструкция: публикация в Дзен. 5 шагов. Аналогия с магазином.", max_tokens=1200, system=FINAL_ANALYSIS_SYSTEM)

    save_research(uid, analysis + "\n\n" + plan, "", "", comp_text)

    await send_long(message, f"🎯 **ПОЛНЫЙ АНАЛИЗ:**\n{analysis}")
    await asyncio.sleep(1.5)
    if plan and not plan.startswith("⚠️"): await send_long(message, f"📅 **ПЛАН:**\n{plan}")
    await asyncio.sleep(1.5)
    if guide and not guide.startswith("⚠️"): await send_long(message, f"📚 **ДЗЕН:**\n{guide}")
    await asyncio.sleep(1)

    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📸 Фото")], [KeyboardButton(text="⏭ Пропустить")]], resize_keyboard=True)
    await message.answer("📸 **Загрузить фото работ?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(Onboarding.photos)

@dp.message(Onboarding.photos, F.text.in_(["⏭ Пропустить", "Позже", "позже", "нет", "Нет"]))
async def skip_photos(message, state: FSMContext):
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.text == "📸 Фото")
async def req_photos(message, state: FSMContext):
    await message.answer("📸 Пришли фото. Закончишь — «готово».", parse_mode="Markdown")

@dp.message(Onboarding.photos, F.text.lower() == "готово")
async def photos_done(message, state: FSMContext):
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.photo)
async def recv_photo(message, state: FSMContext):
    await message.answer("📸 Принял. Ещё или «готово».", parse_mode="Markdown")

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
    await message.answer("🎉 **Готово!**\nНажми 📝 Новая статья.", parse_mode="Markdown", reply_markup=get_main_menu())
    await state.clear()

async def gen_article_logic(message):
    uid = message.from_user.id
    try:
        r = supabase.table("niche_research").select("*").eq("user_id", uid).order("id", desc=True).limit(1).execute()
        if not r.data:
            await message.answer("Сначала пройди онбординг: 🔄 Начать заново", reply_markup=get_main_menu())
            return
        analysis = r.data[0]["keywords"]
    except Exception:
        await message.answer("Сначала пройди онбординг.", reply_markup=get_main_menu())
        return

    await message.answer("✍️ Пишу статью (1-2 мин)...", parse_mode="Markdown")
    article = ask_qwen(f"Анализ: {analysis}\nSEO-статья: 1 ключ + 1 боль. Заголовок, вступление, подзаголовки, 5-7 тыс знаков.\n📄 СТАТЬЯ\n🏷 SEO (Title, Description, хэштеги, Slug)", max_tokens=2500, system=FINAL_ANALYSIS_SYSTEM)

    if not article or article.startswith("⚠️"):
        await message.answer("⚠️ Ошибка генерации. Нажми ▶️ Продолжить.", reply_markup=get_main_menu())
        return
    save_article(uid, "статья", "боль", "статья", article)
    log_usage(uid, "article")
    await send_long(message, f"📄 **СТАТЬЯ:**\n{article}")
    
    # Картинка
    try:
        img_url = "https://image.pollinations.ai/prompt/" + requests.utils.quote("professional blog cover, modern minimal, no text") + "?width=1200&height=630"
        img_data = requests.get(img_url, timeout=60).content
        if len(img_data) > 5000:
            photo = BufferedInputFile(img_data, filename="cover.jpg")
            await message.answer_photo(photo, caption="🖼 Обложка")
    except Exception:
        pass
    
    # Файл
    try:
        doc = BufferedInputFile(article.encode('utf-8'), filename="article.txt")
        await message.answer_document(doc, caption="💾 Скачать")
    except Exception:
        pass
    
    await asyncio.sleep(1)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ vc.ru", callback_data="vc_yes"), InlineKeyboardButton(text="❌ Дзен", callback_data="vc_no")]
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
    vc = ask_qwen(f"Адаптируй под vc.ru: {orig}\nКейс/факап. Тон: коллега. 7-12 тыс.", max_tokens=2500, system=FINAL_ANALYSIS_SYSTEM)
    try: supabase.table("articles").update({"vc_version": vc}).eq("id", aid).execute()
    except Exception: pass
    if vc and not vc.startswith("⚠️"): await send_long(callback.message, f"📰 **vc.ru:**\n{vc}")
    await callback.answer()

@dp.callback_query(F.data == "vc_no")
async def skip_vc(callback):
    await callback.message.answer("👌 Только Дзен.", reply_markup=get_main_menu())
    await callback.answer()

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
    
    # Тест Groq при старте
    test_groq()
    
    logger.info("✅ Threads started, running main...")
    asyncio.run(main())
