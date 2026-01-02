import os
TOKEN = os.getenv("BOT_TOKEN")
import time
import json
import asyncio
import sqlite3
import decimal
import logging
import requests
from typing import Optional

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()

DASH_RPC_URL = os.getenv("DASH_RPC_URL", "http://127.0.0.1:9998/").strip()
DASH_RPC_USER = os.getenv("DASH_RPC_USER", "").strip()
DASH_RPC_PASSWORD = os.getenv("DASH_RPC_PASSWORD", "").strip()

ADMIN_IDS = set()
for x in os.getenv("ADMIN_IDS", "").split(","):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

RATE_LOCK_SECONDS = 15 * 60
CHECK_SECONDS = 10
MIN_CONFIRMATIONS = 0  # 0 = instant detection (you can change to 1 later)

# Prices (USD) - keys are your variant names shown to users
PRICE_TABLE = {
    "Gorilla Glue #4": {"0.5": 22, "1.0": 32, "2.0": 52},
    "Blue Dream": {"0.5": 20, "1.0": 30, "2.0": 50},
}

# Admin-friendly variant aliases (what YOU type in /add)
# /add gorilla area1 1
# /add blue area2 0.5
VARIANT_ALIASES = {
    "gorilla": "Gorilla Glue #4",
    "glue": "Gorilla Glue #4",
    "gg4": "Gorilla Glue #4",

    "blue": "Blue Dream",
    "bluedream": "Blue Dream",
    "blue_dream": "Blue Dream",
}

# Areas: (area_id, button_label)
# You can change labels any time. Keep area_id short + unique.
AREAS = [
    ("area1", "Կենտրոն"),
    ("area2", "Կոմիտաս"),
    ("area3", "Ավան"),
    ("area4", "Մալաթիա"),
    ("area5", "Աջափնյակ"),
    ("area6", "Զեյթուն"),
    ("area7", "Մասիվ"),
    ("area8", "Էրեբունի"),
    ("area9", "Դավթաշեն"),
    ("area10", "Շենգավիթ"),
]

# =========================
# RUNTIME STATE
# =========================
# uid -> (area_id, variant, weight)
ADMIN_ADD_TARGET = {}

# uid -> order_id (waiting for user refund address)
PENDING_REFUND_ADDR = {}

# =========================
# DB HELPERS
# =========================
def db_exec(q, p=()):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(q, p)
    con.commit()
    con.close()

def db_all(q, p=()):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(q, p)
    rows = cur.fetchall()
    con.close()
    return rows

def db_one(q, p=()):
    rows = db_all(q, p)
    return rows[0] if rows else None

