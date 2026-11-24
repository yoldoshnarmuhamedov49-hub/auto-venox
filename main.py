import ast
import asyncio
import base64
import html
import os
import re
import ssl
import urllib.parse
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, BusinessMessagesDeleted,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
)
from aiogram.filters import Command, CommandStart
import aiohttp
from aiohttp import web

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
OWNER_ID = 8527026238

CARD_NUMBER = os.environ.get("CARD_NUMBER", "8600 0000 0000 0000")
CARD_OWNER = os.environ.get("CARD_OWNER", "VENOX")

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
    "Sen Venoxning (@THE_VENOX) shaxsiy AI yordamchisisan (VENOX AI v11.0 PRO). Hozir Venox band.\n"
    "1. Qayta-qayta salomlashma, tabiiy va samimiy gaplash.\n"
    "2. Venox Telegram botlar yasab beradi — agar foydalanuvchi bot yasattirmoqchi bo'lsa, qanday bot kerakligini so'ra.\n"
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

ORDER_KEYWORDS = [
    "bot kerak", "bot yasab", "bot yasat", "bot zakaz", "buyurtma", "narxi qancha",
    "qancha turadi", "do'kon bot", "dokon bot", "admin bot", "ai bot", "avtojavob bot",
    "reklama bot", "to'lov bot", "tolov bot", "o'yin bot", "oyin bot", "biznes bot",
    "e'lon bo'yicha", "elon bo'yicha", "bot yasash"
]

BAD_WORDS = [
    "jalab", "qamchi", "suka", "blyat", "nahuy", "naxuy", "dalbayob", "dolboeb",
    "pidar", "pidaraz", "gandon", "xuy", "qotoq", "ko't", "sikay", "siktir",
    "haromi", "xaromi", "onangni", "enagdi", "otangni", "ambash", "foxisha"
]

message_cache = {}
chat_history = {}
client_states = {}
last_user_messages = {}
admin_states = {}
WORKING_MODELS = []
VISION_MODELS = []

STATS = {
    "total_messages": 0,
    "active_users": set(),
    "ai_replies": 0,
    "venox_requests": 0,
    "spam_blocked": 0,
    "orders_pending": 0,
    "orders_approved": 0,
    "complaints": 0
}

async def health_check(request):
    return web.Response(text="VENOX AI ULTIMATE PRO 24/7 ishlamoqda!")

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

def extract_python_code(text: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()

async def safe_send(message: Message, text: str):
    for _ in range(3):
        try:
            await message.answer(text)
            return
        except Exception:
            await asyncio.sleep(1.5)

async def get_active_models(session: aiohttp.ClientSession):
    global WORKING_MODELS, VISION_MODELS
    if WORKING_MODELS:
        return WORKING_MODELS
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    good = ("llama", "qwen", "deepseek", "mixtral", "mistral", "gemma")
    bad = ("whisper", "guard", "tts", "orpheus", "audio", "embed")
    found = []
    vis = []
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            data = await resp.json()
            for m in data.get("data", []):
                mid = m["id"]
                ml = mid.lower()
                if any(b in ml for b in bad):
                    continue
                if any(v in ml for v in ("vision", "scout", "maverick")):
                    vis.append(mid)
                if any(ml.startswith(p) for p in good) and "vision" not in ml:
                    found.append(mid)
    except Exception:
        pass
    found.sort(key=lambda x: (0 if "llama-3.3-70b" in x.lower() else (1 if "llama-3.1-8b" in x.lower() else (2 if "llama" in x.lower() else 3))))
    WORKING_MODELS = found if found else ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    VISION_MODELS = vis
    return WORKING_MODELS

async def get_ai_response(messages_list: list, max_tokens: int = 450, clean_format: bool = True, use_vision: bool = False) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        models = await get_active_models(session)
        target_models = VISION_MODELS if (use_vision and VISION_MODELS) else models
        last_err = "Ulanish xatosi"

        for model_name in target_models:
            token_limit = min(max_tokens, 900) if "qwen" in model_name.lower() else max_tokens
            payload = {
                "model": model_name,
                "messages": messages_list,
                "temperature": 0.35 if not clean_format else 0.65,
                "max_tokens": token_limit
            }
            try:
                async with session.post(url, headers=headers, json=payload, timeout=60) as resp:
                    data = await resp.json()
                    if "choices" in data:
                        content = data["choices"][0]["message"]["content"]
                        return clean_ai_text(content) if clean_format else content
                    last_err = data.get("error", {}).get("message", str(data))
            except Exception as e:
                last_err = str(e)
                await asyncio.sleep(0.5)
        return f"⚠️ AI ulanish xatosi: {last_err}"

async def analyze_photo(message: Message, prompt_text: str) -> str:
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        b64 = base64.b64encode(file_bytes.read()).decode("utf-8")
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"O'zbek tilida qisqa va samimiy javob ber. Yulduzcha ishlatma. Izoh: {prompt_text}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]
        }]
        reply = await get_ai_response(msgs, max_tokens=300, use_vision=True)
        if not reply.startswith("⚠️"):
            return reply
    except Exception:
        pass
    return "Rasmni ko'rdim 😊! Agar bu bot buyurtmasi yoki Venoxga savol bo'lsa, matn qilib ham yozib yuboring."

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

