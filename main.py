import logging
import os
import requests
import threading
import asyncio
import time
import re
from io import BytesIO
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
from typing import Callable, Dict, Any, Awaitable

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
ADMIN_ID = 1847007101

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BANNED_NICHES = ["обнал", "отмыв", "адалт", "18+", "порн", "оружие", "наркот", "взлом", "хакер"]

# Несколько User-Agent для маскировки под разные браузеры
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]

SYSTEM_PROMPT = """Ты — элитный SEO-стратег и контент-маркетолог с 15-летним опытом.
ПРИНЦИПЫ:
1. Живое интервью: по одному вопросу, внимательно читай ответы
2. Несколько услуг = разные ниши. Спроси приоритет
3. Анализируй ТОЛЬКО реальные данные. Не выдумывай
4. Статья = 1 ключ + 1 боль. Язык читателя, решай боль
5. Форматирование: эмодзи, **жирный**, списки
ВСЕГДА НА РУССКОМ."""

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
                    await event.message.answer(f"⚠️ Ошибка. /start\n{str(e)[:100]}")
                elif hasattr(event, 'callback_query') and event.callback_query:
                    await event.callback_query.message.answer("⚠️ Ошибка. /start")
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

# === ИЗВЛЕЧЕНИЕ ССЫЛОК ===
def extract_urls(text):
    return re.findall(r'https?://[^\s,;]+', text)

def detect_source_type(url):
    u = url.lower()
    if "avito.ru" in u:
        return "avito"
    elif "vk.com" in u or "vk.ru" in u:
        return "vk"
    elif "t.me/" in u:
        return "telegram"
    else:
        return "site"

def parse_source(url):
    st = detect_source_type(url)
    if st == "avito":
        return extract_avito(url), "Авито"
    elif st == "vk":
        return extract_vk_group(url), "ВКонтакте"
    elif st == "telegram":
        return extract_telegram_channel(url), "Telegram"
    else:
        return extract_site_text(url), "Сайт"

# === УЛУЧШЕННЫЕ ПАРСЕРЫ ===
def _request_with_retry(url, timeout=45, is_mobile=False):
    """Делает несколько попыток с разными User-Agent"""
    for i, ua in enumerate(USER_AGENTS[:3]):  # 3 попытки
        try:
            headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "max-age=0"
            }
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            r.encoding = "utf-8"
            if r.status_code == 200:
                logger.info(f"✅ HTTP 200 на попытке {i+1} для {url}")
                return r
            logger.warning(f"⚠️ Status {r.status_code} для {url}, попытка {i+1}")
        except requests.exceptions.Timeout:
            logger.warning(f"⏱️ Таймаут на попытке {i+1} для {url}")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка на попытке {i+1}: {type(e).__name__}")
        time.sleep(1)  # Пауза перед следующей попыткой
    return None

def extract_site_text(url):
    try:
        if not url.startswith("http"):
            url = "https://" + url
        
        # Пробуем с www и без
        r = _request_with_retry(url, timeout=45)
        if not r:
            # Пробуем альтернативный вариант
            alt_url = url.replace("https://", "http://") if url.startswith("https://") else url.replace("http://", "https://")
            r = _request_with_retry(alt_url, timeout=45)
        
        if not r:
            return None
        
        soup = BeautifulSoup(r.text, "lxml")
        for s in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            s.decompose()
        text = soup.get_text(separator="\n", strip=True)
        logger.info(f"✅ Site: {len(text)} chars from {url}")
        return text[:5000] if len(text) > 100 else None
    except Exception as e:
        logger.error(f"❌ Site error {url}: {e}")
        return None

def extract_telegram_channel(url):
    try:
        ch = url.replace("https://t.me/", "").replace("t.me/", "").strip("/").split("/")[0]
        public_url = f"https://t.me/s/{ch}"
        r = _request_with_retry(public_url, timeout=30)
        if not r:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        posts = [p.get_text(strip=True) for p in soup.find_all("div", class_="tgme_widget_message_text") if p.get_text(strip=True)]
        if not posts:
            return None
        logger.info(f"✅ TG: {len(posts)} posts")
        return "\n\n".join(posts[:10])[:5000]
    except Exception as e:
        logger.error(f"❌ TG error: {e}")
        return None

