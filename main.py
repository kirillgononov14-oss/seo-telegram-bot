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

for var_name, var_value in [
    ("BOT_TOKEN", BOT_TOKEN),
    ("GROQ_API_KEY", GROQ_API_KEY),
    ("SUPABASE_URL", SUPABASE_URL),
    ("SUPABASE_KEY", SUPABASE_KEY),
]:
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

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Используем ту модель, которая реально видна в твоём Groq Usage
MODELS = [
    "qwen/qwen3.8-27b",
]

BASE_SYSTEM = """Ты — элитный SEO-стратег и контент-маркетолог с 15-летним опытом.
СТРОГИЕ ПРАВИЛА:
1. Пиши ТОЛЬКО на русском.
2. НЕ используй ### или ##.
3. Используй эмодзи для структуры.
4. Выделяй важное **жирным**.
5. Ответ должен быть полным, но без воды.
6. НИКОГДА не задавай вопросы клиенту, если в задаче прямо не сказано задать вопрос."""

RECON_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Краткая разведка бизнеса.
Максимум 450 слов.
Только:
- сильные стороны
- слабые места
- стиль общения
- 3 идеи для статей
БЕЗ вопросов клиенту. БЕЗ воды."""

FINAL_ANALYSIS_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Полный SEO-анализ ниши.
Максимум 900 слов.
Структура:
🎯 КЛЮЧИ
💥 БОЛИ
⭐ УТП
⚠️ МИНУСЫ
БЕЗ вопросов клиенту."""

QUESTION_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Задать ОДИН вопрос для интервью.
Правила:
- Только ОДИН вопрос.
- Открытый вопрос, не да/нет.
- Без нумерации.
- Без эмодзи.
- Без слова "Вопрос:".
- Максимум 180 слов."""

PLAN_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Контент-план на 7 дней для Дзен.
Максимум 550 слов.
Формат:
📅 ПЛАН НА 7 ДНЕЙ
1. ...
2. ...
БЕЗ вопросов."""

GUIDE_SYSTEM = BASE_SYSTEM + """

ЗАДАЧА: Инструкция для чайника по публикации SEO-статьи в Дзен.
Максимум 450 слов.
5 шагов.
Объясни Title, Description, H1, alt через аналогию с магазином.
БЕЗ вопросов."""


def groq_request(prompt, max_tokens=1200, system=None):
    sys_msg = system or BASE_SYSTEM

    for model in MODELS:
        try:
            logger.info(f"🤖 Groq: trying {model}, max_tokens={max_tokens}...")
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": prompt[:15000]},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }

            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=120)

            if r.status_code == 200:
                data = r.json()
                result = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if result and result.strip():
                    logger.info(f"✅ Groq OK ({model}): {len(result)} chars")
                    return result.strip()
                logger.warning(f"⚠️ Groq empty response ({model})")

            elif r.status_code == 429:
                logger.warning(f"⚠️ Groq rate limit ({model}), waiting 5 sec...")
                time.sleep(5)
                continue

            else:
                logger.error(f"❌ Groq {r.status_code} ({model}): {r.text[:300]}")

        except requests.exceptions.Timeout:
            logger.error(f"⏱️ Groq timeout ({model})")
            continue
        except Exception as e:
            logger.error(f"❌ Groq error ({model}): {type(e).__name__}: {e}")
            continue

    logger.error("❌ ALL GROQ MODELS FAILED")
    return None


async def agroq(prompt, max_tokens=1200, system=None):
    return await asyncio.to_thread(groq_request, prompt, max_tokens, system)


