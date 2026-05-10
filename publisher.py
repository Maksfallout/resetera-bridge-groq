"""
Публикатор v2.1 (Groq):
раз в час выбирает лучшую новую статью через ИИ-куратора,
проверяет на смысловой дубль, скачивает полный текст,
делает рерайт+перевод через ИИ-рерайтера, кладёт результат в feed.xml.
 
Изменения v2.1 (правки промпта под Qwen):
- запрет всех эмодзи в тексте (📰 у "Пикселя" добавляется в коде)
- усилен запрет англицизмов (релиз → выход)
- жёсткий лимит 850 символов на title+text
- запрет любых обращений к читателю и риторических вопросов
- усилены требования к абзацам \\n\\n
- подпись "Пиксель" добавляется в коде, не через ИИ
- словарь японских имён по Поливанову
- расширена самопроверка перед выдачей
"""
 
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
 
import requests
from feedgen.feed import FeedGenerator
 
from common import (
    POOL_FILE, PUBLISHED_FILE, OUTPUT_FEED, ITEM_LINK_PLACEHOLDER,
    REQUEST_TIMEOUT,
    load_json, save_json, get_full_text,
)
 
# ====== НАСТРОЙКИ GROQ ======
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
 
MODEL_CURATOR  = "llama-3.3-70b-versatile"     # Production, 280 t/s, качественная логика
MODEL_DEDUP    = "llama-3.1-8b-instant"        # Production, 560 t/s, для бинарного ответа
MODEL_REWRITER = "qwen/qwen3-32b"              # Preview, 400 t/s, отличный русский
 
# Альтернативы рерайтера если Qwen уберут из Preview:
# MODEL_REWRITER = "openai/gpt-oss-120b"       # Production, 500 t/s, хороший русский
# MODEL_REWRITER = "llama-3.3-70b-versatile"   # Production, средне-хороший русский
 
REQUEST_PAUSE_SEC = 2          # пауза между запросами (Groq free: 30 RPM = 1 запрос / 2 сек)
TEXT_FOR_REWRITER_LIMIT = 8000 # сколько знаков статьи отдаём ИИ
MAX_ITEMS_IN_FEED = 10         # сколько последних постов держим в feed.xml
 
# Подпись добавляется в коде, не через ИИ — гарантирует точное соблюдение формата
PIXEL_SIGNATURE = "📰 Дежурный по новостям: Пиксель."
# Жёсткий лимит на title + text (без подписи) для запаса под Telegram-лимит 1024
MAX_TOTAL_CHARS = 850 - len(PIXEL_SIGNATURE) - 4  # минус подпись и пара переносов строки
# ============================
 
FEED_TITLE = "Gaming Daily Pick"
FEED_LINK = "https://www.resetera.com/forums/gaming-headlines.54/"
FEED_DESCRIPTION = "Главная игровая новость — рерайт на русском"
 