def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Orders include area
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        area TEXT NOT NULL,
        variant TEXT NOT NULL,
        weight TEXT NOT NULL,
        usd_total TEXT NOT NULL,
        rate_usd_per_dash TEXT NOT NULL,
        dash_amount TEXT NOT NULL,
        address TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        paid INTEGER NOT NULL DEFAULT 0,
        delivered INTEGER NOT NULL DEFAULT 0,

        refund_requested INTEGER NOT NULL DEFAULT 0,
        refund_address TEXT,
        refunded INTEGER NOT NULL DEFAULT 0,
        refund_txid TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_pending ON orders(paid, delivered, expires_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_paid_waiting ON orders(paid, delivered)")

    # Media pool includes area
    cur.execute("""
    CREATE TABLE IF NOT EXISTS media_pool (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        area TEXT NOT NULL,
        variant TEXT NOT NULL,
        weight TEXT NOT NULL,
        file_id TEXT NOT NULL,
        added_at INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_media_pool_lookup ON media_pool(area, variant, weight, used, id)")

    con.commit()
    con.close()

# =========================
# UTIL
# =========================
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def norm_weight(w: str) -> str:
    w = (w or "").strip().lower().replace("g", "").strip()
    if w == "1":
        return "1.0"
    if w == "2":
        return "2.0"
    if w == ".5":
        return "0.5"
    return w

def valid_area_ids():
    return {a for a, _ in AREAS}

def area_label(area_id: str) -> str:
    for a, label in AREAS:
        if a == area_id:
            return label
    return area_id

def dash_rpc(method: str, params=None):
    if params is None:
        params = []
    payload = {"jsonrpc": "1.0", "id": "ptb", "method": method, "params": params}
    r = requests.post(
        DASH_RPC_URL,
        auth=(DASH_RPC_USER, DASH_RPC_PASSWORD),
        headers={"content-type": "application/json"},
        data=json.dumps(payload),
        timeout=10,
    )
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    return j["result"]

def get_dash_usd_rate() -> decimal.Decimal:
    url = "https://api.coingecko.com/api/v3/simple/price"
    r = requests.get(url, params={"ids": "dash", "vs_currencies": "usd"}, timeout=10)
    data = r.json()
    usd = data["dash"]["usd"]
    return decimal.Decimal(str(usd))

def take_stock_one(area_id: str, variant: str, weight: str):
    row = db_one(
        "SELECT id, file_id FROM media_pool WHERE area=? AND variant=? AND weight=? AND used=0 ORDER BY id ASC LIMIT 1",
        (area_id, variant, weight),
    )
    return row  # (media_id, file_id) or None

def stock_count(area_id: str, variant: Optional[str] = None, weight: Optional[str] = None) -> int:
    q = "SELECT COUNT(*) FROM media_pool WHERE area=? AND used=0"
    p = [area_id]
    if variant is not None:
        q += " AND variant=?"
        p.append(variant)
    if weight is not None:
        q += " AND weight=?"
        p.append(weight)
    row = db_one(q, tuple(p))
    return int(row[0]) if row else 0

# =========================
# UI (Buttons)
# =========================
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Գնել", callback_data="menu:areas")],
    ])

def kb_areas():
    rows = []
    for area_id, label in AREAS:
        if stock_count(area_id) > 0:
            rows.append([InlineKeyboardButton(label, callback_data=f"area:{area_id}")])

    if not rows:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Հետ", callback_data="menu:home")]])

    rows.append([InlineKeyboardButton("⬅️ Հետ", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

def kb_variants_for_area(area_id: str):
    rows = []
    for v in PRICE_TABLE.keys():
        if stock_count(area_id, variant=v) > 0:
            rows.append([InlineKeyboardButton(v, callback_data=f"buyv:{area_id}:{v}")])

    rows.append([InlineKeyboardButton("⬅️ Հետ", callback_data="menu:areas")])
    return InlineKeyboardMarkup(rows)

def kb_weights(area_id: str, variant: str):
    rows = []
    for w in ["0.5", "1.0", "2.0"]:
        if w not in PRICE_TABLE.get(variant, {}):
            continue
        if stock_count(area_id, variant=variant, weight=w) <= 0:
            continue
        price = PRICE_TABLE[variant][w]
        rows.append([InlineKeyboardButton(f"{w}g — ${price}", callback_data=f"buyw:{area_id}:{variant}:{w}")])

    rows.append([InlineKeyboardButton("⬅️ Հետ", callback_data=f"area:{area_id}")])
    return InlineKeyboardMarkup(rows)

def kb_paid_out_of_stock(order_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Գումարի վերադարձ", callback_data=f"refund:{order_id}")],
        [InlineKeyboardButton("⬅️ Հետ", callback_data="menu:home")],
    ])

# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Բարի գալուստ Լոլա Բոտ 🐰\n", reply_markup=kb_main())

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = kb_areas()
    if len(kb.inline_keyboard) == 1 and kb.inline_keyboard[0][0].callback_data == "menu:home":
        await update.message.reply_text("❌ Հիմա ապրանք չկա (out of stock).", reply_markup=kb_main())
    else:
        await update.message.reply_text("Ընտրիր տարածքը․", reply_markup=kb)

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin upload mode (YOU asked for):
    /add <variant_alias> <area_id> <weight>

    Examples:
    /add gorilla area1 1
    /add blue area2 0.5
    """
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Admin only.")
        return

    if len(context.args) != 3:
        await update.message.reply_text(
            "Usage:\n"
            "/add gorilla area1 1\n"
            "/add blue area2 0.5"
        )
        return

    alias = (context.args[0] or "").strip().lower()
    area_id = (context.args[1] or "").strip()
    weight = norm_weight(context.args[2])

    if alias not in VARIANT_ALIASES:
        await update.message.reply_text("❌ Unknown variant. Use: gorilla / blue")
        return

    variant = VARIANT_ALIASES[alias]

    if area_id not in valid_area_ids():
        await update.message.reply_text("❌ Unknown area_id.")
        return

    if weight not in PRICE_TABLE[variant]:
        await update.message.reply_text("❌ Invalid weight. Use 0.5 / 1 / 2")
        return

    ADMIN_ADD_TARGET[uid] = (area_id, variant, weight)
    await update.message.reply_text(
        "✅ Upload mode ON\n"
        f"Target: {variant} / {area_label(area_id)} / {weight}g\n\n"
        "Now send photos here.\nSend /done when finished."
    )

async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMIN_ADD_TARGET:
        ADMIN_ADD_TARGET.pop(uid, None)
        await update.message.reply_text("✅ Upload mode OFF.")
    else:
        await update.message.reply_text("No upload mode active.")

async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_ADD_TARGET:
        return

    area_id, variant, weight = ADMIN_ADD_TARGET[uid]
    photo = update.message.photo[-1]
    file_id = photo.file_id

    db_exec(
        "INSERT INTO media_pool (area, variant, weight, file_id, added_at, used) VALUES (?, ?, ?, ?, ?, 0)",
        (area_id, variant, weight, file_id, int(time.time())),
    )

    left = stock_count(area_id, variant=variant, weight=weight)
    await update.message.reply_text(
        f"✅ Added to stock: {area_label(area_id)} / {variant} / {weight}g (now {left} left)"
    )

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your ID: {update.effective_user.id}")

# =========================
# CALLBACK BUTTON HANDLER
# =========================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu:home":
        await q.edit_message_text("Ընտրիր տարբերակը․", reply_markup=kb_main())
        return

    if data == "menu:areas":
        kb = kb_areas()
        if len(kb.inline_keyboard) == 1 and kb.inline_keyboard[0][0].callback_data == "menu:home":
            await q.edit_message_text("❌ Հիմա ապրանք չկա (out of stock).", reply_markup=kb_main())
        else:
            await q.edit_message_text("Ընտրիր տարածքը․", reply_markup=kb)
        return

    if data.startswith("area:"):
        area_id = data.split(":", 1)[1]
        context.user_data["area_id"] = area_id

        kb = kb_variants_for_area(area_id)
        has_any_variant = any(
            btn.callback_data.startswith("buyv:")
            for row in kb.inline_keyboard
            for btn in row
        )
        if not has_any_variant:
            await q.edit_message_text("❌ Այս տարածքում հիմա ապրանք չկա։ Ընտրիր ուրիշ տարածք․", reply_markup=kb_areas())
            return

        await q.edit_message_text("Ընտրիր տեսակը․", reply_markup=kb)
        return

    if data.startswith("buyv:"):
        _, area_id, variant = data.split(":", 2)

        if stock_count(area_id, variant=variant) <= 0:
            await q.edit_message_text("❌ Այս տեսակը վերջացել է։ Ընտրիր ուրիշը․", reply_markup=kb_variants_for_area(area_id))
            return

        kb = kb_weights(area_id, variant)
        has_any_weight = any(
            btn.callback_data.startswith("buyw:")
            for row in kb.inline_keyboard
            for btn in row
        )
        if not has_any_weight:
            await q.edit_message_text("❌ Այս տեսակի համար քաշեր չկան (out of stock)։", reply_markup=kb_variants_for_area(area_id))
            return

        await q.edit_message_text(
            f"Տարածք: {area_label(area_id)}\nՏեսակ: {variant}\nԸնտրիր քաշը․",
            reply_markup=kb,
        )
        return
if data.startswith("buyw:"):
    _, area_id, variant, weight = data.split(":", 3)

    text = f"TEST OK\nArea: {area_id}\nVariant: {variant}\nWeight: {weight}"

    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([...]),
        parse_mode="Markdown"
    )
    return

    if data.startswith("buyw:"):
    _, area_id, variant, weight = data.split(":", 3)
    uid = update.effective_user.id

    if stock_count(area_id, variant=variant, weight=weight) <= 0:
        await q.edit_message_text("❌ Այս քաշը վերջացել է։")
        return

        usd_price = decimal.Decimal(str(PRICE_TABLE[variant][weight]))
        rate = get_dash_usd_rate()
        dash_amount = (usd_price / rate).quantize(decimal.Decimal("0.00000001"))
        address = dash_rpc("getnewaddress", ["order"])

        now = int(time.time())
        expires = now + RATE_LOCK_SECONDS

        db_exec(
            "INSERT INTO orders (user_id, area, variant, weight, usd_total, rate_usd_per_dash, dash_amount, address, created_at, expires_at, paid, delivered) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (
                uid,
                area_id,
                variant,
                weight,
                str(usd_price),
                str(rate),
                str(dash_amount),
                address,
                now,
                expires,
            ),
        )

        text = (
            "🧾 Պատվերը ստեղծված է\n"
            f"Տարածք: {area_label(area_id)}\n"
            f"Սորտը: {variant}\n"
            f"Քաշը: {weight}g\n"
            f"Արժեքը: ${usd_price}\n\n"
            f"Պետք է ուղարկել: {dash_amount} DASH\n"
            
f"Հասցեն:\n`{address}`\n\n"
            f"Չենջ: @swopex\n"
            "⏳ Գործարքն ակտիվ է 15 րոպե"
        )