def extract_avito(url):
    """Парсит Авито разными способами"""
    try:
        if not url.startswith("http"):
            url = "https://" + url
        
        # Способ 1: обычная версия
        r = _request_with_retry(url, timeout=45)
        if r:
            soup = BeautifulSoup(r.text, "lxml")
            # Ищем описание объявления
            description = ""
            for tag in soup.find_all(["div", "p", "span"]):
                text = tag.get_text(strip=True)
                if len(text) > 30 and any(c in text for c in ["руб", "₽", "дом", "м²", "участок"]):
                    description += text + "\n"
            
            if len(description) > 200:
                logger.info(f"✅ Avito: {len(description)} chars (v1)")
                return description[:5000]
        
        # Способ 2: мобильная версия
        mobile_url = url.replace("www.avito.ru", "m.avito.ru")
        r = _request_with_retry(mobile_url, timeout=45)
        if r:
            soup = BeautifulSoup(r.text, "lxml")
            text = soup.get_text(separator="\n", strip=True)
            if len(text) > 300:
                logger.info(f"✅ Avito mobile: {len(text)} chars")
                return text[:5000]
        
        # Способ 3: ищем через JSON в HTML
        if r:
            text = r.text
            # Авито иногда вставляет данные в JSON
            if "description" in text.lower() and len(text) > 1000:
                logger.info(f"✅ Avito via page analysis")
                soup = BeautifulSoup(text, "lxml")
                clean_text = soup.get_text(separator="\n", strip=True)
                if len(clean_text) > 200:
                    return clean_text[:5000]
        
        return None
    except Exception as e:
        logger.error(f"❌ Avito error: {e}")
        return None

def extract_vk_group(url):
    """Парсит ВКонтакте"""
    try:
        vk = url.replace("https://vk.com/", "").replace("https://vk.ru/", "")
        vk = vk.replace("vk.com/", "").replace("vk.ru/", "").strip("/").split("/")[0]
        
        # Способ 1: мобильная версия (легче парсится)
        mobile_url = f"https://m.vk.com/{vk}"
        r = _request_with_retry(mobile_url, timeout=45)
        if r:
            soup = BeautifulSoup(r.text, "lxml")
            posts = []
            for p in soup.find_all(["div", "p"]):
                text = p.get_text(strip=True)
                if len(text) > 30 and len(text) < 1000:
                    posts.append(text)
            
            if posts:
                # Берём уникальные
                unique_posts = []
                seen = set()
                for p in posts:
                    if p not in seen:
                        seen.add(p)
                        unique_posts.append(p)
                posts = unique_posts[:15]
            
            desc = ""
            for d in soup.find_all(["div", "p"], class_=["group_info", "page_info", "group_description", "info"]):
                desc += d.get_text(strip=True) + "\n"
            
            if posts or desc:
                result = ""
                if desc:
                    result += "ОПИСАНИЕ:\n" + desc + "\n\n"
                if posts:
                    result += "ПОСТЫ:\n" + "\n---\n".join(posts[:10])
                logger.info(f"✅ VK mobile: {len(result)} chars, {len(posts)} posts")
                return result[:5000]
        
        # Способ 2: публичная страница
        public_url = f"https://vk.com/{vk}"
        r = _request_with_retry(public_url, timeout=45)
        if r:
            soup = BeautifulSoup(r.text, "lxml")
            text = soup.get_text(separator="\n", strip=True)
            if len(text) > 500:
                logger.info(f"✅ VK public: {len(text)} chars")
                return text[:5000]
        
        return None
    except Exception as e:
        logger.error(f"❌ VK error: {e}")
        return None

def search_competitors(q, max_results=5):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{q} цены отзывы", region="ru-ru", max_results=max_results))
            return [{"title": r.get("title", ""), "snippet": r.get("body", "")[:200]} for r in results]
    except Exception as e:
        logger.error(f"❌ Search error: {e}")
        return []