def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Продолжить"), KeyboardButton(text="📝 Новая статья")],
            [KeyboardButton(text="🔄 Начать заново"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Нажми кнопку или напиши сообщение...",
    )


MENU_CONTINUE = "▶️ Продолжить"
MENU_ARTICLE = "📝 Новая статья"
MENU_RESTART = "🔄 Начать заново"
MENU_HELP = "❓ Помощь"

MIN_QUESTIONS = 8
MAX_QUESTIONS = 15


def progress_text(answered):
    return f"📊 Ответов: {answered}/{MAX_QUESTIONS} (минимум {MIN_QUESTIONS})"


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
                if hasattr(event, "message") and event.message:
                    msg = event.message
                elif hasattr(event, "callback_query") and event.callback_query:
                    msg = event.callback_query.message

                if msg:
                    await msg.answer(
                        "⚠️ Произошла ошибка. Нажми ▶️ Продолжить в меню или напиши /start.",
                        reply_markup=get_main_menu(),
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


def test_groq():
    logger.info("🧪 Testing Groq connection...")
    result = groq_request("Ответь одним словом: привет", max_tokens=10)
    if result:
        logger.info(f"✅ GROQ TEST PASSED: {result[:80]}")
        return True
    logger.error("❌ GROQ TEST FAILED! Check API key, model access or limits.")
    return False


# =====================
# SCRAPERAPI / PARSING
# =====================

def scrape_with_api(url, premium=False, render=False, country_code="ru"):
    if not SCRAPER_API_KEY:
        logger.error("❌ SCRAPER_API_KEY missing")
        return None

    params = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "country_code": country_code,
    }
    if premium:
        params["premium"] = "true"
    if render:
        params["render"] = "true"

    try:
        logger.info(f"🌐 Scraping {url[:80]}... premium={premium}, render={render}")
        r = requests.get("http://api.scraperapi.com", params=params, timeout=60)
        if r.status_code == 200 and len(r.text) > 200:
            low = r.text.lower()
            if ("captcha" in low or "robot" in low) and len(r.text) < 1500:
                logger.warning(f"⚠️ Captcha/robot page for {url[:80]}")
                return None
            logger.info(f"✅ Scraped {len(r.text)} chars")
            return r.text
        logger.warning(f"⚠️ ScraperAPI status {r.status_code}, len={len(r.text)}")
        return None
    except Exception as e:
        logger.error(f"❌ Scrape error: {e}")
        return None


def scrape_google_cache(url):
    try:
        r = requests.get(
            f"https://webcache.googleusercontent.com/search?q=cache:{url}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        if r.status_code == 200 and len(r.text) > 500:
            logger.info(f"✅ Google Cache: {len(r.text)} chars")
            return r.text
        return None
    except Exception as e:
        logger.warning(f"⚠️ Google Cache failed: {e}")
        return None


def scrape_web_archive(url):
    try:
        r = requests.get(
            f"https://web.archive.org/web/2024/{url}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        if r.status_code == 200 and len(r.text) > 500:
            logger.info(f"✅ Web Archive: {len(r.text)} chars")
            return r.text
        return None
    except Exception as e:
        logger.warning(f"⚠️ Web Archive failed: {e}")
        return None


def extract_urls(text):
    return re.findall(r"https?://[^\s,;]+", text)


def detect_source_type(url):
    u = url.lower()
    if "avito.ru" in u:
        return "avito"
    if "vk.com" in u or "vk.ru" in u:
        return "vk"
    if "t.me/" in u:
        return "telegram"
    return "site"


def source_display_name(st):
    return {
        "avito": "Авито",
        "vk": "ВКонтакте",
        "telegram": "Telegram",
        "site": "Сайт",
    }.get(st, "Источник")


def parse_source(url):
    st = detect_source_type(url)
    if st == "avito":
        return extract_avito(url), "Авито"
    if st == "vk":
        return extract_vk_group(url), "ВКонтакте"
    if st == "telegram":
        return extract_telegram_channel(url), "Telegram"
    return extract_site_text(url), "Сайт"


def extract_site_text(url):
    try:
        if not url.startswith("http"):
            url = "https://" + url

        html = scrape_with_api(url, premium=False, render=False)
        if not html:
            html = scrape_google_cache(url)
        if not html:
            html = scrape_web_archive(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "lxml")
        for s in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            s.decompose()

        text = soup.get_text(separator="\n", strip=True)
        return text[:5000] if len(text) > 100 else None
    except Exception as e:
        logger.error(f"❌ Site parse error: {e}")
        return None


def extract_telegram_channel(url):
    try:
        ch = url.replace("https://t.me/", "").replace("t.me/", "").strip("/").split("/")[0]
        public_url = f"https://t.me/s/{ch}"

        html = scrape_with_api(public_url, premium=False, render=False)
        if not html:
            html = scrape_google_cache(public_url)
        if not html:
            return None

        soup = BeautifulSoup(html, "lxml")
        posts = [
            p.get_text(strip=True)
            for p in soup.find_all("div", class_="tgme_widget_message_text")
            if p.get_text(strip=True)
        ]
        return "\n\n".join(posts[:10])[:5000] if posts else None
    except Exception as e:
        logger.error(f"❌ TG parse error: {e}")
        return None


def extract_avito(url):
    try:
        if not url.startswith("http"):
            url = "https://" + url

        variants = [
            (url, True, True),
            (url.replace("www.avito.ru", "m.avito.ru"), True, True),
            (url, True, False),
        ]

        for v_url, prem, rend in variants:
            html = scrape_with_api(v_url, premium=prem, render=rend)
            if not html:
                continue

            soup = BeautifulSoup(html, "lxml")
            parts = []

            for tag in soup.find_all(["h1", "h2", "h3"]):
                t = tag.get_text(strip=True)
                if t and len(t) > 5:
                    parts.append(t)

            for tag in soup.find_all(["div", "p", "span"]):
                t = tag.get_text(strip=True)
                if len(t) > 20 and any(
                    w in t.lower()
                    for w in ["руб", "₽", "дом", "м²", "участок", "этаж", "площадь", "окно", "двер", "утепл"]
                ):
                    if t not in parts:
                        parts.append(t)

            result = "\n".join(parts)
            if len(result) > 200:
                logger.info(f"✅ Avito extracted: {len(result)} chars")
                return result[:5000]

            for s in soup(["script", "style"]):
                s.decompose()
            full = soup.get_text(separator="\n", strip=True)
            if len(full) > 500:
                logger.info(f"✅ Avito full extracted: {len(full)} chars")
                return full[:5000]

        html = scrape_google_cache(url)
        if html:
            soup = BeautifulSoup(html, "lxml")
            for s in soup(["script", "style"]):
                s.decompose()
            text = soup.get_text(separator="\n", strip=True)
            if len(text) > 500:
                logger.info(f"✅ Avito via Google Cache: {len(text)} chars")
                return text[:5000]

        return None
    except Exception as e:
        logger.error(f"❌ Avito parse error: {e}")
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
        ]

        for v_url, prem, rend in variants:
            html = scrape_with_api(v_url, premium=prem, render=rend)
            if not html:
                continue

            soup = BeautifulSoup(html, "lxml")
            posts = []

            for p in soup.find_all(["div", "p"]):
                t = p.get_text(strip=True)
                if 40 < len(t) < 2000 and not any(
                    skip in t.lower()
                    for skip in ["cookie", "войти", "зарегистрироваться", "браузер", "приложение"]
                ):
                    posts.append(t)

            unique = list(dict.fromkeys(posts))[:15]

            desc = ""
            for d in soup.find_all(
                ["div", "p"],
                class_=["group_info", "page_info", "group_description", "info"],
            ):
                desc += d.get_text(strip=True) + "\n"

            if unique or desc:
                result = ""
                if desc:
                    result += "ОПИСАНИЕ:\n" + desc + "\n\n"
                if unique:
                    result += "ПОСТЫ:\n" + "\n---\n".join(unique[:10])
                if len(result) > 200:
                    logger.info(f"✅ VK extracted: {len(result)} chars, posts={len(unique)}")
                    return result[:5000]

            for s in soup(["script", "style"]):
                s.decompose()
            full = soup.get_text(separator="\n", strip=True)
            if len(full) > 500:
                logger.info(f"✅ VK full extracted: {len(full)} chars")
                return full[:5000]

        for fallback_url in [f"https://vk.com/{vk}"]:
            for fn in [scrape_google_cache, scrape_web_archive]:
                html = fn(fallback_url)
                if html:
                    soup = BeautifulSoup(html, "lxml")
                    for s in soup(["script", "style"]):
                        s.decompose()
                    full = soup.get_text(separator="\n", strip=True)
                    if len(full) > 500:
                        logger.info(f"✅ VK fallback extracted: {len(full)} chars")
                        return full[:5000]

        return None
    except Exception as e:
        logger.error(f"❌ VK parse error: {e}")
        return None


async def parse_one_source(url):
    st = detect_source_type(url)
    name = source_display_name(st)
    try:
        result = await asyncio.wait_for(asyncio.to_thread(parse_source, url), timeout=110)
        text, typ = result
        return url, typ or name, text
    except asyncio.TimeoutError:
        logger.warning(f"⏱️ Parse timeout for {url}")
        return url, name, None
    except Exception as e:
        logger.error(f"❌ Parse exception for {url}: {e}")
        return url, name, None


def search_competitors(q, max_results=5):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{q} цены отзывы", region="ru-ru", max_results=max_results))
            return [
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("body", "")[:200],
                }
                for r in results
            ]
    except Exception as e:
        logger.error(f"❌ Search error: {e}")
        return []


def get_yandex_suggestions(kw):
    try:
        r = requests.get(
            "https://suggest.yandex.net/suggest-ff.cgi",
            params={"part": kw, "lang": "ru", "v": "3"},
            timeout=10,
        )
        data = r.json()
        return data[1] if isinstance(data, list) and len(data) > 1 else []
    except Exception as e:
        logger.error(f"❌ Yandex suggest error: {e}")
        return []


# =====================
# SUPABASE
# =====================

def get_or_create_user(uid, username, fname):
    try:
        result = supabase.table("users").select("*").eq("id", uid).execute()
        if result.data:
            return result.data[0]
        u = {"id": uid, "username": username or "", "first_name": fname or ""}
        supabase.table("users").insert(u).execute()
        return u
    except Exception as e:
        logger.error(f"❌ User save error: {e}")
        return {"id": uid}


def save_answer(uid, q, a):
    try:
        supabase.table("onboarding_answers").insert(
            {
                "user_id": uid,
                "question": q,
                "answer": str(a)[:1500],
            }
        ).execute()
    except Exception as e:
        logger.error(f"❌ Answer save error: {e}")


def save_research(uid, kw, pains, utp, comp):
    try:
        supabase.table("niche_research").insert(
            {
                "user_id": uid,
                "keywords": str(kw)[:8000],
                "pains": str(pains)[:5000],
                "utp": str(utp)[:2000],
                "competitors": str(comp)[:3000],
            }
        ).execute()
    except Exception as e:
        logger.error(f"❌ Research save error: {e}")


def save_article(uid, kw, pain, title, content):
    try:
        supabase.table("articles").insert(
            {
                "user_id": uid,
                "keyword": str(kw)[:500],
                "pain": str(pain)[:500],
                "title": str(title)[:500],
                "content": str(content)[:15000],
                "status": "draft",
            }
        ).execute()
    except Exception as e:
        logger.error(f"❌ Article save error: {e}")


def log_usage(uid, action):
    try:
        supabase.table("usage_logs").insert({"user_id": uid, "action": action}).execute()
    except Exception:
        pass


# =====================
# HELPERS
# =====================

async def send_long(message, text, reply_markup=None):
    if not text:
        return False

    MAX = 3500
    try:
        if len(text) <= MAX:
            try:
                await message.answer(text, parse_mode="Markdown", reply_markup=reply_markup)
            except Exception:
                await message.answer(text, reply_markup=reply_markup)
            return True

        parts = [text[i:i + MAX] for i in range(0, len(text), MAX)]
        for idx, part in enumerate(parts):
            rm = reply_markup if idx == 0 else None
            try:
                await message.answer(part, parse_mode="Markdown", reply_markup=rm)
            except Exception:
                await message.answer(part, reply_markup=rm)
            await asyncio.sleep(0.4)
        return True
    except Exception as e:
        logger.error(f"❌ send_long error: {e}")
        try:
            await message.answer("⚠️ Не удалось отправить длинное сообщение. Нажми ▶️ Продолжить.", reply_markup=get_main_menu())
        except Exception:
            pass
        return False


def is_banned(text):
    t = text.lower()
    return any(w in t for w in BANNED_NICHES)


USER_LOCKS: Dict[int, asyncio.Lock] = {}


def get_user_lock(uid: int) -> asyncio.Lock:
    if uid not in USER_LOCKS:
        USER_LOCKS[uid] = asyncio.Lock()
    return USER_LOCKS[uid]


async def busy_answer(message):
    await message.answer(
        "⏳ Я ещё обрабатываю предыдущий шаг. Подожди 10–90 секунд.\n"
        "Если совсем зависло — напиши /start.",
        reply_markup=get_main_menu(),
    )


FALLBACK_QUESTIONS = [
    "Что продаёшь? Опиши в 2–3 предложениях.",
    "Кто твой идеальный клиент и почему он выбирает именно тебя, а не конкурентов?",
    "Какие 3 главных возражения или страха возникают у клиента перед покупацией?",
    "Из-за чего чаще всего срывается сделка или клиент перестаёт отвечать?",
    "Какие услуги или товары приносят больше всего прибыли, а какие только привлекают клиента?",
    "Есть ли у тебя кейс с цифрами: было/стало, срок, результат?",
    "Чем ты принципиально не занимаешься и почему это важно для клиента?",
    "Как клиент узнаёт о тебе впервые: поиск, Авито, соцсети, сарафан?",
    "Какой самый частый вопрос в переписке, после которого клиент пропадает?",
    "Что входит в минимальную цену и что почти всегда становится дополнительной оплатой?",
    "Какие ошибки клиентов приводят к плохому результату и как ты их предупреждаешь?",
    "Есть ли сезонность и как ты заполняешь низкий сезон?",
    "Кого ты считаешь главным конкурентом и в чём его слабое место?",
    "Какой один результат клиент получает гарантированно и как ты это подтверждаешь?",
    "Что ты хочешь, чтобы клиент сделал после прочтения статьи: позвонил, написал, приехал, оставил заявку?",
]


def fallback_question(answered: int) -> str:
    idx = min(max(answered, 0), len(FALLBACK_QUESTIONS) - 1)
    return FALLBACK_QUESTIONS[idx]


def clean_question_text(text: str) -> str:
    text = text.strip()
    text = text.replace("**", "").strip()

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if lines:
        text = lines[0]

    prefixes = [
        "Вопрос:", "Вопрос :", "Q:", "q:", "В:", "?", "❓", "1.", "2.", "3.",
    ]
    for p in prefixes:
        if text.lower().startswith(p.lower()):
            text = text[len(p):].strip()

    return text.strip()


async def generate_next_action(history, source_data, priority):
    answered = len(history)

    if answered >= MAX_QUESTIONS:
        return "FINISH", None

    hist_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history]) if history else "Пока нет ответов."
    source_text = (source_data or "")[:3500]

    if answered < MIN_QUESTIONS:
        mode_instruction = (
            "Ответов пока меньше минимума. Обязательно задай ОДИН следующий вопрос. "
            "НЕ отвечай STOP."
        )
    else:
        mode_instruction = (
            f"Ответов уже {answered}. Минимум {MIN_QUESTIONS}, максимум {MAX_QUESTIONS}. "
            "Если данных достаточно, чтобы сделать сильный SEO-анализ и контент-план, ответь ровно STOP. "
            "Если данных мало — задай ОДИН уточняющий вопрос."
        )

    prompt = f"""{mode_instruction}

Приоритетная услуга: {priority or 'не указана'}

История интервью:
{hist_text}

Данные из источников клиента:
{source_text if source_text else 'источники не получены'}

Верни либо ровно STOP, либо один вопрос без префиксов."""

    res = await agroq(prompt, max_tokens=220, system=QUESTION_SYSTEM)

    if not res:
        return "QUESTION", fallback_question(answered)

    clean = res.strip()

    if answered >= MIN_QUESTIONS and clean.upper().startswith("STOP"):
        return "FINISH", None

    clean = clean_question_text(clean)

    if len(clean) < 10:
        return "QUESTION", fallback_question(answered)

    return "QUESTION", clean


