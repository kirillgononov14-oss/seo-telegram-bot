import logging
import os
import requests
import threading
import asyncio
import time
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
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

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BANNED_NICHES = ["обнал", "отмыв", "адалт", "18+", "порн", "оружие", "наркот", "взлом", "хакер"]

# === СИСТЕМНЫЙ ПРОМПТ (БАЗА) ===
BASE_SYSTEM = """Ты — элитный SEO-стратег и контент-маркетолог с 15-летним опытом.
Работаешь как настоящий живой маркетолог.

СТРОГИЕ ПРАВИЛА:
1. Пиши ТОЛЬКО на русском языке
2. НЕ используй символы ### или ##
3. Используй эмодзи (🎯 📌 💥 ⭐ ⚠️ 📅) для заголовков
4. Выделяй важное **жирным**
5. ОТВЕТ ДОЛЖЕН БЫТЬ ПОЛНЫМ — никогда не обрывай
6. НИКОГДА не задавай вопросы клиенту, если тебя об этом явно не попросили в задаче"""

# === ПРОМПТ ДЛЯ РАЗВЕДКИ (анализ источника, БЕЗ вопросов) ===
RECON_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Сделай КРАТКУЮ разведку бизнеса клиента на основе присланных данных.
ВАЖНО: НЕ ЗАДАВАЙ ВОПРОСОВ КЛИЕНТУ. Только анализ.
Отвечай коротко и по делу (максимум 1000 слов)."""

# === ПРОМПТ ДЛЯ ПОЛНОГО АНАЛИЗА (в конце интервью) ===
FINAL_ANALYSIS_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Сделай ПОЛНЫЙ глубокий анализ ниши клиента.
ВАЖНО: НЕ ЗАДАВАЙ ВОПРОСОВ КЛИЕНТУ. Только анализ с разделами.
Будь детальным но не повторяйся."""