def get_yandex_suggestions(kw):
    try:
        r = requests.get("https://suggest.yandex.net/suggest-ff.cgi",
                         params={"part": kw, "lang": "ru", "v": "3"},
                         headers={"User-Agent": USER_AGENTS[0]}, timeout=10)
        data = r.json()
        return data[1] if isinstance(data, list) and len(data) > 1 else []
    except Exception as e:
        logger.error(f"❌ Yandex error: {e}")
        return []

# === SUPABASE ===
def get_or_create_user(uid, username, fname):
    try:
        result = supabase.table("users").select("*").eq("id", uid).execute()
        if result.data:
            return result.data[0]
        u = {"id": uid, "username": username or "", "first_name": fname or ""}
        supabase.table("users").insert(u).execute()
        return u
    except Exception as e:
        logger.error(f"❌ User error: {e}")
        return {"id": uid}

def save_answer(uid, q, a):
    try:
        supabase.table("onboarding_answers").insert({"user_id": uid, "question": q, "answer": str(a)[:1500]}).execute()
    except Exception as e:
        logger.error(f"❌ Save answer: {e}")

def save_research(uid, kw, pains, utp, comp):
    try:
        supabase.table("niche_research").insert({"user_id": uid, "keywords": str(kw)[:8000], "pains": str(pains)[:5000], "utp": str(utp)[:2000], "competitors": str(comp)[:3000]}).execute()
    except Exception as e:
        logger.error(f"❌ Save research: {e}")

def save_article(uid, kw, pain, title, content):
    try:
        supabase.table("articles").insert({"user_id": uid, "keyword": str(kw)[:500], "pain": str(pain)[:500], "title": str(title)[:500], "content": str(content)[:15000], "status": "draft"}).execute()
    except Exception as e:
        logger.error(f"❌ Save article: {e}")

def log_usage(uid, action):
    try:
        supabase.table("usage_logs").insert({"user_id": uid, "action": action}).execute()
    except Exception:
        pass

