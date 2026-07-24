import os
import re
import json
import time
import hashlib
import requests
import feedparser
import google.generativeai as genai
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

# ============================================================
#  НАСТРОЙКИ
# ============================================================

# RSS-ленты: (название, URL)
RSS_FEEDS = [
    # 🚀 Космос
    ("NASA", "https://www.nasa.gov/news-release/feed/"),
    ("Space.com", "https://www.space.com/feeds/all"),
    ("ScienceDaily: Space & Time", "https://www.sciencedaily.com/rss/space_time.xml"),
    ("Universe Today", "https://www.universetoday.com/feed/"),
    # 🧬 Биохакинг / Долголетие
    ("Lifespan.io", "https://www.lifespan.io/feed/"),
    ("Nature", "https://www.nature.com/nature.rss"),
    ("Longevity Advice", "https://longevityadvice.com/feed/"),
    # 🔬 Научные открытия
    ("ScienceDaily: All", "https://www.sciencedaily.com/rss/all.xml"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
    ("Science Magazine", "https://www.science.org/rss/news_current.xml"),
]

# Сколько часов назад искать новости
MAX_AGE_HOURS = 48

# Сколько дней хранить в истории (дедупликация)
HISTORY_DAYS = 14

# Минимальный скор от LLM (0-10) для прохождения фильтра
MIN_SCORE = 5

# Сколько новостей в дайджесте
TOP_N = 3

# Файл истории
HISTORY_FILE = "history.json"

# ============================================================
#  ФУНКЦИИ
# ============================================================


def load_history():
    """Загрузка истории из файла."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_history(history):
    """Сохранение истории в файл."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def cleanup_history(history, days=HISTORY_DAYS):
    """Удаление записей старше N дней."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return {
        url: ts for url, ts in history.items()
        if datetime.fromisoformat(ts.replace("Z", "+00:00")) > cutoff
    }


def strip_html(html_text):
    """Удаление HTML-тегов из текста."""
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    return soup.get_text(separator=" ", strip=True)[:500]


def fetch_feeds():
    """Парсинг всех RSS-лент, возврат кандидатов."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    candidates = []

    for feed_name, feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:30]:  # не более 30 из каждой ленты
                # Дата публикации
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub_date = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                else:
                    pub_date = now  # если даты нет — считаем свежей

                if pub_date < cutoff:
                    continue

                url = entry.get("link", "")
                title = entry.get("title", "")
                summary = strip_html(
                    entry.get("summary", "") or entry.get("description", "")
                )

                if not url or not title:
                    continue

                candidates.append({
                    "source": feed_name,
                    "title": title,
                    "url": url,
                    "summary": summary[:500],
                })

            print(f"  ✅ {feed_name}: {len(feed.entries)} записей")
        except Exception as e:
            print(f"  ❌ {feed_name}: {e}")

    return candidates


def deduplicate(candidates, history):
    """Убираем уже опубликованные URL."""
    return [c for c in candidates if c["url"] not in history]


def llm_score(candidates, model):
    """LLM-оценка новостей от 0 до 10 с задержкой между запросами."""
    # Ограничиваем количество кандидатов для оценки (free tier: 15 RPM)
    MAX_CANDIDATES_TO_SCORE = 12
    scored = []
    for i, c in enumerate(candidates[:MAX_CANDIDATES_TO_SCORE]):
        prompt = f"""Оцени эту научную новость от 0 до 10 по критериям:
- Интересность для широкой аудитории (не специалистов)
- Новизна и значимость открытия
- Потенциальное влияние на жизнь людей
- Наличие конкретных фактов/цифр

Новость: {c['title']}
Источник: {c['source']}
Описание: {c['summary'][:300]}

Ответь ТОЛЬКО одной цифрой от 0 до 10, ничего больше."""
        try:
            response = model.generate_content(prompt)
            score_text = response.text.strip()
            # Извлекаем число
            score = int("".join(filter(str.isdigit, score_text))[:2])
            if score >= MIN_SCORE:
                c["score"] = score
                scored.append(c)
                print(f"  ⭐ [{score}/10] {c['title'][:60]}...")
        except Exception as e:
            print(f"  ⚠️ Ошибка LLM: {e}")
            # При 429 — ждём и пробуем дальше
            if "429" in str(e):
                print("  ⏳ Rate limit, жду 25 сек...")
                time.sleep(25)
                continue
        # Задержка между запросами для соблюдения лимита 15 RPM
        time.sleep(5)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:TOP_N]