CURATOR_PROMPT = """Ты — главный редактор популярного канала о консольных играх и игровой индустрии. Перед тобой список заголовков свежих новостей за последние сутки. Выбери ОДИН — самый интересный для геймерской аудитории.
 
Критерии выбора (в порядке приоритета):
 
ПЕРВЫЙ ПРИОРИТЕТ — новости важные для российской аудитории:
- Официальный выход игры в России (или подтверждение выхода), включая Steam, PS Store, Xbox
- Новости о ценах и доступности консолей и игр для российского рынка
- Новости о сервисах, доступных в России (Game Pass, PS Plus и т.д.)
- Крупные анонсы игр от студий, у которых большая российская аудитория (GTA, FIFA/FC, Call of Duty, CS, Dota, Minecraft и т.п.)
- Новости об уходе или возвращении игровых компаний на российский рынок
 
ВТОРОЙ ПРИОРИТЕТ — важные мировые новости игровой индустрии:
- Крупные анонсы новых игр от известных студий
- Выход крупных игр (релизы AAA и крупных инди)
- Заявления CEO крупных компаний (Sony, Microsoft, Nintendo, Valve, Take-Two)
- Скандалы и конфликты с широким резонансом
- Крупные цифры продаж или финансовые результаты
- Новости о новых консолях и платформах
 
ИЗБЕГАЙ:
- Профсоюзных новостей и трудовых споров (не интересно широкой аудитории)
- Косплея, фан-арта, фан-проектов
- Технических подробностей для разработчиков
- Новостей о конкретных стримерах или ютуберах (если только это не касается платформы в целом)
 
Если все новости средние — всё равно выбери лучшую из имеющихся.
 
Список заголовков:
{titles}
 
Верни ответ СТРОГО в формате JSON, без других слов и без markdown:
{{"guid": "выбранный_guid", "reason": "одно предложение почему"}}"""
 
 
REWRITER_PROMPT = """Ты — автор поста для русскоязычного канала о консольных играх и игровой индустрии. Создай ЗАГОЛОВОК и ТЕКСТ поста по строгим правилам.
 
═══════════════════════════════════════════
БЛОК 1: ЯЗЫК (АБСОЛЮТНЫЙ ПРИОРИТЕТ)
═══════════════════════════════════════════
 
1. Только русский. Полностью БЕЗ англицизмов в тексте и заголовке.
 
2. ЗАПРЕЩЁННЫЕ АНГЛИЦИЗМЫ — заменяй ВСЕГДА (включая заголовок!):
   - «релиз» → «выход» / «выпуск»
   - «казуальные / casual игроки» → «обычные игроки» или «игроки на досуге»
   - «хардкорные» → «увлечённые» / «преданные»
   - «эксклюзив» → «эксклюзивная игра»
   - «гейм-плей» → «игровой процесс»
   - «контент» → «материалы» / «содержимое»
   - «тайтл» → «игра»
   - «ивент» → «событие»
   - «фича» → «возможность» / «особенность»
   - «коммьюнити» → «сообщество»
   - «стартап» → «молодая компания»
   - «коллаборация» → «совместный проект»
   - «лонч» → «запуск» / «старт продаж»
   - «деплой» → «развёртывание»
   - «апдейт» → «обновление»
   - «патч» → «обновление» / «исправление»
 
3. ИМЕНА ЛЮДЕЙ — полностью транслитерируй на русский. НИ ОДНОЙ части имени на латинице.
   ВАЖНО: японские имена пиши по системе Поливанова (официальный стандарт):
   - Sigeru/Shigeru Miyamoto → Сигэру Миямото (НЕ Шигеру!)
   - Shuntaro Furukawa → Сюнтаро Фурукава (НЕ Шунтаро!)
   - Hideo Kojima → Хидэо Кодзима (НЕ Хидео!)
   - Yoshiaki Koizumi → Ёсиаки Коидзуми
   - Phil Spencer → Фил Спенсер
   - Strauss Zelnick → Штраус Зельник
   - Matt Piscatella → Мэтт Пискателла
   Правила Поливанова кратко:
   - «sh» в японском → «с» (НЕ «ш»): Shigeru → Сигэру, Toshihiro → Тосихиро
   - «sho/shu» → «сё/сю»: Shuntaro → Сюнтаро, Sho → Сё
   - «chi» → «ти»: Ichiro → Итиро
   - «ji» → «дзи»: Kojima → Кодзима, Junji → Дзюндзи
   - «ya/yo/yu» → «я/ё/ю»: Hideo → Хидэо, Ryo → Рё
   ДЛЯ ЗАПАДНЫХ ИМЁН — обычная транслитерация по произношению.
 
4. НАЗВАНИЯ компаний, игр, платформ, СМИ — на оригинальном языке:
   PlayStation, Nintendo Switch, Xbox Series X, Cyberpunk 2077, Take-Two,
   Rockstar, Circana, Bloomberg, Wall Street Journal, Eurogamer, IGN, Epic Games.
 
5. Не упоминай войну в реальном мире (не игровом).
 
═══════════════════════════════════════════
БЛОК 2: ЖИВОЙ РУССКИЙ ЯЗЫК
═══════════════════════════════════════════
 
6. Пиши как живой русскоязычный человек, НЕ как переводчик с английского.
 
ЗАПРЕЩЁННЫЕ КАЛЬКИ С АНГЛИЙСКОГО:
- НЕЛЬЗЯ: "выше цена" → ПИШИ: "более высокая цена" / "подорожание"
- НЕЛЬЗЯ: "взял пресс-брифинг / взял слово на брифинге" → ПИШИ: "выступил на брифинге" / "заявил журналистам"
- НЕЛЬЗЯ: "адресовать проблему" → ПИШИ: "решать проблему"
- НЕЛЬЗЯ: "имплементировать изменения" → ПИШИ: "внедрить изменения"
- НЕЛЬЗЯ: "двигать иглу" → ПИШИ: "влиять на ситуацию"
- НЕЛЬЗЯ: "домохозяйства с высоким доходом" → ПИШИ: "обеспеченные семьи"
- НЕЛЬЗЯ: "является важным фактором" → ПИШИ: "это важно потому что"
- НЕЛЬЗЯ: "осуществляет производство" → ПИШИ: "выпускает" / "делает"
- НЕЛЬЗЯ: "в свете последних событий" → ПИШИ: "после того что случилось"
- НЕЛЬЗЯ: "линейка программного обеспечения" → ПИШИ: "набор игр" / "игровая библиотека"
- НЕЛЬЗЯ: "программное обеспечение" в контексте игр → ПИШИ: "игры"
- НЕЛЬЗЯ: "комбайн PlayStation и Xbox" → ПИШИ: "консольный рынок"
- НЕЛЬЗЯ: "данный" → ПИШИ: "этот"
- НЕЛЬЗЯ: "порядка 100 единиц" → ПИШИ: "около 100 штук" / "примерно сотня"
 
ПРАВИЛО ПЕРЕСТРОЙКИ ПРЕДЛОЖЕНИЙ:
Если предложение при буквальном переводе с английского получается корявым — ПЕРЕФОРМУЛИРУЙ его полностью по-русски, не сохраняя оригинальную структуру.
Пример:
- Английское: "Nintendo's president took the press briefing saying higher prices need software to justify them"
- НЕЛЬЗЯ: "Президент Nintendo взял пресс-брифинг, сказав, что выше цены нужно программное обеспечение"
- НАДО: "Президент Nintendo Сюнтаро Фурукава объяснил журналистам: чтобы оправдать рост цен, компания планирует выпустить больше игр"
 
7. Используй естественные обороты: «получается, что», «выходит так», «оказывается», «судя по всему», «как выяснилось».
8. Разговорный стиль — но не панибратский. Как объясняешь другу, но без жаргона.
 
═══════════════════════════════════════════
БЛОК 3: НИКАКИХ ОБРАЩЕНИЙ К ЧИТАТЕЛЮ (КРИТИЧНО!)
═══════════════════════════════════════════
 
9. Это новостная заметка, а НЕ обращение к аудитории. ЗАПРЕЩЕНО ВСЁ:
   - НЕ задавай вопросов читателю («Какие игры вы хотите?», «Как считаете?», «Согласны?»)
   - НЕ используй слова «вы», «вам», «ваш», «ваше», «у вас»
   - НЕ давай советов читателю («то обновление консоли может быть неизбежным», «следите за новинками»)
   - НЕ призывай к действию («поспешите», «не пропустите», «обязательно попробуйте»)
   - НЕ пиши призывов подписаться, ставить лайки, переходить по ссылкам
   - НЕ добавляй ссылки и хештеги
 
10. Текст пишется в третьем лице как сторонний наблюдатель: «компания заявила», «индустрия отреагировала», «эксперты считают».
 
═══════════════════════════════════════════
БЛОК 4: ЛОГИКА И СВЯЗНОСТЬ
═══════════════════════════════════════════
 
11. Текст должен быть ЛОГИЧЕСКИ СВЯЗНЫМ. Каждое следующее предложение вытекает из предыдущего или развивает его.
 
12. Если приводишь факт — объясняй ЗАЧЕМ читателю это знать и КАК это связано с темой поста.
    Не просто «у PS5 продано 100 млн», а «PS5 продано 100 млн — это значит, что у GTA 6 уже огромная база покупателей».
 
13. Если в исходной статье есть причинно-следственная связь между двумя темами — обязательно её сохрани и объясни читателю явно. Не оставляй «висящие» факты без связи с основной темой.
 
═══════════════════════════════════════════
БЛОК 5: ФОРМА И ОБЪЁМ
═══════════════════════════════════════════
 
14. ЖЁСТКИЙ ЛИМИТ: ЗАГОЛОВОК + ТЕКСТ ≤ {max_chars} символов суммарно.
    Если выходишь за лимит — сократи текст. Лучше короткий и плотный, чем длинный с водой.
 
15. ЗАГОЛОВОК:
    - До 100 знаков
    - На русском, цепляющий, без англицизмов
    - ЗАКАНЧИВАЕТСЯ ТОЧКОЙ (либо вопросительным/восклицательным знаком если это вопрос/восклицание)
    - Слово «релиз» в заголовке = серьёзная ошибка. Используй «выход» или «выпуск».
 
16. ТЕКСТ — целевой объём 500-700 знаков:
    - Стиль разговорный, живой
    - НЕ начинай со штампов: «Сегодня...», «На днях...», «Стало известно, что...», «Как сообщает...»
    - Если в исходнике мусор (меню, формы подписки) — игнорируй
 
17. ПУНКТУАЦИЯ: точка ставится сразу после слова без пробела (правильно: «слово.», неправильно: «слово .»).
 
18. ЭМОДЗИ В ТЕКСТЕ ЗАПРЕЩЕНЫ. Ноль эмодзи в title и в text.
    (Захардкоженная подпись с эмодзи добавляется отдельно после текста — её писать НЕ нужно.)
 
═══════════════════════════════════════════
БЛОК 6: СТРУКТУРА АБЗАЦЕВ (ВАЖНО!)
═══════════════════════════════════════════
 
19. ОБЯЗАТЕЛЬНО раздели текст на 2-3 абзаца, разделённые символами `\\n\\n` (двойной перенос строки).
    Текст одной сплошной простынёй БЕЗ `\\n\\n` = ОШИБКА.
 
20. Каждый абзац — одна законченная мысль:
    - Первый абзац: суть события (что случилось и почему важно)
    - Второй абзац: подробности и контекст
    - Третий абзац (если нужен): последствия для индустрии или общая оценка
    
    ВАЖНО: между абзацами в JSON-строке должны быть РОВНО два символа `\\n\\n` подряд.
 
21. Подпись «Дежурный по новостям» НЕ пиши — она добавится автоматически.
    Заверши текст последним смысловым абзацем без призыва, без обращения к читателю.
 
═══════════════════════════════════════════
САМОПРОВЕРКА ПЕРЕД ВЫДАЧЕЙ (обязательно пройди по всем пунктам):
═══════════════════════════════════════════
☐ Слова «релиз», «контент», «эксклюзив», «фича», «коммьюнити», «коллаборация», «апдейт», «патч» отсутствуют?
☐ Все имена транслитерированы (нет латиницы в именах людей)?
☐ Японские имена по Поливанову (Сигэру а не Шигеру, Сюнтаро а не Шунтаро, Хидэо а не Хидео, Кодзима а не Коджима)?
☐ Названия компаний и игр на оригинальном языке (PlayStation, Cyberpunk 2077)?
☐ В тексте НЕТ обращений к читателю («вы», «ваш», вопросов, советов, призывов)?
☐ В тексте НЕТ эмодзи?
☐ Сумма длины title + text НЕ превышает {max_chars} символов?
☐ Текст разбит на 2-3 абзаца через `\\n\\n` (НЕ одной простынёй)?
☐ Заголовок заканчивается точкой/?/!?
☐ Точки и запятые без лишних пробелов перед ними?
☐ Нет калек с английского (корявых конструкций, которые по-русски не говорят)?
☐ Нет «висящих» фактов без связи с главной темой?
 
Текст исходной новости:
{article_text}
 
Верни ответ СТРОГО в формате JSON, без других слов, без markdown-обёрток:
{{"title": "русский заголовок с точкой в конце.", "text": "первый абзац\\n\\nвторой абзац\\n\\nтретий абзац"}}"""
 
 
def call_groq(prompt, api_key, model, json_mode=False, temperature=0.7):
    """
    Делает запрос к Groq Chat Completions API. Возвращает текст ответа или None.
 
    json_mode=True — гарантированный JSON-вывод (response_format).
                     Использовать для куратора и рерайтера.
                     Для дубль-чека (где нужен YES/NO) — оставлять False.
    """
    if not api_key:
        print("✗ GROQ_API_KEY не задан!")
        return None
 
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
 
    try:
        r = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=60)
        if r.status_code != 200:
            print(f"✗ HTTP {r.status_code}: {r.text[:300]}")
            return None
        data = r.json()
        usage = data.get("usage", {})
        if usage:
            print(f"  Токенов: вход {usage.get('prompt_tokens', 0)}, "
                  f"выход {usage.get('completion_tokens', 0)}, "
                  f"всего {usage.get('total_tokens', 0)}")
        choices = data.get("choices", [])
        if not choices:
            print(f"✗ Нет choices в ответе: {data}")
            return None
        return choices[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"✗ Запрос упал: {e}")
        return None
 
 
