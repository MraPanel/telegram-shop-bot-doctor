#!/usr/bin/env python3
"""ربات فروش پنل پاسارگارد (PasarGuard) - تک‌فایل، ساده و بدون دیتابیس خارجی."""
import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import string
import time
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

# ───────────────────────── پیکربندی ─────────────────────────
def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(i) for i in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# ───────────────────────── متن‌های قابل ویرایش ─────────────────────────
TEXTS = {
    "welcome": ("پیام خوش‌آمد", "سلام {name} 👋\nبه ربات خوش اومدی. یکی از گزینه‌ها رو انتخاب کن."),
    "btn_buy": ("دکمه خرید", "🛒 خرید سرویس"),
    "btn_test": ("دکمه تست", "🎁 کانفیگ تست"),
    "btn_services": ("دکمه سرویس‌ها", "📂 سرویس‌های من"),
    "btn_reseller": ("دکمه نمایندگی", "🤝 خرید نمایندگی"),
    "btn_support": ("دکمه پشتیبانی", "💬 پشتیبانی"),
    "support": ("متن پشتیبانی", "برای پشتیبانی به این آیدی پیام بدید:\n{support}"),
    "pay": ("متن پرداخت", "💳 مبلغ <b>{price}</b> تومان را کارت‌به‌کارت کنید:\n\n{card}\nبه نام {holder}\n\nبعد از پرداخت، دکمه «پرداخت کردم» را بزنید."),
    "ask_receipt": ("درخواست رسید", "📸 لطفاً تصویر رسید پرداخت را بفرستید."),
    "done": ("پیام تحویل سرویس", "✅ سرویس شما آماده شد."),
    "pending": ("پیام در انتظار تایید", "⏳ رسید ثبت شد. پس از تایید ادمین، سرویس برایتان ارسال می‌شود."),
    "rejected": ("پیام رد پرداخت", "❌ پرداخت شما تایید نشد و سرویس مربوط به آن حذف شد."),
    "reseller_done": ("پیام فعال‌سازی نمایندگی", "✅ نمایندگی شما فعال شد. از این پس با قیمت نمایندگی خرید می‌کنید."),
}

DEFAULTS = {
    "panel_url": "", "panel_user": "", "panel_pass": "", "panel_groups": "", "prefix": "user",
    "card_number": "", "card_holder": "", "instant": "1",
    "test_enabled": "1", "test_gb": "0.3", "test_hours": "24",
    "reseller_enabled": "1", "reseller_price": "0", "support_id": "",
}
for _k, (_l, _v) in TEXTS.items():
    DEFAULTS["t_" + _k] = _v

# کلید → (عنوان، بخش برگشت، نوع)
FIELDS = {
    "panel_url": ("آدرس پنل (مثلاً https://panel.example.com:8000)", "panel", "str"),
    "panel_user": ("یوزرنیم ادمین پنل", "panel", "str"),
    "panel_pass": ("رمز ادمین پنل", "panel", "str"),
    "panel_groups": ("شناسه گروه‌ها با کاما (مثلاً 1,2). برای خودکار (همه گروه‌ها) بفرستید -", "panel", "opt"),
    "prefix": ("پیشوند نام کاربری سرویس‌ها (فقط حروف انگلیسی/عدد)", "panel", "prefix"),
    "card_number": ("شماره کارت", "pay", "str"),
    "card_holder": ("نام صاحب کارت", "pay", "str"),
    "test_gb": ("حجم کانفیگ تست (گیگابایت، مثلاً 0.3)", "test", "num"),
    "test_hours": ("مدت کانفیگ تست (ساعت)", "test", "num"),
    "reseller_price": ("قیمت خرید نمایندگی (تومان)", "res", "num"),
    "support_id": ("آیدی پشتیبانی (مثلاً @support)", "txt", "str"),
}
TOGGLES = {"instant": "pay", "test_enabled": "test", "reseller_enabled": "res"}

# ───────────────────────── دیتابیس ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
create table if not exists settings(k text primary key, v text);
create table if not exists users(id integer primary key, name text, is_reseller int default 0, test_used int default 0);
create table if not exists products(id integer primary key autoincrement, name text, gb real, days int,
    price int, reseller_price int default 0, active int default 1);
create table if not exists orders(id integer primary key autoincrement, user_id int, kind text, name text,
    gb real, days int, amount int, status text, receipt text, created int);
create table if not exists services(id integer primary key autoincrement, user_id int, order_id int,
    panel_username text, label text, link text, created int);
