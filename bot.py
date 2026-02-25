"""
SnapSell Bot — AI фото для маркетплейсов
Стек: python-telegram-bot + Google Gemini (бесплатно) + Pollinations.AI (бесплатно)
"""

import asyncio
import logging
import os
import base64
import json
import re
import httpx
from io import BytesIO
from datetime import datetime
from pathlib import Path

# ── Загружаем .env автоматически (без лишних библиотек) ──
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, filters, ContextTypes
)
from telegram.constants import ParseMode, ChatAction

from db import Database
from config import config

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

db = Database()

# ─────────────────────────────────────────────
# ТЕКСТЫ
# ─────────────────────────────────────────────

WELCOME = """
✦ *Добро пожаловать в SnapSell!*

Я превращаю фото вашего товара в 4 профессиональных снимка для маркетплейсов:

📸 *Витрина* — товар на красивом стенде/столе
🧍 *Лайфстайл* — товар с человеком
🏠 *Интерьер* — товар в доме или офисе  
🔍 *Крупный план* — детали и текстура

Просто *пришлите фото* вашего товара — никакого текста не нужно!

У вас *{free_left} бесплатных генераций*.
"""

SEND_PHOTO_PROMPT = """
📷 Пришлите фото вашего товара.

Советы для лучшего результата:
• Хорошее освещение
• Товар на нейтральном или однотонном фоне
• Чёткое изображение без размытия
"""

ANALYZING = "🔍 *Шаг 1/3* — Анализирую товар..."
PROMPTING  = "✍️ *Шаг 2/3* — Создаю сцены для фото..."
RENDERING  = "🎨 *Шаг 3/3* — Генерирую изображения..."

PAYWALL = """
⭐ *Бесплатные попытки закончились*

Вы использовали все бесплатные генерации.
Выберите план, чтобы продолжить:
"""

SUCCESS = """
✅ *Готово! Ваши 4 фото для маркетплейса*

Нажмите на любое фото — оно откроется в полном размере.
Можно сразу загружать на Wildberries, Ozon, Avito и другие площадки.
"""

SCENES = [
    {
        "emoji": "🏪",
        "name": "Витрина",
        "prompt_desc": "elegant product display on a premium marble table or illuminated store shelf, professional studio lighting with soft shadows, clean minimal background, high-end retail photography"
    },
    {
        "emoji": "🧍",
        "name": "Лайфстайл",
        "prompt_desc": "lifestyle photography with a person naturally using or wearing the product, warm natural light, blurred modern interior background, authentic candid moment, editorial style"
    },
    {
        "emoji": "🏠",
        "name": "Интерьер",
        "prompt_desc": "product beautifully arranged in a cozy Scandinavian home interior, morning window light, minimalist decor, atmospheric depth of field, hygge aesthetic"
    },
    {
        "emoji": "🔍",
        "name": "Крупный план",
        "prompt_desc": "extreme close-up macro photography of the product, dramatic side lighting highlighting texture and material, ultra-sharp details, dark luxury background, premium hero shot"
    }
]

# ─────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────