def curator_pick(pool_items, published_guids, api_key):
    """Отдаёт куратору список заголовков, получает выбор."""
    candidates = [it for it in pool_items if it["guid"] not in published_guids]
    if not candidates:
        print("В пуле нет неопубликованных кандидатов")
        return None
 
    print(f"\nКуратор выбирает из {len(candidates)} кандидатов (модель: {MODEL_CURATOR})...")
    titles_text = "\n".join(
        f"- guid: {it['guid']} | {it['title']}" for it in candidates
    )
    prompt = CURATOR_PROMPT.format(titles=titles_text)
 
    answer = call_groq(prompt, api_key, model=MODEL_CURATOR, json_mode=True, temperature=0.4)
    if not answer:
        return None
 
    answer = re.sub(r"^```(?:json)?\s*", "", answer)
    answer = re.sub(r"\s*```$", "", answer)
    try:
        decision = json.loads(answer, strict=False)
        chosen_guid = decision.get("guid")
        reason = decision.get("reason", "")
    except Exception as e:
        print(f"✗ Не смог распарсить ответ куратора: {e}")
        print(f"  Ответ был: {answer[:300]}")
        return None
 
    chosen = next((it for it in candidates if it["guid"] == chosen_guid), None)
    if not chosen:
        print(f"✗ Куратор вернул guid {chosen_guid}, но его нет в кандидатах")
        return None
 
    print(f"\n✓ Выбрано: {chosen['title']}")
    print(f"  Причина: {reason}")
    return chosen
 
 