async def send_price_offer(message: Message, state: dict, idea_text: str):
    price_prompt = [
        {
            "role": "system",
            "content": (
                "Sen Telegram bot yasaydigan Venox studiyasining menejerisan. "
                "Mijoz so'ragan botning murakkabligiga qarab O'zbekiston bozori uchun aniq narx belgilagin (50 000 so'mdan 350 000 so'mgacha). "
                "Javobingda: 1) Bot nima qilishini 1 jumlada tasdiqla, 2) Aniq narxini yoz (masalan: 120 000 so'm). "
                "Yulduzcha va panjara ishlatma!"
            )
        },
        {"role": "user", "content": idea_text}
    ]
    price_reply = await get_ai_response(price_prompt, max_tokens=200)
    price_match = re.search(r"(\d[\d\s\.,]*\s*(?:so'm|som|ming|\$))", price_reply, re.IGNORECASE)
    extracted_price = price_match.group(1) if price_match else "Kelishilgan narx"

    state["bot_idea"] = idea_text
    state["price"] = extracted_price
    state["step"] = "WAIT_RECEIPT"

    offer_text = (
        f"{price_reply}\n\n"
        f"💳 To'lov uchun karta: {CARD_NUMBER}\n"
        f"👤 Karta egasi: {CARD_OWNER}\n\n"
        f"To'lovni amalga oshirib, chek rasmini (skrinshot) shu yerga tashlang 📸. "
        f"Admin tasdiqlashi bilan tayyor PRO botingizni yasab beraman!"
    )
    await safe_send(message, offer_text)
    STATS["ai_replies"] += 1

