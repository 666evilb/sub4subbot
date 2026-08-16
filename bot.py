import os
import asyncio
import logging
import datetime
import html
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
MIN_DEPOSIT = 0.5

WEBHOOK_PORT = int(os.getenv("PORT", 8080))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db_pool = None

class OrderState(StatesGroup):
    waiting_for_channel = State()
    waiting_for_count = State()

class DepositState(StatesGroup):
    waiting_for_amount = State()

class AdminAddBalanceState(StatesGroup):
    waiting_for_target_id = State()
    waiting_for_add_amount = State()

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
    logging.info("Ma'lumotlar bazasi tayyorlandi.")

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
            text=f"🎉 <b>New Referral Joined!</b>\n\nUser ID <code>{new_user_id}</code> used your link.\nYou earned <code>+{REFERRAL_BONUS:.3f} USDT</code>!",
            parse_mode="HTML"
        )
    except Exception:
        pass

async def update_balance(user_id: int, amount: float):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

def is_admin(user: types.User):
    return user.username and user.username.lower() == ADMIN_USERNAME.lower()

async def create_oxapay_invoice(user_id: int, amount: float):
    url = "https://api.oxapay.com/merchants/request"
    payload = {
        "merchant": OXAPAY_MERCHANT_KEY,
        "amount": amount,
        "currency": "USDT",
        "lifeTime": 60,
        "feePaidByPayer": 1,
        "underPaidCoverage": 0,
        "callbackUrl": "https://sub4subbot.onrender.com/oxapay_callback",
        "description": f"Deposit for User {user_id}"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("result") == 100:
                track_id = data.get("trackId")
                pay_url = data.get("payLink")
                
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO deposits (track_id, user_id, amount) VALUES ($1, $2, $3)",
                        str(track_id), user_id, amount
                    )
                return pay_url
            return None

# 🌐 CRON-JOB VA HEALTHCHECK UCHUN ASOSIY SAHIFA
async def health_check_handler(request):
    return web.Response(text="Bot is running successfully!", status=200)

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

                    await conn.execute("UPDATE deposits SET status = 'paid' WHERE track_id = $1", track_id)
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

                    await bot.send_message(
                        chat_id=user_id,
                        text=f"✅ <b>DEPOSIT SUCCESSFUL!</b>\n\n"
                             f"💵 Amount: <code>{amount:.3f} USDT</code>\n"
                             f"💳 Your balance has been automatically credited!",
                        parse_mode="HTML"
                    )
        return web.Response(text="OK", status=200)
    except Exception as e:
        logging.error(f"Webhook Error: {e}")
        return web.Response(text="Error", status=400)

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎯 Earn"), KeyboardButton(text="🛒 Create Order")],
        [KeyboardButton(text="💳 Wallet / Deposit"), KeyboardButton(text="🎁 Daily Bonus")],
        [KeyboardButton(text="👥 Referral"), KeyboardButton(text="ℹ️ Help")]
    ],
    resize_keyboard=True
)

@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    await get_or_create_user(message.from_user.id, referrer_id)

    await message.answer(
        "👋 <b>Welcome to the Bot!</b>\n\n"
        "👤 Gain real active subscribers for your Telegram channels or earn USDT by joining channels!\n\n"
        f"📢 <b>Orders Feed Channel:</b> {html.escape(ORDERS_CHANNEL)}\n"
        "For more details, check the <b>«ℹ️ Help»</b> menu.",
        reply_markup=main_menu,
        parse_mode="HTML"
    )

# 📊 ADMIN STATISTIKA BUYRUG'I
@dp.message(Command("stats"))
async def stats_command(message: types.Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user):
        return

    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        active_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE done_count < req_count")
        total_completed_subs = await conn.fetchval("SELECT COUNT(*) FROM completed_subs")
        total_deposits = await conn.fetchval("SELECT COALESCE(SUM(amount), 0.0) FROM deposits WHERE status = 'paid'")

    await message.answer(
        f"📊 <b>BOT SYSTEM STATISTICS:</b>\n\n"
        f"👥 <b>Total Users:</b> <code>{total_users}</code>\n"
        f"🛒 <b>Total Orders Created:</b> <code>{total_orders}</code>\n"
        f"⏳ <b>Active Orders:</b> <code>{active_orders}</code>\n"
        f"✅ <b>Total Completed Subs:</b> <code>{total_completed_subs}</code>\n"
        f"💳 <b>Total Paid Deposits:</b> <code>{total_deposits:.2f} USDT</code>",
        parse_mode="HTML"
    )