def generate_post(top_news, model):
    """Генерация поста-дайджеста через Gemini."""
    news_block = ""
    for i, n in enumerate(top_news, 1):
        news_block += f"""
Новость {i}:
Заголовок: {n['title']}
Источник: {n['source']}
Описание: {n['summary'][:400]}
Ссылка: {n['url']}
"""

    prompt = f"""Ты — автор Telegram-канала «Научпоп», пишешь ОТ ПЕРВОГО ЛИЦА.

Твой характер:
- Очень умный и образованный, но не занудный
- Слегка саркастичный, с тонким сухим чувством юмора
- Иногда иронизируешь над наукой и учёными, но с теплотой
- Как друг, который разбирается в науке и рассказывает за ужином
- Не стесняешься говорить «а вот это круто» или «признайтесь, вы тоже об этом думали»
- Можешь добавить короткую реплику типа «Нет, серьёзно» или «И да, я знаю, о чём вы подумали»

ФОРМАТ ПОСТА:
- Эмодзи и вступление (1 строка, от первого лица)
- Каждую новость нумеруй: 1️⃣, 2️⃣, 3️⃣
- После номера — <b>жирный заголовок</b>
- РАЗВЁРНУТОЕ описание (6-10 предложений): что нашли, детали, почему это важно для нас
- В КАЖДОМ ОПИСАНИИ ОБЯЗАТЕЛЬНО встрой ссылку ВНУТЬ текста, не отдельной строкой. Каждый раз ИСПОЛЬЗУЙ РАЗНУЮ формулировку для ссылки, чередуй из этих вариантов или придумай свой:
  • <a href="URL">Вот полная статья</a>
  • <a href="URL">Тут подробнее</a>
  • <a href="URL">Оригинал здесь</a>
  • <a href="URL">Если хочешь копнуть глубже</a>
  • <a href="URL">Все детали в оригинале</a>
  • <a href="URL">Сама статья — для любопытных</a>
  • <a href="URL">Читай первоисточник</a>
  Никогда не повторяй одну и ту же формулировку дважды в одном посте
- Между новостями — пустая строка
- В конце — 3-5 хэштегов: #космос #наука и т.д.

ЗАПРЕТЫ:
- НИКОГДА не пиши «Подробнее здесь» или «Читайте далее» как отдельную строку
- НИКОГДА не используй ** или * (только <b>, <i>, <a>)
- Не используй обратные слеши

Вот новости:
{news_block}

Сгенерируй пост в HTML (parse_mode=HTML)."""

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"❌ Ошибка генерации поста: {e}")
        return None


def fetch_og_image(url):
    """Извлекает og:image URL из страницы."""
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        # Ищем og:image
        meta = soup.find("meta", property="og:image")
        if meta and meta.get("content"):
            return meta["content"]
        # Фолбэк: twitter:image
        meta = soup.find("meta", attrs={"name": "twitter:image"})
        if meta and meta.get("content"):
            return meta["content"]
    except Exception as e:
        print(f"  ⚠️ Не удалось извлечь картинку: {e}")
    return None


