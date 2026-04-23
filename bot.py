import os
import logging
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import anthropic
import httpx
import base64

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "8776654437:AAGXhvSgpTtZV5T5ZBYRjjIyTP-th_JJunE")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
APPS_SCRIPT_URL   = os.getenv("APPS_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbw7K9Dt68qUyNBiykNw5KxXEhwheJGyzdRLa92Icm2DfghlzLkqB06GJaEMDMliHAEFmg/exec")
APPS_SCRIPT_SECRET = "SMETA_SECRET_2025"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ─── SYSTEM PROMPT ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Ты — SMETA.MD, профессиональный AI-агент сметчик электромонтажных работ для молдавского рынка (цены в MDL).

ГЛАВНЫЕ ПРАВИЛА:
1. Сначала КВАЛИФИЦИРУЙ объект — задай 3-5 конкретных вопросов
2. Смета ВСЕГДА делится на 2 этапа:
   - ЧЕРНОВОЙ МОНТАЖ (штробление, кабель, гофра, подрозетники, щит, ввод)
   - ЧИСТОВОЙ МОНТАЖ (розетки, выключатели, светильники, приборы)
3. КАБЕЛЬ считай по длине трасс + 10% запас
4. МАТЕРИАЛЫ не включай в работы — отдельно по запросу
5. НДС — два варианта: с НДС 20% и без
6. Если указан ID объекта (ID0199 и т.д.) — запомни для CRM

ВОПРОСЫ ДЛЯ КВАЛИФИКАЦИИ:
- Тип объекта (квартира / дом / офис / производство)
- Площадь и количество этажей
- Из чего стены (бетон / газоблок / кирпич / гипсокартон)
- Новострой или реконструкция
- Есть ли дизайн-проект / спецификация

НОРМЫ ДЛЯ РАСЧЁТА КАБЕЛЯ (трасса + 10%):
NYM 3x1.5 освещение: 1.3 м/м²
NYM 3x2.5 розетки: 2.0 м/м²
Гофра 20мм: длина кабеля × 0.95
Штроба: длина кабеля × 0.85

РАСЦЕНКИ НА РАБОТЫ (MDL, Молдова 2025):

ЧЕРНОВОЙ:
Штробление бетон: 220 MDL/м
Штробление газоблок: 120 MDL/м
Заделка штроб: 50 MDL/м
Прокладка гофры 20мм: 12 MDL/м
Прокладка NYM 3x1.5: 25 MDL/м
Прокладка NYM 3x2.5: 30 MDL/м
Прокладка NYM 5x2.5: 45 MDL/м
Прокладка NYM 5x10 (ввод): 80 MDL/м
Подрозетник бетон: 170 MDL/шт
Подрозетник газоблок: 80 MDL/шт
Щит до 12 авт. под ключ: 3000 MDL
Щит до 24 авт. под ключ: 5500 MDL
Щит до 36 авт. под ключ: 8000 MDL
Заземление: 2500 MDL
Межэтажный стояк NYM 5x4: 65 MDL/м

ЧИСТОВОЙ:
Розетка 220V: 100 MDL/шт
Розетка 380V (плита): 800 MDL/шт
Розетка 380V (духовка/кондиц.): 500 MDL/шт
Выключатель: 85 MDL/шт
Выключатель переходной: 130 MDL/шт
Мастер-выключатель: 200 MDL/шт
Розетка интернет RJ45: 150 MDL/шт
Датчик движения: 250 MDL/шт
Терморегулятор: 300 MDL/шт
Светильник потолочный: 80 MDL/шт
Магнитный трек: 250 MDL/м
LED лента: 100 MDL/м
Трансформатор LED: 400 MDL/шт
Привод штор: 350 MDL/шт
Кондиционер: 500 MDL/шт
Посудомойка: 250 MDL/шт
Домофон: 600 MDL/шт
Тёплый пол: 500 MDL/комн

ФОРМАТ СМЕТЫ (используй таблицы с разделителями):

🔨 ЭТАП 1: ЧЕРНОВОЙ МОНТАЖ

Наименование | Ед | Кол | Цена | Сумма
---|---|---|---|---
Штробление бетон | м | 100 | 220 | 22 000

ИТОГО ЧЕРНОВОЙ: X MDL

✨ ЭТАП 2: ЧИСТОВОЙ МОНТАЖ

Наименование | Ед | Кол | Цена | Сумма
---|---|---|---|---

ИТОГО ЧИСТОВОЙ: X MDL

📊 ИТОГ:
Черновой: X MDL
Чистовой: X MDL
БЕЗ НДС: X MDL
С НДС 20%: X MDL

📐 Статистика: кабель ~Xм, точек Xшт, нагрузка ~XкВт
⚠️ Цены ориентировочные. Актуальные цены: volta.md