# === ПРОМПТ ДЛЯ ГЕНЕРАЦИИ ВОПРОСА ===
QUESTION_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Сгенерируй ОДИН следующий вопрос для интервью.
ПРАВИЛА:
- Только ОДИН вопрос, без вступлений
- Вопрос должен быть открытым (не да/нет)
- Вопрос должен быть конкретным и продвигающим диалог
- Без нумерации, без префиксов вроде "Вопрос:"
- Без эмодзи"""

class Onboarding(StatesGroup):
    gathering = State()
    service_priority = State()
    source_link = State()
    photos = State()
    cta_choice = State()
    cta_value = State()

class ErrorHandlerMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable, event: types.Update, data: Dict[str, Any]) -> Any:
        try:
            return await handler(event, data)
        except Exception as e:
            logger.error(f"❌ ОШИБКА: {type(e).__name__}: {e}", exc_info=True)
            try:
                if hasattr(event, 'message') and event.message:
                    await event.message.answer(f"⚠️ Ошибка. Напиши /start\n{str(e)[:100]}")
                elif hasattr(event, 'callback_query') and event.callback_query:
                    await event.callback_query.message.answer("⚠️ Ошибка. Напиши /start")
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
        logger.error("❌ SCRAPER_API_KEY не задан!")
        return None
    params = {"api_key": SCRAPER_API_KEY, "url": url, "country_code": country_code}
    if premium: params["premium"] = "true"
    if render: params["render"] = "true"
    try:
        logger.info(f"🌐 Scraping {url} (premium={premium}, render={render})...")
        r = requests.get("http://api.scraperapi.com", params=params, timeout=90)
        if r.status_code == 200 and len(r.text) > 200:
            if "captcha" in r.text.lower() and len(r.text) < 1000:
                logger.warning(f"⚠️ Captcha on {url}")
                return None
            logger.info(f"✅ Scraped {url}: {len(r.text)} chars")
            return r.text
        logger.warning(f"⚠️ ScraperAPI status {r.status_code}, len={len(r.text)} for {url}")
        return None
    except Exception as e:
        logger.error(f"❌ Scrape error {url}: {e}")
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
        if not html: return None
        soup = BeautifulSoup(html, "lxml")
        posts = [p.get_text(strip=True) for p in soup.find_all("div", class_="tgme_widget_message_text") if p.get_text(strip=True)]
        return "\n\n".join(posts[:10])[:5000] if posts else None
    except Exception as e:
        logger.error(f"❌ TG: {e}")
        return None

def extract_avito(url):
    try:
        if not url.startswith("http"): url = "https://" + url
        variants = [
            (url, True, True),
            (url.replace("www.avito.ru", "m.avito.ru"), True, True),
            (url, True, False),
        ]
        for variant_url, prem, rend in variants:
            html = scrape_with_api(variant_url, premium=prem, render=rend)
            if not html: continue
            soup = BeautifulSoup(html, "lxml")
            parts = []
            for tag in soup.find_all(["h1", "h2", "h3"]):
                t = tag.get_text(strip=True)
                if t and len(t) > 5: parts.append(t)
            for tag in soup.find_all(["div", "p", "span"]):
                t = tag.get_text(strip=True)
                if len(t) > 20 and any(w in t.lower() for w in ["руб", "₽", "дом", "м²", "участок", "этаж", "площадь"]):
                    if t not in parts: parts.append(t)
            result = "\n".join(parts)
            if len(result) > 200:
                logger.info(f"✅ Avito: {len(result)} chars")
                return result[:5000]
            for s in soup(["script", "style"]): s.decompose()
            full = soup.get_text(separator="\n", strip=True)
            if len(full) > 500:
                logger.info(f"✅ Avito full: {len(full)} chars")
                return full[:5000]
        return None
    except Exception as e:
        logger.error(f"❌ Avito: {e}")
        return None

def extract_vk_group(url):
    try:
        vk = url.replace("https://vk.com/", "").replace("https://vk.ru/", "")
        vk = vk.replace("vk.com/", "").replace("vk.ru/", "").strip("/").split("/")[0]
        variants = [
            f"https://m.vk.com/{vk}",
            f"https://vk.com/{vk}",
            f"https://m.vk.com/{vk}?act=info",
        ]
        for variant_url in variants:
            html = scrape_with_api(variant_url, premium=True, render=True)
            if not html: continue
            soup = BeautifulSoup(html, "lxml")
            posts = []
            for p in soup.find_all(["div", "p"]):
                t = p.get_text(strip=True)
                if 40 < len(t) < 2000 and not any(skip in t.lower() for skip in ["cookie", "войти", "зарегистрироваться", "браузер"]):
                    posts.append(t)
            unique = []
            seen = set()
            for p in posts:
                if p not in seen:
                    seen.add(p)
                    unique.append(p)
            posts = unique[:15]
            desc = ""
            for d in soup.find_all(["div", "p"], class_=["group_info", "page_info", "group_description", "info"]):
                desc += d.get_text(strip=True) + "\n"
            if posts or desc:
                result = ""
                if desc: result += "ОПИСАНИЕ:\n" + desc + "\n\n"
                if posts: result += "ПОСТЫ:\n" + "\n---\n".join(posts[:10])
                if len(result) > 200:
                    logger.info(f"✅ VK: {len(result)} chars, {len(posts)} posts")
                    return result[:5000]
            for s in soup(["script", "style"]): s.decompose()
            full = soup.get_text(separator="\n", strip=True)
            if len(full) > 500:
                logger.info(f"✅ VK full: {len(full)} chars")
                return full[:5000]
        return None
    except Exception as e:
        logger.error(f"❌ VK: {e}")
        return None

def search_competitors(q, max_results=5):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{q} цены отзывы", region="ru-ru", max_results=max_results))
            return [{"title": r.get("title", ""), "snippet": r.get("body", "")[:200]} for r in results]
    except Exception as e:
        logger.error(f"❌ Search: {e}")
        return []

def get_yandex_suggestions(kw):
    try:
        r = requests.get("https://suggest.yandex.net/suggest-ff.cgi", params={"part": kw, "lang": "ru", "v": "3"}, timeout=10)
        data = r.json()
        return data[1] if isinstance(data, list) and len(data) > 1 else []
    except Exception as e:
        logger.error(f"❌ Yandex: {e}")
        return []

# === SUPABASE ===
def get_or_create_user(uid, username, fname):
    try:
        result = supabase.table("users").select("*").eq("id", uid).execute()
        if result.data: return result.data[0]
        u = {"id": uid, "username": username or "", "first_name": fname or ""}
        supabase.table("users").insert(u).execute()
        return u
    except Exception as e:
        logger.error(f"❌ User: {e}")
        return {"id": uid}

def save_answer(uid, q, a):
    try:
        supabase.table("onboarding_answers").insert({"user_id": uid, "question": q, "answer": str(a)[:1500]}).execute()
    except Exception as e:
        logger.error(f"❌ Save: {e}")

def save_research(uid, kw, pains, utp, comp):
    try:
        supabase.table("niche_research").insert({"user_id": uid, "keywords": str(kw)[:8000], "pains": str(pains)[:5000], "utp": str(utp)[:2000], "competitors": str(comp)[:3000]}).execute()
    except Exception as e:
        logger.error(f"❌ Research: {e}")

def save_article(uid, kw, pain, title, content):
    try:
        supabase.table("articles").insert({"user_id": uid, "keyword": str(kw)[:500], "pain": str(pain)[:500], "title": str(title)[:500], "content": str(content)[:15000], "status": "draft"}).execute()
    except Exception as e:
        logger.error(f"❌ Article: {e}")

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
            logger.info(f"🤖 Qwen attempt {attempt+1}, max_tokens={max_tokens}...")
            r = groq_client.chat.completions.create(
                model="qwen/qwen3.8-27b",
                messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt[:15000]}],
                max_tokens=max_tokens, temperature=0.7, timeout=120
            )
            result = r.choices[0].message.content
            finish = r.choices[0].finish_reason
            logger.info(f"✅ Qwen: {len(result)} chars, finish={finish}")
            if finish == "length":
                logger.warning(f"⚠️ Response TRUNCATED! Hit max_tokens={max_tokens}")
                result += "\n\n[Ответ был обрезан, но основная информация передана]"
            return result
        except Exception as e:
            logger.error(f"❌ Qwen fail {attempt+1}: {e}")
            if attempt < 2: time.sleep(2 ** attempt)
    return "⚠️ Нейросеть недоступна. Попробуй позже."

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
    except Exception as e:
        logger.error(f"❌ Send: {e}")

def is_banned(text):
    return any(w in text.lower() for w in BANNED_NICHES)

# === СТАРТ ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    logger.info(f"🚀 /start from {message.from_user.id}")
    await state.clear()
    get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "👋 **Привет! Я — твой SEO-стратег.**\n\n"
        "Проведу интервью, изучу нишу и буду писать статьи.\n"
        "⏱ 5-7 минут.\n\n"
        "❓ **Вопрос 1:**\nЧто продаёшь? Опиши в 2-3 предложениях.",
        parse_mode="Markdown"
    )
    await state.set_state(Onboarding.gathering)
    await state.update_data(question_num=1, history=[], source_requested=False)

@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    await message.answer(f"✅ Бот жив! {int(time.time())}")

# === ГЕНЕРАЦИЯ СЛЕДУЮЩЕГО ВОПРОСА (отдельная функция) ===
async def ask_next_interview_question(message: types.Message, state: FSMContext):
    """Задаёт СЛЕДУЮЩИЙ вопрос интервью. Всегда по одному."""
    data = await state.get_data()
    history = data.get("history", [])
    qn = data.get("question_num", 1)
    pri = data.get("priority_service", "")
    
    # Если вопросов уже 8+ — завершаем интервью
    if qn >= 8:
        await finish_interview(message, state, history)
        return
    
    hist = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    pq = f"""Ты уже задал {qn} вопросов клиенту. Вот история:

{hist}
"""
    if pri:
        pq += f"\nПриоритетная услуга клиента: {pri}\n"
    
    pq += """
Задай ОДИН следующий конкретный вопрос (открытый, не да/нет), который поможет:
- понять боли клиентов глубже
- узнать конкурентные преимущества
- получить реальные кейсы

ВАЖНО:
- Напиши ТОЛЬКО вопрос
- Без вступлений и комментариев
- Без нумерации
- Без эмодзи
- Без префиксов типа "Вопрос:" или "Q" """
    
    nq = ask_qwen(pq, max_tokens=150, system=QUESTION_SYSTEM)
    
    if nq.startswith("⚠️"):
        await message.answer(f"⚠️ {nq}\nПопробуй ещё раз или /start")
        return
    
    # Очищаем вопрос от лишнего
    nq = nq.strip()
    # Убираем возможные префиксы
    for prefix in ["Вопрос:", "Q:", "q:", "В:", "**", "❓", "?\n"]:
        nq = nq.replace(prefix, "").strip()
    
    logger.info(f"✅ Generated question {qn+1}: {nq[:50]}...")
    await state.update_data(history=history, question_num=qn + 1)
    await message.answer(f"❓ **Вопрос {qn + 1}:**\n{nq}", parse_mode="Markdown")

# === ИНТЕРВЬЮ ===
@dp.message(Onboarding.gathering)
async def live_interview(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    history = data.get("history", [])
    qn = data.get("question_num", 1)
    src_req = data.get("source_requested", False)
    
    logger.info(f"💬 Response Q{qn} from {uid}: {message.text[:50]}...")

    if qn == 1 and is_banned(message.text):
        await message.answer("❌ Не работаю с этой темой. /start")
        await state.clear()
        return

    history.append({"q": qn, "a": message.text})
    save_answer(uid, f"Q{qn}", message.text)

    # После 1-го вопроса проверяем несколько услуг
    if qn == 1:
        check = ask_qwen(f'Клиент: "{message.text}"\nНесколько РАЗНЫХ услуг? Ответь "YES|у1|у2" или "NO"', max_tokens=100, system=QUESTION_SYSTEM)
        if check.startswith("YES|"):
            services = [s.strip() for s in check.split("|")[1:] if s.strip()]
            if services:
                await state.update_data(history=history, question_num=2, multiple_services=services)
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=s, callback_data=f"svc_{i}")] for i, s in enumerate(services)])
                await message.answer(
                    "🎯 **Вижу несколько направлений:**\n" + "\n".join([f"• {s}" for s in services]) +
                    "\n\nЭто **разные ниши**. По какой делаем анализ первым?",
                    reply_markup=kb, parse_mode="Markdown"
                )
                await state.set_state(Onboarding.service_priority)
                return

    # После 4-го вопроса просим источник (если ещё не просили)
    if qn >= 4 and not src_req:
        await state.update_data(history=history, question_num=qn + 1, source_requested=True)
        await message.answer(
            "✅ **Уже многое понял!**\n\n"
            "🔗 Пришли ссылки на источники (можно несколько в одном сообщении):\n"
            "• 🌐 Сайт\n• ✈️ Telegram-канал\n• 📱 Авито\n• 💬 ВКонтакте\n\n"
            "Изучу каждый сам. Если ничего нет — напиши **«нет»**.",
            parse_mode="Markdown"
        )
        await state.set_state(Onboarding.source_link)
        await state.update_data(waiting_manual=False)
        return

    # Задаём следующий вопрос (один за раз!)
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

    # Запасной вариант: клиент прислал текст
    if waiting_manual:
        await message.answer("⏳ Изучаю текст (30-60 сек)...")
        src = message.text
        save_answer(uid, "source_data", src[:2000])
        
        ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
        pri = data.get("priority_service", "")
        
        # КРАТКАЯ разведка — БЕЗ вопросов
        analysis = ask_qwen(f"""Клиент прислал информацию о бизнесе:
{src}