async def ask_next_interview_question_internal(message: types.Message, state: FSMContext):
    data = await state.get_data()
    history = data.get("history", [])
    source_data = data.get("source_data", "")
    priority = data.get("priority_service", "")

    action, text = await generate_next_action(history, source_data, priority)

    if action == "FINISH":
        await finish_interview(message, state, history)
        return

    next_num = len(history) + 1
    await message.answer(
        f"{progress_text(len(history))}\n\n"
        f"❓ **Вопрос {next_num}:**\n{text}\n\n"
        "💡 Не знаешь — напиши «не знаю» или «пропустить».",
        parse_mode="Markdown",
    )


# =====================
# COMMANDS / MENU
# =====================

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    logger.info(f"🚀 /start from {uid}")

    await state.clear()
    get_or_create_user(uid, message.from_user.username, message.from_user.first_name)

    await message.answer(
        "👋 **Привет! Я — твой SEO-стратег.**\n\n"
        "Проведу интервью, изучу нишу и буду писать статьи.\n"
        f"⏱ Обычно 5–10 минут. Вопросы: от {MIN_QUESTIONS} до {MAX_QUESTIONS}, зависит от глубины ниши.\n\n"
        f"{progress_text(0)}\n\n"
        "❓ **Вопрос 1:**\nЧто продаёшь? Опиши в 2–3 предложениях.\n\n"
        "💡 Используй кнопки в меню внизу.",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )

    await state.set_state(Onboarding.gathering)
    await state.update_data(
        history=[],
        source_requested=False,
        source_data="",
        priority_service="",
        waiting_manual=False,
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📋 **Меню и команды:**\n\n"
        "• ▶️ Продолжить — продолжить с последнего места\n"
        "• 📝 Новая статья — сгенерировать статью\n"
        "• 🔄 Начать заново — начать интервью заново\n"
        "• ❓ Помощь — эта справка\n"
        "• /ping — проверить, жив ли бот\n"
        "• /continue — текстовая команда Продолжить",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


@dp.message(F.text == MENU_RESTART)
async def btn_restart_kb(message: types.Message, state: FSMContext):
    await cmd_start(message, state)


@dp.message(F.text == MENU_HELP)
async def btn_help_kb(message: types.Message):
    await cmd_help(message)


@dp.message(F.text == MENU_CONTINUE)
async def btn_continue_kb(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    lock = get_user_lock(uid)
    if lock.locked():
        await busy_answer(message)
        return

    async with lock:
        await cmd_continue_logic(message, state)


@dp.message(F.text == MENU_ARTICLE)
async def btn_article_kb(message: types.Message):
    uid = message.from_user.id
    lock = get_user_lock(uid)
    if lock.locked():
        await busy_answer(message)
        return

    async with lock:
        await gen_article_logic(message)


@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    await message.answer(f"✅ Бот жив! Server time: {int(time.time())}", reply_markup=get_main_menu())


@dp.message(Command("continue"))
async def cmd_continue(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    lock = get_user_lock(uid)
    if lock.locked():
        await busy_answer(message)
        return

    async with lock:
        await cmd_continue_logic(message, state)


async def cmd_continue_logic(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    data = await state.get_data()
    history = data.get("history", [])
    answered = len(history)

    if not current_state or answered == 0:
        await message.answer(
            "🤔 Активного диалога нет.\nНажми 🔄 Начать заново.",
            reply_markup=get_main_menu(),
        )
        return

    if current_state == "Onboarding:gathering":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Да, продолжить", callback_data="continue_action")],
                [InlineKeyboardButton(text="🔄 Начать заново", callback_data="restart_action")],
            ]
        )
        await message.answer(
            f"👋 **С возвращением!**\n\n"
            f"{progress_text(answered)}\n"
            "Продолжаем интервью?",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return

    if current_state == "Onboarding:service_priority":
        await message.answer(
            "🎯 Ты выбирал приоритетную услугу.\n"
            "Если кнопки пропали — напиши /start.",
            reply_markup=get_main_menu(),
        )
        return

    if current_state == "Onboarding:source_link":
        await message.answer(
            "🔗 Мы ждали ссылки на источники.\n"
            "Пришли сайт, Авито, ВК или Telegram. Либо напиши «нет».",
            parse_mode="Markdown",
        )
        return

    if current_state == "Onboarding:waiting_source_parsing":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить парсинг", callback_data="retry_parsing")]
            ]
        )
        await message.answer(
            "⏳ Мы изучали источники.\n"
            "Если зависло — нажми кнопку ниже.",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return

    if current_state == "Onboarding:photos":
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📸 Фото")],
                [KeyboardButton(text="⏭ Пропустить")],
            ],
            resize_keyboard=True,
        )
        await message.answer("📸 Мы были на этапе фото.", reply_markup=kb)
        return

    if current_state == "Onboarding:cta_choice":
        await ask_cta(message, state)
        return

    if current_state == "Onboarding:cta_value":
        await message.answer(
            "✍️ Ждём ссылку, username или номер для призыва к действию.",
            parse_mode="Markdown",
        )
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить", callback_data="continue_action")],
            [InlineKeyboardButton(text="🔄 Начать заново", callback_data="restart_action")],
        ]
    )
    await message.answer(
        "🤔 Не могу точно определить этап.\nЧто делаем?",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "continue_action")