def is_duplicate_story(chosen_title, published_titles, api_key):
    """Проверяет через ИИ — не является ли выбранная статья дублём уже опубликованных."""
    if not published_titles:
        return False
 
    titles_list = "\n".join(f"- {t}" for t in published_titles[-10:])
    prompt = (
        f"Перед тобой заголовок новой статьи и список уже опубликованных заголовков.\n\n"
        f"Новая статья: {chosen_title}\n\n"
        f"Уже опубликованные заголовки:\n{titles_list}\n\n"
        f"Вопрос: рассказывает ли новая статья по сути об ТОМ ЖЕ событии что и одна из "
        f"уже опубликованных? Учитывай смысл, а не точное совпадение слов.\n\n"
        f"Верни СТРОГО одно слово: YES если это дубль, NO если это другая история."
    )
    print(f"  Дубль-чек (модель: {MODEL_DEDUP})...")
    answer = call_groq(prompt, api_key, model=MODEL_DEDUP, json_mode=False, temperature=0.0)
    if not answer:
        return False
    is_dup = answer.strip().upper().startswith("YES")
    if is_dup:
        print(f"  ⚠ Дубль обнаружен: '{chosen_title}' похожа на уже опубликованную")
    return is_dup
 
 
def strip_emojis(text):
    """
    Удаляет эмодзи из текста (если ИИ всё-таки их вставил, несмотря на запрет).
    Покрывает основные диапазоны Unicode для эмодзи.
    """
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # эмоции
        "\U0001F300-\U0001F5FF"  # символы и пиктограммы
        "\U0001F680-\U0001F6FF"  # транспорт
        "\U0001F700-\U0001F77F"  # алхимия
        "\U0001F780-\U0001F7FF"  # геом. фигуры
        "\U0001F800-\U0001F8FF"  # стрелки доп.
        "\U0001F900-\U0001F9FF"  # доп. эмодзи
        "\U0001FA00-\U0001FA6F"  # шахматы
        "\U0001FA70-\U0001FAFF"  # символы доп.
        "\U00002600-\U000026FF"  # разные символы
        "\U00002700-\U000027BF"  # дингбаты
        "\U0001F1E0-\U0001F1FF"  # флаги
        "\U0001F200-\U0001F2FF"  # CJK символы
        "]+",
        flags=re.UNICODE
    )
    cleaned = emoji_pattern.sub("", text)
    # Убираем двойные пробелы, оставшиеся после удаления эмодзи
    cleaned = re.sub(r"  +", " ", cleaned)
    # Чистим пробелы вокруг переносов строк
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()
 
 
def rewrite_article(article_text, api_key):
    """Просит ИИ сделать рерайт+перевод. Возвращает (title, text) или None.
    Подпись 'Пиксель' добавляется здесь же, в коде."""
    if len(article_text) > TEXT_FOR_REWRITER_LIMIT:
        article_text = article_text[:TEXT_FOR_REWRITER_LIMIT]
        last_dot = article_text.rfind(". ")
        if last_dot > TEXT_FOR_REWRITER_LIMIT * 0.7:
            article_text = article_text[:last_dot + 1]
 
    print(f"\nРерайтер пишет пост на вход {len(article_text)} знаков (модель: {MODEL_REWRITER})...")
    prompt = REWRITER_PROMPT.format(article_text=article_text, max_chars=MAX_TOTAL_CHARS)
    answer = call_groq(prompt, api_key, model=MODEL_REWRITER, json_mode=True, temperature=0.6)
    if not answer:
        return None
 
    answer = re.sub(r"^```(?:json)?\s*", "", answer)
    answer = re.sub(r"\s*```$", "", answer)
    try:
        data = json.loads(answer, strict=False)
        title = data.get("title", "").strip()
        text = data.get("text", "").strip()
        if not title or not text:
            print("✗ В ответе нет title или text")
            print(f"  Ответ: {answer[:300]}")
            return None
 
        # Постобработка — гарантированно убираем эмодзи (если ИИ нарушил)
        title_clean = strip_emojis(title)
        text_clean = strip_emojis(text)
 
        # Если в тексте нет \n\n — пробуем разбить по точкам (грубый фоллбэк)
        if "\n\n" not in text_clean:
            print("  ⚠ ИИ не разбил текст на абзацы. Пытаюсь разбить вручную.")
            sentences = re.split(r'(?<=[.!?])\s+', text_clean)
            if len(sentences) >= 4:
                # 2 абзаца: первая половина + вторая половина
                mid = len(sentences) // 2
                text_clean = " ".join(sentences[:mid]) + "\n\n" + " ".join(sentences[mid:])
 
        # Добавляем захардкоженную подпись
        full_text = text_clean + "\n\n" + PIXEL_SIGNATURE
 
        # Логирование размеров
        ai_chars = len(title_clean) + len(text_clean)
        total_chars = len(title_clean) + len(full_text)
        print(f"✓ Заголовок: {title_clean}")
        print(f"✓ Текст ИИ: {len(text_clean)} знаков")
        print(f"  title + text от ИИ: {ai_chars} символов (лимит {MAX_TOTAL_CHARS})")
        print(f"  Итого с подписью: {total_chars} символов (лимит TG: 1024)")
 
        if ai_chars > MAX_TOTAL_CHARS:
            print(f"  ⚠ Превышение лимита на {ai_chars - MAX_TOTAL_CHARS} симв. Пост всё равно отправляется.")
        if total_chars > 1024:
            print(f"  ⚠ ПРЕВЫШЕНИЕ TELEGRAM-ЛИМИТА на {total_chars - 1024} симв. Возможна обрезка!")
 
        return title_clean, full_text
    except Exception as e:
        print(f"✗ Не смог распарсить JSON рерайтера: {e}")
        print(f"  Ответ был: {answer[:300]}")
        return None
 
 