Отвечай на русском. Цены только в MDL. Будь конкретным."""

# ─── CONVERSATION STORAGE ─────────────────────────────────────────────────────
# chat_id -> list of messages
conversations = {}

def get_history(chat_id: int) -> list:
    return conversations.get(chat_id, [])

def add_message(chat_id: int, role: str, content):
    if chat_id not in conversations:
        conversations[chat_id] = []
    conversations[chat_id].append({"role": role, "content": content})
    # Keep last 20 messages to avoid token overflow
    if len(conversations[chat_id]) > 20:
        conversations[chat_id] = conversations[chat_id][-20:]

def clear_history(chat_id: int):
    conversations[chat_id] = []

# ─── CLAUDE API ───────────────────────────────────────────────────────────────
async def ask_claude(chat_id: int, user_content) -> str:
    """Send message to Claude and get response."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    add_message(chat_id, "user", user_content)
    history = get_history(chat_id)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=history
        )
        reply = response.content[0].text
        add_message(chat_id, "assistant", reply)
        return reply
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return f"⚠️ Ошибка API: {str(e)}"

# ─── SYNC TO GOOGLE SHEETS ────────────────────────────────────────────────────
async def sync_to_sheets(object_id: str, smeta_url: str = "") -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(APPS_SCRIPT_URL, json={
                "secret": APPS_SCRIPT_SECRET,
                "action": "writeSmetaLink",
                "objectId": object_id,
                "smetaUrl": smeta_url or f"Смета {object_id} — Telegram"
            })
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ─── EXTRACT OBJECT ID ────────────────────────────────────────────────────────
def extract_object_id(text: str) -> str | None:
    import re
    m = re.search(r'ID\d{4,}', text, re.IGNORECASE)
    return m.group(0).upper() if m else None

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    clear_history(chat_id)

    keyboard = [
        [InlineKeyboardButton("🏠 Квартира", callback_data="type_apartment"),
         InlineKeyboardButton("🏡 Частный дом", callback_data="type_house")],
        [InlineKeyboardButton("🏢 Офис/Магазин", callback_data="type_office"),
         InlineKeyboardButton("🏭 Производство", callback_data="type_industrial")],
        [InlineKeyboardButton("📎 Загрузить проект (PDF)", callback_data="type_pdf")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⚡ *SMETA.MD — AI Агент-сметчик*\n\n"
        "Составляю профессиональные сметы на электромонтаж:\n\n"
        "🔨 *Черновой монтаж* — кабель, штробление, щит\n"
        "✨ *Чистовой монтаж* — розетки, светильники, приборы\n"
        "💰 *Два варианта* — с НДС 20% и без\n"
        "📊 *CRM* — смета записывается в Google Sheets\n\n"
        "Выберите тип объекта или опишите своими словами:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    clear_history(chat_id)
    await update.message.reply_text("🔄 Начинаем новый расчёт!\n\nОпишите объект или отправьте PDF проекта.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *SMETA.MD — Помощь*\n\n"
        "📌 *Команды:*\n"
        "/start — Начать новую смету\n"
        "/new — Сбросить и начать заново\n"
        "/help — Эта справка\n\n"
        "📌 *Как пользоваться:*\n"
        "1. Опишите объект текстом\n"
        "2. Или отправьте PDF проекта\n"
        "3. Ответьте на вопросы агента\n"
        "4. Получите готовую смету\n"
        "5. Смета записывается в CRM автоматически\n\n"
        "📌 *Примеры запросов:*\n"
        "• _ID0199, квартира 2к, 65м², новострой_\n"
        "• _Дом 200м², 2 этажа, газоблок_\n"
        "• _Офис 100м², 10 рабочих мест_",
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    prompts = {
        "type_apartment":  "Квартира. Помоги составить смету на электромонтаж.",
        "type_house":      "Частный дом. Нужна смета на электромонтаж.",
        "type_office":     "Офис или торговое помещение. Нужна смета.",
        "type_industrial": "Производственный объект 380В. Нужна смета.",
        "type_pdf":        None,
    }

    if query.data == "type_pdf":
        await query.edit_message_text("📎 Отправьте PDF проект или фото плана — я его прочитаю и составлю смету.")
        return

    prompt = prompts.get(query.data)
    if not prompt:
        return

    await query.edit_message_text(f"✅ {query.data.replace('type_', '').title()} выбран. Отвечаю...")

    msg = await query.message.reply_text("⏳ Анализирую...")
    reply = await ask_claude(chat_id, prompt)

    # Split long messages
    await send_long_message(query.message, reply)
    await msg.delete()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    # Show typing
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Check for object ID
    obj_id = extract_object_id(text)
    if obj_id:
        context.user_data["object_id"] = obj_id

    reply = await ask_claude(chat_id, text)

    # Check if estimate is ready and offer CRM sync
    has_estimate = "ИТОГО ЧЕРНОВОЙ" in reply or "ИТОГО РАБОТЫ" in reply or "БЕЗ НДС" in reply

    if has_estimate and obj_id:
        keyboard = [[
            InlineKeyboardButton("📊 Записать в CRM", callback_data=f"sync_{obj_id}"),
            InlineKeyboardButton("🔄 Новый расчёт", callback_data="new_calc"),
        ]]
        await send_long_message(update.message, reply, InlineKeyboardMarkup(keyboard))
    elif has_estimate:
        keyboard = [[InlineKeyboardButton("🔄 Новый расчёт", callback_data="new_calc")]]
        await send_long_message(update.message, reply, InlineKeyboardMarkup(keyboard))
    else:
        await send_long_message(update.message, reply)

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF documents."""
    chat_id = update.effective_chat.id
    doc = update.message.document

    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("⚠️ Поддерживаются только PDF файлы.")
        return

    msg = await update.message.reply_text("📄 Читаю проект...")
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # Download file
        file = await context.bot.get_file(doc.file_id)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            await file.download_to_drive(f.name)
            with open(f.name, "rb") as pdf_file:
                pdf_data = base64.standard_b64encode(pdf_file.read()).decode("utf-8")

        # Send to Claude with PDF
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        caption = update.message.caption or "Проанализируй этот проект и составь подробную смету на электромонтаж. Раздели на черновой и чистовой этапы."

        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_data}},
            {"type": "text", "text": caption}
        ]

        add_message(chat_id, "user", content)
        history = get_history(chat_id)

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=history
        )
        reply = response.content[0].text
        add_message(chat_id, "assistant", reply)

        await msg.delete()
        await send_long_message(update.message, reply)

    except Exception as e:
        await msg.edit_text(f"⚠️ Ошибка при обработке PDF: {str(e)}")

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo/image of project plan."""
    chat_id = update.effective_chat.id

    msg = await update.message.reply_text("🖼 Читаю план...")
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        photo = update.message.photo[-1]  # highest resolution
        file = await context.bot.get_file(photo.file_id)

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            await file.download_to_drive(f.name)
            with open(f.name, "rb") as img_file:
                img_data = base64.standard_b64encode(img_file.read()).decode("utf-8")

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        caption = update.message.caption or "Проанализируй этот план и составь смету на электромонтаж."

        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_data}},
            {"type": "text", "text": caption}
        ]

        add_message(chat_id, "user", content)
        history = get_history(chat_id)

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=history
        )
        reply = response.content[0].text
        add_message(chat_id, "assistant", reply)

        await msg.delete()
        await send_long_message(update.message, reply)

    except Exception as e:
        await msg.edit_text(f"⚠️ Ошибка: {str(e)}")