Из интервью известно:
{ht}
Приоритет: {pri or 'не указан'}

Сделай КРАТКУЮ разведку (не больше 600 слов):
📌 **Сильные стороны** (3 пункта, коротко)
📌 **Слабые места** (2-3 пункта, коротко)
📌 **Стиль общения** (1-2 предложения)
📌 **Идеи для статей** (2-3 идеи, одной строкой каждая)

НЕ ЗАДАВАЙ ВОПРОСОВ КЛИЕНТУ. Только краткий анализ.""", max_tokens=1500, system=RECON_SYSTEM)
        
        if not analysis.startswith("⚠️"):
            await send_long(message, f"📊 **Краткая разведка:**\n\n{analysis}")
        
        await state.update_data(
            history=history, 
            question_num=data.get("question_num", 5), 
            source_requested=True, 
            source_data=src[:2000], 
            waiting_manual=False
        )
        await state.set_state(Onboarding.gathering)
        await message.answer("Продолжаем интервью. Готов ответить на следующий вопрос?", parse_mode="Markdown")
        await ask_next_interview_question(message, state)
        return

    answer = message.text.strip()
    save_answer(uid, "source_link", answer)

    if answer.lower() in ["нет", "нету", "-", "0", "нет источника", "отсутствует"]:
        await message.answer("👌 Работаем без источника.", parse_mode="Markdown")
        await state.update_data(source_requested=True, source_data=None, waiting_manual=False)
        await state.set_state(Onboarding.gathering)
        await ask_next_interview_question(message, state)
        return

    urls = extract_urls(answer)
    if not urls:
        if answer.isdigit():
            urls = [f"https://www.avito.ru/user/{answer}/shop"]
        else:
            urls = [answer]

    await message.answer(f"⏳ **Нашёл {len(urls)} источник(ов). Изучаю каждый (30-90 сек)...**", parse_mode="Markdown")

    all_data = ""
    results = []
    for url in urls:
        logger.info(f"🔍 Parsing: {url}")
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

        # КРАТКАЯ разведка — БЕЗ вопросов внутри
        analysis = ask_qwen(f"""Данные из источников клиента:
{all_data[:6000]}