def add_to_feed(item, post_text):
    """Добавляет одну запись в feed.xml (или создаёт его)."""
    existing = []
    if os.path.exists(OUTPUT_FEED):
        try:
            import feedparser
            parsed = feedparser.parse(OUTPUT_FEED)
            for e in parsed.entries:
                pubdate = datetime.now(timezone.utc)
                if e.get("published_parsed"):
                    try:
                        pubdate = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                existing.append({
                    "title": e.get("title", ""),
                    "guid": e.get("id", e.get("link", "")),
                    "pubdate": pubdate,
                    "fulltext": e.get("summary", ""),
                })
        except Exception as e:
            print(f"Не удалось прочитать старый feed: {e}")
 
    new_entry = {
        "title": item["title"],
        "guid": item["guid"],
        "pubdate": datetime.now(timezone.utc),
        "fulltext": post_text,
    }
 
    all_items = [new_entry] + [
        e for e in existing if e["guid"] != new_entry["guid"]
    ]
    all_items.sort(key=lambda x: x["pubdate"], reverse=True)
    all_items = all_items[:MAX_ITEMS_IN_FEED]
 
    fg = FeedGenerator()
    fg.title(FEED_TITLE)
    fg.link(href=FEED_LINK, rel="alternate")
    fg.description(FEED_DESCRIPTION)
    fg.language("ru")
 
    for it in all_items:
        fe = fg.add_entry(order='append')
        fe.title(it["title"])
        fe.link(href=ITEM_LINK_PLACEHOLDER)
        fe.guid(it["guid"], permalink=False)
        fe.pubDate(it["pubdate"])
        fe.description(it["fulltext"])
 
    fg.rss_file(OUTPUT_FEED, pretty=True)
    print(f"\n✓ feed.xml обновлён ({len(all_items)} записей)")
 
 