async def callback_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle CRM sync and new calc buttons."""
    query = update.callback_query
    await query.answer()

    if query.data.startswith("sync_"):
        obj_id = query.data.replace("sync_", "")
        await query.edit_message_reply_markup(None)
        msg = await query.message.reply_text(f"⏳ Записываю смету для {obj_id} в CRM...")
        result = await sync_to_sheets(obj_id)
        if result.get("ok"):
            await msg.edit_text(f"✅ Смета записана в CRM!\nОбъект: *{result.get('objectName', obj_id)}*\nСтрока: {result.get('row', '?')}", parse_mode="Markdown")
        else:
            await msg.edit_text(f"❌ Ошибка CRM: {result.get('error', 'Неизвестная ошибка')}")

    elif query.data == "new_calc":
        chat_id = query.message.chat_id
        clear_history(chat_id)
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("🔄 Начинаем новый расчёт!\n\nОпишите объект или отправьте PDF проекта.")

# ─── HELPER: SPLIT LONG MESSAGES ─────────────────────────────────────────────
async def send_long_message(message, text: str, reply_markup=None):
    """Telegram max message length is 4096 chars. Split if needed."""
    max_len = 4000
    if len(text) <= max_len:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode=None)
        return

    # Split by paragraphs
    parts = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            if current:
                parts.append(current.strip())
            current = line
        else:
            current += "\n" + line
    if current:
        parts.append(current.strip())

    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        await message.reply_text(part, reply_markup=reply_markup if is_last else None, parse_mode=None)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not ANTHROPIC_API_KEY:
        log.error("❌ ANTHROPIC_API_KEY не установлен!")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(callback_sync, pattern="^(sync_|new_calc)"))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.PDF, document_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    log.info("🚀 SMETA.MD Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