# === QWEN ===
def ask_qwen(prompt, max_tokens=1200, system=None):
    sys_msg = system or SYSTEM_PROMPT
    for attempt in range(3):
        try:
            logger.info(f"🤖 Qwen attempt {attempt+1}...")
            r = groq_client.chat.completions.create(
                model="qwen/qwen3.8-27b",
                messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt[:15000]}],
                max_tokens=max_tokens, temperature=0.7, timeout=90
            )
            result = r.choices[0].message.content
            logger.info(f"✅ Qwen: {len(result)} chars")
            return result
        except Exception as e:
            logger.error(f"❌ Qwen fail {attempt+1}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return f"⚠️ Нейросеть недоступна. Попробуй позже."

# === ОТПРАВКА ===
async def send_long(message, text):
    MAX = 4000
    try:
        if len(text) <= MAX:
            try:
                await message.answer(text, parse_mode="Markdown")
            except Exception:
                await message.answer(text)
        else:
            for part in [text[i:i+MAX] for i in range(0, len(text), MAX)]:
                try:
                    await message.answer(part, parse_mode="Markdown")
                except Exception:
                    await message.answer(part)
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

# === ИНТЕРВЬЮ ===
@dp.message(Onboarding.gathering)
async def live_interview(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    logger.info(f"💬 Response from {uid}: {message.text[:50]}...")
    data = await state.get_data()
    history = data.get("history", [])
    qn = data.get("question_num", 1)
    src_req = data.get("source_requested", False)

    if qn == 1 and is_banned(message.text):
        await message.answer("❌ Не работаю с этой темой. /start")
        await state.clear()
        return

    history.append({"q": qn, "a": message.text})
    save_answer(uid, f"Q{qn}", message.text)

    if qn == 1:
        check = ask_qwen(f'Клиент: "{message.text}"\nНесколько РАЗНЫХ услуг? Ответь "YES|у1|у2" или "NO"', max_tokens=100)
        if check.startswith("YES|"):
            services = [s.strip() for s in check.split("|")[1:] if s.strip()]
            if services:
                await state.update_data(history=history, question_num=2, multiple_services=services)
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=s, callback_data=f"svc_{i}")] for i, s in enumerate(services)])
                await message.answer(
                    f"🎯 **Вижу несколько направлений:**\n" + "\n".join([f"• {s}" for s in services]) +
                    f"\n\nЭто **разные ниши**. По какой делаем анализ первым?",
                    reply_markup=kb, parse_mode="Markdown"
                )
                await state.set_state(Onboarding.service_priority)
                return

    if qn >= 4 and not src_req:
        await state.update_data(history=history, question_num=qn + 1, source_requested=True)
        await message.answer(
            "✅ **Уже многое понял!**\n\n"
            "🔗 Пришли ссылки на источники (можно несколько):\n"
            "• 🌐 Сайт\n• ✈️ Telegram-канал\n• 📱 Авито\n• 💬 ВКонтакте\n\n"
            "Изучу каждый. Нет источника — напиши **«нет»**.",
            parse_mode="Markdown"
        )
        await state.set_state(Onboarding.source_link)
        await state.update_data(waiting_manual=False)
        return

    if qn >= 8:
        await finish_interview(message, state, history)
        return

    hist = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    pri = data.get("priority_service", "")
    pq = f"История ({qn} вопросов):\n{hist}\n" + (f"Приоритет: {pri}\n" if pri else "") + "\nЗадай следующий вопрос (открытый). ТОЛЬКО вопрос."
    nq = ask_qwen(pq, max_tokens=200)
    if nq.startswith("⚠️"):
        await message.answer(f"⚠️ {nq}\nПопробуй ещё раз или /start")
        return
    await state.update_data(history=history, question_num=qn + 1)
    await message.answer(f"❓ **Вопрос {qn + 1}:**\n{nq}", parse_mode="Markdown")

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
    ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    nq = ask_qwen(f"Клиент выбрал: {pri}\nБизнес: {ht}\nВопрос о страхах. ТОЛЬКО вопрос.", max_tokens=200)
    if not nq.startswith("⚠️"):
        await callback.message.answer(f"❓ **Вопрос 2:**\n{nq}", parse_mode="Markdown")
    await callback.answer()

# === ИСТОЧНИКИ ===
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
        analysis = ask_qwen(f"""Клиент прислал: {src}
Интервью: {ht}
Приоритет: {pri or 'не указан'}

Проанализируй ЧЕСТНО:
📌 Сильные стороны (3-4)
📌 Слабые места (2-3)
📌 Стиль общения
📌 Что использовать в статьях
Задай 2-3 вопроса.""", max_tokens=700)
        if not analysis.startswith("⚠️"):
            await send_long(message, f"✅ **Изучил!**\n\n{analysis}")
        await state.update_data(history=history, question_num=data.get("question_num", 4), source_requested=True, source_data=src[:2000], waiting_manual=False)
        await state.set_state(Onboarding.gathering)
        return

    answer = message.text.strip()
    save_answer(uid, "source_link", answer)

    if answer.lower() in ["нет", "нету", "-", "0", "нет источника"]:
        await message.answer("👌 Работаем без источника.", parse_mode="Markdown")
        await state.update_data(source_requested=True, source_data=None, waiting_manual=False)
        await state.set_state(Onboarding.gathering)
        await continue_interview(message, state, history)
        return

    urls = extract_urls(answer)
    if not urls:
        if answer.isdigit():
            urls = [f"https://www.avito.ru/user/{answer}/shop"]
        else:
            urls = [answer]

    await message.answer(f"⏳ **Нашёл {len(urls)} источник(ов). Изучаю каждый (это может занять 30-60 сек)...**", parse_mode="Markdown")

    all_data = ""
    results = []
    for url in urls:
        logger.info(f"🔍 Parsing: {url}")
        src_text, src_type = parse_source(url)
        if src_text:
            results.append(f"✅ **{src_type}** — изучен!")
            all_data += f"\n=== {src_type}: {url} ===\n{src_text}\n"
        else:
            results.append(f"⚠️ **{src_type}** ({url}) — не открылся (сайт блокирует зарубежные серверы)")

    results_text = "\n".join(results)

    if all_data:
        save_answer(uid, "source_data", all_data[:3000])
        ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
        pri = data.get("priority_service", "")
        analysis = ask_qwen(f"""Данные источников:
{all_data[:5000]}

Интервью: {ht}
Приоритет: {pri or 'не указан'}

Проанализируй ЧЕСТНО:
📌 Сильные стороны (3-4)
📌 Слабые места (2-3)
📌 Стиль общения
📌 Что использовать в статьях
Задай 2-3 вопроса.""", max_tokens=700)
        if not analysis.startswith("⚠️"):
            await send_long(message, f"📊 **Результаты:**\n{results_text}\n\n{analysis}")
        else:
            await send_long(message, f"📊 **Результаты:**\n{results_text}\n\n{analysis}")
        await state.update_data(history=history, question_num=data.get("question_num", 4), source_requested=True, source_data=all_data[:3000], waiting_manual=False)
    else:
        # Ни один не открылся — честно объясняем причину и просим текст
        await message.answer(
            f"📊 **Результаты изучения:**\n{results_text}\n\n"
            f"⚠️ **Почему так:** мой сервер находится в Германии, "
            f"а многие российские сайты (Авито, ВК, некоторые сайты) "
            f"блокируют зарубежные запросы или отвечают слишком медленно.\n\n"
            f"💡 **Что делать:** просто **скопируй текст** со своих источников "
            f"(описание, цены, услуги) и пришли мне одним сообщением — "
            f"я его изучу и сделаю такой же качественный анализ.\n\n"
            f"Это займёт 1 минуту, и результат будет не хуже.",
            parse_mode="Markdown"
        )
        await state.update_data(waiting_manual=True)
        return

    await state.set_state(Onboarding.gathering)