Из интервью:
{ht}
Приоритет: {pri or 'не указан'}

Сделай КРАТКУЮ разведку (не больше 600 слов):
📌 **Сильные стороны** (3 пункта, коротко)
📌 **Слабые места** (2-3 пункта, коротко)
📌 **Стиль общения** (1-2 предложения)
📌 **Идеи для статей** (2-3 идеи, одной строкой каждая)

ВАЖНО: НЕ ЗАДАВАЙ ВОПРОСОВ КЛИЕНТУ. Только анализ.""", max_tokens=1500, system=RECON_SYSTEM)

        if not analysis.startswith("⚠️"):
            await send_long(message, f"📊 **Результаты изучения:**\n{results_text}\n\n**Краткая разведка:**\n{analysis}")
        else:
            await send_long(message, f"📊 **Результаты:**\n{results_text}\n\n{analysis}")

        await state.update_data(
            history=history,
            question_num=data.get("question_num", 5),
            source_requested=True,
            source_data=all_data[:3000],
            waiting_manual=False
        )
        await state.set_state(Onboarding.gathering)
        # Продолжаем интервью ОДНИМ вопросом
        await message.answer("Отлично, продолжаем интервью.", parse_mode="Markdown")
        await ask_next_interview_question(message, state)
    else:
        await message.answer(
            f"📊 **Результаты:**\n{results_text}\n\n"
            f"Не смог открыть автоматически.\n"
            f"Пришли **текстом**: описание, цены, услуги.",
            parse_mode="Markdown"
        )
        await state.update_data(waiting_manual=True)
        return

# === ЗАВЕРШЕНИЕ (ПОЛНЫЙ АНАЛИЗ) ===
async def finish_interview(message, state, history):
    uid = message.from_user.id
    data = await state.get_data()
    pri = data.get("priority_service", "")
    src = data.get("source_data", "")
    await message.answer("✅ **Интервью завершено!**\n⏳ Делаю **полный анализ ниши**... 2-3 минуты.", parse_mode="Markdown")

    ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    nq = pri if pri else (history[0]["a"] if history else "")
    competitors = search_competitors(nq[:80])
    suggestions = get_yandex_suggestions(nq[:30])
    comp_text = "\n".join([f"- {c['title']}: {c['snippet']}" for c in competitors[:5]]) if competitors else "нет"
    sugg_text = ", ".join(suggestions[:10]) if suggestions else "пусто"
    src_sec = f"\n=== ДАННЫЕ ИЗ ИСТОЧНИКОВ ===\n{src}\n" if src else ""

    # ПОЛНЫЙ анализ — большой max_tokens
    analysis = ask_qwen(f"""Интервью:
{ht}{src_sec}

