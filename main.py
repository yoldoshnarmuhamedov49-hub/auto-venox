import asyncio
import html
import os
import re
import ssl
import urllib.parse
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BusinessMessagesDeleted, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command, CommandStart
import aiohttp
from aiohttp import web

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
OWNER_ID = 8527026238

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

AI_ENABLED = True
CURRENT_MODE = "AUTOPILOT"

MODE_INSTRUCTIONS = {
    "AUTOPILOT": "Tabiiy, samimiy, aqlli va vaziyatga mos javob ber.",
    "QUIET": "Juda qisqa, tinch va ortiqcha gaplarsiz (1-2 jumla) javob ber.",
    "BRAIN": "Murakkab savollarga chuqur tahlil va aniq misollar bilan javob ber.",
    "GHOST": "Minimal matn bilan, faqat eng zarur faktni yoz.",
    "TECH": "Dasturlash, o'yinlar va texnik muammolarga aniq yechim ber."
}

CUSTOM_SYSTEM_PROMPT = (
    "Sen Venoxning shaxsiy AI yordamchisisan (VENOX AI v10.0). Hozir Venox band.\n"
    "1. Qayta-qayta salomlashma, tabiiy va samimiy gaplash.\n"
    "2. Venox nomidan pul, to'lov, buyurtma yoki shaxsiy va'dalar berma.\n"
    "3. Har doim rost gapir, bilmagan narsangni to'qima.\n"
    "4. Dasturlash, matematika, o'yinlar va texnik savollarda aniq yordam ber.\n"
    "5. Matnda aslo yulduzcha yoki panjara belgilarini ishlatma, chiroyli emojilar qo'sh."
)

VENOX_TRIGGERS = [
    "venoxga savolim bor", "venoxga gapim bor", "venoxga ayt", "venoxga yetkaz",
    "venox bilan gaplashishim kerak", "venoxga yozmoqchiman", "venoxga iltimosim bor",
    "venoxga xabar yubor", "venoxdan so'ramoqchi edim", "venox kerak",
    "venox bilan bog'lanmoqchiman", "venoxga murojaatim bor", "savolim bor", "gapim bor"
]

BAD_WORDS = [
    "jalab", "qamchi", "suka", "blyat", "nahuy", "naxuy", "dalbayob", "dolboeb",
    "pidar", "pidaraz", "gandon", "xuy", "qotoq", "ko't", "sikay", "siktir",
    "haromi", "xaromi", "onangni", "enagdi", "otangni", "ambash", "foxisha"
]

message_cache = {}
chat_history = {}
greeted_users = set()
waiting_for_venox_msg = set()
last_user_messages = {}
admin_states = {}
WORKING_MODELS = []
STATS = {"total_messages": 0, "active_users": set(), "venox_requests": 0, "spam_blocked": 0, "ai_replies": 0}