async def continue_interview(message, state, history):
    data = await state.get_data()
    qn = data.get("question_num", 5)
    if qn >= 8:
        await finish_interview(message, state, history)
        return
    ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    nq = ask_qwen(f"История: {ht}\nЗадай следующий вопрос. ТОЛЬКО вопрос.", max_tokens=200)
    if not nq.startswith("⚠️"):
        await state.update_data(history=history, question_num=qn + 1)
        await message.answer(f"❓ **Вопрос {qn + 1}:**\n{nq}", parse_mode="Markdown")

# === ЗАВЕРШЕНИЕ ===
async def finish_interview(message, state, history):
    uid = message.from_user.id
    data = await state.get_data()
    pri = data.get("priority_service", "")
    src = data.get("source_data", "")
    await message.answer("✅ **Интервью завершено!**\n⏳ Анализирую... **1-2 мин**.", parse_mode="Markdown")

    ht = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    nq = pri if pri else (history[0]["a"] if history else "")
    competitors = search_competitors(nq[:80])
    suggestions = get_yandex_suggestions(nq[:30])
    comp_text = "\n".join([f"- {c['title']}: {c['snippet']}" for c in competitors[:5]]) if competitors else "нет"
    sugg_text = ", ".join(suggestions[:10]) if suggestions else "пусто"
    src_sec = f"\n=== ИСТОЧНИК ===\n{src}\n" if src else ""

    analysis = ask_qwen(f"""Интервью: {ht}{src_sec}
Приоритет: {pri or 'нет'}
Конкуренты: {comp_text}
Яндекс: {sugg_text}

1. 15 ключей
2. 10 болей ЦА
3. УТП
4. Минусы

=== 🎯 КЛЮЧИ ===
=== 💥 БОЛИ ===
=== ⭐ УТП ===
=== ⚠️ МИНУСЫ ===""", max_tokens=1200)

    if analysis.startswith("⚠️"):
        await message.answer(f"⚠️ {analysis}")
        return

    plan = ask_qwen(f"""Анализ: {analysis}
Контент-план на 7 дней для Дзен. 1 статья = 1 ключ + 1 боль. День + ЧАС.
=== 📅 ПЛАН ===""", max_tokens=900)

    guide = ask_qwen("""Инструкция чайнику: публикация в Дзен. 5 шагов. Аналогия с магазином.""", max_tokens=800)

    save_research(uid, analysis + "\n\n" + plan, "", "", comp_text)

    await send_long(message, f"🎯 **АНАЛИЗ НИШИ:**\n\n{analysis}")
    await asyncio.sleep(1)
    if not plan.startswith("⚠️"):
        await send_long(message, f"📅 **ПЛАН:**\n\n{plan}")
    await asyncio.sleep(1)
    if not guide.startswith("⚠️"):
        await send_long(message, f"📚 **ДЗЕН:**\n\n{guide}")
    await asyncio.sleep(1)

    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📸 Фото")], [KeyboardButton(text="⏭ Пропустить")]], resize_keyboard=True)
    await message.answer("📸 **Загрузить фото работ?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(Onboarding.photos)

# === ФОТО ===
@dp.message(Onboarding.photos, F.text.in_(["⏭ Пропустить", "Позже", "позже", "Не сейчас", "нет", "Нет"]))
async def skip_photos(message, state: FSMContext):
    await message.answer("👌 Генерирую сам.")
    await ask_cta(message, state)

@dp.message(Onboarding.photos, F.text == "📸 Фото")
async def req_photos(message, state: FSMContext):
    await message.answer("📸 Пришли фото. Закончишь — **«готово»** или **«позже»**.", parse_mode="Markdown")

@dp.message(Onboarding.photos, F.text.lower() == "готово")
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
    try:
        supabase.table("users").update({"cta_type": ct}).eq("id", callback.from_user.id).execute()
    except Exception:
        pass
    prompts = {"site": "🌐 Ссылка на форму", "tg": "✈️ @username", "wa": "📱 Номер +7...", "max": "📣 Ссылка МАХ", "phone": "📞 Номер"}
    await state.update_data(cta_type=ct)
    await state.set_state(Onboarding.cta_value)
    await callback.message.answer(f"✅ {ct}\n{prompts.get(ct, '')}", parse_mode="Markdown")
    await callback.answer()

@dp.message(Onboarding.cta_value)
async def cta_value(message, state: FSMContext):
    try:
        supabase.table("users").update({"cta_value": message.text.strip()}).eq("id", message.from_user.id).execute()
    except Exception:
        pass
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

    await message.answer("✍️ **Пишу...** 1-2 мин.", parse_mode="Markdown")
    article = ask_qwen(f"""Анализ: {analysis}
SEO-статья: 1 ключ + 1 боль. Заголовок, вступление с болью, подзаголовки, 5-7 тыс знаков, мягкий призыв.
В конце: Title, Description, хэштеги, Slug.
=== 📄 СТАТЬЯ ===
=== 🏷 SEO ===""", max_tokens=1400)

    if article.startswith("⚠️"):
        await message.answer(f"⚠️ {article}")
        return
    save_article(uid, "статья", "боль", "статья", article)
    log_usage(uid, "article")
    await send_long(message, f"📄 **СТАТЬЯ:**\n\n{article}")
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
        if not a.data:
            return
        orig = a.data[0]["content"]
        aid = a.data[0]["id"]
    except Exception:
        return
    vc = ask_qwen(f"Адаптируй под vc.ru: {orig}\nКейс/факап. Тон: коллега. 7-12 тыс.", max_tokens=1400)
    try:
        supabase.table("articles").update({"vc_version": vc}).eq("id", aid).execute()
    except Exception:
        pass
    if not vc.startswith("⚠️"):
        await send_long(callback.message, f"📰 **vc.ru:**\n\n{vc}")
    await callback.answer()

@dp.callback_query(F.data == "vc_no")
async def skip_vc(callback):
    await callback.message.answer("👌 Дзен.")
    await callback.answer()

# === HELP / ADMIN ===
@dp.message(Command("help"))
async def cmd_help(message):
    await message.answer("📋 /start — заново | /article — статья | /ping — жив? | /help — справка")

@dp.message(Command("admin"))
async def cmd_admin(message):
    if message.from_user.id != ADMIN_ID:
        return
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