async def cb_continue_action(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    lock = get_user_lock(uid)
    if lock.locked():
        await callback.answer("Я ещё обрабатываю предыдущий шаг ⏳", show_alert=True)
        return

    async with lock:
        data = await state.get_data()
        history = data.get("history", [])
        current_state = await state.get_state()

        if current_state == "Onboarding:waiting_source_parsing":
            await state.set_state(Onboarding.source_link)
            await state.update_data(waiting_manual=False)
            await callback.message.answer(
                "🔗 Пришли ссылки ещё раз. Или напиши «нет».",
                parse_mode="Markdown",
            )
            await callback.answer()
            return

        await ask_next_interview_question_internal(callback.message, state)
        await callback.answer()


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
        "🔗 Пришли ссылки ещё раз. Или напиши «нет».",
        parse_mode="Markdown",
    )


# =====================
# INTERVIEW
# =====================

@dp.message(Onboarding.gathering)
async def live_interview(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    lock = get_user_lock(uid)

    if lock.locked():
        await busy_answer(message)
        return

    async with lock:
        data = await state.get_data()
        history = data.get("history", [])
        src_req = data.get("source_requested", False)

        question_number = len(history) + 1
        text = message.text.strip()

        if question_number == 1 and is_banned(text):
            await message.answer(
                "❌ Я не работаю с этой темой.\n"
                "Если ошибся — опиши нишу иначе или напиши /start.",
                reply_markup=get_main_menu(),
            )
            await state.clear()
            return

        skip_words = ["не знаю", "пропустить", "незнаю", "пропусти", "дальше", "skip", "потом"]
        if text.lower() in skip_words:
            history.append({"q": question_number, "a": "Клиент пропустил вопрос / не знает"})
            await state.update_data(history=history)
            save_answer(uid, f"Q{question_number}", "Пропущено")
            await message.answer("👌 Пропускаю этот вопрос.", parse_mode="Markdown")
            await ask_next_interview_question_internal(message, state)
            return

        history.append({"q": question_number, "a": text})
        await state.update_data(history=history)
        save_answer(uid, f"Q{question_number}", text)

        # Проверка нескольких услуг после первого вопроса
        if question_number == 1:
            check = await agroq(
                f'Клиент описал бизнес: "{text}"\n'
                'Есть ли несколько РАЗНЫХ услуг/направлений?\n'
                'Ответь строго: "YES|услуга1|услуга2" или "NO"',
                max_tokens=120,
                system=QUESTION_SYSTEM,
            )

            if check and check.startswith("YES|"):
                services = [s.strip() for s in check.split("|")[1:] if s.strip()]
                if services:
                    await state.update_data(
                        history=history,
                        multiple_services=services,
                    )
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text=s, callback_data=f"svc_{i}")]
                            for i, s in enumerate(services)
                        ]
                    )
                    await message.answer(
                        "🎯 **Вижу несколько направлений:**\n"
                        + "\n".join([f"• {s}" for s in services])
                        + "\n\nЭто разные ниши. По какой делаем анализ первым?",
                        reply_markup=kb,
                        parse_mode="Markdown",
                    )
                    await state.set_state(Onboarding.service_priority)
                    return

        # После 4 ответов просим источники
        if len(history) >= 4 and not src_req:
            await state.update_data(source_requested=True)
            await message.answer(
                f"{progress_text(len(history))}\n\n"
                "✅ **Уже многое понял!**\n\n"
                "🔗 Пришли ссылки на источники, можно несколько в одном сообщении:\n"
                "• 🌐 Сайт\n"
                "• ✈️ Telegram-канал\n"
                "• 📱 Авито\n"
                "• 💬 ВКонтакте\n\n"
                "Я изучу каждый. Это займёт 30–90 секунд.\n"
                "Если источников нет — напиши «нет».",
                parse_mode="Markdown",
            )
            await state.set_state(Onboarding.source_link)
            await state.update_data(waiting_manual=False)
            return

        await ask_next_interview_question_internal(message, state)


