import os
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
MIN_SCORE = 7

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

    prompt = f"""Ты — автор Telegram-канала «Научпоп: Космос, биохакинг и открытия».

Напиши пост-дайджест из 3 научных новостей. Стиль:
- «Объясни мне, как пятилетнему»: простым языком, без заумных терминов
- Если термин unavoidable — сразу объясняешь его в скобках
- Каждая новость — 2-4 предложения: что нашли + почему это важно для нас
- Между новостями — эмодзи-разделители
- В конце — хэштеги (3-5 штук)
- НИКОГДА не используй Markdown-звездочки **bold** или *italic*
- Форматирование ТОЛЬКО в HTML: <b>жирный</b>, <i>курсив</i>, <a href="...">ссылка</a>
- Не используй обратные слеши \\ для экранирования
- Начни пост с приветствия «🌌 Научпоп-дайджест» и заканчивай призывом подписаться

Вот новости:
{news_block}

Сгенерируй пост в HTML-формате (parse_mode=HTML для Telegram)."""

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"❌ Ошибка генерации поста: {e}")
        return None


def send_to_telegram(text, bot_token, channel_id):
    """Отправка поста в Telegram."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": channel_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code == 200:
        print("✅ Пост успешно отправлен в Telegram!")
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

    # Настройка Gemini
    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-3.1-flash-lite")

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
    top_news = llm_score(candidates, model)
    print(f"  🏆 Прошли фильтр: {len(top_news)}")

    if not top_news:
        print("⚠️ Ни одна новость не набрала достаточный скор. Завершаю.")
        return

    # 4. Генерация поста
    print("\n✍️ Шаг 4: Генерация поста...")
    post = generate_post(top_news, model)
    if not post:
        print("❌ Не удалось сгенерировать пост. Завершаю.")
        return

    print(f"  📝 Длина поста: {len(post)} символов")

    # 5. Отправка в Telegram
    print("\n📤 Шаг 5: Отправка в Telegram...")
    success = send_to_telegram(post, bot_token, channel_id)

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
