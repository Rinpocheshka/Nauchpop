# 🌌 Научпоп: Космос, биохакинг и открытия

Telegram-бот, который каждый день собирает научные новости из RSS-лент, фильтрует их через Gemini и публикует дайджест в канал @pizzoff.

## 🛠 Технологический стек

- **Python 3** + feedparser, requests, google-generativeai, beautifulsoup4
- **LLM:** Gemini 3.1 Flash Lite (скоринг + генерация поста)
- **Инфраструктура:** GitHub Actions (cron по расписанию)
- **Хранение:** `history.json` (дедупликация, коммитится обратно в репозиторий)

## 📡 RSS-ленты

| Категория | Источник |
|-----------|----------|
| 🚀 Космос | NASA, Space.com, ScienceDaily: Space & Time, Universe Today |
| 🧬 Биохакинг | Lifespan.io, Nature, Longevity Advice |
| 🔬 Открытия | ScienceDaily, MIT Tech Review, Science Magazine |

## ⚙️ Настройка

### 1. Создай Telegram-бота
1. Напиши `/newbot` в [@BotFather](https://t.me/BotFather)
2. Сохрани токен
3. Добавь бота в канал как администратора

### 2. Настрой GitHub Secrets
В репозитории: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Значение |
|--------|----------|
| `GEMINI_API_KEY` | Ключ от [Google AI Studio](https://aistudio.google.com/apikey) |
| `TELEGRAM_BOT_TOKEN` | Токен от BotFather |
| `TELEGRAM_CHANNEL_ID` | `@pizzoff` или числовой ID канала |

### 3. Настрой Workflow Permissions
**Settings → Actions → General → Workflow permissions → Read and write permissions**

(Без этого `history.json` не будет коммититься и бот начнёт дублировать новости)

## 🔄 Как это работает

```
RSS-ленты → Фильтр (48ч) → Дедупликация (history.json) → Gemini Score (≥7) → Генерация поста → Telegram
```

1. **Сбор:** Парсим 10 RSS-лент, берём новости за 48 часов
2. **Дедупликация:** Убираем уже опубликованные URL (за 14 дней)
3. **Скоринг:** Gemini оценивает каждую новость от 0 до 10
4. **Генерация:** Топ-3 новости → пост в стиле «как пятилетнему»
5. **Публикация:** Отправляем HTML-пост в канал

## 📋 Расписание

- **Ежедневно в 18:38 МСК** (15:38 UTC)
- Ручной запуск: кнопка **Run workflow** в Actions

## 🐛 Подводные камни

1. **GitHub Actions permissions** — обязательно выставь Read and write (см. выше)
2. **HTML, не Markdown** — Gemini генерирует HTML-разметку для Telegram (`<b>`, `<i>`, `<a>`)
3. **Лимиты Gemini Free Tier** — 1500 запросов/день, хватит с запасом
4. **Nature RSS** — RDF формат, feedparser справляется нормально