Приоритет: {pri or 'нет'}
Конкуренты:
{comp_text}
Подсказки Яндекса: {sugg_text}

Сделай ПОЛНЫЙ глубокий анализ ниши с разделами:

🎯 **КЛЮЧИ** (15 запросов, по одному на строку)
💥 **БОЛИ** (10 конкретных болей ЦА, по одной на строку)
⭐ **УТП** (3-4 предложения, чем клиент лучше конкурентов)
⚠️ **МИНУСЫ** (2-3 слабых места в позиционировании)

НЕ ЗАДАВАЙ ВОПРОСОВ КЛИЕНТУ. Только анализ.""", max_tokens=2500, system=FINAL_ANALYSIS_SYSTEM)

    if analysis.startswith("⚠️"):
        await message.answer(f"⚠️ {analysis}")
        return

    # Контент-план (отдельный запрос)
    plan = ask_qwen(f"""На основе анализа:
{analysis[:3000]}

Составь **контент-план на 7 дней** для Дзен.
1 статья = 1 ключ + 1 боль.
Для каждой: ключ, боль, тип, день недели, ЧАС публикации, длина.

Формат:
📅 **ПЛАН НА 7 ДНЕЙ**
1. [день] ...
2. [день] ...

НЕ ЗАДАВАЙ ВОПРОСОВ.""", max_tokens=1500, system=FINAL_ANALYSIS_SYSTEM)

    # Инструкция
    guide = ask_qwen("""Инструкция для чайника: как опубликовать SEO-статью в Дзен для трафика из Яндекса и Гугла.
5 простых шагов. Объясни Title, Description, H1, alt-текст через аналогию с магазином.
Формат:
📚 **ПУБЛИКАЦИЯ В ДЗЕН**
Шаг 1...
Шаг 2...

НЕ ЗАДАВАЙ ВОПРОСОВ.""", max_tokens=1200, system=FINAL_ANALYSIS_SYSTEM)

    save_research(uid, analysis + "\n\n" + plan, "", "", comp_text)

    # Отправляем по частям с задержкой
    await send_long(message, f"🎯 **ПОЛНЫЙ АНАЛИЗ ТВОЕЙ НИШИ:**\n\n{analysis}")
    await asyncio.sleep(1.5)
    if not plan.startswith("⚠️"):
        await send_long(message, f"📅 **КОНТЕНТ-ПЛАН:**\n\n{plan}")
    await asyncio.sleep(1.5)
    if not guide.startswith("⚠️"):
        await send_long(message, f"📚 **КАК ПУБЛИКОВАТЬ В ДЗЕН:**\n\n{guide}")
    await asyncio.sleep(1)

    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📸 Фото")], [KeyboardButton(text="⏭ Пропустить")]], resize_keyboard=True)
    await message.answer("📸 **Загрузить фото работ?** Можно сейчас или позже.", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(Onboarding.photos)

# === ФОТО ===
@dp.message(Onboarding.photos, F.text.in_(["⏭ Пропустить", "Позже", "позже", "Не сейчас", "нет", "Нет", "пропустить"]))
async def skip_photos(message, state: FSMContext):
    await message.answer("👌 Генерирую сам.")
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.text == "📸 Фото")
async def req_photos(message, state: FSMContext):
    await message.answer("📸 Пришли фото. Закончишь — **«готово»** или **«позже»**.", parse_mode="Markdown")

@dp.message(Onboarding.photos, F.text.lower().in_(["готово", "Готово"]))
async def photos_done(message, state: FSMContext):
    await message.answer("✅ Принял!")
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
    await message.answer("🎉 **Готово!**\n`/article` — статья\n`/help` — команды", parse_mode="Markdown")
    await state.clear()

# === СТАТЬЯ ===
@dp.message(Command("article"))
async def gen_article(message):
    uid = message.from_user.id
    try:
        r = supabase.table("niche_research").select("*").eq("user_id", uid).order("id", desc=True).limit(1).execute()
        if not r.data:
            await message.answer("Сначала /start")
            return
        analysis = r.data[0]["keywords"]
    except Exception:
        await message.answer("Сначала /start")
        return

    await message.answer("✍️ **Пишу статью...** 1-2 мин.", parse_mode="Markdown")
    article = ask_qwen(f"""Анализ: {analysis}