@dp.message(Command("admin"))
async def admin_panel(message: types.Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user):
        return

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Add Balance / Giveaway", callback_data="admin_add_bal")]
    ])

    await message.answer(
        f"👑 <b>Welcome Admin, @{html.escape(ADMIN_USERNAME)}!</b>\n\nSelect an action:",
        reply_markup=admin_kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_add_bal")
async def admin_start_add_bal(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        return
    await call.message.answer("📥 <b>Enter User Telegram ID:</b>", parse_mode="HTML")
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
        f"👤 <b>Target User:</b> <code>{target_id}</code>\n"
        f"💳 <b>Current Balance:</b> <code>{row['balance']:.3f} USDT</code>\n\n"
        f"💵 Enter USDT amount to add:",
        parse_mode="HTML"
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
        f"✅ <b>Balance Updated!</b>\n\n"
        f"👤 User ID: <code>{target_id}</code>\n"
        f"➕ Added: <code>{add_amount:.3f} USDT</code>\n"
        f"💳 New Balance: <code>{new_bal:.3f} USDT</code>",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            chat_id=target_id,
            text=f"🎁 <b>GIVEAWAY BONUS RECEIVED!</b>\n\nYou received <code>+{add_amount:.3f} USDT</code>!\n💳 <b>New Balance:</b> <code>{new_bal:.3f} USDT</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await state.clear()

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
                f"🎉 <b>Daily Bonus Claimed!</b>\n\n"
                f"You received <code>+{DAILY_BONUS:.3f} USDT</code>.\n"
                f"Current Balance: <code>{new_row['balance']:.3f} USDT</code>",
                parse_mode="HTML"
            )

@dp.message(F.text == "ℹ️ Help")
async def help_handler(message: types.Message, state: FSMContext):
    await state.clear()
    help_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Orders Feed Channel", url=f"https://t.me/{ORDERS_CHANNEL.replace('@', '')}")],
        [InlineKeyboardButton(text="💬 Contact Support", url=f"https://t.me/{ADMIN_USERNAME}")]
    ])
    await message.answer(
        "ℹ️ <b>Help & Information Guide:</b>\n\n"
        "• <b>🎯 Earn:</b> Subscribe to promoted channels and earn real USDT.\n"
        "• <b>🛒 Create Order:</b> Promote your channel to get real active subscribers.\n"
        "• <b>💳 Wallet / Deposit:</b> Top up your balance via Crypto (USDT).\n"
        "• <b>🎁 Daily Bonus:</b> Claim free USDT rewards every 24 hours.\n"
        "• <b>👥 Referral:</b> Invite friends and earn commission on their activity.\n\n"
        f"👨‍💻 <b>Admin / Support:</b> @{html.escape(ADMIN_USERNAME)}\n"
        f"📢 <b>Public Orders Feed:</b> {html.escape(ORDERS_CHANNEL)}",
        reply_markup=help_kb,
        parse_mode="HTML"
    )

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
        f"💳 <b>Your Account Balance</b>\n\n"
        f"Balance: <code>{balance:.3f} USDT</code>\n"
        f"User ID: <code>{message.from_user.id}</code>\n\n"
        f"Click the button below to top up automatically via OxaPay:",
        reply_markup=deposit_kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "start_deposit")
