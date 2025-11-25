import os
import logging
from typing import Dict, Set, Optional
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot token — Render.com orqali muhit o'zgaruvchisi sifatida beriladi
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Masalan: https://your-render-app.onrender.com/webhook

if not BOT_TOKEN or not WEBHOOK_URL:
    raise ValueError("BOT_TOKEN va WEBHOOK_URL muhit o'zgaruvchilari talab qilinadi.")

# Global o'yin holatlari: chat_id → o'yin ma'lumotlari
games: Dict[int, dict] = {}

# FastAPI ilova
app = FastAPI()

# Telegram Application (python-telegram-bot v20+)
application = Application.builder().token(BOT_TOKEN).build()


# --- Utility funksiyalar ---

def assign_roles(player_ids: list) -> dict:
    """Oyinchilarga rollarni tayinlaydi."""
    from random import shuffle
    n = len(player_ids)
    if n < 3:
        raise ValueError("Kamida 3 ta o'yinchi kerak.")

    roles = ["Mafia"] + ["Doktor", "Komissar"] + ["Fuqaro"] * (n - 3)
    shuffle(roles)
    return dict(zip(player_ids, roles))


def check_game_end(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Agar o'yin tugagan bo'lsa, natijani chiqaradi va True qaytaradi."""
    game = games[chat_id]
    alive = list(game["alive"])
    if not alive:
        return False

    mafia = [p for p in alive if game["roles"][p] == "Mafia"]
    fuqaro = [p for p in alive if game["roles"][p] != "Mafia"]

    if len(mafia) == 0:
        context.bot.send_message(chat_id=chat_id, text="✅ Tinch fuqarolar g'olib bo'ldi!")
        del games[chat_id]
        return True
    elif len(mafia) >= len(fuqaro):
        context.bot.send_message(chat_id=chat_id, text="💀 Mafia g'olib bo'ldi!")
        del games[chat_id]
        return True
    return False


async def end_night_phase(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Kechani yakunlab, natijani e'lon qiladi."""
    game = games[chat_id]
    night_actions = game.get("night_actions", {})

    mafia_target = night_actions.get("mafia", None)
    doctor_protect = night_actions.get("doctor", None)
    sheriff_check = night_actions.get("sheriff", None)

    victim = None
    if mafia_target and mafia_target != doctor_protect:
        victim = mafia_target

    # Natijalarni xabar qilish
    msg = "🌙 Kecha tugadi.\n"
    if victim:
        game["alive"].discard(victim)
        victim_name = game["players"][victim]
        msg += f"💀 {victim_name} o'ldirildi!\n"
    else:
        msg += "Hech kim o'ldirilmadi.\n"

    if sheriff_check:
        role = game["roles"][sheriff_check]
        sheriff_id = next(uid for uid, r in game["roles"].items() if r == "Komissar")
        sheriff_name = game["players"][sheriff_id]
        msg += f"👮 Komissar tekshirdi: {game['players'][sheriff_check]} — {role}\n"

    alive_names = [game["players"][uid] for uid in game["alive"]]
    msg += f"\nTiriklar: {', '.join(alive_names)}"
    await context.bot.send_message(chat_id=chat_id, text=msg)

    # O'yin tugashini tekshirish
    if not check_game_end(chat_id, context):
        game["phase"] = "day"
        game["votes"] = {}
        await context.bot.send_message(chat_id=chat_id, text="☀️ Kun boshlandi. Ovoz berish uchun /vote [ism] buyrug'ini yuboring.")


# --- Buyruqlar ---

async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    games[chat_id] = {
        "players": {},
        "roles": {},
        "alive": set(),
        "started": False,
        "phase": "day",
        "votes": {},
        "night_actions": {},
    }
    await update.message.reply_text("🆕 Yangi o'yin yaratildi! /join orqali qo'shiling.")


async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id not in games:
        await update.message.reply_text("Avval /newgame yuboring.")
        return

    game = games[chat_id]
    if game["started"]:
        await update.message.reply_text("O'yin allaqachon boshlangan. Qo'shila olmaysiz.")
        return

    if user.id in game["players"]:
        await update.message.reply_text("Siz allaqachon ro'yxatdan o'tgansiz.")
        return

    game["players"][user.id] = user.first_name or f"User{user.id}"
    await update.message.reply_text(f"✅ {user.first_name} o'yinga qo'shildi!")


async def players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        await update.message.reply_text("Hali o'yin boshlanmadi. /newgame yuboring.")
        return

    game = games[chat_id]
    if not game["players"]:
        await update.message.reply_text("Hozircha hech kim qo'shilmagan.")
        return

    names = [name for name in game["players"].values()]
    await update.message.reply_text("👥 O'yinchilar:\n" + "\n".join(f"- {n}" for n in names))


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        await update.message.reply_text("Avval /newgame yuboring.")
        return

    game = games[chat_id]
    players = list(game["players"].keys())
    if len(players) < 3:
        await update.message.reply_text("Kamida 3 ta o'yinchi kerak!")
        return

    try:
        game["roles"] = assign_roles(players)
        game["alive"] = set(players)
        game["started"] = True
        game["phase"] = "night"

        # Har bir o'yinchiga roli shaxsiy xabar orqali yuboriladi
        for uid, role in game["roles"].items():
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"Sizning rolingiz: *{role}*",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Shaxsiy xabar yuborishda xatolik: {e}")

        await update.message.reply_text("✅ O'yin boshlandi! Rollar maxfiy xabar orqali yuborildi.\nKechani boshlash uchun /night buyrug'ini yuboring.")
    except Exception as e:
        await update.message.reply_text(f"Xatolik: {e}")


async def night(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        await update.message.reply_text("O'yin mavjud emas.")
        return

    game = games[chat_id]
    if not game["started"]:
        await update.message.reply_text("O'yin hali boshlanmadi. /begin yuboring.")
        return

    if game["phase"] != "day":
        await update.message.reply_text("Hozir kecha emas.")
        return

    game["phase"] = "night"
    game["night_actions"] = {}

    # Har bir roliga tegishli o'yinchilarga alohida tugmalar yuborish
    for uid, role in game["roles"].items():
        if uid not in game["alive"]:
            continue  # O'liklarga yuborilmaydi

        if role == "Mafia":
            buttons = [
                [InlineKeyboardButton(name, callback_data=f"mafia_{target}")]
                for target, name in game["players"].items()
                if target in game["alive"] and target != uid
            ]
            if buttons:
                await context.bot.send_message(
                    chat_id=uid,
                    text="💀 Kimni o'ldirmoqchisiz?",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
        elif role == "Doktor":
            buttons = [
                [InlineKeyboardButton(name, callback_data=f"doctor_{target}")]
                for target, name in game["players"].items()
                if target in game["alive"]
            ]
            await context.bot.send_message(
                chat_id=uid,
                text="⚕️ Kimni davolamoqchisiz?",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        elif role == "Komissar":
            buttons = [
                [InlineKeyboardButton(name, callback_data=f"sheriff_{target}")]
                for target, name in game["players"].items()
                if target in game["alive"] and target != uid
            ]
            await context.bot.send_message(
                chat_id=uid,
                text="👮 Kimni tekshirmoqchisiz?",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

    await update.message.reply_text("🌙 Kecha boshlandi. Har bir aktiv rol o'z harakatini shaxsiy xabar orqali tanlamoqda...")


async def day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        await update.message.reply_text("O'yin mavjud emas.")
        return

    game = games[chat_id]
    if not game["started"]:
        await update.message.reply_text("O'yin hali boshlanmadi.")
        return

    if game["phase"] != "night":
        await update.message.reply_text("Hozir kunduz emas.")
        return

    # Kechani yakunlash
    await end_night_phase(chat_id, context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        await update.message.reply_text("O'yin mavjud emas.")
        return

    game = games[chat_id]
    alive_names = [game["players"][uid] for uid in game["alive"]]
    phase = "Kunduz" if game["phase"] == "day" else "Kecha"
    await update.message.reply_text(
        f"📊 Holat:\n"
        f"Faza: {phase}\n"
        f"Tiriklar: {', '.join(alive_names)}"
    )


async def vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kunduz ovoz berish uchun. Format: /vote Ism"""
    chat_id = update.effective_chat.id
    if chat_id not in games:
        await update.message.reply_text("O'yin mavjud emas.")
        return

    game = games[chat_id]
    if game["phase"] != "day":
        await update.message.reply_text("Hozir kunduz emas.")
        return

    user_id = update.effective_user.id
    if user_id not in game["alive"]:
        await update.message.reply_text("Siz o'likmisiz 👻")
        return

    if not context.args:
        await update.message.reply_text("Foydalanish: /vote [Ism]")
        return

    target_name = " ".join(context.args).strip()
    # Ism bo'yicha id topish
    target_id = None
    for uid, name in game["players"].items():
        if name.lower() == target_name.lower() and uid in game["alive"]:
            target_id = uid
            break

    if not target_id:
        await update.message.reply_text("Bunday tirik o'yinchi topilmadi.")
        return

    game["votes"][user_id] = target_id
    await update.message.reply_text(f"Siz {target_name}ga ovoz berdingiz.")

    # Ovozlar sonini tekshirish — barcha tiriklar ovoz berganmi?
    if len(game["votes"]) == len(game["alive"]):
        # Ovozlar natijasini hisoblash
        vote_count = {}
        for v in game["votes"].values():
            vote_count[v] = vote_count.get(v, 0) + 1

        max_votes = max(vote_count.values())
        candidates = [uid for uid, count in vote_count.items() if count == max_votes]

        if len(candidates) == 1:
            eliminated = candidates[0]
            game["alive"].discard(eliminated)
            await update.message.reply_chat_action(chat_id=chat_id)
            await update.message.reply_text(f"🗳️ {game['players'][eliminated]} chiqarildi!")
        else:
            await update.message.reply_text("Bir xil ovoz — hech kim chiqarilmadi.")

        # O'yin tugashini tekshirish
        if not check_game_end(chat_id, context):
            # Kechaga o'tish uchun /night buyrug'ini so'rash
            game["phase"] = "night"
            await update.message.reply_text("🌙 Kechaga o'tish uchun admin /night yuborishi kerak.")


# --- Callback tugmalar uchun handler ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id  # Shaxsiy chat, lekin biz game topish uchun global chat_id kerak

    # Shaxsiy chatdan kelgan bo'lsa, global chat_id topish qiyin.
    # Shu sababli, biz har bir game alohida saqlanadi, lekin shaxsiy chatda kelganligi sababli
    # bizda chat_id = user_id. Shu sababli, biz **barcha chatlarni tekshiramiz**.
    # Bu noqulay, lekin oddiy yechim.

    target_chat = None
    for cid, game in games.items():
        if user_id in game["players"] and user_id in game["alive"]:
            target_chat = cid
            break

    if not target_chat:
        await query.message.reply_text("Siz hech qaysi o'yinda emassiz.")
        return

    game = games[target_chat]
    data = query.data

    if data.startswith("mafia_"):
        target = int(data.split("_")[1])
        game["night_actions"]["mafia"] = target
        await query.message.edit_text(f"✅ Siz {game['players'][target]}ni o'ldirishga qaror qildingiz.")
    elif data.startswith("doctor_"):
        target = int(data.split("_")[1])
        game["night_actions"]["doctor"] = target
        await query.message.edit_text(f"✅ Siz {game['players'][target]}ni davolamoqchisiz.")
    elif data.startswith("sheriff_"):
        target = int(data.split("_")[1])
        game["night_actions"]["sheriff"] = target
        await query.message.edit_text(f"✅ Siz {game['players'][target]}ni tekshirmoqchisiz.")

    # Agar barcha aktiv rollar harakat qilgan bo'lsa, kechani yakunlash (soddalashtirilgan)
    # Haqiqiy loyihada: "aktiv rolar sonini" hisoblash kerak, lekin hozir oddiy variant:
    # Admin /day buyrug'i orqali kechani yakunlaydi.


# --- Webhook va ilova ishga tushirish ---

@app.on_event("startup")
async def set_webhook():
    await application.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook {WEBHOOK_URL} ga o'rnatildi.")


@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Telegramdan keladigan webhookni qayta ishlash."""
    update = Update.de_json(await request.json(), application.bot)
    await application.process_update(update)
    return {"status": "ok"}


# --- Handlerlarni ro'yxatdan o'tkazish ---

application.add_handler(CommandHandler("newgame", newgame))
application.add_handler(CommandHandler("join", join))
application.add_handler(CommandHandler("players", players))
application.add_handler(CommandHandler("begin", begin))
application.add_handler(CommandHandler("night", night))
application.add_handler(CommandHandler("day", day))
application.add_handler(CommandHandler("status", status))
application.add_handler(CommandHandler("vote", vote))
application.add_handler(CallbackQueryHandler(button_handler))

# Ilova ishga tushganda botni ishlatish (lekin polling emas)
@app.on_event("startup")
async def startup():
    await application.initialize()
    await application.start()


@app.on_event("shutdown")
async def shutdown():
    await application.stop()
    await application.shutdown()