await message.answer(
    text,
    reply_markup=InlineKeyboardMarkup([...])
)

await q.edit_message_text(
    text,
    reply_markup=InlineKeyboardMarkup([...]),
    parse_mode="Markdown"
)
return

    if data.startswith("refund:"):
        uid = update.effective_user.id
        order_id = int(data.split(":", 1)[1])

        row = db_one("SELECT id, user_id, paid, delivered, refunded FROM orders WHERE id=?", (order_id,))
        if not row:
            await q.edit_message_text("Order not found.", reply_markup=kb_main())
            return

        oid, owner, paid, delivered, refunded = row
        if owner != uid:
            await q.edit_message_text("❌ This is not your order.")
            return
        if refunded == 1:
            await q.edit_message_text("✅ Already refunded.")
            return

        PENDING_REFUND_ADDR[uid] = oid
        db_exec("UPDATE orders SET refund_requested=1 WHERE id=?", (oid,))
        await q.edit_message_text(
            "💸 Refund requested.\n\nSend your **Dash address** in a message now.\nExample: X....",
            parse_mode="Markdown",
        )
        return

# =========================
# REFUND ADDRESS MESSAGE HANDLER
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()

    if uid in PENDING_REFUND_ADDR:
        oid = PENDING_REFUND_ADDR.pop(uid)
        if len(txt) < 20 or " " in txt:
            await update.message.reply_text("❌ That doesn't look like a Dash address. Try again.")
            PENDING_REFUND_ADDR[uid] = oid
            return

        db_exec("UPDATE orders SET refund_address=? WHERE id=?", (txt, oid))
        await update.message.reply_text("✅ Refund address saved. Admin will process refund if needed.")
        return

    return