async def start_deposit_callback(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer(
        f"💵 <b>Enter deposit amount in USDT</b> (Minimum: <code>{MIN_DEPOSIT:.2f} USDT</code>):",
        parse_mode="HTML"
    )
    await state.set_state(DepositState.waiting_for_amount)

@dp.message(DepositState.waiting_for_amount)
async def process_deposit_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < MIN_DEPOSIT:
            await message.answer(f"❌ Minimum deposit amount is <code>{MIN_DEPOSIT:.2f} USDT</code>!", parse_mode="HTML")
            return

        pay_url = await create_oxapay_invoice(message.from_user.id, amount)

        if pay_url:
            pay_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Pay via OxaPay", url=pay_url)]
            ])
            await message.answer(
                f"🧾 <b>Invoice Created!</b>\n\n"
                f"Amount: <code>{amount:.2f} USDT</code>\n"
                f"Status: Waiting for payment...\n\n"
                f"Click the button below to complete the payment. Your balance will be credited automatically upon confirmation!",
                reply_markup=pay_kb,
                parse_mode="HTML"
            )
        else:
            await message.answer("⚠️ Error creating payment link. Please try again or contact support.")
    except ValueError:
        await message.answer("❌ Invalid amount. Please enter a valid number (e.g. 1.0 or 5).")
    finally:
        await state.clear()

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
        f"👥 <b>Referral Program</b>\n\n"
        f"Earn <b>{REFERRAL_BONUS:.3f} USDT</b> for every active user invited!\n\n"
        f"📊 <b>Total Invited:</b> <code>{ref_count}</code> users\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{ref_link}</code>",
        parse_mode="HTML"
    )

@dp.message(F.text == "🎯 Earn")
async def earn_handler(message: types.Message, state: FSMContext):
    await state.clear()
    earn_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👣 Open Feed Channel", url=f"https://t.me/{ORDERS_CHANNEL.replace('@', '')}")]
    ])
    await message.answer(
        f"👣 Go to the <b>{html.escape(ORDERS_CHANNEL)}</b> channel and subscribe to the advertised channels. "
        f"You will receive <b>{SUB_REWARD:.3f} USDT</b> for each channel you join!\n\n"
        f"⚠️ <b>Do not leave the subscribed channel or group for 15 days!</b>\n"
        f"🚫 If you unsubscribe before 15 days, a penalty of <b>{SUB_REWARD * 2:.3f} USDT</b> will be deducted from your balance!",
        reply_markup=earn_kb,
        parse_mode="HTML"
    )

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
            clean_ch_name = html.escape(channel)
            req_c = order_data['req_count']
            done_c = order_data['done_count']
            msg_id = order_data['channel_msg_id']

            already_sub = await conn.fetchrow("SELECT 1 FROM completed_subs WHERE user_id = $1 AND LOWER(channel_username) = LOWER($2)", user_id, channel)
            if already_sub:
                await call.answer("❌ You have already claimed reward for joining this channel!", show_alert=True)
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
                    clean_url_channel = channel.replace("@", "")

                    if msg_id:
                        if new_done_c >= req_c:
                            updated_text = (
                                f"✅ <b>ORDER COMPLETED</b>\n\n"
                                f"📢 <b>Channel:</b> {clean_ch_name}\n"
                                f"🎯 <b>Goal Reached:</b> {new_done_c}/{req_c} Subscribers\n"
                                f"🆔 <b>Order ID:</b> #{order_id}"
                            )
                            kb = None
                        else:
                            updated_text = (
                                f"📌 <b>NEW ORDER CREATED</b>\n\n"
                                f"📢 <b>Channel:</b> {clean_ch_name}\n"
                                f"📊 <b>Progress:</b> {new_done_c}/{req_c} Subscribers\n"
                                f"💰 <b>Reward per sub:</b> <code>{SUB_REWARD:.3f} USDT</code>\n"
                                f"🆔 <b>Order ID:</b> #{order_id}"
                            )
                            kb = InlineKeyboardMarkup(inline_keyboard=[
                                [
                                    InlineKeyboardButton(text="📢 Join Channel", url=f"https://t.me/{clean_url_channel}"),
                                    InlineKeyboardButton(text="✅ Verify", callback_data=f"check_{order_id}")
                                ]
                            ])

                        try:
                            await bot.edit_message_text(chat_id=ORDERS_CHANNEL, message_id=msg_id, text=updated_text, reply_markup=kb, parse_mode="HTML")
                        except Exception:
                            pass

                    await call.answer(f"🎉 Success! +{SUB_REWARD:.3f} USDT added to your balance.", show_alert=True)
                else:
                    await call.answer("❌ You haven't subscribed to the channel yet!", show_alert=True)
            except Exception:
                await call.answer("⚠️ Bot is not an admin in the channel or channel not found.", show_alert=True)
    except Exception as e:
        await call.answer(f"⚠️ Error: {str(e)}", show_alert=True)

