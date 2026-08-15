import os
import asyncio
import logging
import datetime
import asyncpg
from aiohttp import web
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import load_dotenv

# .env faylini yuklash
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
OXAPAY_MERCHANT_KEY = os.getenv("OXAPAY_MERCHANT_KEY")

ADMIN_USERNAME = "byorderofmeself"
ORDERS_CHANNEL = "@zsubscriberorders"

SUB_REWARD = 0.003
SUB_PRICE = 0.005
REFERRAL_BONUS = 0.02
DAILY_BONUS = 0.005
MIN_DEPOSIT = 0.5  # Minimal to'lov summasi USDT

WEBHOOK_PORT = int(os.getenv("PORT", 8080))  # Webhook porti

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db_pool = None

# ----------------- FSM STATES -----------------
class OrderState(StatesGroup):
    waiting_for_channel = State()
    waiting_for_count = State()

class DepositState(StatesGroup):
    waiting_for_amount = State()

class AdminAddBalanceState(StatesGroup):
    waiting_for_target_id = State()
    waiting_for_add_amount = State()

# ----------------- DATABASE (SUPABASE / POSTGRESQL) -----------------
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        statement_cache_size=0,
        ssl="require",
        min_size=1,
        max_size=10
    )
    
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance DOUBLE PRECISION DEFAULT 0.0,
                referrer_id BIGINT DEFAULT NULL,
                last_bonus TEXT DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                channel_username TEXT,
                req_count INT,
                done_count INT DEFAULT 0,
                channel_msg_id BIGINT DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS completed_subs (
                user_id BIGINT,
                channel_username TEXT,
                PRIMARY KEY (user_id, channel_username)
            );
            CREATE TABLE IF NOT EXISTS deposits (
                track_id TEXT PRIMARY KEY,
                user_id BIGINT,
                amount DOUBLE PRECISION,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_subs ON completed_subs(user_id, channel_username);
            CREATE INDEX IF NOT EXISTS idx_ref ON users(referrer_id);
        ''')
    logging.info("Ma'lumotlar bazasi va jadvallar muvaffaqiyatli tayyorlandi.")

async def get_or_create_user(user_id: int, referrer_id: int = None):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        if not row:
            valid_ref = referrer_id if (referrer_id and referrer_id != user_id) else None
            if valid_ref:
                ref_exists = await conn.fetchrow("SELECT 1 FROM users WHERE user_id = $1", valid_ref)
                if not ref_exists:
                    valid_ref = None

            await conn.execute("INSERT INTO users (user_id, balance, referrer_id) VALUES ($1, 0.0, $2)", user_id, valid_ref)

            if valid_ref:
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", REFERRAL_BONUS, valid_ref)
                asyncio.create_task(send_ref_notification(valid_ref, user_id))

            return 0.0
        return row['balance']

async def send_ref_notification(referrer_id: int, new_user_id: int):
    try:
        await bot.send_message(
            chat_id=referrer_id,
            text=f"🎉 **New Referral Joined!**\n\nUser ID `{new_user_id}` used your link.\nYou earned `+{REFERRAL_BONUS:.3f} USDT`!",
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def update_balance(user_id: int, amount: float):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

def is_admin(user: types.User):
    return user.username and user.username.lower() == ADMIN_USERNAME.lower()

# ----------------- OXAPAY INTEGRATION -----------------
async def create_oxapay_invoice(user_id: int, amount: float):
    url = "https://api.oxapay.com/merchants/request"
    payload = {
        "merchant": OXAPAY_MERCHANT_KEY,
        "amount": amount,
        "currency": "USDT",
        "lifeTime": 60,
        "feePaidByPayer": 1,
        "underPaidCoverage": 0,
        "callbackUrl": f"https://your-domain.com/oxapay_callback", # O'zingizning server/ngrok havolangiz
        "description": f"Deposit for User {user_id}"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("result") == 100:
                track_id = data.get("trackId")
                pay_url = data.get("payLink")
                
                # Bazaga pending to'lovni saqlash
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO deposits (track_id, user_id, amount) VALUES ($1, $2, $3)",
                        str(track_id), user_id, amount
                    )
                return pay_url
            return None

# Webhook xabarlarini qabul qilish (OxaPay callback)
async def oxapay_webhook_handler(request):
    try:
        data = await request.json()
        track_id = str(data.get("trackId"))
        status = data.get("status")

        if status == "Paid":
            async with db_pool.acquire() as conn:
                dep = await conn.fetchrow("SELECT user_id, amount, status FROM deposits WHERE track_id = $1", track_id)
                if dep and dep['status'] == 'pending':
                    user_id = dep['user_id']
                    amount = dep['amount']

                    # Statusni yangilash va balansni oshirish
                    await conn.execute("UPDATE deposits SET status = 'paid' WHERE track_id = $1", track_id)
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

                    # Foydalanuvchiga xabar yuborish
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"✅ **DEPOSIT SUCCESSFUL!**\n\n"
                             f"💵 Amount: `{amount:.3f} USDT`\n"
                             f"💳 Your balance has been automatically credited!",
                        parse_mode="Markdown"
                    )
        return web.Response(text="OK", status=200)
    except Exception as e:
        logging.error(f"Webhook Error: {e}")
        return web.Response(text="Error", status=400)

# ----------------- KEYBOARDS -----------------
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎯 Earn"), KeyboardButton(text="🛒 Create Order")],
        [KeyboardButton(text="💳 Wallet / Deposit"), KeyboardButton(text="🎁 Daily Bonus")],
        [KeyboardButton(text="👥 Referral"), KeyboardButton(text="ℹ️ Help")]
    ],
    resize_keyboard=True
)

# ----------------- HANDLERS -----------------

@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    await get_or_create_user(message.from_user.id, referrer_id)

    await message.answer(
        "👋 **Welcome to the Bot!**\n\n"
        "👤 Gain real active subscribers for your Telegram channels or earn USDT by joining channels!\n\n"
        f"📢 **Orders Feed Channel:** {ORDERS_CHANNEL}\n"
        "For more details, check the **«ℹ️ Help»** menu.",
        reply_markup=main_menu,
        parse_mode="Markdown"
    )

# 👑 ADMIN PANEL
@dp.message(Command("admin"))
async def admin_panel(message: types.Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user):
        return

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Add Balance / Giveaway", callback_data="admin_add_bal")]
    ])

    await message.answer(
        f"👑 **Welcome Admin, @{ADMIN_USERNAME}!**\n\nSelect an action:",
        reply_markup=admin_kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_add_bal")
async def admin_start_add_bal(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        return
    await call.message.answer("📥 **Enter User Telegram ID:**")
    await state.set_state(AdminAddBalanceState.waiting_for_target_id)

@dp.message(AdminAddBalanceState.waiting_for_target_id)
async def admin_process_target_id(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    if not message.text.isdigit():
        await message.answer("❌ Invalid User ID. Enter a numeric ID.")
        return

    target_id = int(message.text)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", target_id)
        if not row:
            await message.answer("⚠️ User not found in database.")
            await state.clear()
            return

    await state.update_data(target_id=target_id)
    await message.answer(
        f"👤 **Target User:** `{target_id}`\n"
        f"💳 **Current Balance:** `{row['balance']:.3f} USDT`\n\n"
        f"💵 Enter USDT amount to add:",
        parse_mode="Markdown"
    )
    await state.set_state(AdminAddBalanceState.waiting_for_add_amount)

@dp.message(AdminAddBalanceState.waiting_for_add_amount)
async def admin_process_add_amount(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    try:
        add_amount = float(message.text)
    except ValueError:
        await message.answer("❌ Invalid amount.")
        return

    data = await state.get_data()
    target_id = data['target_id']

    await update_balance(target_id, add_amount)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", target_id)
        new_bal = row['balance']

    await message.answer(
        f"✅ **Balance Updated!**\n\n"
        f"👤 User ID: `{target_id}`\n"
        f"➕ Added: `{add_amount:.3f} USDT`\n"
        f"💳 New Balance: `{new_bal:.3f} USDT`",
        parse_mode="Markdown"
    )

    try:
        await bot.send_message(
            chat_id=target_id,
            text=f"🎁 **GIVEAWAY BONUS RECEIVED!**\n\nYou received `+{add_amount:.3f} USDT`!\n💳 **New Balance:** `{new_bal:.3f} USDT`",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    await state.clear()

# 🎁 DAILY BONUS
@dp.message(F.text == "🎁 Daily Bonus")
async def bonus_handler(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    await get_or_create_user(user_id)

    today_str = str(datetime.date.today())

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_bonus FROM users WHERE user_id = $1", user_id)
        last_bonus = row['last_bonus'] if row else None

        if last_bonus == today_str:
            await message.answer("❌ You have already claimed your daily bonus today!")
        else:
            await conn.execute("UPDATE users SET last_bonus = $1 WHERE user_id = $2", today_str, user_id)
            await update_balance(user_id, DAILY_BONUS)
            new_row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)

            await message.answer(
                f"🎉 **Daily Bonus Claimed!**\n\n"
                f"You received `+{DAILY_BONUS:.3f} USDT`.\n"
                f"Current Balance: `{new_row['balance']:.3f} USDT`",
                parse_mode="Markdown"
            )

# ℹ️ HELP
@dp.message(F.text == "ℹ️ Help")
async def help_handler(message: types.Message, state: FSMContext):
    await state.clear()
    help_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Orders Feed Channel", url=f"https://t.me/{ORDERS_CHANNEL[1:]}")],
        [InlineKeyboardButton(text="💬 Contact Support", url=f"https://t.me/{ADMIN_USERNAME}")]
    ])
    await message.answer(
        "ℹ️ **Help & Information Guide:**\n\n"
        "• **🎯 Earn:** Subscribe to promoted channels and earn real USDT.\n"
        "• **🛒 Create Order:** Promote your channel to get real active subscribers.\n"
        "• **💳 Wallet / Deposit:** Top up your balance via Crypto (USDT).\n"
        "• **🎁 Daily Bonus:** Claim free USDT rewards every 24 hours.\n"
        "• **👥 Referral:** Invite friends and earn commission on their activity.\n\n"
        f"👨‍💻 **Admin / Support:** @{ADMIN_USERNAME}\n"
        f"📢 **Public Orders Feed:** {ORDERS_CHANNEL}",
        reply_markup=help_kb,
        parse_mode="Markdown"
    )

# 💳 WALLET & AVTOMATIK DEPOSIT
@dp.message(F.text == "💳 Wallet / Deposit")
async def wallet_handler(message: types.Message, state: FSMContext):
    await state.clear()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        balance = row['balance'] if row else 0.0

    deposit_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Top Up Balance (Crypto / OxaPay)", callback_data="start_deposit")]
    ])

    await message.answer(
        f"💳 **Your Account Balance**\n\n"
        f"Balance: `{balance:.3f} USDT`\n"
        f"User ID: `{message.from_user.id}`\n\n"
        f"Click the button below to top up automatically via OxaPay:",
        reply_markup=deposit_kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "start_deposit")
async def start_deposit_callback(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer(
        f"💵 **Enter deposit amount in USDT** (Minimum: `{MIN_DEPOSIT:.2f} USDT`):",
        parse_mode="Markdown"
    )
    await state.set_state(DepositState.waiting_for_amount)

@dp.message(DepositState.waiting_for_amount)
async def process_deposit_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < MIN_DEPOSIT:
            await message.answer(f"❌ Minimum deposit amount is `{MIN_DEPOSIT:.2f} USDT`!")
            return

        pay_url = await create_oxapay_invoice(message.from_user.id, amount)

        if pay_url:
            pay_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Pay via OxaPay", url=pay_url)]
            ])
            await message.answer(
                f"🧾 **Invoice Created!**\n\n"
                f"Amount: `{amount:.2f} USDT`\n"
                f"Status: Waiting for payment...\n\n"
                f"Click the button below to complete the payment. Your balance will be credited automatically upon confirmation!",
                reply_markup=pay_kb,
                parse_mode="Markdown"
            )
        else:
            await message.answer("⚠️ Error creating payment link. Please try again or contact support.")
    except ValueError:
        await message.answer("❌ Invalid amount. Please enter a valid number (e.g. 1.0 or 5).")
    finally:
        await state.clear()

# 👥 REFERRAL
@dp.message(F.text == "👥 Referral")
async def ref_handler(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    await get_or_create_user(user_id)

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"

    async with db_pool.acquire() as conn:
        ref_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id = $1", user_id)

    await message.answer(
        f"👥 **Referral Program**\n\n"
        f"Earn **{REFERRAL_BONUS:.3f} USDT** for every active user invited!\n\n"
        f"📊 **Total Invited:** `{ref_count}` users\n"
        f"🔗 **Your Referral Link:**\n`{ref_link}`",
        parse_mode="Markdown"
    )

# 🎯 EARN
@dp.message(F.text == "🎯 Earn")
async def earn_handler(message: types.Message, state: FSMContext):
    await state.clear()
    earn_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👣 Open Feed Channel", url=f"https://t.me/{ORDERS_CHANNEL[1:]}")]
    ])
    await message.answer(
        f"👣 Go to the **{ORDERS_CHANNEL}** channel and subscribe to the advertised channels. "
        f"You will receive **{SUB_REWARD:.3f} USDT** for each channel you join!\n\n"
        f"⚠️ **Do not leave the subscribed channel or group for 15 days!**\n"
        f"🚫 If you unsubscribe before 15 days, a penalty of **{SUB_REWARD * 2:.3f} USDT** will be deducted from your balance!",
        reply_markup=earn_kb,
        parse_mode="Markdown"
    )

# 🔄 TASK VERIFICATION
@dp.callback_query(F.data.startswith("check_"))
async def check_subscription(call: types.CallbackQuery):
    try:
        order_id = int(call.data.split("_")[1])
        user_id = call.from_user.id

        await get_or_create_user(user_id)

        async with db_pool.acquire() as conn:
            order_data = await conn.fetchrow("SELECT channel_username, req_count, done_count, channel_msg_id FROM orders WHERE id = $1", order_id)

            if not order_data:
                await call.answer("❌ Order no longer exists or completed!", show_alert=True)
                return

            channel = order_data['channel_username']
            req_c = order_data['req_count']
            done_c = order_data['done_count']
            msg_id = order_data['channel_msg_id']

            already_sub = await conn.fetchrow("SELECT 1 FROM completed_subs WHERE user_id = $1 AND channel_username = $2", user_id, channel)
            if already_sub:
                await call.answer("❌ You have already claimed the reward for this channel!", show_alert=True)
                return

            if done_c >= req_c:
                await call.answer("🔒 This order is already fully completed!", show_alert=True)
                return

            try:
                member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
                if member.status in ["member", "administrator", "creator"]:
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", SUB_REWARD, user_id)
                    await conn.execute("INSERT INTO completed_subs (user_id, channel_username) VALUES ($1, $2)", user_id, channel)
                    await conn.execute("UPDATE orders SET done_count = done_count + 1 WHERE id = $1", order_id)

                    new_done_c = done_c + 1

                    if msg_id:
                        if new_done_c >= req_c:
                            updated_text = (
                                f"✅ **ORDER COMPLETED**\n\n"
                                f"📢 **Channel:** {channel}\n"
                                f"🎯 **Goal Reached:** {new_done_c}/{req_c} Subscribers\n"
                                f"🆔 **Order ID:** #{order_id}"
                            )
                            kb = None
                        else:
                            updated_text = (
                                f"📌 **NEW ORDER AVAILABLE**\n\n"
                                f"📢 **Channel:** {channel}\n"
                                f"📊 **Progress:** {new_done_c}/{req_c} Subscribers\n"
                                f"💰 **Reward per sub:** `{SUB_REWARD:.3f} USDT`\n"
                                f"🆔 **Order ID:** #{order_id}"
                            )
                            kb = InlineKeyboardMarkup(inline_keyboard=[
                                [
                                    InlineKeyboardButton(text="📢 Join Channel", url=f"https://t.me/{channel[1:]}"),
                                    InlineKeyboardButton(text="✅ Verify", callback_data=f"check_{order_id}")
                                ]
                            ])

                        try:
                            await bot.edit_message_text(chat_id=ORDERS_CHANNEL, message_id=msg_id, text=updated_text, reply_markup=kb, parse_mode="Markdown")
                        except Exception:
                            pass

                    await call.answer(f"🎉 Success! +{SUB_REWARD:.3f} USDT added to your balance.", show_alert=True)
                else:
                    await call.answer("❌ You haven't subscribed to the channel yet!", show_alert=True)
            except Exception:
                await call.answer("⚠️ Bot is not an admin in the channel or channel not found.", show_alert=True)
    except Exception as e:
        await call.answer(f"⚠️ Error: {str(e)}", show_alert=True)

# 🛒 CREATE ORDER
@dp.message(F.text == "🛒 Create Order")
async def order_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("📢 Send your channel username (e.g., `@my_channel`):")
    await state.set_state(OrderState.waiting_for_channel)

@dp.message(OrderState.waiting_for_channel)
async def process_channel(message: types.Message, state: FSMContext):
    channel = message.text.strip()

    if channel in ["🎯 Earn", "🛒 Create Order", "💳 Wallet / Deposit", "🎁 Daily Bonus", "👥 Referral", "ℹ️ Help"]:
        await state.clear()
        return

    if not channel.startswith("@"):
        await message.answer("❌ Invalid format! Channel username must start with `@` (e.g. `@my_channel`).")
        return

    await state.update_data(channel=channel)
    await message.answer(f"🔢 Enter subscriber count (Price: **{SUB_PRICE:.3f} USDT** per sub):", parse_mode="Markdown")
    await state.set_state(OrderState.waiting_for_count)

@dp.message(OrderState.waiting_for_count)
async def process_count(message: types.Message, state: FSMContext):
    if message.text in ["🎯 Earn", "🛒 Create Order", "💳 Wallet / Deposit", "🎁 Daily Bonus", "👥 Referral", "ℹ️ Help"]:
        await state.clear()
        return

    if not message.text.isdigit():
        await message.answer("❌ Please enter a valid number!")
        return

    count = int(message.text)
    if count < 5:
        await message.answer("⚠️ Minimum order size is 5 subscribers.")
        return

    user_id = message.from_user.id
    total_price = count * SUB_PRICE

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        balance = row['balance'] if row else 0.0

        if balance < total_price:
            await message.answer(
                f"❌ **Insufficient Balance!**\n\n"
                f"Total Required: `{total_price:.3f} USDT`\n"
                f"Your Balance: `{balance:.3f} USDT`\n\n"
                f"Please top up your balance via **Wallet / Deposit**.",
                parse_mode="Markdown"
            )
            await state.clear()
            return

        data = await state.get_data()
        channel = data['channel']

        try:
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", total_price, user_id)
            order_id = await conn.fetchval(
                "INSERT INTO orders (user_id, channel_username, req_count) VALUES ($1, $2, $3) RETURNING id",
                user_id, channel, count
            )

            feed_kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="📢 Join Channel", url=f"https://t.me/{channel[1:]}"),
                    InlineKeyboardButton(text="✅ Verify", callback_data=f"check_{order_id}")
                ]
            ])

            feed_msg = await bot.send_message(
                chat_id=ORDERS_CHANNEL,
                text=(
                    f"📌 **NEW ORDER CREATED**\n\n"
                    f"📢 **Channel:** {channel}\n"
                    f"📊 **Progress:** 0/{count} Subscribers\n"
                    f"💰 **Reward per sub:** `{SUB_REWARD:.3f} USDT`\n"
                    f"🆔 **Order ID:** #{order_id}"
                ),
                reply_markup=feed_kb,
                parse_mode="Markdown"
            )

            await conn.execute("UPDATE orders SET channel_msg_id = $1 WHERE id = $2", feed_msg.message_id, order_id)

            new_bal_row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
            new_balance = new_bal_row['balance']

            await message.answer(
                f"✅ **ORDER PLACED SUCCESSFULLY!**\n\n"
                f"🆔 **Order ID:** #{order_id}\n"
                f"📢 **Target Channel:** {channel}\n"
                f"👥 **Subscribers Ordered:** {count}\n"
                f"💵 **Total Cost:** `{total_price:.3f} USDT`\n"
                f"💳 **Remaining Balance:** `{new_balance:.3f} USDT`\n\n"
                f"🚀 Your task is now live in {ORDERS_CHANNEL}!",
                parse_mode="Markdown"
            )
        except Exception as e:
            await message.answer(f"⚠️ **Error publishing order:**\n`{str(e)}`", parse_mode="Markdown")

    await state.clear()

# ----------------- MAIN SERVER RUNNER -----------------
async def main():
    try:
        await init_db()
        logging.info("✅ Supabase Connected.")

        # Webhook va Bot Pollingni parallel yurgazish uchun Aiohttp Server
        app = web.Application()
        app.router.add_post("/oxapay_callback", oxapay_webhook_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
        await site.start()
        logging.info(f"🌐 Webhook Server running on port {WEBHOOK_PORT}")

        logging.info("🤖 Bot Polling started...")
        await dp.start_polling(bot)

    finally:
        if db_pool:
            await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())