async def analyze_product_with_gemini(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Используем Google Gemini для анализа товара и генерации промтов."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    PROMPT = """You are a professional commercial photographer and product marketing expert.

Analyze this product image carefully and return ONLY a valid JSON object (no markdown, no explanation):

{
  "product_en": "concise English product name (e.g. 'ceramic coffee mug', 'leather wallet', 'silk scarf')",
  "product_ru": "название товара по-русски",
  "category": "category in English: clothing/accessories/electronics/food/cosmetics/jewelry/home_decor/toys/sports/other",
  "colors": ["primary color", "secondary color if any"],
  "style": "one word: modern/vintage/luxury/casual/minimalist/bohemian/sporty/classic",
  "material": "main material if visible, else empty string",
  "features": "2-3 key visual characteristics, comma separated",
  "scenes": {
    "display": "hyper-detailed 80-word prompt for studio/shelf scene of THIS EXACT product with these features and colors",
    "lifestyle": "hyper-detailed 80-word prompt for lifestyle/person scene of THIS EXACT product",
    "interior": "hyper-detailed 80-word prompt for home/office interior scene of THIS EXACT product",
    "closeup": "hyper-detailed 80-word prompt for macro/closeup scene of THIS EXACT product"
  }
}

Each scene prompt MUST:
- Start with: "Professional commercial photography,"
- Include exact product description with its specific colors and materials
- Include specific lighting, background, camera angle
- End with: "photorealistic, 8K resolution, sharp focus, commercial product photography"
"""

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64
                        }
                    },
                    {"text": PROMPT}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2000,
        }
    }

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={config.GEMINI_API_KEY}"
    )

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)


async def generate_image_pollinations(prompt: str, seed: int = 42, width: int = 1024, height: int = 1024) -> bytes:
    """Генерируем изображение через Pollinations.AI (бесплатно, без ключа)."""
    # Кодируем промт
    import urllib.parse
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&seed={seed}&model=flux&nologo=true&enhance=true"

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def build_scene_prompt(product_info: dict, scene_key: str, scene_cfg: dict) -> str:
    """Строим финальный промт из анализа Claude + описания сцены."""
    base = product_info.get("scenes", {}).get(scene_key, "")
    if base:
        return base
    # Fallback если Claude не вернул сцены
    name = product_info.get("product_en", "product")
    colors = ", ".join(product_info.get("colors", ["neutral"]))
    style = product_info.get("style", "modern")
    return (
        f"Professional commercial photography, {name}, {colors} colors, {style} style, "
        f"{scene_cfg['prompt_desc']}, photorealistic, 8K resolution, sharp focus, commercial product photography"
    )


# ─────────────────────────────────────────────
# ХЭНДЛЕРЫ
# ─────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")
    free_left = max(0, config.FREE_GENERATIONS - db.get_uses(user.id))

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📸 Загрузить фото товара", callback_data="send_photo")
    ]])

    await update.message.reply_text(
        WELCOME.format(free_left=free_left),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Как пользоваться SnapSell:*\n\n"
        "1. Отправьте фото вашего товара\n"
        "2. Подождите ~30–60 секунд\n"
        "3. Получите 4 профессиональных фото\n\n"
        "*Команды:*\n"
        "/start — Главное меню\n"
        "/balance — Проверить баланс генераций\n"
        "/plans — Тарифные планы\n"
        "/help — Эта справка\n\n"
        "📞 Поддержка: @your\\_support"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id)
    uses = db.get_uses(user_id)
    plan = db.get_plan(user_id)
    is_paid = plan in ("basic", "pro")

    if is_paid:
        if plan == "pro":
            text = "⭐ *Тариф PRO* — безлимитные генерации"
        else:
            remaining = db.get_paid_remaining(user_id)
            text = f"💎 *Тариф Базовый* — осталось генераций: *{remaining}*"
    else:
        free_left = max(0, config.FREE_GENERATIONS - uses)
        text = (
            f"🆓 *Бесплатный план*\n"
            f"Использовано: {uses} / {config.FREE_GENERATIONS}\n"
            f"Осталось: *{free_left}*"
        )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💳 Купить генерации", callback_data="show_plans")
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_plans_message(update.message)


async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "send_photo":
        await query.message.reply_text(SEND_PHOTO_PROMPT, parse_mode=ParseMode.MARKDOWN)

    elif data == "show_plans":
        await show_plans_message(query.message)

    elif data.startswith("buy_"):
        plan = data.split("_", 1)[1]
        await initiate_payment(query, ctx, plan)


async def show_plans_message(message):
    text = (
        "💳 *Тарифные планы SnapSell*\n\n"
        "🆓 *Бесплатно* — 3 генерации\n\n"
        "💎 *Базовый — 149 ⭐ Звёзд*\n"
        "  • 30 генераций\n"
        "  • Все 4 сцены\n"
        "  • Скачивание в высоком качестве\n\n"
        "🚀 *PRO — 499 ⭐ Звёзд / мес*\n"
        "  • Безлимитные генерации\n"
        "  • Приоритетная обработка\n"
        "  • Поддержка в чате\n\n"
        "_1 ⭐ Telegram Star ≈ 0.013 USD_"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Базовый — 149 ⭐", callback_data="buy_basic")],
        [InlineKeyboardButton("🚀 PRO — 499 ⭐", callback_data="buy_pro")],
    ])
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def initiate_payment(query, ctx: ContextTypes.DEFAULT_TYPE, plan: str):
    plans = {
        "basic": {"title": "SnapSell Базовый", "desc": "30 генераций профессиональных фото", "stars": 149, "label": "30 генераций"},
        "pro":   {"title": "SnapSell PRO",     "desc": "Безлимитные генерации на 30 дней",  "stars": 499, "label": "PRO на 30 дней"},
    }
    p = plans.get(plan)
    if not p:
        return

    await ctx.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=p["title"],
        description=p["desc"],
        payload=f"plan_{plan}_{query.from_user.id}",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label=p["label"], amount=p["stars"])],
        provider_token="",  # пусто для Stars
    )


async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    user_id = update.effective_user.id

    if payload.startswith("plan_basic_"):
        db.set_plan(user_id, "basic", generations=30)
        text = "✅ *Оплата прошла!* Вам зачислено *30 генераций*.\nОтправьте фото товара!"
    elif payload.startswith("plan_pro_"):
        db.set_plan(user_id, "pro", days=30)
        text = "✅ *PRO активирован!* У вас безлимитные генерации на 30 дней.\nОтправьте фото товара!"
    else:
        text = "✅ Оплата получена!"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")

    # Проверяем доступ
    if not db.can_generate(user.id):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💳 Выбрать план", callback_data="show_plans")
        ]])
        await update.message.reply_text(PAYWALL, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    # Сообщение о начале работы
    status_msg = await update.message.reply_text(
        ANALYZING, parse_mode=ParseMode.MARKDOWN
    )

    try:
        # ── ШАГ 1: Скачиваем фото ──
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        photo = update.message.photo[-1]  # наибольшее разрешение
        file = await ctx.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        image_bytes = buf.getvalue()

        # ── ШАГ 2: Claude анализирует товар ──
        product_info = await analyze_product_with_gemini(image_bytes)
        product_ru = product_info.get("product_ru", "товар")
        logger.info(f"User {user.id} | Product: {product_info.get('product_en')} | Category: {product_info.get('category')}")

        await status_msg.edit_text(PROMPTING, parse_mode=ParseMode.MARKDOWN)

        # ── ШАГ 3: Генерируем 4 изображения ──
        await status_msg.edit_text(RENDERING, parse_mode=ParseMode.MARKDOWN)
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

        scene_keys = ["display", "lifestyle", "interior", "closeup"]
        tasks = []
        for i, (key, scene_cfg) in enumerate(zip(scene_keys, SCENES)):
            prompt = build_scene_prompt(product_info, key, scene_cfg)
            seed = user.id % 9999 + i * 1000  # уникальный seed на пользователя
            tasks.append(generate_image_pollinations(prompt, seed=seed))

        # Параллельная генерация всех 4 изображений
        images_bytes = await asyncio.gather(*tasks)

        # ── ШАГ 4: Отправляем результат ──
        media_group = []
        for i, (img_bytes, scene_cfg) in enumerate(zip(images_bytes, SCENES)):
            caption = f"{scene_cfg['emoji']} *{scene_cfg['name']}*" if i == 0 else ""
            media_group.append(
                InputMediaPhoto(
                    media=BytesIO(img_bytes),
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN if caption else None
                )
            )

        await status_msg.delete()

        # Отправляем альбом
        await update.message.reply_media_group(media=media_group)

        # Сообщение об успехе
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 Новый товар", callback_data="send_photo")],
            [InlineKeyboardButton("💳 Купить генерации", callback_data="show_plans")],
        ])
        uses_after = db.increment_uses(user.id)
        free_left = max(0, config.FREE_GENERATIONS - uses_after)
        plan = db.get_plan(user.id)

        footer = ""
        if plan == "free":
            footer = f"\n\n🆓 Осталось бесплатных генераций: *{free_left}*"
        elif plan == "basic":
            footer = f"\n\n💎 Осталось генераций: *{db.get_paid_remaining(user.id)}*"
        else:
            footer = "\n\n🚀 PRO активен — генерируйте без ограничений"

        await update.message.reply_text(
            SUCCESS + footer,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )

        db.log_generation(user.id, product_info.get("product_en", "unknown"))

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error for user {user.id}: {e}")
        await status_msg.edit_text(
            "❌ Ошибка при обращении к API. Попробуйте позже или обратитесь в поддержку: @your_support"
        )
    except json.JSONDecodeError:
        logger.error(f"JSON parse error for user {user.id}")
        await status_msg.edit_text(
            "❌ Не удалось распознать товар на фото. Попробуйте другое фото с более чётким изображением товара."
        )
    except Exception as e:
        logger.error(f"Unexpected error for user {user.id}: {e}", exc_info=True)
        await status_msg.edit_text(
            "❌ Что-то пошло не так. Попробуйте ещё раз или напишите в поддержку: @your_support"
        )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Если пользователь написал текст вместо фото."""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📸 Я понял, отправляю фото!", callback_data="send_photo")
    ]])
    await update.message.reply_text(
        "📷 Пожалуйста, отправьте *фото товара* — текст не нужен!\n\n"
        "Просто пришлите снимок, и я всё сделаю сам.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )


# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────

def main():
    logger.info("Запуск SnapSell Bot...")
    app = Application.builder().token(config.BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("plans",   cmd_plans))

    # Фото
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Документы как фото (некоторые клиенты отправляют так)
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))

    # Кнопки
    app.add_handler(CallbackQueryHandler(cb_handler))

    # Платежи (Telegram Stars)
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Любой текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