Напиши SEO-статью по правилу "1 ключ + 1 боль".
Выбери самую сильную связку ключ+боль из анализа.

СТРУКТУРА:
📄 **СТАТЬЯ**
- Цепляющий заголовок с ключом
- Вступление с болью (2-3 абзаца)
- Подзаголовки, списки
- 5-7 тыс. знаков
- Цифры, кейсы, шаги, ошибки
- Мягкий призыв в конце

🏷 **SEO-ПАКЕТ**
- **Title:** (до 60 символов)
- **Description:** (до 160 символов)
- **Хэштеги:** 2-3 штуки
- **Slug:** ЧПУ-ссылка латиницей

НЕ ЗАДАВАЙ ВОПРОСОВ. Полный ответ.""", max_tokens=2500, system=FINAL_ANALYSIS_SYSTEM)

    if article.startswith("⚠️"):
        await message.answer(f"⚠️ {article}")
        return
    save_article(uid, "статья", "боль", "статья", article)
    log_usage(uid, "article")
    await send_long(message, f"📄 **СТАТЬЯ ГОТОВА:**\n\n{article}")
    await asyncio.sleep(1)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ vc.ru", callback_data="vc_yes"), InlineKeyboardButton(text="❌ Дзен", callback_data="vc_no")]
    ])
    await message.answer("📰 **Адаптировать под vc.ru?**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "vc_yes")
async def adapt_vc(callback):
    await callback.message.answer("⏳ Адаптирую...")
    try:
        a = supabase.table("articles").select("*").eq("user_id", callback.from_user.id).order("id", desc=True).limit(1).execute()
        if not a.data: return
        orig = a.data[0]["content"]
        aid = a.data[0]["id"]
    except Exception: return
    vc = ask_qwen(f"""Адаптируй под vc.ru:
{orig}

Угол: кейс / факап / внутренняя кухня
Тон: коллега делится опытом
Длина: 7-12 тыс. знаков
Без хэштегов и прямой рекламы

НЕ ЗАДАВАЙ ВОПРОСОВ.""", max_tokens=2500, system=FINAL_ANALYSIS_SYSTEM)
    try: supabase.table("articles").update({"vc_version": vc}).eq("id", aid).execute()
    except Exception: pass
    if not vc.startswith("⚠️"):
        await send_long(callback.message, f"📰 **vc.ru:**\n\n{vc}")
    await callback.answer()

@dp.callback_query(F.data == "vc_no")
async def skip_vc(callback):
    await callback.message.answer("👌 Только Дзен.")
    await callback.answer()

# === HELP / ADMIN ===
@dp.message(Command("help"))
async def cmd_help(message):
    await message.answer("📋 /start — заново | /article — статья | /ping — жив? | /help — справка")

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
    logger.info("🚀 Starting...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    asyncio.run(main())