def main():
    print(f"=== Публикатор v2.1 (Groq): {datetime.now(timezone.utc).isoformat()} ===")
 
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("✗ Переменная GROQ_API_KEY не задана. Прерываюсь.")
        sys.exit(1)
 
    pool = load_json(POOL_FILE, {"items": []})
    pool_items = pool.get("items", [])
    if not pool_items:
        print("Пул пустой, нечего публиковать")
        return
 
    published = load_json(PUBLISHED_FILE, {"guids": [], "titles": []})
    published_guids = set(published.get("guids", []))
    published_titles = published.get("titles", [])
 
    max_attempts = 5
    attempt = 0
    success = False
 
    while attempt < max_attempts and not success:
        attempt += 1
        print(f"\n--- Итерация {attempt} из {max_attempts} ---")
 
        chosen = curator_pick(pool_items, published_guids, api_key)
        if not chosen:
            print("Куратор не смог выбрать (или пул исчерпан). Завершаю поиск.")
            break
 
        time.sleep(REQUEST_PAUSE_SEC)
 
        if is_duplicate_story(chosen["title"], published_titles, api_key):
            print("Выбранная статья — смысловой дубль. Помечаю GUID и ищу следующую.")
            published_guids.add(chosen["guid"])
            save_json(PUBLISHED_FILE, {
                "guids": list(published_guids)[-100:],
                "titles": published_titles
            })
            time.sleep(REQUEST_PAUSE_SEC)
            continue
 
        time.sleep(REQUEST_PAUSE_SEC)
 
        print(f"\nСкачиваю полный текст: {chosen['original_url']}")
        fulltext, _ = get_full_text(chosen["original_url"])
        if not fulltext:
            print("✗ Не удалось скачать текст. Помечу GUID и ищу дальше.")
            published_guids.add(chosen["guid"])
            save_json(PUBLISHED_FILE, {
                "guids": list(published_guids)[-100:],
                "titles": published_titles
            })
            continue
 
        rewrite_result = rewrite_article(fulltext, api_key)
        if not rewrite_result:
            print("✗ Рерайт не получился. Помечу GUID и ищу дальше.")
            published_guids.add(chosen["guid"])
            save_json(PUBLISHED_FILE, {
                "guids": list(published_guids)[-100:],
                "titles": published_titles
            })
            time.sleep(REQUEST_PAUSE_SEC)
            continue
 
        post_title, post_text = rewrite_result
        chosen_with_ru_title = dict(chosen)
        chosen_with_ru_title["title"] = post_title
        add_to_feed(chosen_with_ru_title, post_text)
 
        published_guids.add(chosen["guid"])
        published_titles.append(post_title)
        save_json(PUBLISHED_FILE, {
            "guids": list(published_guids)[-100:],
            "titles": published_titles[-30:]
        })
 
        success = True
        print("\n=== Публикатор v2.1 успешно завершил работу ===")
 
    if not success:
        print("\n✗ Не удалось опубликовать статью за отведённое число попыток.")
 
 
if __name__ == "__main__":
    main()
 