def send_to_telegram(text, bot_token, channel_id, image_url=None):
    """Отправка поста в Telegram. Если image_url — отправляем фото + текст."""
    if image_url:
        # 1. Скачиваем картинку
        try:
            img_resp = requests.get(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if img_resp.status_code != 200:
                print(f"  ⚠️ Не удалось скачать картинку ({img_resp.status_code}), отправляю без неё")
                image_url = None
        except Exception as e:
            print(f"  ⚠️ Ошибка скачивания картинки: {e}")
            image_url = None

    if image_url:
        # Разбиваем текст на заголовок (до первого описания) и остальное
        # Ищем первую новость: от вступления до второго номера
        parts = re.split(r'(1️⃣|2️⃣|3️⃣)', text, maxsplit=2)
        # parts = ['intro', '1️⃣', 'описание1...2️⃣', 'описание2...3️⃣', 'описание3...']
        intro_and_first = ""
        rest = text
        if len(parts) >= 3:
            intro_and_first = parts[0] + parts[1] + parts[2]
            rest = "".join(parts[3:]) if len(parts) > 3 else ""
            # Убираем лишний пробел в начале rest
            rest = rest.lstrip()

        # Ограничиваем caption (Telegram: 1024 символа)
        if len(intro_and_first) > 1024:
            intro_and_first = intro_and_first[:1020] + "..."

        # Отправляем фото с caption
        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        with open("/tmp/news_image.jpg", "wb") as f:
            f.write(img_resp.content)
        with open("/tmp/news_image.jpg", "rb") as photo:
            payload = {
                "chat_id": channel_id,
                "caption": intro_and_first,
                "parse_mode": "HTML",
            }
            resp = requests.post(url, data=payload, files={"photo": photo}, timeout=30)

        if resp.status_code != 200:
            print(f"  ⚠️ Ошибка sendPhoto ({resp.status_code}): {resp.text}")
            # Фолбэк — отправляем просто текст
            return _send_text(text, bot_token, channel_id)

        print("  📸 Картинка отправлена")

        # Отправляем остаток текста
        if rest.strip():
            return _send_text(rest, bot_token, channel_id)
        return True
    else:
        return _send_text(text, bot_token, channel_id)


def _send_text(text, bot_token, channel_id):
    """Отправка текстового сообщения."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": channel_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code == 200:
        print("✅ Текст отправлен в Telegram!")
        return True
    else:
        print(f"❌ Ошибка Telegram API ({resp.status_code}): {resp.text}")
        return False


# ============================================================
#  MAIN
# ============================================================


def main():
    print("=" * 60)
    print("🌌 Science-Pop Bot — Космос, биохакинг и открытия")
    print("=" * 60)

    # Читаем переменные окружения
    gemini_key = os.environ.get("GEMINI_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID")

    if not all([gemini_key, bot_token, channel_id]):
        print("❌ Не заданы переменные окружения!")
        print("   Нужны: GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID")
        return

    # Настройка Gemini — два модели
    genai.configure(api_key=gemini_key)
    model_lite = genai.GenerativeModel("gemini-3.5-flash-lite")  # скоринг (много запросов)
    model_flash = genai.GenerativeModel("gemini-3.5-flash")      # финальный пост (1 запрос, качественнее)

    # Загружаем и чистим историю
    history = load_history()
    history = cleanup_history(history)
    print(f"📂 В истории: {len(history)} записей")

    # 1. Сбор кандидатов
    print("\n📡 Шаг 1: Сбор новостей из RSS...")
    candidates = fetch_feeds()
    print(f"  📋 Всего кандидатов: {len(candidates)}")

    # 2. Дедупликация
    print("\n🔄 Шаг 2: Дедупликация...")
    candidates = deduplicate(candidates, history)
    print(f"  📋 После дедупликации: {len(candidates)}")

    if not candidates:
        print("⚠️ Нет свежих новостей для публикации. Завершаю.")
        return

    # 3. LLM-оценка
    print(f"\n🧠 Шаг 3: LLM-оценка (score >= {MIN_SCORE})...")
    top_news = llm_score(candidates, model_lite)
    print(f"  🏆 Прошли фильтр: {len(top_news)}")

    if not top_news:
        print("⚠️ Ни одна новость не набрала достаточный скор. Завершаю.")
        return

    # 4. Генерация поста
    print("\n✍️ Шаг 4: Генерация поста...")
    post = generate_post(top_news, model_flash)
    if not post:
        print("❌ Не удалось сгенерировать пост. Завершаю.")
        return

    print(f"  📝 Длина поста: {len(post)} символов")

    # 5. Отправка в Telegram
    print("\n📤 Шаг 5: Отправка в Telegram...")
    # Извлекаем картинку из первой новости
    image_url = None
    if top_news:
        image_url = fetch_og_image(top_news[0]["url"])
        if image_url:
            print(f"  🖼️ Картинка: {image_url[:80]}...")
    success = send_to_telegram(post, bot_token, channel_id, image_url=image_url)

    # 6. Обновление истории
    if success:
        for n in top_news:
            history[n["url"]] = datetime.now(timezone.utc).isoformat()
        save_history(history)
        print(f"💾 История обновлена (+{len(top_news)} записей)")
    else:
        print("⚠️ Пост не отправлен, история не обновлена.")

    print("\n✅ Готово!")


if __name__ == "__main__":
    main()