# =========================
# ADMIN REFUND SEND
# =========================
async def refundsend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Admin only.")
        return

    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /refundsend <order_id>")
        return

    oid = int(context.args[0])
    row = db_one(
        "SELECT user_id, dash_amount, refund_address, refunded FROM orders WHERE id=?",
        (oid,),
    )
    if not row:
        await update.message.reply_text("Order not found.")
        return

    user_id, dash_amount, refund_address, refunded = row
    if refunded == 1:
        await update.message.reply_text("Already refunded.")
        return
    if not refund_address:
        await update.message.reply_text("No refund address saved for this order.")
        return

    try:
        txid = dash_rpc("sendtoaddress", [refund_address, float(dash_amount)])
    except Exception as e:
        await update.message.reply_text(f"❌ Refund failed: {e}")
        return

    db_exec("UPDATE orders SET refunded=1, refund_txid=? WHERE id=?", (str(txid), oid))
    await update.message.reply_text(f"✅ Refunded.\nTXID: {txid}")

    try:
        await context.bot.send_message(chat_id=user_id, text=f"✅ Refund sent.\nTXID: {txid}")
    except Exception:
        pass

# =========================
# PAYMENT WATCHER
# =========================
async def payment_watcher(app: Application):
    while True:
        try:
            # 1) Deliver any paid but not delivered
            waiting = db_all(
                "SELECT id, user_id, area, variant, weight FROM orders WHERE paid=1 AND delivered=0 AND refunded=0 ORDER BY id ASC"
            )
            for oid, user_id, area_id, variant, weight in waiting:
                stock = take_stock_one(area_id, variant, weight)
                if stock:
                    media_id, file_id = stock
                    await app.bot.send_photo(
                        chat_id=user_id,
                        photo=file_id,
                        caption="✅ Վճարումն հաստատված է, ձեր նկարը 📸",
                    )
                    db_exec("UPDATE media_pool SET used=1 WHERE id=?", (media_id,))
                    db_exec("UPDATE orders SET delivered=1 WHERE id=?", (oid,))

            # 2) Check unpaid orders for payment
            now = int(time.time())
            unpaid = db_all(
                "SELECT id, user_id, area, variant, weight, dash_amount, address, expires_at "
                "FROM orders WHERE paid=0 AND delivered=0 AND refunded=0 ORDER BY id ASC"
            )

            for oid, user_id, area_id, variant, weight, dash_amount, address, expires_at in unpaid:
                if now > int(expires_at):
                    continue

                try:
                    received = dash_rpc("getreceivedbyaddress", [address, MIN_CONFIRMATIONS])
                    need = decimal.Decimal(str(dash_amount))
                    got = decimal.Decimal(str(received))
                except Exception:
                    continue

                if got >= need:
                    db_exec("UPDATE orders SET paid=1 WHERE id=?", (oid,))

                    stock = take_stock_one(area_id, variant, weight)
                    if stock:
                        media_id, file_id = stock
                        await app.bot.send_photo(
                            chat_id=user_id,
                            photo=file_id,
                            caption="✅ Վճարումն հաստատված է, ձեր նկարը 📸",
                        )
                        db_exec("UPDATE media_pool SET used=1 WHERE id=?", (media_id,))
                        db_exec("UPDATE orders SET delivered=1 WHERE id=?", (oid,))
                    else:
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"✅ Payment received, but out of stock for {area_label(area_id)} / {variant} / {weight}g.\n"
                                f"Admin: /add {('gorilla' if variant == 'Gorilla Glue #4' else 'blue')} {area_id} {weight}\n\n"
                                f"You can request a refund:"
                            ),
                            reply_markup=kb_paid_out_of_stock(oid),
                        )

        except Exception as e:
            logging.exception("payment_watcher error: %s", e)

        await asyncio.sleep(CHECK_SECONDS)

async def post_init(app: Application):
    app.create_task(payment_watcher(app))

# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")

    db_init()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("refundsend", refundsend))

    # Buttons
    app.add_handler(CallbackQueryHandler(on_button))

    # Photo upload for admin add mode
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_upload))

    # Text (refund address)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()

if __name__ == "__main__":
    main()