@dp.message(F.text == "🛒 Create Order")
async def order_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("📢 Send your channel username (e.g., <code>@my_channel</code>):", parse_mode="HTML")
    await state.set_state(OrderState.waiting_for_channel)

@dp.message(OrderState.waiting_for_channel)
async def process_channel(message: types.Message, state: FSMContext):
    channel = message.text.strip()

    if channel in ["🎯 Earn", "🛒 Create Order", "💳 Wallet / Deposit", "🎁 Daily Bonus", "👥 Referral", "ℹ️ Help"]:
        await state.clear()
        return

    if not channel.startswith("@"):
        await message.answer("❌ Invalid format! Channel username must start with <code>@</code> (e.g. <code>@my_channel</code>).", parse_mode="HTML")
        return

    await state.update_data(channel=channel)
    await message.answer(f"🔢 Enter subscriber count (Price: <b>{SUB_PRICE:.3f} USDT</b> per sub):", parse_mode="HTML")
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
                f"❌ <b>Insufficient Balance!</b>\n\n"
                f"Total Required: <code>{total_price:.3f} USDT</code>\n"
                f"Your Balance: <code>{balance:.3f} USDT</code>\n\n"
                f"Please top up your balance via <b>Wallet / Deposit</b>.",
                parse_mode="HTML"
            )
            await state.clear()
            return

        data = await state.get_data()
        channel = data['channel']
        clean_ch_name = html.escape(channel)
        clean_url_channel = channel.replace("@", "")

        try:
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", total_price, user_id)
            order_id = await conn.fetchval(
                "INSERT INTO orders (user_id, channel_username, req_count) VALUES ($1, $2, $3) RETURNING id",
                user_id, channel, count
            )

            feed_kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="📢 Join Channel", url=f"https://t.me/{clean_url_channel}"),
                    InlineKeyboardButton(text="✅ Verify", callback_data=f"check_{order_id}")
                ]
            ])

            feed_msg = await bot.send_message(
                chat_id=ORDERS_CHANNEL,
                text=(
                    f"📌 <b>NEW ORDER CREATED</b>\n\n"
                    f"📢 <b>Channel:</b> {clean_ch_name}\n"
                    f"📊 <b>Progress:</b> 0/{count} Subscribers\n"
                    f"💰 <b>Reward per sub:</b> <code>{SUB_REWARD:.3f} USDT</code>\n"
                    f"🆔 <b>Order ID:</b> #{order_id}"
                ),
                reply_markup=feed_kb,
                parse_mode="HTML"
            )

            await conn.execute("UPDATE orders SET channel_msg_id = $1 WHERE id = $2", feed_msg.message_id, order_id)

            new_bal_row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
            new_balance = new_bal_row['balance']

            await message.answer(
                f"✅ <b>ORDER PLACED SUCCESSFULLY!</b>\n\n"
                f"🆔 <b>Order ID:</b> #{order_id}\n"
                f"📢 <b>Target Channel:</b> {clean_ch_name}\n"
                f"👥 <b>Subscribers Ordered:</b> {count}\n"
                f"💵 <b>Total Cost:</b> <code>{total_price:.3f} USDT</code>\n"
                f"💳 <b>Remaining Balance:</b> <code>{new_balance:.3f} USDT</code>\n\n"
                f"🚀 Your task is now live in {html.escape(ORDERS_CHANNEL)}!",
                parse_mode="HTML"
            )
        except Exception as e:
            await message.answer(f"⚠️ <b>Error publishing order:</b>\n<code>{html.escape(str(e))}</code>", parse_mode="HTML")

    await state.clear()

async def main():
    try:
        await init_db()
        logging.info("✅ Database connected.")

        app = web.Application()
        # Marshrutlar sozlandi
        app.router.add_get("/", health_check_handler)
        app.router.add_post("/oxapay_callback", oxapay_webhook_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
        await site.start()

        logging.info("🤖 Bot Polling started...")
        await dp.start_polling(bot)

    finally:
        if db_pool:
            await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())