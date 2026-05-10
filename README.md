# resetera-bridge-groq (v2)

Автоматический медиа-канал по консольным играм. **Версия 2** — на бесплатном Groq API
(вместо платного ChadGPT в v1). Публикует **раз в час** вместо раза в сутки.

## Архитектура

```
RSS resetera.com → collector.py (раз в час) → pool.json
                                                   ↓
                                        publisher.py (раз в час)
                                                   ↓
                                              feed.xml (GitHub Pages)
                                                   ↓
                                              Hooppy.ru → ВК / TG / Макс
```

## Используемые модели Groq

| Шаг | Модель | Почему |
|---|---|---|
| Куратор (выбор статьи) | `llama-3.3-70b-versatile` | Production, хорошая логика, 280 t/s |
| Дубль-чек (YES/NO) | `llama-3.1-8b-instant` | Самая быстрая, для бинарного ответа достаточно |
| Рерайтер (русский) | `qwen/qwen3-32b` | Лучший русский из доступных, 400 t/s |

Все три модели — через один Groq API-ключ.

## Лимиты Groq Free Tier

- **Llama 3.3 70B**: 1000 запросов/день, 30 RPM, 300K TPM
- **Llama 3.1 8B**: 14400 запросов/день, 30 RPM, 250K TPM
- **Qwen3-32B**: 1000 запросов/день, 30 RPM, 300K TPM

Наша реальная нагрузка при ежечасных публикациях:
- 24 запуска/день × 3 запроса = **~72 запроса/день** на каждую модель
- Запас по лимиту: **~14×**

Никакой кредитной карты, никаких платежей.

## Структура файлов

```
common.py                       # ОБЩИЕ функции (RSS, скачивание текста, JSON)
collector.py                    # сборщик (БЕЗ изменений из v1)
publisher.py                    # публикатор V2 (Groq)
requirements.txt                # Python-зависимости
.gitignore
seen.json                       # GUID статей, которые сборщик уже видел
pool.json                       # пул кандидатов для куратора
published.json                  # GUID + заголовки опубликованных
feed.xml                        # выходной RSS для Hooppy
.github/workflows/
    collect.yml                 # workflow для сборщика
    publish.yml                 # workflow для публикатора
```

## Как настроить (шаги)

### 1. Получить API-ключ Groq

1. Зайти на [console.groq.com](https://console.groq.com)
2. Войти через Google (без кредитки)
3. Перейти в **API Keys** → **Create API Key**
4. Скопировать ключ (показывается один раз!) — формат: `gsk_...`

### 2. Создать репозиторий на GitHub

Назвать `resetera-bridge-groq` (или другое имя). Залить в него файлы:
- `common.py` (скопировать из v1 БЕЗ изменений)
- `collector.py` (скопировать из v1 БЕЗ изменений)
- `publisher.py` (этот репо — новый)
- `requirements.txt`
- `.gitignore`
- `.github/workflows/collect.yml`
- `.github/workflows/publish.yml`

### 3. Добавить API-ключ в Secrets

Settings репозитория → **Secrets and variables** → **Actions** → **New repository secret**
- Name: `GROQ_API_KEY`
- Value: ключ от Groq

### 4. Включить GitHub Pages

Settings → **Pages** → Source: **Deploy from a branch** → Branch: **main** → Folder: **/ (root)**

URL для feed.xml будет: `https://<твой_логин>.github.io/resetera-bridge-groq/feed.xml`

### 5. Настроить cron-job.org

Создать **два** cron-задания (как в v1, но на новый репо):

**Сборщик** — раз в час:
- URL: `https://api.github.com/repos/<твой_логин>/resetera-bridge-groq/actions/workflows/collect.yml/dispatches`
- Method: POST
- Headers: `Authorization: Bearer <твой_GitHub_PAT>`, `Accept: application/vnd.github+json`
- Body: `{"ref":"main"}`
- Schedule: каждый час в `:00`

**Публикатор** — раз в час со сдвигом 30 минут:
- URL: `https://api.github.com/repos/<твой_логин>/resetera-bridge-groq/actions/workflows/publish.yml/dispatches`
- Method: POST
- Headers: те же
- Body: `{"ref":"main"}`
- Schedule: каждый час в `:30` (чтобы сборщик успел обновить pool до публикатора)

### 6. Настроить Hooppy

В Hooppy.ru изменить URL источника RSS на новый:
`https://<твой_логин>.github.io/resetera-bridge-groq/feed.xml`

Частота проверки RSS — раз в 15-30 минут.

## Тестирование перед запуском

После заливки кода — запустить вручную:
1. **Actions** → **collect** → **Run workflow** — проверить что pool.json создаётся
2. Подождать 5 минут
3. **Actions** → **publish** → **Run workflow** — проверить что feed.xml создаётся
4. Открыть feed.xml в браузере — посмотреть на качество русского

## Если качество русского у Qwen3-32B окажется плохим

В `publisher.py` есть закомментированные альтернативы:
```python
# MODEL_REWRITER = "openai/gpt-oss-120b"       # альтернатива 1
# MODEL_REWRITER = "llama-3.3-70b-versatile"   # альтернатива 2
```

Просто закомментировать текущий и раскомментировать один из вариантов.

## Сравнение v1 и v2

| | v1 (ChadGPT) | v2 (Groq) |
|---|---|---|
| Стоимость | 300-700 ₽/мес | **0 ₽** |
| Частота публикаций | 1/день | **24/день** |
| Модель ИИ | GPT-5.4 Mini | Llama 3.3 70B + Qwen3-32B |
| Качество русского | отличное | хорошее (зависит от модели) |
| Платёжные лимиты | искры (заканчиваются) | Rate limits (сбрасываются ежедневно) |