def get_main_menu():
    status = "🟢 AI Yoqilgan (O'chirish)" if AI_ENABLED else "🔴 AI O'chirilgan (Yoqish)"
    kb = [
        [KeyboardButton(text=status), KeyboardButton(text="💳 Karta Sozlash")],
        [KeyboardButton(text="✏️ AI Javobini Sozlash"), KeyboardButton(text="⚙️ Rejimlar (/ai_mode)")],
        [KeyboardButton(text="🎨 Rasm Yaratish"), KeyboardButton(text="🐍 Python Kod To'g'irlash")],
        [KeyboardButton(text="🕒 Aniq Soat"), KeyboardButton(text="🌤 Ob-havo")],
        [KeyboardButton(text="🧮 Matematika Yordamchi"), KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="🗑 Keshni Tozalash")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.business_message()
async def handle_business_message(message: Message):
    try:
        chat_id = message.chat.id
        biz_id = message.business_connection_id
        message_cache.setdefault(chat_id, {})[message.message_id] = message

        if message.from_user.id == OWNER_ID or not AI_ENABLED:
            return

        user_text = message.text or message.caption or ""
        STATS["total_messages"] += 1
        STATS["active_users"].add(message.from_user.id)

        if contains_profanity(user_text) or (user_text and is_spam(chat_id, user_text)):
            STATS["spam_blocked"] += 1
            return

        state = client_states.setdefault(chat_id, {
            "step": "IDLE",
            "greeted": False,
            "bot_idea": "",
            "price": "",
            "biz_id": biz_id
        })
        state["biz_id"] = biz_id
        step = state["step"]
        low = user_text.lower().strip()

        if step == "WAIT_VENOX_MSG":
            state["step"] = "IDLE"
            STATS["venox_requests"] += 1
            uname = f"@{message.from_user.username}" if message.from_user.username else "yo'q"
            safe_name = html.escape(message.from_user.full_name or "Noma'lum")
            safe_text = html.escape(user_text or "Media xabar")
            report = (
                f"📨 <b>VENOXGA YANGI MUROJAAT</b>\n\n"
                f"⚡ <b>Muhimlik:</b> {detect_priority(user_text)}\n"
                f"👤 <b>Kimdan:</b> {safe_name} ({uname})\n"
                f"🕒 <b>Vaqt:</b> {get_uzb_time()}\n"
                f"💬 <b>Murojaat:</b> {safe_text}"
            )
            try:
                if message.photo:
                    await bot.send_photo(OWNER_ID, message.photo[-1].file_id, caption=report, parse_mode="HTML")
                elif message.video:
                    await bot.send_video(OWNER_ID, message.video.file_id, caption=report, parse_mode="HTML")
                else:
                    await bot.send_message(OWNER_ID, report, parse_mode="HTML")
                reply = "Venoxga xabaringiz yuborildi ✅. Iltimos, kutib turing, bo'shashi bilan o'zi javob beradi."
            except Exception as e:
                print(f"Ownerga yuborishda xato: {e}")
                reply = "Kechirasiz, xabarni hozir Venoxga yubora olmadim ⚠️. Birozdan so'ng qayta urinib ko'ring."
            await safe_send(message, reply)
            STATS["ai_replies"] += 1
            return

        if step == "WAIT_RECEIPT":
            if message.photo or message.document:
                state["step"] = "WAIT_ADMIN_APPROVAL"
                STATS["orders_pending"] += 1
                uname = f"@{message.from_user.username}" if message.from_user.username else "yo'q"
                safe_name = html.escape(message.from_user.full_name or "Mijoz")
                safe_idea = html.escape(state["bot_idea"])
                safe_price = html.escape(state["price"])

                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Tasdiqlash (PRO Bot yasash)", callback_data=f"approve:{chat_id}"),
                    InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject:{chat_id}")
                ]])

                caption = (
                    f"💰 <b>YANGI TO'LOV CHEKI KELDI!</b>\n\n"
                    f"👤 <b>Mijoz:</b> {safe_name} ({uname})\n"
                    f"🆔 <b>ID:</b> <code>{chat_id}</code>\n"
                    f"🤖 <b>Bot turi:</b> {safe_idea}\n"
                    f"💵 <b>Narxi:</b> {safe_price}\n"
                    f"🕒 <b>Vaqt:</b> {get_uzb_time()}"
                )
                try:
                    if message.photo:
                        await bot.send_photo(OWNER_ID, message.photo[-1].file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
                    else:
                        await bot.send_document(OWNER_ID, message.document.file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
                    await safe_send(message, "Chekingiz qabul qilindi ✅. Admin tekshirib tasdiqlashini kuting, darhol botingizni yasashni boshlayman ⏳")
                except Exception as e:
                    print(f"Chek yuborishda xato: {e}")
                    state["step"] = "WAIT_RECEIPT"
                    await safe_send(message, "Chekni yuborishda uzilish bo'ldi ⚠️. Iltimos, rasmni qayta tashlang.")
                STATS["ai_replies"] += 1
                return
            elif any(w in low for w in ["bekor", "kerakmas", "qimmat", "o'zgartir", "keyinroq"]):
                state["step"] = "IDLE"
                await safe_send(message, "Tushunarli 😊. Boshqa turdagi bot kerak bo'lsa yoki savolingiz bo'lsa bemalol yozing.")
                STATS["ai_replies"] += 1
                return
            else:
                await safe_send(
                    message,
                    f"To'lovni amalga oshirgach, chek rasmini (skrinshot) shu yerga tashlang 📸\n\n"
                    f"💳 Karta: {CARD_NUMBER}\n👤 Egasi: {CARD_OWNER}\n\n"
                    f"(Buyurtmani to'xtatish uchun 'bekor' deb yozing)"
                )
                STATS["ai_replies"] += 1
                return

        if step == "WAIT_ADMIN_APPROVAL":
            await safe_send(message, "Chekingiz adminga yuborilgan ⏳. Tasdiqlashi bilan tayyor botingizni shu chatga tashlayman!")
            STATS["ai_replies"] += 1
            return

        if step == "WAIT_FEEDBACK":
            state["step"] = "IDLE"
            if any(w in low for w in ["ha", "yoqdi", "zor", "zo'r", "ajoyib", "rahmat", "raxmat", "yaxshi", "super", "👍", "🔥", "❤️"]):
                await safe_send(message, "Xursandman! 😊 Botingiz unumli xizmat qilsin. Yana yangi bot yoki yordam kerak bo'lsa, har doim shu yerdaman 🚀")
            else:
                STATS["complaints"] += 1
                uname = f"@{message.from_user.username}" if message.from_user.username else "yo'q"
                safe_name = html.escape(message.from_user.full_name or "Mijoz")
                safe_msg = html.escape(user_text or "Media xabar")
                alert = (
                    f"⚠️ <b>MIJOZGA BOT YOQMADI / O'ZGARTIRISH KERAK!</b>\n\n"
                    f"👤 <b>Mijoz:</b> {safe_name} ({uname})\n"
                    f"🤖 <b>Buyurtma:</b> {html.escape(state['bot_idea'])}\n"
                    f"💬 <b>Mijoz gapi:</b> {safe_msg}\n\n"
                    f"👉 Lichkasiga kirib o'zingiz javob bering!"
                )
                try:
                    await bot.send_message(OWNER_ID, alert, parse_mode="HTML")
                except Exception:
                    pass
                await safe_send(message, "Tushunarli, xavotir olmang! Hozir adminga (@THE_VENOX) xabar berdim, o'zi siz bilan bog'lanib to'g'irlab beradi 👨‍💻")
            STATS["ai_replies"] += 1
            return

        if step == "WAIT_BOT_IDEA":
            if any(p in low for p in VENOX_TRIGGERS):
                state["step"] = "WAIT_VENOX_MSG"
                await safe_send(message, "Aha xo'p, gapiring savolingizni, men Venoxga yetkazaman ✍️")
                STATS["ai_replies"] += 1
                return
            if any(w in low for w in ["bekor", "yo'q", "yoq", "gaplashamiz", "gaplashaman"]):
                state["step"] = "IDLE"
                await safe_send(message, "Ajoyib! 😊 Qanday mavzuda gaplashamiz? Savolingiz bo'lsa bemalol yozing ✨")
                STATS["ai_replies"] += 1
                return
            if len(user_text.strip()) >= 4:
                await send_price_offer(message, state, user_text)
                return

        if any(p in low for p in VENOX_TRIGGERS):
            state["greeted"] = True
            state["step"] = "WAIT_VENOX_MSG"
            await safe_send(message, "Aha xo'p, gapiring savolingizni, men Venoxga yetkazaman ✍️")
            STATS["ai_replies"] += 1
            return

        if any(k in low for k in ORDER_KEYWORDS):
            state["greeted"] = True
            if len(user_text.split()) <= 4:
                state["step"] = "WAIT_BOT_IDEA"
                await safe_send(message, "Ajoyib! 🚀 Qanday bot kerakligini va qanday funksiyalari bo'lishini yozing (masalan: internet-do'kon bot, AI bot, guruh admin bot...)")
                STATS["ai_replies"] += 1
                return
            await send_price_offer(message, state, user_text)
            return

        if not state["greeted"]:
            state["greeted"] = True
            greeting = (
                "Assalomu alaykum! 😊 Men Venoxning shaxsiy yordamchisiman.\n"
                "Siz ham Telegram bot yasattirish uchun yozdingizmi? 🤖\n\n"
                "Qanday bot kerakligini yozing — narxini hisoblab beraman, yoki Venoxga savolingiz bo'lsa / gaplashmoqchi bo'lsangiz bemalol ayting ✨"
            )
            await safe_send(message, greeting)
            STATS["ai_replies"] += 1
            return

        if low in ("ha", "xa", "ha shunaqa", "ha bot", "bot", "zakaz"):
            state["step"] = "WAIT_BOT_IDEA"
            await safe_send(message, "Ajoyib! 🚀 Qanday bot kerak va qanday funksiyalari bo'lsin? Batafsilroq yozing, darhol narxini hisoblab beraman.")
            STATS["ai_replies"] += 1
            return

        if any(p in low for p in ["gaplashamiz", "gaplashaman", "suhbatlashamiz"]):
            await safe_send(message, "Ajoyib! 😊 Qanday mavzuda gaplashamiz? Savolingiz bo'lsa bemalol yozing ✨")
            STATS["ai_replies"] += 1
            return

        if message.photo:
            reply = await analyze_photo(message, user_text or "Bu rasmda nima bor?")
            await safe_send(message, reply)
            STATS["ai_replies"] += 1
            return

        if message.sticker:
            user_text = f"[Foydalanuvchi {message.sticker.emoji or '😊'} stiker yubordi]"
        elif message.animation:
            user_text = f"[Foydalanuvchi GIF yubordi. Izoh: {user_text}]"
        elif message.video or message.video_note:
            user_text = f"[Foydalanuvchi video yubordi. Izoh: {user_text}]"
        elif not user_text:
            user_text = "[Foydalanuvchi media fayl yubordi]"

        sys_prompt = f"{CUSTOM_SYSTEM_PROMPT}\nRejim ({CURRENT_MODE}): {MODE_INSTRUCTIONS.get(CURRENT_MODE)}\nVaqt: {get_uzb_time()}."
        hist = chat_history.setdefault(chat_id, [{"role": "system", "content": sys_prompt}])
        hist[0]["content"] = sys_prompt
        hist.append({"role": "user", "content": user_text})
        if len(hist) > 7:
            chat_history[chat_id] = [hist[0]] + hist[-6:]
            hist = chat_history[chat_id]

        tokens = 200 if CURRENT_MODE in ("QUIET", "GHOST") else 450
        ai_reply = await get_ai_response(list(hist), max_tokens=tokens)
        chat_history[chat_id].append({"role": "assistant", "content": ai_reply})
        await safe_send(message, ai_reply)
        STATS["ai_replies"] += 1

    except Exception as e:
        print(f"Biznes xabarda xato: {e}")

@dp.callback_query(F.from_user.id == OWNER_ID)
async def handle_order_callbacks(callback: CallbackQuery):
    data = callback.data or ""
    if not (data.startswith("approve:") or data.startswith("reject:")):
        return

    action, client_id_str = data.split(":")
    client_id = int(client_id_str)
    state = client_states.get(client_id)

    if not state:
        await callback.answer("Bu buyurtma xotirada topilmadi!", show_alert=True)
        return

    biz_id = state.get("biz_id")

    if action == "reject":
        state["step"] = "WAIT_RECEIPT"
        STATS["orders_pending"] = max(0, STATS["orders_pending"] - 1)
        await callback.message.edit_caption(
            caption=(callback.message.caption or "") + "\n\n❌ <b>RAD ETILDI</b>",
            parse_mode="HTML",
            reply_markup=None
        )
        await callback.answer("To'lov rad etildi.")
        try:
            await bot.send_message(
                chat_id=client_id,
                text="❌ Kechirasiz, admin to'lov chekini tasdiqlamadi. Iltimos, to'g'ri chekni qayta yuboring yoki @THE_VENOX bilan bog'laning.",
                business_connection_id=biz_id
            )
        except Exception as e:
            print(f"Rad etish xabarida xato: {e}")
        return

    if action == "approve":
        STATS["orders_approved"] += 1
        STATS["orders_pending"] = max(0, STATS["orders_pending"] - 1)
        await callback.message.edit_caption(
            caption=(callback.message.caption or "") + "\n\n✅ <b>TASDIQLANDI! PRO Bot yasalmoqda...</b>",
            parse_mode="HTML",
            reply_markup=None
        )
        await callback.answer("Tasdiqlandi! Kuchli PRO bot yasalmoqda...")

        try:
            await bot.send_message(
                chat_id=client_id,
                text="✅ Admin to'lovingizni tasdiqladi!\n⚙️ Kuchli PRO botingizni yasashni boshladim (SQLite baza, Admin panel va barcha funksiyalar bilan). 25-40 soniya kuting...",
                business_connection_id=biz_id
            )
        except Exception as e:
            print(f"Mijozga boshlash xabarida xato: {e}")
            return

        bot_idea = state["bot_idea"]
        code_prompt = [
            {
                "role": "system",
                "content": (
                    "You are a World-Class Senior Python Telegram Bot Architect. "
                    "Build a COMPLETE, PRODUCTION-GRADE, POWERFUL single-file Telegram bot using aiogram 3.x (venox_bot.py).\n"
                    "MANDATORY REQUIREMENTS:\n"
                    "1. Use aiogram 3.x with Router, FSMContext (StatesGroup), ReplyKeyboardMarkup, and InlineKeyboardMarkup.\n"
                    "2. Include a built-in SQLite3 database (database.db) with automatic table creation (users table + feature-specific tables).\n"
                    "3. Include a full Admin Panel (/admin) restricted to ADMIN_ID with live statistics, broadcast to all users, and management buttons.\n"
                    "4. Implement EVERY feature requested by the client in full detail — NO placeholders, NO 'TODO', NO 'pass'.\n"
                    "5. Write all user-facing texts and buttons in clean Uzbek language with tasteful emojis.\n"
                    "6. Put BOT_TOKEN = 'YOUR_BOT_TOKEN_HERE' and ADMIN_ID = 123456789 at the very top.\n"
                    "7. Ensure 100% valid Python 3 syntax.\n"
                    "Output ONLY the complete Python code inside a single ```python ... ``` block."
                )
            },
            {"role": "user", "content": f"Mijozning to'liq buyurtmasi: {bot_idea}"}
        ]
        raw_code = await get_ai_response(code_prompt, max_tokens=4096, clean_format=False)
        final_code = extract_python_code(raw_code)

        try:
            ast.parse(final_code)
        except SyntaxError as syn_err:
            fix_prompt = [
                {"role": "system", "content": "Fix the Python syntax error in this aiogram 3.x bot code and return ONLY the complete valid Python code inside ```python ... ```."},
                {"role": "user", "content": f"SyntaxError: {syn_err}\n\nCode:\n{final_code}"}
            ]
            fixed_raw = await get_ai_response(fix_prompt, max_tokens=4096, clean_format=False)
            final_code = extract_python_code(fixed_raw)

        file_bytes = final_code.encode("utf-8")
        client_doc = BufferedInputFile(file_bytes, filename="venox_bot.py")
        owner_doc = BufferedInputFile(file_bytes, filename=f"order_{client_id}_bot.py")

        caption_client = (
            "🎉 Mana sizning tayyor PRO botingiz (venox_bot.py)!\n"
            "Ichida: SQLite baza, Admin panel (/admin), menyular va barcha so'ragan funksiyalaringiz bor ⚡\n\n"
            "📌 Ishga tushirish:\n"
            "1. BotFather'dan token olib YOUR_BOT_TOKEN_HERE o'rniga, o'z ID ingizni ADMIN_ID o'rniga qo'ying.\n"
            "2. Terminalda: pip install aiogram\n"
            "3. Ishga tushirish: python venox_bot.py\n\n"
            "❓ Bot sizga yoqdimi? (Ha / Yo'q deb yozing)"
        )

        try:
            await bot.send_document(
                chat_id=client_id,
                document=client_doc,
                caption=caption_client,
                business_connection_id=biz_id
            )
            state["step"] = "WAIT_FEEDBACK"
            await bot.send_document(
                chat_id=OWNER_ID,
                document=owner_doc,
                caption=f"✅ Mijozga (<code>{client_id}</code>) yuborilgan PRO bot nusxasi!",
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"Bot faylini yuborishda xato: {e}")
            await bot.send_message(OWNER_ID, f"⚠️ Mijozga ({client_id}) fayl yuborishda xato: {e}")

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
    await message.answer(
        f"👋 Salom, Venox! <b>VENOX AI v11.0 PRO</b> tayyor.\n"
        f"💳 Karta: <code>{CARD_NUMBER}</code> ({CARD_OWNER})\n"
        f"🛠 Buyruqlar: /ai_help",
        parse_mode="HTML",
        reply_markup=get_main_menu()
    )

@dp.message(Command("ai_help"), F.from_user.id == OWNER_ID)
async def cmd_help(message: Message):
    await message.answer(
        "🛠 <b>VENOX OWNER COMMANDS:</b>\n\n"
        "/ai_status — Bot holati va karta\n"
        "/ai_mode — Rejimlar ro'yxati\n"
        "/ai_autopilot — Standart rejim\n"
        "/ai_quiet — Qisqa javoblar\n"
        "/ai_brain — Chuqur tahlil\n"
        "/ai_stats — Suhbat va biznes statistikasi\n"
        "/ai_memory — Xotira holati\n"
        "/ai_restart_context — Xotirani tozalash",
        parse_mode="HTML"
    )

@dp.message(Command("ai_status"), F.from_user.id == OWNER_ID)
async def cmd_status(message: Message):
    h = "🟢 Yoqilgan" if AI_ENABLED else "🔴 O'chirilgan"
    m = WORKING_MODELS[0] if WORKING_MODELS else "Avto"
    await message.answer(
        f"🤖 <b>VENOX AI v11.0 PRO</b>\n"
        f"📡 Holat: {h}\n"
        f"⚙️ Rejim: {CURRENT_MODE}\n"
        f"🧠 Model: {m}\n"
        f"💳 Karta: <code>{CARD_NUMBER}</code> ({CARD_OWNER})\n"
        f"🕒 Vaqt: {get_uzb_time()}",
        parse_mode="HTML"
    )

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
        f"📊 <b>UMUMIY VA BIZNES STATISTIKA</b>\n\n"
        f"💬 Jami xabarlar: {STATS['total_messages']}\n"
        f"👥 Faol odamlar: {len(STATS['active_users'])}\n"
        f"🤖 AI javoblari: {STATS['ai_replies']}\n"
        f"📨 Venoxga murojaatlar: {STATS['venox_requests']}\n"
        f"⏳ Kutilayotgan cheklar: {STATS['orders_pending']}\n"
        f"✅ Yasab berilgan botlar: {STATS['orders_approved']}\n"
        f"⚠️ Shikoyat/O'zgartirishlar: {STATS['complaints']}\n"
        f"🛡 Bloklangan spam/haqorat: {STATS['spam_blocked']}",
        parse_mode="HTML"
    )

@dp.message(Command("ai_memory"), F.from_user.id == OWNER_ID)
async def cmd_mem(message: Message):
    await message.answer(f"🧠 Suhbat tarixi: {len(chat_history)}\nFaol mijoz holatlari: {len(client_states)}")

@dp.message(Command("ai_restart_context"), F.from_user.id == OWNER_ID)
async def cmd_reset(message: Message):
    chat_history.clear()
    client_states.clear()
    WORKING_MODELS.clear()
    VISION_MODELS.clear()
    await message.answer("🧹 Barcha suhbat xotirasi va kesh tozalandi!")

@dp.message(F.from_user.id == OWNER_ID)
async def handle_owner(message: Message):
    global AI_ENABLED, CUSTOM_SYSTEM_PROMPT, CARD_NUMBER, CARD_OWNER
    text = message.text or ""

    if "AI Yoqilgan" in text or "AI O'chirilgan" in text:
        AI_ENABLED = not AI_ENABLED
        await message.answer(f"Avto-javob va savdo: {'YOQILDI ✅' if AI_ENABLED else 'OCHIRILDI ❌'}", reply_markup=get_main_menu())
        return
    elif text == "💳 Karta Sozlash":
        admin_states[OWNER_ID] = "WAIT_CARD"
        await message.answer("💳 Karta raqami va egasini vergul bilan yozing.\nMasalan:\n<code>8600 1234 5678 9012, VENOX</code>", parse_mode="HTML")
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

    if state == "WAIT_CARD":
        if "," in text:
            num, owner = text.split(",", 1)
            CARD_NUMBER = num.strip()
            CARD_OWNER = owner.strip()
        else:
            CARD_NUMBER = text.strip()
        await message.answer(f"✅ Karta yangilandi!\n💳 {CARD_NUMBER}\n👤 {CARD_OWNER}", reply_markup=get_main_menu())
        return
    elif state == "WAIT_PROMPT":
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
            {"role": "system", "content": "Senior Python dasturchi sifatida kodni to'g'irlab ber."},
            {"role": "user", "content": text}
        ], max_tokens=1000, clean_format=False)
        await w.edit_text(rep[:4000])
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
    print("VENOX AI v11.0 ULTIMATE PRO ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