async def health_check(request):
    return web.Response(text="VENOX AI v10.0 24/7 ishlamoqda!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()

def get_uzb_time() -> str:
    return datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%d | %H:%M:%S")

def contains_profanity(text: str) -> bool:
    if not text:
        return False
    clean = text.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", clean) for w in BAD_WORDS)

def is_spam(chat_id: int, text: str) -> bool:
    hist = last_user_messages.setdefault(chat_id, [])
    hist.append(text.lower().strip())
    if len(hist) > 3:
        hist.pop(0)
    return len(hist) == 3 and len(set(hist)) == 1

def detect_priority(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["shoshilinch", "tezkor", "urgent", "muhim", "yordam bering", "muammo"]):
        return "🔴 URGENT"
    if any(w in t for w in ["pul", "to'lov", "buyurtma", "shikoyat", "hamkorlik", "reklama"]):
        return "🟠 HIGH"
    return "🟢 NORMAL"

def clean_ai_text(text: str) -> str:
    return re.sub(r"[#*]+", "", text).strip()

async def safe_send(message: Message, text: str):
    for _ in range(3):
        try:
            await message.answer(text)
            return
        except Exception:
            await asyncio.sleep(1.5)

async def get_active_models(session: aiohttp.ClientSession):
    global WORKING_MODELS
    if WORKING_MODELS:
        return WORKING_MODELS
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    good = ("llama", "gemma", "mixtral", "mistral", "qwen", "deepseek")
    bad = ("whisper", "guard", "vision", "tts", "orpheus", "audio", "embed")
    found = []
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            data = await resp.json()
            for m in data.get("data", []):
                mid = m["id"]
                ml = mid.lower()
                if any(ml.startswith(p) for p in good) and not any(b in ml for b in bad):
                    found.append(mid)
    except Exception:
        pass
    found.sort(key=lambda x: (0 if "llama" in x.lower() else 1))
    WORKING_MODELS = found if found else ["llama-3.1-8b-instant"]
    return WORKING_MODELS

async def get_ai_response(messages_list: list, max_tokens: int = 450) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        models = await get_active_models(session)
        last_err = "Noma'lum xato"
        for model_name in models:
            payload = {"model": model_name, "messages": messages_list, "temperature": 0.7, "max_tokens": max_tokens}
            try:
                async with session.post(url, headers=headers, json=payload, timeout=25) as resp:
                    data = await resp.json()
                    if "choices" in data:
                        return clean_ai_text(data["choices"][0]["message"]["content"])
                    last_err = data.get("error", {}).get("message", str(data))
            except Exception as e:
                last_err = str(e)
                await asyncio.sleep(0.5)
        return f"⚠️ AI ulanish xatosi: {last_err}"

async def get_weather(city: str = "Tashkent") -> str:
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return f"🌤 Ob-havo ma'lumoti:\n{(await resp.text()).strip()}"
        except Exception:
            pass
    return "❌ Ob-havo ma'lumotini olib bo'lmadi."

def get_main_menu():
    status = "🟢 AI Yoqilgan (O'chirish)" if AI_ENABLED else "🔴 AI O'chirilgan (Yoqish)"
    kb = [
        [KeyboardButton(text=status), KeyboardButton(text="✏️ AI Javobini Sozlash")],
        [KeyboardButton(text="🎨 Rasm Yaratish"), KeyboardButton(text="🐍 Python Kod To'g'irlash")],
        [KeyboardButton(text="🕒 Aniq Soat"), KeyboardButton(text="🌤 Ob-havo")],
        [KeyboardButton(text="🧮 Matematika Yordamchi"), KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="⚙️ Rejimlar (/ai_mode)"), KeyboardButton(text="🗑 Keshni Tozalash")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.business_message()
async def handle_business_message(message: Message):
    try:
        chat_id = message.chat.id
        message_cache.setdefault(chat_id, {})[message.message_id] = message
        if message.from_user.id == OWNER_ID or not AI_ENABLED:
            return

        user_text = message.text or message.caption or ""
        STATS["total_messages"] += 1
        STATS["active_users"].add(message.from_user.id)

        if contains_profanity(user_text) or (user_text and is_spam(chat_id, user_text)):
            STATS["spam_blocked"] += 1
            return

        if not user_text:
            user_text = "Media fayl yubordi"

        if chat_id in waiting_for_venox_msg:
            waiting_for_venox_msg.discard(chat_id)
            STATS["venox_requests"] += 1
            uname = f"@{message.from_user.username}" if message.from_user.username else "yo'q"
            safe_name = html.escape(message.from_user.full_name or "Noma'lum")
            safe_text = html.escape(user_text)
            report = (
                f"📨 <b>VENOXGA YANGI MUROJAAT</b>\n\n"
                f"⚡ <b>Muhimlik:</b> {detect_priority(user_text)}\n"
                f"👤 <b>Kimdan:</b> {safe_name} ({uname})\n"
                f"🕒 <b>Vaqt:</b> {get_uzb_time()}\n"
                f"💬 <b>Murojaat:</b> {safe_text}"
            )
            try:
                await bot.send_message(OWNER_ID, report, parse_mode="HTML")
                reply = "Venoxga xabaringiz yuborildi ✅. Iltimos, kutib turing, bo'shashi bilan o'zi javob beradi."
            except Exception as e:
                print(f"Ownerga yuborishda xato: {e}")
                reply = "Kechirasiz, xabarni hozir Venoxga yubora olmadim ⚠️. Birozdan so'ng qayta urinib ko'ring."
            await safe_send(message, reply)
            STATS["ai_replies"] += 1
            return

        sys_prompt = f"{CUSTOM_SYSTEM_PROMPT}\nRejim ({CURRENT_MODE}): {MODE_INSTRUCTIONS.get(CURRENT_MODE)}\nVaqt: {get_uzb_time()}."
        hist = chat_history.setdefault(chat_id, [{"role": "system", "content": sys_prompt}])
        hist[0]["content"] = sys_prompt
        hist.append({"role": "user", "content": user_text})
        if len(hist) > 7:
            chat_history[chat_id] = [hist[0]] + hist[-6:]
            hist = chat_history[chat_id]

        low = user_text.lower()
        if any(p in low for p in VENOX_TRIGGERS):
            greeted_users.add(chat_id)
            waiting_for_venox_msg.add(chat_id)
            ai_reply = "Aha xo'p, gapiring savolingizni, men Venoxga yetkazaman ✍️"
        elif any(p in low for p in ["gaplashamiz", "gaplashaman", "suhbatlashamiz"]):
            greeted_users.add(chat_id)
            ai_reply = "Ajoyib! 😊 Qanday mavzuda gaplashamiz? Savolingiz bo'lsa bemalol yozing ✨"
        elif chat_id not in greeted_users:
            greeted_users.add(chat_id)
            ai_reply = "Salom! Men Venoxning shaxsiy yordamchisiman 😊. Gaplashamizmi yoki Venoxga savolingiz bormi?"
        else:
            ai_reply = await get_ai_response(list(hist), max_tokens=200 if CURRENT_MODE in ("QUIET", "GHOST") else 450)

        if chat_id in chat_history:
            chat_history[chat_id].append({"role": "assistant", "content": ai_reply})
        await safe_send(message, ai_reply)
        STATS["ai_replies"] += 1
    except Exception as e:
        print(f"Xatolik: {e}")

@dp.deleted_business_messages()
async def handle_deleted_messages(deleted_msg: BusinessMessagesDeleted):
    chat_id = deleted_msg.chat.id
    del_time = get_uzb_time()
    for mid in deleted_msg.message_ids:
        old = message_cache.get(chat_id, {}).get(mid)
        if old:
            safe_name = html.escape(old.from_user.full_name or "Noma'lum")
            safe_text = html.escape(old.text or old.caption or "Matn yo'q")
            info = (
                f"🗑 <b>DIQQAT, XABAR O'CHIRILDI!</b>\n\n"
                f"👤 <b>Kim:</b> {safe_name}\n"
                f"🕒 <b>Vaqt:</b> {del_time}\n"
                f"💬 <b>Matn:</b> {safe_text}"
            )
            try:
                await bot.send_message(OWNER_ID, info, parse_mode="HTML")
                if old.photo:
                    await bot.send_photo(OWNER_ID, old.photo[-1].file_id, caption="☝️ O'chirilgan rasm")
                elif old.video:
                    await bot.send_video(OWNER_ID, old.video.file_id, caption="☝️ O'chirilgan video")
            except Exception:
                pass

@dp.message(F.from_user.id != OWNER_ID)
async def block_others(message: Message):
    return

@dp.message(CommandStart(), F.from_user.id == OWNER_ID)
async def cmd_start(message: Message):
    admin_states[OWNER_ID] = None
    await message.answer("👋 Salom, Venox! VENOX AI v10.0 tayyor. Buyruqlar: /ai_help", reply_markup=get_main_menu())

@dp.message(Command("ai_help"), F.from_user.id == OWNER_ID)
async def cmd_help(message: Message):
    await message.answer(
        "🛠 <b>VENOX OWNER COMMANDS:</b>\n\n"
        "/ai_status — Bot holati\n/ai_mode — Rejimlar\n/ai_autopilot — Standart rejim\n"
        "/ai_quiet — Qisqa javoblar\n/ai_brain — Chuqur tahlil\n/ai_stats — Statistika\n"
        "/ai_memory — Xotira holati\n/ai_restart_context — Xotirani tozalash",
        parse_mode="HTML"
    )

@dp.message(Command("ai_status"), F.from_user.id == OWNER_ID)
async def cmd_status(message: Message):
    h = "🟢 Yoqilgan" if AI_ENABLED else "🔴 O'chirilgan"
    m = WORKING_MODELS[0] if WORKING_MODELS else "Avto"
    await message.answer(f"🤖 <b>VENOX AI v10.0</b>\n📡 Holat: {h}\n⚙️ Rejim: {CURRENT_MODE}\n🧠 Model: {m}\n🕒 Vaqt: {get_uzb_time()}", parse_mode="HTML")

@dp.message(Command("ai_mode"), F.from_user.id == OWNER_ID)
async def cmd_mode(message: Message):
    await message.answer(f"⚙️ Faol rejim: <b>{CURRENT_MODE}</b>\n\n/ai_autopilot\n/ai_quiet\n/ai_brain", parse_mode="HTML")

@dp.message(Command("ai_autopilot"), F.from_user.id == OWNER_ID)
async def cmd_auto(message: Message):
    global CURRENT_MODE
    CURRENT_MODE = "AUTOPILOT"
    await message.answer("✅ Rejim: AUTOPILOT")

@dp.message(Command("ai_quiet"), F.from_user.id == OWNER_ID)
async def cmd_quiet(message: Message):
    global CURRENT_MODE
    CURRENT_MODE = "QUIET"
    await message.answer("✅ Rejim: QUIET")

@dp.message(Command("ai_brain"), F.from_user.id == OWNER_ID)
async def cmd_brain(message: Message):
    global CURRENT_MODE
    CURRENT_MODE = "BRAIN"
    await message.answer("✅ Rejim: BRAIN")

@dp.message(Command("ai_stats"), F.from_user.id == OWNER_ID)
async def cmd_stats(message: Message):
    await message.answer(
        f"📊 <b>STATISTIKA</b>\n💬 Xabarlar: {STATS['total_messages']}\n👥 Foydalanuvchilar: {len(STATS['active_users'])}\n"
        f"🤖 AI javoblari: {STATS['ai_replies']}\n📨 Murojaatlar: {STATS['venox_requests']}\n🛡 Spam/Haqorat: {STATS['spam_blocked']}",
        parse_mode="HTML"
    )

@dp.message(Command("ai_memory"), F.from_user.id == OWNER_ID)
async def cmd_mem(message: Message):
    await message.answer(f"🧠 Faol suhbatlar: {len(chat_history)}\nSalomlashilganlar: {len(greeted_users)}\nKutilayotgan murojaatlar: {len(waiting_for_venox_msg)}")

@dp.message(Command("ai_restart_context"), F.from_user.id == OWNER_ID)
async def cmd_reset(message: Message):
    chat_history.clear()
    greeted_users.clear()
    waiting_for_venox_msg.clear()
    WORKING_MODELS.clear()
    await message.answer("🧹 Xotira va kesh tozalandi!")

@dp.message(F.from_user.id == OWNER_ID)
async def handle_owner(message: Message):
    global AI_ENABLED, CUSTOM_SYSTEM_PROMPT
    text = message.text or ""

    if "AI Yoqilgan" in text or "AI O'chirilgan" in text:
        AI_ENABLED = not AI_ENABLED
        await message.answer(f"Avto-javob: {'YOQILDI ✅' if AI_ENABLED else 'OCHIRILDI ❌'}", reply_markup=get_main_menu())
        return
    elif text == "✏️ AI Javobini Sozlash":
        admin_states[OWNER_ID] = "WAIT_PROMPT"
        await message.answer("✍️ Yangi yo'riqnomani yozing:")
        return
    elif text == "🎨 Rasm Yaratish":
        admin_states[OWNER_ID] = "WAIT_IMAGE"
        await message.answer("🎨 Rasm tavsifini yozing:")
        return
    elif text == "🐍 Python Kod To'g'irlash":
        admin_states[OWNER_ID] = "WAIT_CODE"
        await message.answer("🐍 Xato Python kodni tashlang:")
        return
    elif text == "🕒 Aniq Soat":
        await message.answer(f"🕒 Toshkent vaqti: <b>{get_uzb_time()}</b>", parse_mode="HTML")
        return
    elif text == "🌤 Ob-havo":
        admin_states[OWNER_ID] = "WAIT_WEATHER"
        await message.answer("🌍 Shahar nomini yozing (Masalan: Tashkent):")
        return
    elif text == "🧮 Matematika Yordamchi":
        admin_states[OWNER_ID] = "WAIT_MATH"
        await message.answer("🧮 Misolni yozing:")
        return
    elif text == "📊 Statistika":
        await cmd_stats(message)
        return
    elif text == "⚙️ Rejimlar (/ai_mode)":
        await cmd_mode(message)
        return
    elif text == "🗑 Keshni Tozalash":
        await cmd_reset(message)
        return

    state = admin_states.get(OWNER_ID)
    admin_states[OWNER_ID] = None

    if state == "WAIT_PROMPT":
        CUSTOM_SYSTEM_PROMPT = text
        chat_history.clear()
        await message.answer("✅ AI uslubi yangilandi!", reply_markup=get_main_menu())
        return
    elif state == "WAIT_IMAGE":
        w = await message.answer("🎨 Rasm chizilmoqda...")
        eng = await get_ai_response([
            {"role": "system", "content": "Translate to a short detailed English image prompt only."},
            {"role": "user", "content": text}
        ], max_tokens=150)
        if eng.startswith("⚠️"):
            eng = text
        url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(eng + ', 8k, ultra detailed')}?width=1024&height=1024&nologo=true"
        try:
            await message.answer_photo(photo=url, caption=f"✨ {text}")
            await w.delete()
        except Exception:
            await w.edit_text("❌ Rasm yaratishda xatolik.")
        return
    elif state == "WAIT_CODE":
        w = await message.answer("🔍 Kod tekshirilmoqda...")
        rep = await get_ai_response([
            {"role": "system", "content": "Senior Python dasturchi sifatida kodni to'g'irlab ber. Yulduzcha va panjara ishlatma."},
            {"role": "user", "content": text}
        ], max_tokens=750)
        await w.edit_text(rep)
        return
    elif state == "WAIT_WEATHER":
        await message.answer(await get_weather(text))
        return
    elif state == "WAIT_MATH":
        w = await message.answer("🧮 Hisoblanmoqda...")
        rep = await get_ai_response([
            {"role": "system", "content": "Matematika ustasi sifatida aniq yechib ber. Ko'paytirish uchun × ishlat, yulduzcha ishlatma."},
            {"role": "user", "content": text}
        ], max_tokens=550)
        await w.edit_text(rep)
        return

    w = await message.answer("⏳ O'ylanmoqdaman...")
    rep = await get_ai_response([
        {"role": "system", "content": f"Sen Venoxning shaxsiy yordamchisi va dasturchisan. Vaqt: {get_uzb_time()}. Emojilar bilan aniq javob ber, yulduzcha va panjara ishlatma."},
        {"role": "user", "content": text}
    ], max_tokens=650)
    if len(rep) > 4000:
        await w.delete()
        for i in range(0, len(rep), 4000):
            await safe_send(message, rep[i:i+4000])
    else:
        try:
            await w.edit_text(rep)
        except Exception:
            await safe_send(message, rep)

async def main():
    await start_web_server()
    print("VENOX AI v10.0 ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