@dp.callback_query(F.data.startswith("svc_"))
async def service_chosen(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    lock = get_user_lock(uid)

    if lock.locked():
        await callback.answer("Я ещё обрабатываю предыдущий шаг ⏳", show_alert=True)
        return

    async with lock:
        idx = int(callback.data.replace("svc_", ""))
        data = await state.get_data()
        services = data.get("multiple_services", [])
        history = data.get("history", [])

        priority = services[idx].strip() if idx < len(services) else ""
        save_answer(uid, "priority_service", priority)

        await callback.message.answer(
            f"✅ **Акцент на услуге:** {priority}",
            parse_mode="Markdown",
        )

        await state.update_data(
            priority_service=priority,
            history=history,
        )
        await state.set_state(Onboarding.gathering)

        await ask_next_interview_question_internal(callback.message, state)
        await callback.answer()


# =====================
# SOURCES
# =====================

@dp.message(Onboarding.source_link)
async def get_source(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    lock = get_user_lock(uid)

    if lock.locked():
        await busy_answer(message)
        return

    async with lock:
        data = await state.get_data()
        history = data.get("history", [])
        waiting_manual = data.get("waiting_manual", False)

        # Клиент прислал текст вручную
        if waiting_manual:
            await message.answer("⏳ Изучаю присланный текст...")
            src = message.text.strip()
            save_answer(uid, "source_data", src[:3000])

            hist_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
            priority = data.get("priority_service", "")

            analysis = await agroq(
                f"""Клиент прислал информацию о бизнесе:
{src[:6000]}

Из интервью:
{hist_text}

Приоритетная услуга: {priority or 'не указана'}

Сделай краткую разведку.
БЕЗ вопросов клиенту.""",
                max_tokens=1300,
                system=RECON_SYSTEM,
            )

            if analysis:
                await send_long(message, f"📊 **Разведка по тексту:**\n\n{analysis}")
            else:
                await message.answer("⚠️ Не удалось изучить текст. Попробуй ещё раз или /start.")

            await state.update_data(
                history=history,
                source_requested=True,
                source_data=src[:3000],
                waiting_manual=False,
            )
            await state.set_state(Onboarding.gathering)
            await ask_next_interview_question_internal(message, state)
            return

        answer = message.text.strip()
        save_answer(uid, "source_link", answer)

        if answer.lower() in ["нет", "нету", "-", "0", "нет источника", "отсутствует"]:
            await message.answer("👌 Хорошо, работаем без внешних источников.", parse_mode="Markdown")
            await state.update_data(
                source_requested=True,
                source_data="",
                waiting_manual=False,
            )
            await state.set_state(Onboarding.gathering)
            await ask_next_interview_question_internal(message, state)
            return

        urls = extract_urls(answer)
        if not urls:
            if answer.isdigit():
                urls = [f"https://www.avito.ru/user/{answer}/shop"]
            else:
                urls = [answer]

        await state.set_state(Onboarding.waiting_source_parsing)
        await state.update_data(urls_to_parse=urls, waiting_manual=False)

        await message.answer(
            f"⏳ **Нашёл {len(urls)} источник(ов). Изучаю параллельно...**\n"
            "Это может занять 30–110 секунд.\n"
            "💡 Если долго — не спаамь, подожди. В крайнем случае /start.",
            parse_mode="Markdown",
        )

        parsed = await asyncio.gather(
            *[parse_one_source(url) for url in urls],
            return_exceptions=True,
        )

        all_data = ""
        results = []

        for item in parsed:
            if isinstance(item, Exception):
                logger.error(f"❌ Gather parse exception: {item}")
                continue

            url, typ, text = item
            if text:
                results.append(f"✅ **{typ}** — изучен!")
                all_data += f"\n=== {typ}: {url} ===\n{text}\n"
            else:
                results.append(f"⚠️ **{typ}** ({url}) — не открылся")

        results_text = "\n".join(results) if results else "Источники не обработались."

        if all_data:
            save_answer(uid, "source_data", all_data[:3000])

            hist_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
            priority = data.get("priority_service", "")

            analysis = await agroq(
                f"""Данные из источников клиента:
{all_data[:7000]}

Из интервью:
{hist_text}

Приоритетная услуга: {priority or 'не указана'}

Сделай краткую разведку.
БЕЗ вопросов клиенту.""",
                max_tokens=1300,
                system=RECON_SYSTEM,
            )

            if analysis:
                await send_long(
                    message,
                    f"📊 **Результаты изучения:**\n{results_text}\n\n🔎 **Краткая разведка:**\n{analysis}",
                )
            else:
                await send_long(message, f"📊 **Результаты изучения:**\n{results_text}")

            await state.update_data(
                history=history,
                source_requested=True,
                source_data=all_data[:3000],
                waiting_manual=False,
            )
            await state.set_state(Onboarding.gathering)

            await message.answer("✅ Источники изучены. Продолжаем интервью.", parse_mode="Markdown")
            await ask_next_interview_question_internal(message, state)
        else:
            await message.answer(
                f"📊 **Результаты:**\n{results_text}\n\n"
                "Не смог открыть автоматически.\n"
                "Пришли **текстом**: описание, цены, услуги, оффер.",
                parse_mode="Markdown",
            )
            await state.update_data(waiting_manual=True)
            await state.set_state(Onboarding.source_link)


# =====================
# FINAL ANALYSIS
# =====================

async def finish_interview(message: types.Message, state: FSMContext, history):
    uid = message.from_user.id
    data = await state.get_data()
    priority = data.get("priority_service", "")
    source_data = data.get("source_data", "")

    await message.answer(
        "✅ **Интервью завершено!**\n\n"
        "⏳ Делаю полный SEO-анализ, контент-план и инструкцию.\n"
        "Это займёт 1–3 минуты.",
        parse_mode="Markdown",
    )

    hist_text = "\n".join([f"Q{i['q']}: {i['a']}" for i in history])
    niche_query = priority if priority else (history[0]["a"] if history else "")

    competitors, suggestions = await asyncio.gather(
        asyncio.to_thread(search_competitors, niche_query[:80], 5),
        asyncio.to_thread(get_yandex_suggestions, niche_query[:30]),
    )

    comp_text = "\n".join([f"- {c['title']}: {c['snippet']}" for c in competitors[:5]]) if competitors else "нет"
    sugg_text = ", ".join(suggestions[:10]) if suggestions else "пусто"
    src_section = f"\n\n=== ДАННЫЕ ИЗ ИСТОЧНИКОВ ===\n{source_data[:5000]}\n" if source_data else ""

    analysis = await agroq(
        f"""Интервью:
{hist_text}
{src_section}

Приоритетная услуга: {priority or 'не указана'}

Конкуренты:
{comp_text}

Подсказки Яндекса:
{sugg_text}

Сделай полный SEO-анализ ниши.
Нужно:
- 15 ключевых запросов
- 10 болей ЦА
- УТП
- минусы позиционирования
БЕЗ вопросов клиенту.""",
        max_tokens=2200,
        system=FINAL_ANALYSIS_SYSTEM,
    )

    if not analysis:
        await message.answer(
            "⚠️ Не удалось сделать полный анализ. Нажми ▶️ Продолжить или напиши /start.",
            reply_markup=get_main_menu(),
        )
        return

    plan = await agroq(
        f"""На основе анализа:
{analysis[:4000]}

Составь контент-план на 7 дней для Дзен.
Правило: 1 статья = 1 ключ + 1 боль.
Укажи день, время публикации, ключ, боль, тип статьи.
БЕЗ вопросов.""",
        max_tokens=1300,
        system=PLAN_SYSTEM,
    )

    guide = await agroq(
        """Напиши инструкцию для чайника: как опубликовать SEO-статью в Дзен,
чтобы получать трафик из Яндекса и Гугла.
5 шагов. Объясни Title, Description, H1, alt через аналогию с магазином.
БЕЗ вопросов.""",
        max_tokens=1000,
        system=GUIDE_SYSTEM,
    )

    save_research(uid, analysis + "\n\n" + (plan or ""), "", "", comp_text)

    await send_long(message, f"🎯 **ПОЛНЫЙ АНАЛИЗ НИШИ:**\n\n{analysis}")
    await asyncio.sleep(1)

    if plan:
        await send_long(message, f"📅 **КОНТЕНТ-ПЛАН НА 7 ДНЕЙ:**\n\n{plan}")
        await asyncio.sleep(1)

    if guide:
        await send_long(message, f"📚 **КАК ПУБЛИКОВАТЬ В ДЗЕН:**\n\n{guide}")
        await asyncio.sleep(1)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📸 Фото")],
            [KeyboardButton(text="⏭ Пропустить")],
        ],
        resize_keyboard=True,
    )
    await message.answer(
        "📸 **Хочешь загрузить фото работ/товара/команды?**\n"
        "Я буду использовать их в статьях. Можно сейчас или позже.",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await state.set_state(Onboarding.photos)


# =====================
# PHOTOS
# =====================

@dp.message(
    Onboarding.photos,
    F.text.in_(["⏭ Пропустить", "Позже", "позже", "Не сейчас", "нет", "Нет", "пропустить"]),
)
async def skip_photos(message: types.Message, state: FSMContext):
    await message.answer("👌 Ок, буду генерировать картинки сам.", parse_mode="Markdown")
    await ask_cta(message, state)


@dp.message(Onboarding.photos, F.text == "📸 Фото")
async def req_photos(message: types.Message, state: FSMContext):
    await message.answer(
        "📸 Пришли фото — по одному или пачкой.\n"
        "Когда закончишь — напиши «готово» или «позже».",
        parse_mode="Markdown",
    )


@dp.message(Onboarding.photos, F.text.lower() == "готово")
async def photos_done(message: types.Message, state: FSMContext):
    await message.answer("✅ Фото приняты!", parse_mode="Markdown")
    await ask_cta(message, state)


@dp.message(Onboarding.photos, F.photo)
async def recv_photo(message: types.Message, state: FSMContext):
    await message.answer(
        "📸 Фото принял. Загружай ещё или напиши «готово» / «позже».",
        parse_mode="Markdown",
    )


# =====================
# CTA
# =====================

async def ask_cta(message: types.Message, state: FSMContext):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌐 Сайт", callback_data="cta_site"),
                InlineKeyboardButton(text="✈️ Telegram", callback_data="cta_tg"),
            ],
            [
                InlineKeyboardButton(text="📱 WhatsApp", callback_data="cta_wa"),
                InlineKeyboardButton(text="📣 МАХ", callback_data="cta_max"),
            ],
            [
                InlineKeyboardButton(text="📞 Телефон", callback_data="cta_phone"),
            ],
        ]
    )
    await message.answer(
        "📍 **Куда тебе удобнее принимать заявки?**\n"
        "Я буду вставлять мягкий призыв в статьи.",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await state.set_state(Onboarding.cta_choice)


@dp.callback_query(F.data.startswith("cta_"))
async def cta_chosen(callback: types.CallbackQuery, state: FSMContext):
    ct = callback.data.replace("cta_", "")
    uid = callback.from_user.id

    try:
        supabase.table("users").update({"cta_type": ct}).eq("id", uid).execute()
    except Exception as e:
        logger.error(f"❌ CTA save error: {e}")

    prompts = {
        "site": "🌐 Пришли ссылку на страницу с формой заявки",
        "tg": "✈️ Пришли username через @ или ссылку на Telegram",
        "wa": "📱 Пришли номер в формате +7...",
        "max": "📣 Пришли ссылку на канал/чат в МАХ",
        "phone": "📞 Пришли номер телефона",
    }

    await state.update_data(cta_type=ct)
    await state.set_state(Onboarding.cta_value)
    await callback.message.answer(
        f"✅ Выбрано: {ct}\n\n{prompts.get(ct, '')}",
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.message(Onboarding.cta_value)
async def cta_value_received(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    value = message.text.strip()

    try:
        supabase.table("users").update({"cta_value": value}).eq("id", uid).execute()
    except Exception as e:
        logger.error(f"❌ CTA value save error: {e}")

    await message.answer(
        "🎉 **Онбординг завершён!**\n\n"
        "Теперь нажми 📝 Новая статья, чтобы получить первую статью.\n"
        "Если что-то зависнет — ▶️ Продолжить или /start.",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )
    await state.clear()


# =====================
# ARTICLE
# =====================

async def gen_article_logic(message: types.Message):
    uid = message.from_user.id

    try:
        r = (
            supabase.table("niche_research")
            .select("*")
            .eq("user_id", uid)
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        if not r.data:
            await message.answer(
                "Сначала пройди онбординг: нажми 🔄 Начать заново.",
                reply_markup=get_main_menu(),
            )
            return
        analysis = r.data[0]["keywords"]
    except Exception as e:
        logger.error(f"❌ Article research error: {e}")
        await message.answer(
            "Сначала пройди онбординг: нажми 🔄 Начать заново.",
            reply_markup=get_main_menu(),
        )
        return

    await message.answer(
        "✍️ **Пишу статью...**\n"
        "Это займёт 1–2 минуты.",
        parse_mode="Markdown",
    )

    article = await agroq(
        f"""Анализ ниши:
{analysis[:7000]}

Напиши одну SEO-статью по правилу: 1 ключ + 1 боль.
Выбери самую сильную связку.

Требования:
- цепляющий заголовок с ключом
- вступление с болью
- подзаголовки и списки
- язык читателя, не специалиста
- 5–7 тыс. знаков
- цифры, ошибки, шаги, мини-кейсы
- мягкий призыв в конце
- в конце SEO-пакет: Title, Description, хэштеги, Slug

БЕЗ вопросов клиенту.""",
        max_tokens=2600,
        system=FINAL_ANALYSIS_SYSTEM,
    )

    if not article:
        await message.answer(
            "⚠️ Не удалось сгенерировать статью. Нажми ▶️ Продолжить позже или /start.",
            reply_markup=get_main_menu(),
        )
        return

    save_article(uid, "статья", "боль", "статья", article)
    log_usage(uid, "article_generated")

    await send_long(message, f"📄 **СТАТЬЯ ГОТОВА:**\n\n{article}")

    # Обложка
    try:
        def fetch_cover():
            prompt = "professional blog cover image, modern minimal style, business theme, no text"
            url = "https://image.pollinations.ai/prompt/" + requests.utils.quote(prompt) + "?width=1200&height=630"
            rr = requests.get(url, timeout=60)
            if rr.status_code == 200 and len(rr.content) > 5000:
                return rr.content
            return None

        img = await asyncio.to_thread(fetch_cover)
        if img:
            photo = BufferedInputFile(img, filename="cover.jpg")
            await message.answer_photo(photo, caption="🖼 Обложка для статьи")
    except Exception as e:
        logger.warning(f"⚠️ Cover generation failed: {e}")

    # Файл
    try:
        doc = BufferedInputFile(article.encode("utf-8"), filename="article.txt")
        await message.answer_document(doc, caption="💾 Скачать статью файлом")
    except Exception as e:
        logger.warning(f"⚠️ Article file failed: {e}")

    await asyncio.sleep(1)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Адаптировать под vc.ru", callback_data="vc_yes"),
                InlineKeyboardButton(text="❌ Только Дзен", callback_data="vc_no"),
            ]
        ]
    )
    await message.answer(
        "📰 **Адаптировать эту статью под vc.ru?**\n"
        "Это даст дополнительный трафик.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@dp.message(Command("article"))
async def cmd_article(message: types.Message):
    uid = message.from_user.id
    lock = get_user_lock(uid)
    if lock.locked():
        await busy_answer(message)
        return

    async with lock:
        await gen_article_logic(message)


@dp.callback_query(F.data == "vc_yes")
async def adapt_vc(callback: types.CallbackQuery):
    uid = callback.from_user.id
    lock = get_user_lock(uid)

    if lock.locked():
        await callback.answer("Я ещё обрабатываю предыдущий шаг ⏳", show_alert=True)
        return

    async with lock:
        await callback.message.answer("⏳ Адаптирую под vc.ru...")

        try:
            a = (
                supabase.table("articles")
                .select("*")
                .eq("user_id", uid)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            if not a.data:
                await callback.message.answer("❌ Статья не найдена.")
                await callback.answer()
                return
            original = a.data[0]["content"]
            article_id = a.data[0]["id"]
        except Exception as e:
            logger.error(f"❌ vc article load error: {e}")
            await callback.message.answer("❌ Ошибка базы данных.")
            await callback.answer()
            return

        vc = await agroq(
            f"""Адаптируй статью под vc.ru:
{original[:9000]}

Требования:
- угол: кейс, факап, внутренняя кухня
- тон: коллега делится опытом
- длина: 7–12 тыс. знаков
- без хэштегов
- без прямой рекламы
- живой заголовок

БЕЗ вопросов.""",
            max_tokens=2600,
            system=FINAL_ANALYSIS_SYSTEM,
        )

        try:
            supabase.table("articles").update({"vc_version": vc}).eq("id", article_id).execute()
        except Exception as e:
            logger.error(f"❌ vc save error: {e}")

        if vc:
            await send_long(callback.message, f"📰 **ВЕРСИЯ ДЛЯ vc.ru:**\n\n{vc}")
        else:
            await callback.message.answer("⚠️ Не удалось адаптировать. Попробуй позже.")

        await callback.answer()


@dp.callback_query(F.data == "vc_no")
async def skip_vc(callback: types.CallbackQuery):
    await callback.message.answer("👌 Ок, оставляем только версию для Дзена.", reply_markup=get_main_menu())
    await callback.answer()


# =====================
# ADMIN
# =====================

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        u = supabase.table("users").select("id").execute()
        a = supabase.table("articles").select("id").execute()
        await message.answer(
            f"📊 **Статистика:**\n"
            f"👥 Клиентов: {len(u.data)}\n"
            f"📝 Статей: {len(a.data)}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")


# =====================
# STARTUP
# =====================

async def main():
    logger.info("🚀 Starting bot...")

    # Защита от конфликта getUpdates при рестарте
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"⚠️ delete_webhook error: {e}")

    await asyncio.sleep(2)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()

    test_groq()

    logger.info("✅ Threads started, running main...")
    asyncio.run(main())