""")


def q(sql, *a):
    return db.execute(sql, a).fetchall()


def q1(sql, *a):
    return db.execute(sql, a).fetchone()


def x(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur.lastrowid


def S(k):
    r = q1("select v from settings where k=?", k)
    return r["v"] if r else DEFAULTS[k]


def setS(k, v):
    x("insert into settings(k,v) values(?,?) on conflict(k) do update set v=excluded.v", k, v)


def T(key, **kw):
    """متن ویرایش‌پذیر؛ متن ادمین escape می‌شود و مقادیر kw باید از قبل امن باشند."""
    t = html.escape(S("t_" + key))
    for k, v in kw.items():
        t = t.replace("{" + k + "}", str(v))
    return t


def raw(key):
    return S("t_" + key)


esc = html.escape
_digits = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def num(s):
    return float(s.translate(_digits).replace(",", "").replace("٬", "").strip())


def fmt(n):
    return f"{int(n):,}"


def is_admin(uid):
    return uid in ADMIN_IDS


def ensure_user(tg):
    x("insert into users(id,name) values(?,?) on conflict(id) do update set name=excluded.name",
      tg.id, tg.full_name)
    return q1("select * from users where id=?", tg.id)


# ───────────────────────── کلاینت پاسارگارد ─────────────────────────
class PanelError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


class Panel:
    token = None

    def base(self):
        b = S("panel_url").strip().rstrip("/")
        if not b:
            raise PanelError("آدرس پنل تنظیم نشده")
        return b

    async def _http(self, method, url, **kw):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.request(method, url, **kw) as r:
                    text = await r.text()
                    try:
                        data = json.loads(text) if text else None
                    except ValueError:
                        data = text
                    return r.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise PanelError(f"اتصال به پنل ناموفق: {type(e).__name__}")

    async def login(self):
        st, data = await self._http("POST", self.base() + "/api/admin/token",
                                    data={"username": S("panel_user"), "password": S("panel_pass")})
        if st != 200 or not isinstance(data, dict) or "access_token" not in data:
            raise PanelError(f"ورود به پنل ناموفق ({st}) - یوزرنیم/رمز را بررسی کنید", st)
        self.token = data["access_token"]

    async def req(self, method, path, **kw):
        for attempt in (0, 1):
            if not self.token:
                await self.login()
            st, data = await self._http(method, self.base() + path,
                                        headers={"Authorization": "Bearer " + self.token}, **kw)
            if st == 401 and attempt == 0:
                self.token = None
                continue
            if st >= 400:
                raise PanelError(f"{st}: {str(data)[:120]}", st)
            return data

    async def group_ids(self):
        g = S("panel_groups").replace(" ", "")
        if g:
            return [int(i) for i in g.split(",") if i.isdigit()]
        d = await self.req("GET", "/api/groups")
        lst = d.get("groups", []) if isinstance(d, dict) else d
        return [i["id"] for i in lst]

    async def create_user(self, username, gb, expire_ts):
        body = {"username": username, "status": "active", "data_limit": int(gb * 1024 ** 3),
                "group_ids": await self.group_ids(), "data_limit_reset_strategy": "no_reset"}
        iso = datetime.fromtimestamp(expire_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            return await self.req("POST", "/api/user", json={**body, "expire": iso})
        except PanelError as e:
            if e.status != 422:
                raise
            return await self.req("POST", "/api/user", json={**body, "expire": int(expire_ts)})

    async def get_user(self, username):
        return await self.req("GET", f"/api/user/{username}")

    async def delete_user(self, username):
        try:
            await self.req("DELETE", f"/api/user/{username}")
        except PanelError as e:
            if e.status != 404:  # قبلاً حذف شده = مشکلی نیست
                raise


panel = Panel()
BOT = None

# ───────────────────────── ابزارهای رابط ─────────────────────────
r = Router()
waiting = {}  # uid -> (action, extra)


def K(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


async def show(ev, text, kb=None):
    """روی callback همان پیام را ویرایش می‌کند (روان و بدون شلوغی)."""
    if isinstance(ev, CallbackQuery):
        try:
            if ev.message.photo:
                await ev.message.edit_caption(caption=text, reply_markup=kb)
            else:
                await ev.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await ev.message.answer(text, reply_markup=kb)
    else:
        await ev.answer(text, reply_markup=kb)


def home_kb(uid):
    u = q1("select * from users where id=?", uid)
    rows = [[(raw("btn_buy"), "buy")]]
    row = []
    if S("test_enabled") == "1":
        row.append((raw("btn_test"), "test"))
    row.append((raw("btn_services"), "svc"))
    rows.append(row)
    row = []
    if S("reseller_enabled") == "1" and u and not u["is_reseller"]:
        row.append((raw("btn_reseller"), "res"))
    row.append((raw("btn_support"), "sup"))
    rows.append(row)
    if is_admin(uid):
        rows.append([("⚙️ تنظیمات", "adm")])
    return K(*rows)


def welcome_text(tg):
    return T("welcome", name=esc(tg.full_name))


def price_for(p, u):
    if u["is_reseller"] and p["reseller_price"] > 0:
        return p["reseller_price"]
    return p["price"]


def gb_txt(gb):
    return "نامحدود" if not gb else f"{gb:g} گیگ"


async def alert_admins(text):
    for a in ADMIN_IDS:
        try:
            await BOT.send_message(a, text)
        except Exception:
            pass


# ───────────────────────── ساخت سرویس ─────────────────────────
async def provision(user_id, gb, days=None, hours=None, order_id=None, label=""):
    uname = S("prefix") + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    secs = days * 86400 if days else hours * 3600
    u = await panel.create_user(uname, gb, time.time() + secs)
    link = (u or {}).get("subscription_url", "") or ""
    if link.startswith("/"):
        link = panel.base() + link
    sid = x("insert into services(user_id,order_id,panel_username,label,link,created) values(?,?,?,?,?,?)",
            user_id, order_id, uname, label, link, int(time.time()))
    return sid, link


async def deliver(o):
    if o["kind"] == "p":
        _, link = await provision(o["user_id"], o["gb"], days=o["days"], order_id=o["id"], label=o["name"])
        return f"{T('done')}\n\n📦 {esc(o['name'])}\n🔗 <code>{esc(link)}</code>"
    x("update users set is_reseller=1 where id=?", o["user_id"])
    return T("reseller_done")


async def revoke(o):
    """حذف همه‌چیزی که با این پرداخت ساخته شده. برمی‌گرداند: (تعداد حذف‌شده، خطاها)"""
    done, fails = 0, []
    for s in q("select * from services where order_id=?", o["id"]):
        try:
            await panel.delete_user(s["panel_username"])
            x("delete from services where id=?", s["id"])
            done += 1
        except PanelError as e:
            fails.append(f"{s['panel_username']}: {e}")
    if o["kind"] == "r":
        x("update users set is_reseller=0 where id=?", o["user_id"])
    return done, fails


def order_caption(o, note=""):
    u = q1("select * from users where id=?", o["user_id"])
    st = {"delivered": "✅ تحویل شد (تایید‌نشده)", "pending": "⏳ منتظر تایید",
          "confirmed": "✔️ تایید نهایی", "rejected": "🗑 رد و حذف شد"}.get(o["status"], o["status"])
    return (f"🧾 سفارش #{o['id']}\n👤 {esc(u['name'] if u else '')} (<code>{o['user_id']}</code>)\n"
            f"📦 {esc(o['name'])}\n💰 {fmt(o['amount'])} تومان\nوضعیت: {st}" + (f"\n{note}" if note else ""))


def order_kb(o):
    if o["status"] == "delivered":
        return K([("✅ تایید نهایی", f"ap:{o['id']}"), ("❌ رد و حذف همه‌چیز", f"rj:{o['id']}")])
    if o["status"] == "pending":
        return K([("✅ تایید و ارسال", f"ap:{o['id']}"), ("❌ رد", f"rj:{o['id']}")])
    return None


async def send_order_to(chat, o, note=""):
    cap, kb = order_caption(o, note), order_kb(o)
    try:
        if o["receipt"]:
            await BOT.send_photo(chat, o["receipt"], caption=cap, reply_markup=kb)
        else:
            await BOT.send_message(chat, cap, reply_markup=kb)
    except Exception:
        logging.exception("notify failed")


# ───────────────────────── کاربران ─────────────────────────
@r.message(CommandStart())
async def start(m: Message):
    ensure_user(m.from_user)
    waiting.pop(m.from_user.id, None)
    await m.answer(welcome_text(m.from_user), reply_markup=home_kb(m.from_user.id))


@r.message(Command("cancel"))
async def cancel_cmd(m: Message):
    waiting.pop(m.from_user.id, None)
    await m.answer("لغو شد.", reply_markup=home_kb(m.from_user.id))


@r.message(Command("admin"))
async def admin_cmd(m: Message):
    if is_admin(m.from_user.id):
        await m.answer(*admin_home())


@r.callback_query(F.data.in_({"home", "cancel"}))
async def home(cb: CallbackQuery):
    await cb.answer()
    ensure_user(cb.from_user)
    waiting.pop(cb.from_user.id, None)
    await show(cb, welcome_text(cb.from_user), home_kb(cb.from_user.id))


@r.callback_query(F.data == "sup")
async def support(cb: CallbackQuery):
    await cb.answer()
    await show(cb, T("support", support=esc(S("support_id") or "—")), K([("🔙 بازگشت", "home")]))


@r.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    await cb.answer()
    u = ensure_user(cb.from_user)
    prods = q("select * from products where active=1 order by price")
    if not prods:
        return await show(cb, "فعلاً محصولی موجود نیست.", K([("🔙 بازگشت", "home")]))
    rows = [[(f"{p['name']} — {fmt(price_for(p, u))} تومان", f"pay:p:{p['id']}")] for p in prods]
    rows.append([("🔙 بازگشت", "home")])
    await show(cb, "🛒 یکی از پلن‌ها را انتخاب کنید:", K(*rows))


@r.callback_query(F.data == "res")
async def reseller(cb: CallbackQuery):
    u = ensure_user(cb.from_user)
    if u["is_reseller"] or S("reseller_enabled") != "1":
        return await cb.answer("نمایندگی شما فعال است." if u["is_reseller"] else "در دسترس نیست.", show_alert=True)
    await cb.answer()
    price = num(S("reseller_price"))
    await show(cb, f"🤝 <b>خرید نمایندگی</b>\nبا نمایندگی، همه پلن‌ها را با قیمت ویژه می‌خرید.\n\n💰 قیمت: {fmt(price)} تومان",
               K([("💳 ادامه و پرداخت", "pay:r:0")], [("🔙 بازگشت", "home")]))


@r.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery):
    _, kind, pid = cb.data.split(":")
    u = ensure_user(cb.from_user)
    if not S("card_number"):
        return await cb.answer("درگاه پرداخت هنوز تنظیم نشده.", show_alert=True)
    await cb.answer()
    if kind == "p":
        p = q1("select * from products where id=? and active=1", int(pid))
        if not p:
            return await show(cb, "این پلن دیگر موجود نیست.", K([("🔙 بازگشت", "buy")]))
        head, amount, back = f"📦 <b>{esc(p['name'])}</b>\n{gb_txt(p['gb'])} | {p['days']} روز\n\n", price_for(p, u), "buy"
    else:
        head, amount, back = "🤝 <b>نمایندگی</b>\n\n", num(S("reseller_price")), "res"
    text = head + T("pay", price=fmt(amount), card=f"<code>{esc(S('card_number'))}</code>", holder=esc(S("card_holder")))
    await show(cb, text, K([("✅ پرداخت کردم", f"paid:{kind}:{pid}")], [("🔙 بازگشت", back)]))


@r.callback_query(F.data.startswith("paid:"))
async def paid(cb: CallbackQuery):
    _, kind, pid = cb.data.split(":")
    u = ensure_user(cb.from_user)
    if kind == "p":
        p = q1("select * from products where id=? and active=1", int(pid))
        if not p:
            return await cb.answer("این پلن دیگر موجود نیست.", show_alert=True)
        name, gb, days, amount = p["name"], p["gb"], p["days"], price_for(p, u)
    else:
        if u["is_reseller"]:
            return await cb.answer("نمایندگی شما فعال است.", show_alert=True)
        name, gb, days, amount = "نمایندگی", 0, 0, num(S("reseller_price"))
    await cb.answer()
    oid = x("insert into orders(user_id,kind,name,gb,days,amount,status,created) values(?,?,?,?,?,?,?,?)",
            u["id"], kind, name, gb, days, amount, "waiting", int(time.time()))
    waiting[u["id"]] = ("receipt", oid)
    await show(cb, T("ask_receipt"), K([("❌ انصراف", "cancel")]))


async def finalize(m: Message, oid, file_id):
    o = q1("select * from orders where id=?", oid)
    if not o or o["status"] != "waiting":
        return
    x("update orders set receipt=? where id=?", file_id, oid)
    back = K([("🏠 منوی اصلی", "home")])
    note = ""
    if S("instant") == "1":
        try:
            text = await deliver(o)
            x("update orders set status='delivered' where id=?", oid)
            await m.answer(text, reply_markup=back)
        except PanelError as e:
            x("update orders set status='pending' where id=?", oid)
            await m.answer(T("pending"), reply_markup=back)
            note = f"⚠️ ساخت خودکار ناموفق بود: {esc(str(e))}"
    else:
        x("update orders set status='pending' where id=?", oid)
        await m.answer(T("pending"), reply_markup=back)
    o = q1("select * from orders where id=?", oid)
    for a in ADMIN_IDS:
        await send_order_to(a, o, note)


@r.callback_query(F.data == "test")
async def test(cb: CallbackQuery):
    u = ensure_user(cb.from_user)
    if S("test_enabled") != "1":
        return await cb.answer("کانفیگ تست فعال نیست.", show_alert=True)
    if u["test_used"]:
        return await cb.answer("شما قبلاً کانفیگ تست گرفته‌اید.", show_alert=True)
    await cb.answer("⏳ در حال ساخت...")
    x("update users set test_used=1 where id=?", u["id"])  # جلوگیری از کلیک دوباره
    try:
        _, link = await provision(u["id"], num(S("test_gb")), hours=num(S("test_hours")), label="تست")
    except PanelError as e:
        x("update users set test_used=0 where id=?", u["id"])
        await alert_admins(f"⚠️ ساخت کانفیگ تست برای {u['id']} ناموفق بود:\n{e}")
        return await show(cb, "❌ فعلاً امکان ساخت نیست، کمی بعد دوباره تلاش کنید.", K([("🔙 بازگشت", "home")]))
    await show(cb, f"🎁 کانفیگ تست شما ({S('test_gb')} گیگ / {S('test_hours')} ساعت):\n\n<code>{esc(link)}</code>",
               K([("🏠 منوی اصلی", "home")]))


@r.callback_query(F.data == "svc")
async def svc(cb: CallbackQuery):
    await cb.answer()
    rows = [[(f"{s['label']} · {s['panel_username']}", f"sv:{s['id']}")]
            for s in q("select * from services where user_id=? order by id desc limit 30", cb.from_user.id)]
    if not rows:
        return await show(cb, "هنوز سرویسی ندارید.", K([("🔙 بازگشت", "home")]))
    rows.append([("🔙 بازگشت", "home")])
    await show(cb, "📂 سرویس‌های شما:", K(*rows))


@r.callback_query(F.data.startswith("sv:"))
async def sv(cb: CallbackQuery):
    s = q1("select * from services where id=? and user_id=?", int(cb.data[3:]), cb.from_user.id)
    if not s:
        return await cb.answer("پیدا نشد.", show_alert=True)
    await cb.answer()
    info = ""
    try:
        d = await panel.get_user(s["panel_username"])
        used = (d.get("used_traffic") or 0) / 1024 ** 3
        lim = (d.get("data_limit") or 0) / 1024 ** 3
        info = (f"\n📊 مصرف: {used:.2f} / {'∞' if not lim else f'{lim:.2f}'} گیگ"
                f"\n⏳ انقضا: {str(d.get('expire') or '∞')[:10]}\n🔘 وضعیت: {esc(str(d.get('status', '')))}")
    except PanelError:
        pass
    await show(cb, f"📦 <b>{esc(s['label'])}</b>{info}\n\n🔗 <code>{esc(s['link'])}</code>",
               K([("🔙 بازگشت", "svc")]))


# ───────────────────────── پنل مدیریت ─────────────────────────
def admin_home():
    pend = q1("select count(*) c from orders where status in ('delivered','pending')")["c"]
    users = q1("select count(*) c from users")["c"]
    return (f"⚙️ <b>تنظیمات</b>\n\n👥 کاربران: {users}\n🧾 سفارش تایید‌نشده: {pend}",
            K([("🔌 پنل", "a:panel"), ("📦 محصولات", "a:prod")],
              [("🎁 تست", "a:test"), ("🤝 نمایندگی", "a:res")],
              [("💳 پرداخت", "a:pay"), ("✏️ متن‌ها", "a:txt")],
              [("🧾 سفارش‌های تایید‌نشده", "a:pending")],
              [("🏠 منوی اصلی", "home")]))


def onoff(k):
    return "🟢 روشن" if S(k) == "1" else "🔴 خاموش"


def section(name):
    back = [("🔙 بازگشت", "adm")]
    if name == "panel":
        return (f"🔌 <b>اتصال به پنل</b>\n\n🌐 آدرس: <code>{esc(S('panel_url') or '—')}</code>\n"
                f"👤 یوزرنیم: <code>{esc(S('panel_user') or '—')}</code>\n🔑 رمز: {'••••••' if S('panel_pass') else '—'}\n"
                f"👥 گروه‌ها: {esc(S('panel_groups') or 'خودکار (همه)')}\n🏷 پیشوند: {esc(S('prefix'))}",
                K([("🌐 آدرس", "set:panel_url"), ("👤 یوزرنیم", "set:panel_user")],
                  [("🔑 رمز", "set:panel_pass"), ("👥 گروه‌ها", "set:panel_groups")],
                  [("🏷 پیشوند", "set:prefix"), ("🔎 تست اتصال", "a:ptest")], back))
    if name == "pay":
        return (f"💳 <b>پرداخت</b>\n\nکارت: <code>{esc(S('card_number') or '—')}</code>\nبه نام: {esc(S('card_holder') or '—')}\n\n"
                f"⚡ تحویل فوری (بدون بررسی رسید): {onoff('instant')}\n"
                "در حالت روشن، پس از ارسال رسید سرویس بلافاصله ساخته می‌شود و بعداً می‌توانید آن را تایید یا رد (و حذف) کنید.",
                K([("💳 شماره کارت", "set:card_number"), ("👤 صاحب کارت", "set:card_holder")],
                  [(f"⚡ تحویل فوری: {onoff('instant')}", "tg:instant")], back))
    if name == "test":
        return (f"🎁 <b>کانفیگ تست</b>\n\nوضعیت: {onoff('test_enabled')}\nحجم: {S('test_gb')} گیگ\nمدت: {S('test_hours')} ساعت",
                K([(f"وضعیت: {onoff('test_enabled')}", "tg:test_enabled")],
                  [("📦 حجم", "set:test_gb"), ("⏱ مدت", "set:test_hours")], back))
    if name == "res":
        return (f"🤝 <b>نمایندگی</b>\n\nوضعیت: {onoff('reseller_enabled')}\nقیمت خرید نمایندگی: {fmt(num(S('reseller_price')))} تومان\n\n"
                "قیمت نمایندگی هر پلن را در بخش محصولات تعیین کنید.",
                K([(f"وضعیت: {onoff('reseller_enabled')}", "tg:reseller_enabled")],
                  [("💰 قیمت نمایندگی", "set:reseller_price")], back))
    if name == "prod":
        rows = [[(f"{'🟢' if p['active'] else '🔴'} {p['name']} — {fmt(p['price'])}", f"pr:{p['id']}")]
                for p in q("select * from products order by id")]
        rows += [[("➕ افزودن محصول", "padd")], back]
        return "📦 <b>محصولات</b>\nبرای ویرایش یا حذف روی محصول بزنید.", K(*rows)
    if name == "txt":
        rows = [[(lbl, f"txt:{k}")] for k, (lbl, _) in TEXTS.items()]
        rows.append([("👤 آیدی پشتیبانی", "set:support_id")])
        return "✏️ <b>متن‌ها</b>\nکدام متن را تغییر می‌دهید؟", K(*rows, back)
    if name == "pending":
        os_ = q("select * from orders where status in ('delivered','pending') order by id desc limit 20")
        rows = [[(f"#{o['id']} · {o['name']} · {fmt(o['amount'])}", f"o:{o['id']}")] for o in os_]
        return ("🧾 <b>سفارش‌های تایید‌نشده</b>" + ("" if rows else "\n\nموردی نیست ✨")), K(*rows, back)


def admin_only(fn):
    async def w(cb: CallbackQuery):
        if not is_admin(cb.from_user.id):
            return await cb.answer("دسترسی ندارید.", show_alert=True)
        return await fn(cb)
    return w


@r.callback_query(F.data == "adm")
@admin_only
async def adm(cb: CallbackQuery):
    await cb.answer()
    waiting.pop(cb.from_user.id, None)
    await show(cb, *admin_home())


@r.callback_query(F.data.regexp(r"^a:(panel|pay|test|res|prod|txt|pending)$"))
@admin_only
async def adm_section(cb: CallbackQuery):
    await cb.answer()
    waiting.pop(cb.from_user.id, None)
    await show(cb, *section(cb.data[2:]))


@r.callback_query(F.data == "a:ptest")
@admin_only
async def ptest(cb: CallbackQuery):
    panel.token = None
    try:
        await panel.login()
        gids = await panel.group_ids()
        await cb.answer(f"✅ اتصال موفق. گروه‌ها: {gids}", show_alert=True)
    except Exception as e:
        await cb.answer(f"❌ {str(e)[:150]}", show_alert=True)


@r.callback_query(F.data.startswith("tg:"))
@admin_only
async def toggle(cb: CallbackQuery):
    k = cb.data[3:]
    if k in TOGGLES:
        setS(k, "0" if S(k) == "1" else "1")
        await cb.answer()
        await show(cb, *section(TOGGLES[k]))


@r.callback_query(F.data.startswith("set:"))
@admin_only
async def set_prompt(cb: CallbackQuery):
    k = cb.data[4:]
    if k not in FIELDS:
        return await cb.answer()
    await cb.answer()
    waiting[cb.from_user.id] = ("set", k)
    await show(cb, f"✏️ {FIELDS[k][0]}\n\nمقدار جدید را بفرستید:", K([("❌ انصراف", f"a:{FIELDS[k][1]}")]))


@r.callback_query(F.data.startswith("txt:"))
@admin_only
async def txt_prompt(cb: CallbackQuery):
    k = cb.data[4:]
    if k not in TEXTS:
        return await cb.answer()
    await cb.answer()
    waiting[cb.from_user.id] = ("txt", k)
    hint = "\n\nمتغیرها: {name} {price} {card} {holder} {support}" if k in ("welcome", "pay", "support") else ""
    await show(cb, f"✏️ <b>{TEXTS[k][0]}</b>\n\nمتن فعلی:\n<pre>{esc(S('t_' + k))}</pre>{hint}\n\n"
                   "متن جدید را بفرستید (برای بازگشت به پیش‌فرض، - بفرستید):", K([("❌ انصراف", "a:txt")]))


@r.callback_query(F.data == "padd")
@admin_only
async def padd(cb: CallbackQuery):
    await cb.answer()
    waiting[cb.from_user.id] = ("padd", None)
    await show(cb, "➕ محصول جدید را در یک خط بفرستید:\n\n<code>نام | حجم(GB) | روز | قیمت | قیمت نمایندگی</code>\n\n"
                   "مثال:\n<code>۳۰ گیگ ماهانه | 30 | 30 | 150000 | 120000</code>\n"
                   "حجم 0 = نامحدود. قیمت نمایندگی اختیاری است.", K([("❌ انصراف", "a:prod")]))


@r.callback_query(F.data.regexp(r"^(pr|ptg|pdel):\d+$"))
@admin_only
async def product_actions(cb: CallbackQuery):
    act, pid = cb.data.split(":")
    pid = int(pid)
    if act == "ptg":
        x("update products set active=1-active where id=?", pid)
    if act == "pdel":
        x("delete from products where id=?", pid)
        await cb.answer("حذف شد.")
        return await show(cb, *section("prod"))
    p = q1("select * from products where id=?", pid)
    if not p:
        return await cb.answer("پیدا نشد.", show_alert=True)
    await cb.answer()
    await show(cb, f"📦 <b>{esc(p['name'])}</b>\nحجم: {gb_txt(p['gb'])}\nمدت: {p['days']} روز\n"
                   f"قیمت: {fmt(p['price'])}\nقیمت نمایندگی: {fmt(p['reseller_price']) if p['reseller_price'] else '—'}\n"
                   f"وضعیت: {'🟢 فعال' if p['active'] else '🔴 غیرفعال'}",
               K([(("🔴 غیرفعال کن" if p["active"] else "🟢 فعال کن"), f"ptg:{pid}"), ("🗑 حذف", f"pdel:{pid}")],
                 [("🔙 بازگشت", "a:prod")]))


@r.callback_query(F.data.startswith("o:"))
@admin_only
async def order_view(cb: CallbackQuery):
    o = q1("select * from orders where id=?", int(cb.data[2:]))
    await cb.answer()
    if o:
        await send_order_to(cb.from_user.id, o)


async def edit_order_msg(cb, o, note=""):
    o = q1("select * from orders where id=?", o["id"])
    await show(cb, order_caption(o, note), order_kb(o))


@r.callback_query(F.data.startswith("ap:"))
@admin_only
async def approve(cb: CallbackQuery):
    o = q1("select * from orders where id=?", int(cb.data[3:]))
    if not o or o["status"] not in ("delivered", "pending"):
        return await cb.answer("این سفارش قبلاً بررسی شده.", show_alert=True)
    if o["status"] == "pending":
        try:
            text = await deliver(o)
        except PanelError as e:
            return await cb.answer(f"❌ خطای پنل: {str(e)[:150]}", show_alert=True)
        await BOT.send_message(o["user_id"], text, reply_markup=K([("🏠 منوی اصلی", "home")]))
    x("update orders set status='confirmed' where id=?", o["id"])
    await cb.answer("تایید شد ✅")
    await edit_order_msg(cb, o)


@r.callback_query(F.data.startswith("rj:"))
@admin_only
async def reject(cb: CallbackQuery):
    o = q1("select * from orders where id=?", int(cb.data[3:]))
    if not o or o["status"] not in ("delivered", "pending"):
        return await cb.answer("این سفارش قبلاً بررسی شده.", show_alert=True)
    done, fails = await revoke(o)
    x("update orders set status='rejected' where id=?", o["id"])
    await cb.answer("رد شد.")
    note = f"🗑 سرویس حذف‌شده: {done}" + (f"\n⚠️ حذف نشد: {esc(' | '.join(fails))}" if fails else "")
    await edit_order_msg(cb, o, note)
    try:
        await BOT.send_message(o["user_id"], T("rejected"))
    except Exception:
        pass


# ───────────────────────── دریافت پیام‌های متنی/عکس ─────────────────────────
@r.message()
async def on_message(m: Message):
    uid = m.from_user.id
    st = waiting.get(uid)
    if not st:
        return
    action, extra = st

    if action == "receipt":
        if not m.photo:
            return await m.answer("لطفاً فقط تصویر رسید را بفرستید 📸")
        waiting.pop(uid, None)
        return await finalize(m, extra, m.photo[-1].file_id)

    if not is_admin(uid) or not m.text:
        return
    val = m.text.strip()

    if action == "txt":
        if val == "-":
            x("delete from settings where k=?", "t_" + extra)
        else:
            setS("t_" + extra, val)
        waiting.pop(uid, None)
        return await m.answer("✅ ذخیره شد.", reply_markup=section("txt")[1])

    if action == "padd":
        try:
            parts = [p.strip() for p in val.split("|")]
            name, gb, days, price = parts[0], num(parts[1]), int(num(parts[2])), int(num(parts[3]))
            rp = int(num(parts[4])) if len(parts) > 4 and parts[4] else 0
            assert name and days > 0 and gb >= 0 and price >= 0
        except Exception:
            return await m.answer("❌ فرمت درست نیست. نمونه:\n<code>۳۰ گیگ | 30 | 30 | 150000 | 120000</code>")
        x("insert into products(name,gb,days,price,reseller_price) values(?,?,?,?,?)", name, gb, days, price, rp)
        waiting.pop(uid, None)
        return await m.answer("✅ محصول اضافه شد.", reply_markup=section("prod")[1])

    if action == "set":
        label, sec, typ = FIELDS[extra]
        if typ == "num":
            try:
                val = str(num(val))
                assert float(val) >= 0
            except Exception:
                return await m.answer("❌ فقط عدد بفرستید.")
        elif typ == "opt":
            val = "" if val == "-" else val.replace(" ", "")
            if val and not re.fullmatch(r"\d+(,\d+)*", val):
                return await m.answer("❌ مثل 1,2 بفرستید یا - برای خودکار.")
        elif typ == "prefix":
            val = val.lower()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", val):
                return await m.answer("❌ فقط حروف انگلیسی کوچک/عدد/_ و حداکثر ۱۶ کاراکتر.")
        elif extra == "panel_url" and not val.startswith(("http://", "https://")):
            return await m.answer("❌ آدرس باید با http:// یا https:// شروع شود.")
        setS(extra, val)
        if extra.startswith("panel_"):
            panel.token = None
        waiting.pop(uid, None)
        if extra == "panel_pass":  # رمز در چت نماند
            try:
                await m.delete()
            except Exception:
                pass
        await m.answer("✅ ذخیره شد.")
        return await m.answer(*section(sec))


# ───────────────────────── اجرا ─────────────────────────
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN or not ADMIN_IDS:
        raise SystemExit("BOT_TOKEN و ADMIN_IDS را در فایل .env تنظیم کنید.")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    global BOT
    BOT = bot
    dp = Dispatcher()
    dp.include_router(r)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
