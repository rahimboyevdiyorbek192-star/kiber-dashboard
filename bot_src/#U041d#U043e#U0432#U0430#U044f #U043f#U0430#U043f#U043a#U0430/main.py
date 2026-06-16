import logging
import re
import time

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot_errors.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Telethon ichki ogohlantirishlarini o'chirish (eski xabar ID, MTProto spam)
logging.getLogger('telethon').setLevel(logging.ERROR)
logging.getLogger('telethon.network').setLevel(logging.ERROR)
logging.getLogger('telethon.crypto').setLevel(logging.ERROR)

# main.py
import os
import sys
import json
import random
import asyncio
from datetime import datetime
import openpyxl
import aiosqlite
from telethon import TelegramClient, events, Button
from telethon.tl.functions.users import GetFullUserRequest
from telethon.errors import FloodWaitError, RpcCallFailError
try:
    import socks as _socks
    _SOCKS_AVAILABLE = True
except ImportError:
    _socks = None
    _SOCKS_AVAILABLE = False
from dotenv import load_dotenv
import database as db_mod
import tg_scrapers as engine
import music_scanner as music
import phishing_checker as phish_mod

load_dotenv()

def _pm(event, group: int = 1) -> str:
    """pattern_match.group(N) yoki tv_input_handler fake event uchun message.text fallback."""
    pm = getattr(event, 'pattern_match', None)
    if pm is not None:
        v = pm.group(group)
        return v.strip() if isinstance(v, str) else (v or '')
    # Fake event: /cmd [subverb] arg1 arg2 ...
    txt = (getattr(getattr(event, 'message', None), 'text', '') or '').strip()
    parts = txt.split()
    # Command prefix: skip '/' words and lowercase subcommands (add, new, del, report...)
    skip = 0
    for p in parts:
        if p.startswith('/') or (p.isalpha() and p.islower() and len(p) <= 12):
            skip += 1
        else:
            break
    args = parts[skip:]
    if not args:
        return ''
    if group == 1:
        return args[0]
    # group 2+: join remaining so multiword values work
    return ' '.join(args[group - 1:]) if len(args) >= group else ''

API_ID         = int(os.getenv("API_ID"))
API_HASH       = os.getenv("API_HASH").strip()
BOT_TOKEN      = os.getenv("BOT_TOKEN").strip()

# ── 1-TUZATMA: SUPER_ADMIN_ID va ADMIN_IDS .env dan o'qiladi ────────
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))

# ADMIN_IDS=123456,789012,345678 ko'rinishida yoziladi
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
EXTRA_ADMIN_IDS = set()
for _x in _admin_ids_raw.split(","):
    _x = _x.strip()
    if _x.isdigit():
        EXTRA_ADMIN_IDS.add(int(_x))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Doimiy Excel fayl — har safar yangi yaratmaydi, ustiga qo'shib boradi
EXCEL_PERSIST_PATH = os.path.join(BASE_DIR, "OSINT_Monitoring.xlsx")
EXCEL_IDS_PATH     = os.path.join(BASE_DIR, "excel_exported_ids.json")

def _load_excel_ids():
    if os.path.exists(EXCEL_IDS_PATH):
        with open(EXCEL_IDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("tc", [])), set(data.get("all", []))
    return set(), set()

def _save_excel_ids(tc_ids: set, all_ids: set):
    with open(EXCEL_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump({"tc": list(tc_ids), "all": list(all_ids)}, f)

_UB1_PROXY_URL = os.getenv("USERBOT1_PROXY", "").strip()

# ── N-USERBOT TIZIMI ──────────────────────────────────────────────────
# .env da yoki session fayl mavjud bo'lsa avtomatik qo'shiladi.
# Yangi userbot qo'shish uchun:
#   .env ga: USERBOT2_PHONE=+998..., USERBOT3_PHONE=+998... (ixtiyoriy)
#   yoki userbot2_session.session, userbot3_session.session fayllarini qo'ying
# Kod o'zgarmaydi — faqat .env yoki session fayl qo'shiladi.
# ─────────────────────────────────────────────────────────────────────

def _parse_proxy(proxy_url: str):
    """
    Proxy URL ni Telethon formatiga o'giradi.
    Qo'llab-quvvatlanadigan formatlar:
      socks5://user:pass@ip:port
      socks4://ip:port
      http://user:pass@ip:port
    Qaytaradi: (socks.SOCKS5, host, port, True, user, pass) yoki None.
    """
    if not proxy_url or not proxy_url.strip():
        return None
    if not _SOCKS_AVAILABLE:
        print("[OGOHLANTIRISH] PySocks o'rnatilmagan — proxy ishlamaydi! pip install PySocks")
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url.strip())
        scheme = (parsed.scheme or "").lower()
        host   = parsed.hostname
        port   = parsed.port or (1080 if "socks" in scheme else 8080)
        user   = parsed.username or None
        pwd    = parsed.password or None
        if scheme in ("socks5", "socks5h"):
            proxy_type = _socks.SOCKS5
        elif scheme in ("socks4", "socks4a"):
            proxy_type = _socks.SOCKS4
        elif scheme in ("http", "https"):
            proxy_type = _socks.HTTP
        else:
            print(f"[OGOHLANTIRISH] Noma'lum proxy schema: {scheme} — proxy o'tkazib yuboriladi")
            return None
        # Telethon proxy tuple: (type, addr, port, rdns, username, password)
        return (proxy_type, host, port, True, user, pwd)
    except Exception as e:
        print(f"[OGOHLANTIRISH] Proxy URL parse xatosi ({proxy_url!r}): {e}")
        return None


# UB1 (asosiy userbot) va bot uchun TelegramClient yaratish
_ub1_proxy = _parse_proxy(_UB1_PROXY_URL)
_ub1_kwargs = {"proxy": _ub1_proxy} if _ub1_proxy else {}
userbot = TelegramClient(os.path.join(BASE_DIR, 'userbot_session'), API_ID, API_HASH, **_ub1_kwargs)
bot     = TelegramClient(os.path.join(BASE_DIR, 'bot_session'),     API_ID, API_HASH)


def _build_extra_userbots():
    """
    USERBOT2..USERBOT9 ni .env va session fayllaridan avtomatik topadi.
    Har biri uchun alohida API_ID/API_HASH bo'lishi mumkin (ixtiyoriy).
    """
    extras = []
    i = 2
    while True:
        phone_key    = f"USERBOT{i}_PHONE"
        api_id_key   = f"USERBOT{i}_API_ID"
        api_hash_key = f"USERBOT{i}_API_HASH"
        proxy_key    = f"USERBOT{i}_PROXY"
        session_name = f"userbot{i}_session"
        session_path = os.path.join(BASE_DIR, session_name)
        phone       = os.getenv(phone_key, "").strip()
        ub_api_id   = os.getenv(api_id_key, "").strip()
        ub_api_hash = os.getenv(api_hash_key, "").strip()
        ub_proxy_url = os.getenv(proxy_key, "").strip()
        exists      = os.path.exists(session_path + '.session')

        if not phone and not exists:
            break  # birinchi bo'sh USERBOT{i} da to'xtaydi — cheksiz

        # API_ID/HASH bo'sh bo'lsa — ogohlantirish, o'sha userbotni o'tkazib yuborish
        if not ub_api_id or not ub_api_hash:
            print(f"[OGOHLANTIRISH] USERBOT{i}: {api_id_key} va {api_hash_key} bo'sh!")
            print(f"[OGOHLANTIRISH] my.telegram.org dan alohida API oling — Telegram ban qilishi mumkin!")
            print(f"[OGOHLANTIRISH] USERBOT{i} o'tkazib yuborilmoqda...")
            i += 1
            continue

        ub_proxy = _parse_proxy(ub_proxy_url)
        ub_kwargs = {"proxy": ub_proxy} if ub_proxy else {}
        ub = TelegramClient(session_path, int(ub_api_id), ub_api_hash, **ub_kwargs)
        ub._ub_phone  = phone
        ub._ub_exists = exists
        ub._ub_idx    = i - 1
        extras.append(ub)
        i += 1
    return extras

_EXTRA_USERBOTS = _build_extra_userbots()
# Orqaga moslik uchun (eski kod ishlatadigan joylar)
userbot2 = _EXTRA_USERBOTS[0] if _EXTRA_USERBOTS else None

MAIN_KEYBOARD = [
    [Button.text("🔍 Skanerlash")],
    [Button.text("🔎 Kalit So'z Qidiruv")],
    [Button.text("📊 Kuzatuv Holati (Status)"),  Button.text("📥 Monitoringdan Fayl Olish")],
    [Button.text("📁 Arxiv / Savatcha"),         Button.text("🔒 Maxfiy Kanal Qo'shish")],
    [Button.text("👥 Adminlar Ro'yxati"),     Button.text("📋 Manbalar Ro'yxati")],
    [Button.text("🎼 Ko'p Musiqa Qidirish"),  Button.text("🔔 Kuzatiladigan Musiqalar")],
    [Button.text("🛡 Havola Tekshirish"),      Button.text("💬 Kamentariya Xisoboti")],
    [Button.text("🔄 Botni Qayta Yuklash")]
]

USER_STATES = {}

BUTTON_TEXTS = {
    "🔍 Skanerlash",
    "🔎 Kalit So'z Qidiruv",
    "📊 Kuzatuv Holati (Status)", "📥 Monitoringdan Fayl Olish",
    "📁 Arxiv / Savatcha", "🔒 Maxfiy Kanal Qo'shish",
    "👥 Adminlar Ro'yxati", "📋 Manbalar Ro'yxati",
    "🎼 Ko'p Musiqa Qidirish", "🔔 Kuzatiladigan Musiqalar",
    "🛡 Havola Tekshirish",
    "💬 Kamentariya Xisoboti",
    "🔄 Botni Qayta Yuklash",
}

# ─────────────────────────────────────────────────────────────────────
# ADMIN TEKSHIRUVI
# Ustuvorlik: SUPER_ADMIN_ID > EXTRA_ADMIN_IDS (.env) > DB adminlar
# ─────────────────────────────────────────────────────────────────────

async def is_admin(uid):
    if uid == SUPER_ADMIN_ID:
        return True
    if uid in EXTRA_ADMIN_IDS:
        return True
    return await db_mod.is_admin(uid, SUPER_ADMIN_ID)

# ─────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if not await is_admin(event.sender_id):
        await event.respond("🔒 **Tizimga kirish taqiqlangan!**")
        return
    USER_STATES[event.sender_id] = None
    badge = "👑 Super Admin" if event.sender_id == SUPER_ADMIN_ID else "👤 Admin"
    await event.respond(
        f"🛰 **Kiber-Stansiya OSINT Pro ishga tushdi**\n"
        f"Sizning darajangiz: {badge}",
        buttons=MAIN_KEYBOARD
    )

# ─────────────────────────────────────────────────────────────────────
# ADMIN BOSHQARUVI (faqat Super Admin)
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/add_admin (\d+)'))
async def add_admin_handler(event):
    if event.sender_id != SUPER_ADMIN_ID:
        return
    new_id = int(_pm(event, 1))
    await db_mod.add_admin(new_id)
    await event.respond(f"✅ `{new_id}` adminlar ro'yxatiga qo'shildi.")

@bot.on(events.NewMessage(pattern=r'/del_admin (\d+)'))
async def del_admin_handler(event):
    if event.sender_id != SUPER_ADMIN_ID:
        return
    del_id = int(_pm(event, 1))
    await db_mod.remove_admin(del_id)
    await event.respond(f"❌ `{del_id}` o'chirildi.")

@bot.on(events.NewMessage(pattern=r'/yuklash(?:\s+(\d+))?'))
async def music_parallel_handler(event):
    """Har userbot bir vaqtda nechta musiqa parallel yuklashini sozlaydi."""
    if not await is_admin(event.sender_id):
        return
    arg = _pm(event, 1)
    if not arg:
        await event.respond(
            f"🎵 **Musiqa parallel yuklash**\n\n"
            f"Hozir: har userbot bir vaqtda `{engine.MUSIC_PARALLEL}` ta musiqa yuklaydi.\n\n"
            f"O'zgartirish: `/yuklash 5` (1–10 oralig'ida)\n"
            f"⚠️ Ko'p qilsangiz tezroq, lekin Telegram flood berishi mumkin."
        )
        return
    n = engine.set_music_parallel(int(arg))
    await event.respond(
        f"✅ Endi har userbot bir vaqtda `{n}` ta musiqa parallel yuklaydi.\n"
        f"📌 Bu qiymat saqlandi — bot qayta yonsa ham `{n}` qoladi.\n"
        f"ℹ️ Yangi kanallardan boshlab kuchga kiradi."
    )

# ─────────────────────────────────────────────────────────────────────
# TUGMA: SKANERLASH (guruh / kanal / yopiq — avtomatik aniqlanadi)
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="🔍 Skanerlash"))
async def btn_scan(event):
    if not await is_admin(event.sender_id):
        return
    USER_STATES[event.sender_id] = 'waiting_auto_scan_link'
    await event.respond(
        "🔍 **Skanerlash linkini kiriting:**\n"
        "• Ochiq guruh: `@username` yoki `t.me/username`\n"
        "• Yopiq guruh: `t.me/+inviteHash`\n"
        "• Kanal: `@channel` yoki `t.me/channel`"
    )


# ─────────────────────────────────────────────────────────────────────
# TUGMA: KALIT SO'Z QIDIRUV
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="🔎 Kalit So'z Qidiruv"))
async def btn_keyword_search(event):
    if not await is_admin(event.sender_id):
        return
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT group_link) FROM users_memory_bank "
            "WHERE group_link IS NOT NULL AND group_link != ''"
        ) as cur:
            source_count = (await cur.fetchone())[0]
    USER_STATES[event.sender_id] = 'waiting_keyword_word'
    await event.respond(
        f"🔎 **Kalit so'zni kiriting:**\n"
        f"_Bazada `{source_count}` ta manba mavjud — hammasidan qidiriladi_\n\n"
        "Bir nechta so'z bo'lsa vergul bilan ajrating:\n"
        "`sotaman, telefon, uy`"
    )

# ─────────────────────────────────────────────────────────────────────
# TUGMA: KUZATUV HOLATI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r"📊 Kuzatuv Holati \(Status\)"))
async def btn_status(event):
    if not await is_admin(event.sender_id):
        return

    # Asosiy statistika
    from datetime import datetime as _dt
    today_str = _dt.now().strftime("%Y-%m-%d")
    async with db_mod.connect(db_mod.DB_NAME, timeout=10) as db:
        total   = (await (await db.execute("SELECT COUNT(DISTINCT user_id) FROM users_memory_bank")).fetchone())[0]
        pending = (await (await db.execute("SELECT COUNT(*) FROM hidden_channel_knocker WHERE status='pending'")).fetchone())[0]
        joined  = (await (await db.execute("SELECT COUNT(*) FROM hidden_channel_knocker WHERE status='joined'")).fetchone())[0]
        running = (await (await db.execute("SELECT COUNT(*) FROM scan_resume WHERE status='running'")).fetchone())[0]
        # Har userbot uchun bugungi so'rovnomalar soni
        _knock_counts = []
        for _ki in range(len([userbot] + _EXTRA_USERBOTS)):
            _kc = (await (await db.execute(
                "SELECT COUNT(*) FROM hidden_channel_knocker WHERE userbot_idx=? AND last_request_time LIKE ?",
                (_ki, today_str + "%",)
            )).fetchone())[0]
            _knock_counts.append(_kc)
        knock_ub1 = _knock_counts[0] if _knock_counts else 0
        knock_ub2 = _knock_counts[1] if len(_knock_counts) > 1 else 0
        # Barcha kanallar soni (users_memory_bank + hidden_channel_knocker joined)
        all_sources = (await (await db.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT DISTINCT group_link as src FROM users_memory_bank WHERE group_link IS NOT NULL AND group_link != ''"
            "  UNION"
            "  SELECT channel_id as src FROM hidden_channel_knocker WHERE status='joined'"
            ")"
        )).fetchone())[0]

    # Musiqa statistika
    total_fp, total_ch = await music.get_stats()

    # Profillardan nechta musiqa skanerlandi
    async with db_mod.connect(music.MUSIC_DB, timeout=10) as mdb:
        async with mdb.execute(
            "SELECT COUNT(DISTINCT channel_id) FROM music_fingerprints "
            "WHERE file_name LIKE 'profile_%'"
        ) as _cur:
            profile_scanned = ((await _cur.fetchone()) or (0,))[0]

        async with mdb.execute(
            "SELECT COUNT(DISTINCT channel_id) FROM music_fingerprints "
            "WHERE file_name LIKE 'msg_%'"
        ) as _cur:
            channel_scanned = ((await _cur.fetchone()) or (0,))[0]

        channel_progress = 0

    # music_channel_progress asosiy DBda saqlanadi (music DBda emas)
    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _mdb2:
            await _mdb2.execute(
                "CREATE TABLE IF NOT EXISTS music_channel_progress "
                "(channel_id TEXT PRIMARY KEY, last_msg_id INTEGER)"
            )
            await _mdb2.commit()
            channel_progress = (await (await _mdb2.execute(
                "SELECT COUNT(*) FROM music_channel_progress"
            )).fetchone())[0]
    except Exception:
        channel_progress = 0

    # Qolgan profillar (musiqasi tekshirilmagan)
    profiles_left   = total - profile_scanned
    channels_left   = all_sources - channel_scanned

    # Navbat holati
    queue_info = ""
    if _SCAN_QUEUE and not _SCAN_QUEUE.empty():
        queue_info = f"\n📋 Navbatda: `{_SCAN_QUEUE.qsize()}` ta topshiriq"

    await event.respond(
        f"🛰 **KIBER-STANSIYA STATUSI:**\n\n"
        f"⚙️ **{engine.resource_status()}**\n"
        f"👥 Monitoringdagi profillar: `{total}` ta\n"
        f"📥 Kutilayotgan maxfiy kanallar: `{pending}` ta\n"
        f"🔓 Kirilgan maxfiy kanallar: `{joined}` ta\n"
        f"📨 Bugungi so'rovnomalar: " + " | ".join(
            f"UB{i+1}:`{c}`" for i, c in enumerate(_knock_counts)
        ) + " ta\n"
        f"⚡️ Skaner: **{'⏸ PAUZADA' if engine.SCANNER_PAUSED else '▶️ ISHLAMOQDA'}**"
        + queue_info +
        f"\n\n🎵 **MUSIQA SKANERLASH:**\n"
        f"👤 Profil musiqasi skanerlandi: `{profile_scanned}` ta | Qoldi: `{profiles_left}` ta\n"
        f"📢 Kanal musiqasi skanerlandi: `{channel_scanned}` ta | {'✅ Hammasi tayyor' if channels_left <= 0 else f'Qoldi: `{channels_left}` ta'}\n"
        f"🎶 Jami fingerprint: `{total_fp}` ta"
        + (f"\n♻️ Davom ettiriladigan: `{running}` ta" if running else "")
    )

# ─────────────────────────────────────────────────────────────────────
# TUGMA: MONITORING DAN FAYL OLISH
# ── 3-TUZATMA: has_hidden da invite link bo'lsa Maxfiy Kanal ustuniga
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="📥 Monitoringdan Fayl Olish"))
async def btn_fayl(event):
    if not await is_admin(event.sender_id):
        return
    status_msg = await event.respond("⏳ Bazadan yangi ma'lumotlar tekshirilmoqda...")

    # ── Avvalgi eksport ID larini yuklash ────────────────────────────
    tc_exported, all_exported = _load_excel_ids()
    file_exists = os.path.exists(EXCEL_PERSIST_PATH)

    # ── Excel faylni yuklash yoki yangi yaratish ─────────────────────
    _loop = asyncio.get_event_loop()
    if file_exists:
        wb = await _loop.run_in_executor(None, openpyxl.load_workbook, EXCEL_PERSIST_PATH)
        sh  = wb["Target Channels"] if "Target Channels" in wb.sheetnames else wb.active
        sh2 = wb["Лист1"] if "Лист1" in wb.sheetnames else wb.create_sheet("Лист1")
        tc_start_row  = sh.max_row  # keyingi satr davom etadi
        all_start_row = sh2.max_row
    else:
        wb  = openpyxl.Workbook()
        sh  = wb.active
        sh.title = "Target Channels"
        sh.append([
            "№", "Telegram ID", "Kanal ID", "Ism", "Username", "Telefon",
            "Bio Linklar", "Shaxsiy Kanal Linki", "Maxfiy Kanal Linki",
            "Bio", "Manba Guruh", "O'zgargan Sana"
        ])
        sh2 = wb.create_sheet(title="Лист1")
        sh2.append([
            "№", "Telegram ID", "Kanal ID", "Ism", "Familya", "Username",
            "Telefon", "Bio", "Ochiq Kanallar", "Maxfiy Kanal",
            "Manba Guruh/Kanal", "Qo'shilgan Sana", "Yangilangan Sana"
        ])
        tc_start_row  = 1
        all_start_row = 1

    async with db_mod.connect(db_mod.DB_NAME, timeout=15) as db:
        async with db.execute(
            "SELECT user_id, first_name, username, phone, open_channels, "
            "has_hidden, bio, group_link, MAX(last_updated) as last_updated "
            "FROM users_memory_bank "
            "WHERE (has_hidden IS NOT NULL AND has_hidden != '' AND has_hidden != '❌') "
            "   OR (open_channels IS NOT NULL AND open_channels != '' AND open_channels != 'Yo''q') "
            "GROUP BY user_id "
            "ORDER BY last_updated DESC"
        ) as cur:
            rows = await cur.fetchall()

    # Yangi foydalanuvchilar — allaqachon eksport qilinmaganlar
    seen_uids = set()
    new_rows = []
    for row in rows:
        uid = row[0]
        if uid not in seen_uids and uid not in tc_exported:
            seen_uids.add(uid)
            new_rows.append(row)
    rows = new_rows

    # Yangi ma'lumot yo'q — faylni qayta yuborish
    if not rows and file_exists:
        await status_msg.edit("✅ Yangi ma'lumot yo'q — fayl allaqachon yangilangan.")
        await bot.send_file(
            event.sender_id, EXCEL_PERSIST_PATH,
            caption=f"📊 **Monitoring fayli** (yangilanmadi)\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        return
    elif not rows and not file_exists:
        await status_msg.edit("📭 Hozircha kanal aniqlangan profil yo'q. Avval guruh skanerlang.")
        return

    await status_msg.edit(f"⏳ {len(rows)} ta yangi profil qo'shilmoqda...")

    # Barcha kanal ID larni olish (N+1 dan qochish)
    uid_list = [r[0] for r in rows]
    uid_to_kanal = {}

    # 1. hidden_channel_knocker — maxfiy kanallar (joined bo'lganda numeric_id saqlanadi)
    if uid_list:
        placeholders = ",".join("?" * len(uid_list))
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            async with db.execute(
                f"SELECT creator_id, channel_id, numeric_id FROM hidden_channel_knocker "
                f"WHERE creator_id IN ({placeholders})",
                uid_list
            ) as cur:
                for _creator_id, _channel_id, _numeric_id in await cur.fetchall():
                    if _creator_id not in uid_to_kanal:
                        if _numeric_id and str(_numeric_id).strip():
                            uid_to_kanal[_creator_id] = str(_numeric_id).strip()
                        else:
                            _raw = str(_channel_id).strip()
                            if _raw.lstrip('-').isdigit():
                                _cid = _raw.lstrip('-')
                                uid_to_kanal[_creator_id] = (
                                    f"-100{_cid}" if not _raw.startswith('-100') else _raw
                                )
                            else:
                                uid_to_kanal[_creator_id] = _raw

    # 2. resolved_channel_ids — shaxsiy/ochiq kanallar
    #    (skanerlash paytida resolve_personal_channel tomonidan saqlanadi)
    all_links = set()
    for _uid, _fn, _un, _ph, _oc, _hh, _bio, _gl, _lu in rows:
        if _oc and _oc not in ("", "Yo'q"):
            for lnk in _oc.split(','):
                lnk = lnk.strip()
                if lnk:
                    all_links.add(lnk)
        if _hh and _hh not in ("", "❌") and "Maxfiy" not in (_hh or ""):
            for lnk in _hh.split(','):
                lnk = lnk.strip()
                if lnk:
                    all_links.add(lnk)

    link_to_numid = {}
    if all_links:
        _links_list = list(all_links)
        _ph = ",".join("?" * len(_links_list))
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            async with db.execute(
                f"SELECT channel_link, numeric_id FROM resolved_channel_ids "
                f"WHERE channel_link IN ({_ph})",
                _links_list
            ) as cur:
                for _cl, _nid in await cur.fetchall():
                    if _nid:
                        link_to_numid[_cl] = _nid

    # uid → kanal_id: knocker dan topilmasa, open_channels/has_hidden linkini keshdan qidirish
    for _uid, _fn, _un, _ph, _oc, _hh, _bio, _gl, _lu in rows:
        if _uid in uid_to_kanal:
            continue
        # open_channels dan birinchi linki tekshirish
        if _oc and _oc not in ("", "Yo'q"):
            for lnk in _oc.split(','):
                lnk = lnk.strip()
                if lnk and lnk in link_to_numid:
                    uid_to_kanal[_uid] = link_to_numid[lnk]
                    break
        if _uid in uid_to_kanal:
            continue
        # has_hidden dan tekshirish
        if _hh and _hh not in ("", "❌") and "Maxfiy" not in (_hh or ""):
            for lnk in _hh.split(','):
                lnk = lnk.strip()
                if lnk and lnk in link_to_numid:
                    uid_to_kanal[_uid] = link_to_numid[lnk]
                    break

    for i, (uid, fname, uname, phone, open_ch, has_hidden, bio, grp_link, last_updated) in enumerate(rows, tc_start_row):
        bio_links_list = engine.extract_bio_links(bio) if bio else []
        bio_links_str  = ", ".join(bio_links_list) if bio_links_list else ""
        phone_str      = ("+" + phone) if phone else ""

        shaxsiy = ""
        maxfiy  = ""
        if has_hidden:
            if "t.me/+" in has_hidden or "t.me/joinchat/" in has_hidden:
                maxfiy = has_hidden
            elif has_hidden.startswith("http"):
                shaxsiy = has_hidden
            elif "Maxfiy" in has_hidden:
                inv_links = engine.extract_invite_links(bio) if bio else []
                maxfiy = ", ".join(inv_links) if inv_links else "🔒 Maxfiy (link yo'q)"
        if not maxfiy and bio:
            inv_links = engine.extract_invite_links(bio)
            if inv_links:
                maxfiy = ", ".join(inv_links)

        kanal_id = uid_to_kanal.get(uid, "")

        sh.append([
            i, uid, kanal_id, fname or "", uname or "", phone_str,
            bio_links_str, shaxsiy, maxfiy,
            bio or "", grp_link or "", last_updated or ""
        ])

    # ── ЛИСТ1: faqat yangi skanerlangan foydalanuvchilar ─────────────
    await status_msg.edit("⏳ Лист1 ma'lumotlari yuklanmoqda...")

    async with db_mod.connect(db_mod.DB_NAME, timeout=60) as db:
        async with db.execute(
            """
            SELECT
                user_id,
                MAX(first_name)   AS first_name,
                MAX(last_name)    AS last_name,
                MAX(username)     AS username,
                MAX(phone)        AS phone,
                MAX(bio)          AS bio,
                MAX(open_channels) AS open_channels,
                MAX(has_hidden)   AS has_hidden,
                GROUP_CONCAT(DISTINCT group_link) AS all_groups,
                MIN(added_date)   AS added_date,
                MAX(last_updated) AS last_updated
            FROM users_memory_bank
            GROUP BY user_id
            ORDER BY last_updated DESC
            """
        ) as cur:
            all_users_raw = await cur.fetchall()

    # Faqat yangi foydalanuvchilar
    new_all_users = [u for u in all_users_raw if u[0] not in all_exported]
    await status_msg.edit(f"⏳ Лист1 yozilmoqda... ({len(new_all_users)} ta yangi profil)")

    for j, (uid2, fn2, ln2, un2, ph2, bio2,
            oc2, hh2, grp2, added2, upd2) in enumerate(new_all_users, all_start_row):
        ph2_str  = ("+" + ph2) if ph2 else ""
        un2_str  = ("@" + un2) if un2 else ""
        kanal_id2 = uid_to_kanal.get(uid2, "")
        maxfiy2  = ""
        shaxsiy2 = ""
        if hh2 and hh2 not in ("", "❌"):
            if "t.me/+" in hh2 or "t.me/joinchat/" in hh2:
                maxfiy2 = hh2
            elif hh2.startswith("http"):
                shaxsiy2 = hh2
            elif "Maxfiy" in hh2:
                maxfiy2 = "🔒 Maxfiy"
        sh2.append([
            j, uid2, kanal_id2, fn2 or "", ln2 or "", un2_str, ph2_str,
            bio2 or "", oc2 or shaxsiy2 or "", maxfiy2,
            grp2 or "", added2 or "", upd2 or ""
        ])

    _loop = asyncio.get_event_loop()
    await status_msg.edit("⏳ Excel formatlash...")
    await _loop.run_in_executor(None, engine.apply_excel_styles, sh, sh.max_row - 1)
    await _loop.run_in_executor(None, engine.apply_excel_styles, sh2, sh2.max_row - 1)
    await status_msg.edit("⏳ Fayl saqlanmoqda...")

    # Faylni saqlash
    await _loop.run_in_executor(None, wb.save, EXCEL_PERSIST_PATH)

    # Eksport qilingan ID larni yangilash
    tc_exported.update(r[0] for r in rows)
    all_exported.update(u[0] for u in new_all_users)
    _save_excel_ids(tc_exported, all_exported)

    # Umumiy statistika
    total_tc  = sh.max_row - 1
    total_all = sh2.max_row - 1

    await bot.send_file(
        event.sender_id, EXCEL_PERSIST_PATH,
        caption=(
            f"📊 **Monitoring fayli** (yangilandi)\n\n"
            f"📋 **Target Channels** (1-varaq): `{total_tc}` ta — kanali bor profillar\n"
            f"   ➕ Yangi qo'shildi: `{len(rows)}` ta\n"
            f"📋 **Лист1** (2-varaq): `{total_all}` ta — barcha skanerlangan\n"
            f"   ➕ Yangi qo'shildi: `{len(new_all_users)}` ta\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
    )
    await status_msg.delete()

# ─────────────────────────────────────────────────────────────────────
# TUGMA: ARXIV
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="📁 Arxiv / Savatcha"))
async def btn_arxiv(event):
    if not await is_admin(event.sender_id):
        return
    USER_STATES[event.sender_id] = 'waiting_archive_date'
    await event.respond("📅 **Arxiv sanasini kiriting (YYYY-MM-DD):**")

# ─────────────────────────────────────────────────────────────────────
# TUGMA: MAXFIY KANAL QO'SHISH
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="🔒 Maxfiy Kanal Qo'shish"))
async def btn_maxfiy(event):
    if not await is_admin(event.sender_id):
        return
    USER_STATES[event.sender_id] = 'waiting_custom_channel'
    await event.respond("🔗 **24 soatlik monitoringga qo'shiladigan maxfiy kanal linkini yuboring:**")

# ─────────────────────────────────────────────────────────────────────
# TUGMA: ADMINLAR RO'YXATI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="👥 Adminlar Ro'yxati"))
async def btn_admins(event):
    if not await is_admin(event.sender_id):
        return
    admins = await db_mod.get_all_admins()
    lines  = [f"👑 **Super Admin:** `{SUPER_ADMIN_ID}`\n"]
    if EXTRA_ADMIN_IDS:
        lines.append("📋 **.env adminlar:**")
        for aid in sorted(EXTRA_ADMIN_IDS):
            lines.append(f"  • `{aid}` (statik)")
    if admins:
        lines.append("📋 **Qo'shilgan adminlar:**")
        for i, (aid,) in enumerate(admins, 1):
            lines.append(f"  {i}. `{aid}`")
    if not EXTRA_ADMIN_IDS and not admins:
        lines.append("📋 Hali qo'shimcha admin qo'shilmagan.")
    if event.sender_id == SUPER_ADMIN_ID:
        lines.append(
            "\n\n📌 **Buyruqlar:**\n"
            "`/add_admin [ID]` — admin qo'shish\n"
            "`/del_admin [ID]` — adminni o'chirish\n\n"
            "💡 Yoki `.env` faylida `ADMIN_IDS=ID1,ID2` qilib yozing"
        )
    await event.respond("\n".join(lines))

# ─────────────────────────────────────────────────────────────────────
# TUGMA: MANBALAR RO'YXATI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="📋 Manbalar Ro'yxati"))
async def btn_sources(event):
    if not await is_admin(event.sender_id):
        return
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT group_link, COUNT(DISTINCT user_id) as cnt "
            "FROM users_memory_bank "
            "WHERE group_link IS NOT NULL AND group_link != '' "
            "GROUP BY group_link ORDER BY cnt DESC"
        ) as cur:
            sources = await cur.fetchall()

    if not sources:
        await event.respond("📭 Hali hech qanday manba skanerlanbagan.")
        return

    lines = [f"📋 **Skanerlangan manbalar:** `{len(sources)}` ta\n"]
    total_users = 0
    for i, (link, cnt) in enumerate(sources, 1):
        lines.append(f"  {i}. `{link}` — `{cnt}` ta profil")
        total_users += cnt

    lines.append(f"\n👥 **Jami profillar:** `{total_users}` ta")

    # Telegram 4096 belgi limitiga bo'lish
    text = "\n".join(lines)
    if len(text) > 3800:
        chunks = []
        chunk  = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 3800:
                chunks.append(chunk)
                chunk = line
            else:
                chunk += ("\n" if chunk else "") + line
        if chunk:
            chunks.append(chunk)
        for ch in chunks:
            await event.respond(ch)
    else:
        await event.respond(text)


# ─────────────────────────────────────────────────────────────────────
# INLINE CALLBACK: KALIT SO'Z SANA TANLASH
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.CallbackQuery(pattern=b"kw_date_"))
async def keyword_date_callback(event):
    if not await is_admin(event.sender_id):
        return
    state = USER_STATES.get(event.sender_id)
    if not isinstance(state, dict) or state.get('state') != 'waiting_keyword_date':
        await event.answer("⚠️ Sessiya tugagan. Qaytadan boshlang.")
        return

    keyword  = state['keyword']
    data     = event.data.decode()
    days_map = {
        'kw_date_1':   (1,   "Oxirgi 1 kun"),
        'kw_date_7':   (7,   "Oxirgi 7 kun"),
        'kw_date_30':  (30,  "Oxirgi 1 oy"),
        'kw_date_365': (365, "Oxirgi 1 yil"),
        'kw_date_all': (None,"Hammasi"),
    }
    days, label = days_map.get(data, (None, "Hammasi"))

    USER_STATES[event.sender_id] = None
    await event.answer(f"✅ {label} tanlandi")
    await event.edit(
        f"🔎 **Qidirilmoqda:** `{keyword}`\n"
        f"📅 Davr: **{label}**\n"
        f"📍 Bazadagi barcha manbalardan qidirilmoqda..."
    )
    status = await event.get_message()
    if engine._SCAN_COUNT > 0:
        await event.respond(
            f"⚠️ Hozir skanerlash ishlayapti!\n"
            f"Navbatga qo\'shildi — tugagach boshlanadi."
        )
        if _SCAN_QUEUE is not None:
            _SCAN_QUEUE.put_nowait(('keyword', event.sender_id, (keyword, days, status)))
        return
    asyncio.create_task(run_keyword_search(event.sender_id, keyword, status, days))


# ─────────────────────────────────────────────────────────────────────
# GLOBAL INPUT PROCESSOR
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage)
async def global_input_processor(event):
    if not await is_admin(event.sender_id):
        return
    txt = (event.text or "").strip()
    if txt and (txt in BUTTON_TEXTS or txt.startswith('/')):
        return

    # ── 🛡 Havola tekshirish rejimi ──────────────────────────────────
    if event.sender_id in _PHISHING_WAIT:
        if txt == "❌ Bekor Qilish":
            _PHISHING_WAIT.discard(event.sender_id)
            await event.respond("❌ Bekor qilindi.", buttons=MAIN_KEYBOARD)
            return
        asyncio.create_task(_run_phishing_check(event.sender_id, event.message))
        return

    state = USER_STATES.get(event.sender_id)
    if not state:
        return

    # ── 1. Avtomatik skanerlash (guruh / kanal / yopiq) ─────────────
    if state == 'waiting_auto_scan_link':
        if not event.text:
            return
        ok, target = engine.validate_target(event.text)
        if not ok:
            await event.respond("⚠️ Noto'g'ri format. @username, t.me/link yoki guruh ID kiriting.")
            return
        USER_STATES[event.sender_id] = None
        # Invite link (yopiq guruh) aniqlash
        is_invite = bool(re.search(r't\.me/\+|joinchat', target))
        if engine._SCAN_COUNT > 0:
            await event.respond(
                f"⚠️ Hozir skanerlash ishlayapti!\n"
                f"Navbatga qo\'shildi — tugagach boshlanadi."
            )
            if _SCAN_QUEUE is not None:
                _SCAN_QUEUE.put_nowait(('msg' if is_invite else 'auto', event.sender_id, target))
            return
        if is_invite:
            status = await event.respond("⏳ Yopiq guruh xabarlari o'qilmoqda...")
            fname  = f"Msg_OSINT_{datetime.now().strftime('%d_%H%M')}.xlsx"
            fpath  = os.path.join(BASE_DIR, fname)
            asyncio.create_task(run_msg_scan(event.sender_id, target, fpath, status))
        else:
            status = await event.respond("⏳ Skanerlash boshlandi, tur aniqlanmoqda...")
            asyncio.create_task(run_auto_scan(event.sender_id, target, status))

    # ── 1d. Kalit so'z qidiruv — so'z kiritish ──────────────────────
    elif state == 'waiting_keyword_word':
        if not event.text:
            return
        input_text = event.text.strip()

        # ── ID qidiruvi: sof raqam kiritilgan bo'lsa ────────────────
        if input_text.lstrip('-').isdigit():
            USER_STATES.pop(event.sender_id, None)
            status_msg = await event.respond("🔍 ID qidirilmoqda...")
            asyncio.create_task(run_id_search(event.sender_id, input_text, status_msg))
            return

        ok, keyword = engine.validate_keyword(input_text)
        if not ok:
            await event.respond("⚠️ Kalit so'z 1-200 belgi bo'lishi kerak.")
            return
        USER_STATES[event.sender_id] = {'state': 'waiting_keyword_date', 'keyword': keyword}
        await event.respond(
            f"✅ Kalit so'z: `{keyword}`\n\n"
            "📅 **Qidiruv davrini tanlang:**",
            buttons=[
                [Button.inline("📅 Oxirgi 1 kun",   data="kw_date_1")],
                [Button.inline("📅 Oxirgi 7 kun",   data="kw_date_7")],
                [Button.inline("📅 Oxirgi 1 oy",    data="kw_date_30")],
                [Button.inline("📅 Oxirgi 1 yil",   data="kw_date_365")],
                [Button.inline("📅 Hammasi",         data="kw_date_all")],
            ]
        )


    # ── 3. Arxiv sanasi ──────────────────────────────────────────────
    elif state == 'waiting_archive_date':
        if not event.text:
            return
        USER_STATES[event.sender_id] = None
        sana = event.text.strip()
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            async with db.execute(
                "SELECT file_name, file_path FROM archive_bin WHERE created_date=?", (sana,)
            ) as cur:
                files = await cur.fetchall()
        if files:
            for fn, fp in files:
                if os.path.exists(fp):
                    await bot.send_file(event.sender_id, fp, caption=f"📁 `{fn}`")
        else:
            await event.respond("📭 Bu sanada fayl topilmadi.")

    # ── 4. Maxfiy kanal qo'shish ─────────────────────────────────────
    elif state == 'waiting_tergov_id':
        if not event.text:
            return
        ident = event.text.strip()
        if not ident:
            return
        USER_STATES.pop(event.sender_id, None)
        status_msg = await event.respond(
            f"🕵️ `{ident}` bo'yicha ma'lumot to'planmoqda...\n"
            "📸 Rasmlar, xabarlar, statistika yuklanmoqda..."
        )
        asyncio.create_task(run_tergov_pdf(event.sender_id, ident, status_msg))
        return

    elif state == 'waiting_custom_channel':
        if not event.text:
            return
        # Faqat invite link yoki oddiy link qabul qilish
        ch = event.text.strip()
        _is_valid_invite = (
            ch.startswith("https://t.me/+") or
            ch.startswith("https://t.me/joinchat/") or
            ch.startswith("t.me/+") or
            ch.startswith("t.me/joinchat/")
        )
        if not _is_valid_invite or len(ch) > 512:
            await event.respond("⚠️ Faqat invite link qabul qilinadi: https://t.me/+XXXX")
            return
        USER_STATES[event.sender_id] = None
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            await db.execute(
                "INSERT OR IGNORE INTO hidden_channel_knocker "
                "(channel_id, creator_id, source_group, last_request_time) "
                "VALUES (?, 0, 'Qo''lda qo''shilgan', ?)",
                (ch, now_str)
            )
            await db.commit()
        await event.respond(f"📥 `{ch}` 24 soatlik kuzatuvga qo'shildi.")


# ─────────────────────────────────────────────────────────────────────
# RAZVEDKA — BITTA RAQAM
# ── 2-TUZATMA: timeout va to'g'ri xato tutish
# ─────────────────────────────────────────────────────────────────────






def _scan_err(e: Exception) -> str:
    """Xato turini aniqlab, foydalanuvchiga qulay xabar qaytaradi."""
    s = str(e)
    if "not part of" in s or "Guruhga ulanib" in s or "not a member" in s:
        return ("🔒 **Userbot bu guruhga a'zo emas**\n\n"
                "Userbot hisobini guruhga qo'lda qo'shib, qayta urining.\n"
                "Sabab: admin botni chiqarib yuborgan bo'lishi mumkin.")
    if "ChannelPrivate" in s or "private" in s.lower():
        return "🔒 **Guruh/kanal yopiq** — kirish taqiqlangan."
    if "flood" in s.lower() or "FloodWait" in s:
        return "⏳ **Telegram vaqtincha chekladi.** Biroz kutib, qayta urining."
    if "ResolveUsername" in s or "USERNAME" in s.upper():
        return "⏳ **Username so'rovi cheklangan.** 30 daqiqa kutib, qayta urining."
    if "Topilmadi" in s or "not found" in s.lower():
        return "❌ **Guruh/kanal topilmadi.** Linkni tekshirib qayta urining."
    return f"❌ Xatolik: {e}"


def _md_escape(text: str) -> str:
    if not text:
        return text
    for ch in ['\\', '*', '_', '`', '[', ']', '~']:
        text = text.replace(ch, '\\' + ch)
    return text


async def run_keyword_search(sender_id, keyword, status_msg, days=None):
    try:
        # ── 1. Lokal keshdan qidiruv (Telegram API yo'q) ─────────────
        cache_total, cache_sources, last_sync = await engine.get_cache_stats()

        if cache_total > 0:
            try:
                await status_msg.edit(
                    f"🔎 **`{keyword}`** lokal bazadan qidirilmoqda...\n"
                    f"💾 Kesh: `{cache_total:,}` ta xabar | `{cache_sources}` ta manba\n"
                    f"🕐 Oxirgi sinxron: {last_sync or 'nomalum'}"
                )
            except Exception as e:
                engine._dbg("main.run_keyword_search", e)

            all_results = await engine.search_keywords_local(keyword, days=days)

            davr_str = (
                f"Oxirgi {days} kun" if days and days < 365
                else ("Oxirgi 1 yil" if days == 365 else "Hammasi")
            )

            if not all_results:
                await bot.send_message(
                    sender_id,
                    f"🔎 **`{keyword}`** — lokal bazada topilmadi.\n"
                    f"💾 Kesh: `{cache_total:,}` xabar | Davr: {davr_str}\n"
                    f"💡 Kesh yangilanmagan bo'lsa /sync_cache buyrug'ini yuboring."
                )
                return

            header = (
                f"🔎 **Qidiruv natijalari (lokal):** `{keyword}`\n"
                f"📅 Davr: **{davr_str}**\n"
                f"📊 Topildi: `{len(all_results)}` ta\n"
                f"💾 Lokal keshdan — Telegram API ishlatilmadi\n"
                f"{'─' * 30}\n\n"
            )

        else:
            # ── 2. Kesh bo'sh — Telegram dan qidiruv (bir martalik) ──
            try:
                await status_msg.edit(
                    "⚠️ Lokal kesh hali to'ldirilmagan.\n"
                    "📡 Telegram'dan qidirilmoqda (bu oxirgi marta)...\n"
                    "💡 Keyingi qidiruvlar tezkor bo'ladi."
                )
            except Exception as e:
                engine._dbg("main.run_keyword_search", e)

            sources_set = set()
            async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                async with db.execute(
                    "SELECT DISTINCT group_link FROM users_memory_bank "
                    "WHERE group_link IS NOT NULL AND group_link != ''", ()
                ) as cur:
                    for (link,) in await cur.fetchall():
                        sources_set.add(link)
                async with db.execute(
                    "SELECT channel_id FROM hidden_channel_knocker WHERE status='joined'"
                ) as cur:
                    for (ch_id,) in await cur.fetchall():
                        sources_set.add(ch_id)

            sources = list(sources_set)
            if not sources:
                await bot.send_message(
                    sender_id, "📭 Bazada manba yo'q. Avval guruh skanerlang."
                )
                return

            all_results = []
            for i, source in enumerate(sources):
                try:
                    await status_msg.edit(
                        f"📡 `{source}` ({i+1}/{len(sources)})"
                    )
                except Exception as e:
                    engine._dbg("main.run_keyword_search", e)
                try:
                    r = await engine.search_keywords(userbot, source, keyword, status_msg, days=days)
                    all_results.extend(r)
                except Exception:
                    continue
                await asyncio.sleep(1.5)

            davr_str = (
                f"Oxirgi {days} kun" if days and days < 365
                else ("Oxirgi 1 yil" if days == 365 else "Hammasi")
            )
            if not all_results:
                await bot.send_message(
                    sender_id,
                    f"🔎 **`{keyword}`** — `{len(sources)}` ta manbadan topilmadi."
                )
                return

            header = (
                f"🔎 **Qidiruv natijalari:** `{keyword}`\n"
                f"📅 Davr: **{davr_str}**\n"
                f"📊 Topildi: `{len(all_results)}` ta | Manbalar: `{len(sources)}` ta\n"
                f"{'─' * 30}\n\n"
            )

        # ── Natijalarni yuborish ──────────────────────────────────────
        chunk     = header
        chunk_num = 1
        for r in all_results:
            line = (
                f"👤 **{_md_escape(r['name'])}**"
                + (f" (@{_md_escape(r['username'])})" if r['username'] else "")
                + f"\n🆔 `{r['user_id']}`\n"
                f"📍 {_md_escape(r['source'])}\n"
                f"📅 {r['date']}\n"
                f"💬 {_md_escape(r['text'])}\n"
                f"{'─' * 25}\n\n"
            )
            if len(chunk) + len(line) > 3800:
                try:
                    await bot.send_message(sender_id, chunk)
                except Exception:
                    await bot.send_message(sender_id, chunk, parse_mode=None)
                await asyncio.sleep(0.5)
                chunk      = f"_(davomi {chunk_num + 1})_\n\n" + line
                chunk_num += 1
            else:
                chunk += line
        if chunk.strip():
            try:
                await bot.send_message(sender_id, chunk)
            except Exception:
                await bot.send_message(sender_id, chunk, parse_mode=None)

    except Exception as e:
        await bot.send_message(sender_id, _scan_err(e))
    finally:
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_keyword_search", e)


async def run_comment_scan(sender_id, target, fpath, status_msg, _ub=None):
    try:
        await status_msg.edit(
            f"💬 Kanal comment skanerlash boshlandi...\n"
            f"📍 Manba: `{target}`"
        )
    except Exception as e:
        engine._dbg("main.run_comment_scan", e)
    try:
        if _ub is None:
            _ub = await engine.get_ub_for_channel(str(target)) or userbot
            await engine.auto_assign_channel(str(target))
        result = await engine.scan_channel_comments(_ub, target, fpath, status_msg)
        count, ch_title = result
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            await db.execute(
                "INSERT INTO archive_bin (file_name, file_path, created_date) VALUES (?, ?, ?)",
                (os.path.basename(fpath), fpath, datetime.now().strftime("%Y-%m-%d"))
            )
            await db.commit()
        await bot.send_file(
            sender_id, fpath,
            caption=(
                f"✅ **{ch_title}** comment skanerlash yakunlandi!\n"
                f"👥 Jami: `{count}` ta profil yozildi."
            )
        )
    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        await bot.send_message(
            sender_id,
            f"❌ Comment skanerlashda xatolik:\n`{e}`\n\n"
            f"Sabab: discussion guruh topilmadi yoki kanal yopiq."
        )
    finally:
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_comment_scan", e)


async def run_msg_scan(sender_id, target, fpath, status_msg):
    try:
        _ub = await engine.get_ub_for_channel(str(target)) or userbot
        count = await engine.scan_messages(_ub, target, fpath, status_msg)
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            await db.execute(
                "INSERT INTO archive_bin (file_name, file_path, created_date) VALUES (?, ?, ?)",
                (os.path.basename(fpath), fpath, datetime.now().strftime("%Y-%m-%d"))
            )
            await db.commit()
        await bot.send_file(
            sender_id, fpath,
            caption=f"✅ Xabar skanerlash yakunlandi! `{count}` ta profil yozildi."
        )
    except FloodWaitError as e:
        h = e.seconds // 3600
        m = (e.seconds % 3600) // 60
        await bot.send_message(sender_id,
            f"⏳ **Telegram FloodWait**\n⏱ Kutish: **{h} soat {m} daqiqa**\nBu vaqt o'tgach qayta urining.")
    except Exception as e:
        err = str(e)
        if "not part of" in err or "Guruhga ulanib" in err or "not a member" in err:
            await bot.send_message(sender_id,
                "🔒 **Userbot bu guruhga a'zo emas**\n\n"
                "Userbot hisobini guruhga qo'lda qo'shib, qayta urinib ko'ring.\n"
                "Sabab: guruh admini botni chiqarib yuborgan bo'lishi mumkin.")
        elif "private" in err.lower() or "ChannelPrivate" in err:
            await bot.send_message(sender_id,
                "🔒 **Guruh yopiq**\n\nBu guruhga kirish taqiqlangan.")
        elif "flood" in err.lower():
            await bot.send_message(sender_id,
                "⏳ **Telegram vaqtincha chekladi**\nBiroz kutib, qayta urining.")
        else:
            await bot.send_message(sender_id, f"❌ Xabar skanerlashda xatolik: {e}")
    finally:
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_msg_scan", e)


async def run_auto_scan(sender_id, target, status_msg):
    """Link turini avtomatik aniqlab tegishli skanerlovchini ishga tushiradi."""
    try:
        from telethon.tl.types import Channel
        _ub = await engine.get_ub_for_channel(str(target)) or userbot
        await engine.auto_assign_channel(str(target))
        ent = await _ub.get_entity(target)  # faqat 1 marta resolve
        if isinstance(ent, Channel) and ent.broadcast:
            fname = f"Comment_OSINT_{datetime.now().strftime('%d_%H%M')}.xlsx"
            fpath = os.path.join(BASE_DIR, fname)
            await run_comment_scan(sender_id, target, fpath, status_msg, _ub=_ub)
        else:
            fname = f"Group_OSINT_{datetime.now().strftime('%d_%H%M')}.xlsx"
            fpath = os.path.join(BASE_DIR, fname)
            await run_background_scan(sender_id, target, fpath, status_msg, pre_entity=ent, _ub=_ub)
    except FloodWaitError as e:
        h = e.seconds // 3600
        m = (e.seconds % 3600) // 60
        await bot.send_message(
            sender_id,
            f"⏳ **Telegram FloodWait xatosi**\n\n"
            f"Userbot hisobi vaqtincha cheklangan.\n"
            f"⏱ Kutish vaqti: **{h} soat {m} daqiqa**\n\n"
            f"Bu vaqt o'tgach qayta urinib ko'ring."
        )
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_auto_scan", e)
    except RpcCallFailError:
        await bot.send_message(
            sender_id,
            "⚠️ **Telegram server xatosi**\n\n"
            "Telegram o'z serverlarida vaqtinchalik muammo aniqladi.\n"
            "Bir necha daqiqa kutib, qayta urining."
        )
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_auto_scan", e)
    except Exception as e:
        await bot.send_message(sender_id, _scan_err(e))
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_auto_scan", e)


async def run_background_scan(sender_id, target, fpath, status_msg, pre_entity=None, _ub=None):
    global _last_scan_end_time
    try:
        if _ub is None:
            _ub = await engine.get_ub_for_channel(str(target)) or userbot
            await engine.auto_assign_channel(str(target))
        count = await engine.deep_scan_group(
            _ub, target, fpath, status_msg, pre_entity=pre_entity,
            extra_userbots=_EXTRA_USERBOTS
        )
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            await db.execute(
                "INSERT INTO archive_bin (file_name, file_path, created_date) VALUES (?, ?, ?)",
                (os.path.basename(fpath), fpath, datetime.now().strftime("%Y-%m-%d"))
            )
            await db.commit()
        await bot.send_file(
            sender_id, fpath,
            caption=f"✅ Skanerlash yakunlandi! `{count}` ta profil yozildi."
        )
    except FloodWaitError as e:
        h = e.seconds // 3600
        m = (e.seconds % 3600) // 60
        await bot.send_message(
            sender_id,
            f"⏳ **Telegram FloodWait xatosi**\n\n"
            f"Skanerlash jarayonida userbot hisobi cheklandi.\n"
            f"⏱ Kutish vaqti: **{h} soat {m} daqiqa**\n\n"
            f"Bu vaqt o'tgach qayta urinib ko'ring."
        )
    except RpcCallFailError:
        await bot.send_message(
            sender_id,
            "⚠️ **Telegram server xatosi**\n\n"
            "Telegram o'z serverlarida vaqtinchalik muammo aniqladi.\n"
            "Bir necha daqiqa kutib, qayta urining."
        )
    except Exception as e:
        await bot.send_message(sender_id, _scan_err(e))
    finally:
        _last_scan_end_time = time.time()
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_background_scan", e)


# ─────────────────────────────────────────────────────────────────────
# ISHGA TUSHIRISH
# ─────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────
# TUGMA: KO'P MUSIQA QIDIRISH
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="🎼 Ko'p Musiqa Qidirish"))
async def btn_multi_music_search(event):
    if not await is_admin(event.sender_id):
        return
    total_fp, total_ch = await music.get_stats()
    if total_fp == 0:
        await event.respond(
            "⚠️ Baza bo\'sh!\n"
            "Monitoring ishlayotganini tekshiring."
        )
        return
    USER_STATES[event.sender_id] = {
        'state': 'waiting_multi_music',
        'files': []
    }
    await event.respond(
        f"🎼 **Ko\'p Musiqa Qidirish**\n\n"
        f"📊 Bazada `{total_fp}` ta fingerprint (`{total_ch}` manba)\n\n"
        f"Musiqalarni birin-ketin yuboring (10+ ta bo\'lishi mumkin)\n"
        f"Yuborib bo\'lgach `/search_done` yozing"
    )


@bot.on(events.NewMessage(pattern='/search_done'))
async def multi_music_done(event):
    if not await is_admin(event.sender_id):
        return
    state = USER_STATES.get(event.sender_id)
    if not isinstance(state, dict) or state.get('state') != 'waiting_multi_music':
        await event.respond("⚠️ Avval **🎼 Ko\'p Musiqa Qidirish** tugmasini bosing.")
        return
    files = state.get('files', [])
    if not files:
        await event.respond("⚠️ Hech qanday musiqa yuborilmadi.")
        return
    USER_STATES[event.sender_id] = None
    status = await event.respond(
        f"🎼 `{len(files)}` ta musiqa tahlil qilinmoqda...\n"
        f"Biroz kuting."
    )
    asyncio.create_task(run_multi_music_search(event.sender_id, files, status))


@bot.on(events.NewMessage(func=lambda e: True))
async def multi_music_file_handler(event):
    if not await is_admin(event.sender_id):
        return
    state = USER_STATES.get(event.sender_id)
    if not isinstance(state, dict) or state.get('state') != 'waiting_multi_music':
        return
    if not (event.audio or event.voice or event.document):
        return

    # Faylni saqlash
    tmp_path = os.path.join(BASE_DIR, f"multi_{event.sender_id}_{len(state['files'])}.ogg")
    try:
        await event.download_media(file=tmp_path)
        state['files'].append(tmp_path)
        count = len(state['files'])
        await event.respond(
            f"✅ `{count}` ta musiqa qabul qilindi\n"
            f"Yana musiqa yuboring yoki qidiruvni boshlang:",
            buttons=[
                [Button.inline("🔍 Qidiruvni Boshlash", data="multi_search_start")],
            ]
        )
    except Exception as e:
        await event.respond(f"❌ Xatolik: {e}")


@bot.on(events.CallbackQuery(pattern=b"multi_search_start"))
async def multi_search_start_callback(event):
    if not await is_admin(event.sender_id):
        return
    state = USER_STATES.get(event.sender_id)
    if not isinstance(state, dict) or state.get('state') != 'waiting_multi_music':
        await event.answer("⚠️ Sessiya tugagan.")
        return
    files = state.get('files', [])
    if not files:
        await event.answer("⚠️ Hech qanday musiqa yuborilmadi.")
        return
    USER_STATES[event.sender_id] = None
    await event.answer("🔍 Qidiruv boshlandi!")
    status = await event.respond(
        f"🎼 `{len(files)}` ta musiqa tahlil qilinmoqda...\n"
        f"Biroz kuting."
    )
    asyncio.create_task(run_multi_music_search(event.sender_id, files, status))


async def run_multi_music_search(sender_id, files, status_msg):
    try:
        # Excel tayyorlash
        wb = openpyxl.Workbook()
        sh = wb.active
        sh.title = "Ko'p Musiqa Qidiruv"
        sh.append([
            "№", "Sizning Musiqa", "Manba Turi", "Kanal/Profil Nomi",
            "Telegram ID", "Kanal ID", "Ism", "Username", "Telefon",
            "Bio", "Maxfiy Kanal", "Shaxsiy Kanal", "O\'xshashlik %", "Manba Guruh"
        ])

        row_num   = 0
        found_any = False

        for idx, fpath in enumerate(files):
            music_name = f"Musiqa {idx + 1}"
            try:
                await status_msg.edit(
                    f"🎼 `{idx+1}/{len(files)}` tahlil qilinmoqda..."
                )
            except Exception as e:
                engine._dbg("main.run_multi_music_search", e)

            try:
                results = await music.search_music(fpath)
            except Exception:
                results = []

            if not results:
                row_num += 1
                sh.append([
                    row_num, music_name, "—", "Topilmadi",
                    "", "", "", "", "", "", "", "", "0%", ""
                ])
                continue

            found_any = True
            for r in results[:20]:  # Har musiqa uchun top 20
                row_num += 1
                is_profile = r['file_name'].startswith('profile_')
                manba_turi = "👤 Profil" if is_profile else "📢 Kanal"

                fname = uname = phone = bio = has_hidden = open_ch = grp_link = ""
                kanal_id = ""
                async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                    async with db.execute(
                        "SELECT first_name, username, phone, bio, "
                        "has_hidden, open_channels, group_link "
                        "FROM users_memory_bank WHERE user_id=? LIMIT 1",
                        (r['channel_id'],)
                    ) as cur:
                        row = await cur.fetchone()
                    if row:
                        fname, uname, phone, bio, has_hidden, open_ch, grp_link = row
                        uname = ('@' + uname) if uname else ''
                        phone = ('+' + phone) if phone else ''

                    # Kanal ID (-100 formatida)
                    async with db.execute(
                        "SELECT channel_id FROM hidden_channel_knocker "
                        "WHERE creator_id=? LIMIT 1",
                        (r['channel_id'],)
                    ) as cur:
                        crow = await cur.fetchone()
                    if crow:
                        ch_raw = str(crow[0]).lstrip('-')
                        kanal_id = f"-100{ch_raw}" if not str(crow[0]).startswith('-100') else crow[0]

                shaxsiy = has_hidden if (has_hidden and has_hidden.startswith("http") and "t.me/+" not in (has_hidden or "")) else ""
                maxfiy  = has_hidden if (has_hidden and ("t.me/+" in (has_hidden or "") or "Maxfiy" in (has_hidden or ""))) else ""

                sh.append([
                    row_num, music_name, manba_turi, r['channel_name'],
                    r['channel_id'], kanal_id, fname, uname, phone,
                    bio or "", maxfiy, shaxsiy,
                    f"{r['score']}%", grp_link
                ])

        if not found_any:
            await bot.send_message(
                sender_id,
                f"📭 `{len(files)}` ta musiqadan hech biri bazada topilmadi."
            )
            return

        _loop = asyncio.get_event_loop()
        await _loop.run_in_executor(None, engine.apply_excel_styles, sh, row_num)
        out_name = f"Multi_Musiqa_{datetime.now().strftime('%d_%H%M')}.xlsx"
        out_path = os.path.join(BASE_DIR, out_name)
        await _loop.run_in_executor(None, wb.save, out_path)

        await bot.send_file(
            sender_id, out_path,
            caption=(
                f"🎼 **Ko\'p Musiqa Qidiruv yakunlandi!**\n\n"
                f"🎵 Tekshirildi: `{len(files)}` ta musiqa\n"
                f"📊 Natijalar: `{row_num}` ta\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
        )

    except Exception as e:
        await bot.send_message(sender_id, f"❌ Xatolik: {e}")
    finally:
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_multi_music_search", e)
        for fpath in files:
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception as e:
                engine._dbg("main.run_multi_music_search", e)


# ─────────────────────────────────────────────────────────────────────
# EXCEL BATCH SKANERLASH — xlsx fayl yuklash + userbot tanlash
# ─────────────────────────────────────────────────────────────────────

_XLSX_MIME = (
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-excel',
)

@bot.on(events.NewMessage(func=lambda e: e.document is not None))
async def excel_upload_handler(event):
    if not await is_admin(event.sender_id):
        return
    doc  = event.document
    mime = getattr(doc, 'mime_type', '') or ''
    if mime not in _XLSX_MIME:
        return
    # Fayl nomini ham tekshirish (.xlsx / .xls)
    fname_attr = next(
        (a for a in getattr(doc, 'attributes', []) if hasattr(a, 'file_name')),
        None
    )
    fname_lower = (getattr(fname_attr, 'file_name', '') or '').lower()
    if fname_lower and not (fname_lower.endswith('.xlsx') or fname_lower.endswith('.xls')):
        return

    tmp_path = os.path.join(BASE_DIR, f"excel_upload_{event.sender_id}.xlsx")
    try:
        await event.download_media(file=tmp_path)
    except Exception as e:
        await event.respond(f"❌ Fayl yuklab olinmadi: {e}")
        return

    # Excel o'qish — birinchi ustundagi kanallar
    channels = []
    _SKIP = {'channel', 'link', 'kanal', 'havola', 'url', 'username',
              'group', 'guruh', '#', 'no', 'raqam', 'linki'}
    try:
        wb = openpyxl.load_workbook(tmp_path, read_only=True, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            val = row[0] if row else None
            if val is None:
                continue
            ch = str(val).strip()
            if not ch or ch.lower() in _SKIP or ch.lower().startswith('none'):
                continue
            channels.append(ch)
        wb.close()
    except Exception as e:
        await event.respond(f"❌ Excel o'qishda xatolik: {e}")
        return
    finally:
        try:
            os.remove(tmp_path)
        except Exception as e:
            engine._dbg("main.excel_upload_handler", e)

    if not channels:
        await event.respond(
            "⚠️ Excel faylda kanal linki topilmadi.\n"
            "Birinchi ustunda kanallar ro'yxati bo'lishi kerak:\n"
            "`A1: @channel1`\n`A2: t.me/channel2`\n..."
        )
        return

    USER_STATES[event.sender_id] = {'state': 'excel_ub_pending', 'channels': channels}
    ub2_ok   = userbot2 is not None
    ub2_text = "✅ Tayyor" if ub2_ok else "❌ Ulanmagan"
    mid      = (len(channels) + 1) // 2

    btns = [
        [Button.inline("🤖 Faqat 1-Userbot",       data="excel_ub_1")],
        [Button.inline(
            "🤖 Faqat 2-Userbot" if ub2_ok else "⚠️ 2-Userbot ulanmagan",
            data="excel_ub_2" if ub2_ok else "excel_ub_none"
        )],
        [Button.inline("🤖🤖 Ikkalasi (parallel)", data="excel_ub_both")],
        [Button.inline("❌ Bekor Qilish",           data="excel_ub_cancel")],
    ]
    await event.respond(
        f"📋 **Excel Batch Skanerlash**\n\n"
        f"📊 Kanallar: `{len(channels)}` ta\n"
        f"🤖 Userbot1: `{mid}` ta | 🤖 Userbot2 ({ub2_text}): `{len(channels) - mid}` ta\n"
        f"⏱ Har kanal orasida **15 daqiqa** pauza\n\n"
        f"**Qaysi userbot bilan skanerlasin?**",
        buttons=btns
    )


@bot.on(events.CallbackQuery(pattern=b"excel_ub_"))
async def excel_ub_callback(event):
    if not await is_admin(event.sender_id):
        await event.answer("❌ Ruxsat yo'q")
        return
    state = USER_STATES.get(event.sender_id)
    if not isinstance(state, dict) or state.get('state') != 'excel_ub_pending':
        await event.answer("⚠️ Sessiya tugagan. Qayta Excel yuboring.")
        return

    action   = event.data.decode()
    channels = state.get('channels', [])
    USER_STATES[event.sender_id] = None

    if action in ("excel_ub_cancel", "excel_ub_none") or not channels:
        await event.answer("❌ Bekor qilindi")
        await event.edit("❌ Excel skanerlash bekor qilindi.")
        return

    await event.answer("✅ Skanerlash boshlandi!")

    if action == "excel_ub_1":
        await event.edit(
            f"🚀 **1-Userbot** bilan `{len(channels)}` ta kanal skanerlanmoqda...\n"
            f"⏱ Har kanal orasida 15 daqiqa pauza.\nTugaganda xabar beraman!"
        )
        asyncio.create_task(
            _run_excel_batch(event.sender_id, channels, ub_num=1)
        )

    elif action == "excel_ub_2":
        if not userbot2:
            await event.edit("❌ 2-Userbot ulanmagan.")
            return
        await event.edit(
            f"🚀 **2-Userbot** bilan `{len(channels)}` ta kanal skanerlanmoqda...\n"
            f"⏱ Har kanal orasida 15 daqiqa pauza.\nTugaganda xabar beraman!"
        )
        asyncio.create_task(
            _run_excel_batch(event.sender_id, channels, ub_num=2)
        )

    elif action == "excel_ub_both":
        mid   = (len(channels) + 1) // 2
        part1 = channels[:mid]
        part2 = channels[mid:]
        await event.edit(
            f"🚀 **Ikkalasi parallel** skanerlanmoqda:\n"
            f"🤖 Userbot1: `{len(part1)}` ta kanal\n"
            f"🤖 Userbot2: `{len(part2)}` ta kanal\n"
            f"⏱ Har kanal orasida 15 daqiqa pauza.\nTugaganda xabar beraman!"
        )
        asyncio.create_task(
            _run_excel_batch_both(event.sender_id, part1, part2)
        )


async def _run_excel_batch(sender_id, channels, ub_num=1):
    ub = userbot if ub_num == 1 else userbot2
    if ub is None:
        await bot.send_message(sender_id, f"❌ Userbot{ub_num} ulanmagan!")
        return
    await engine.excel_batch_scanner(ub, channels, bot, sender_id, userbot_idx=ub_num - 1)


async def _run_excel_batch_both(sender_id, part1, part2):
    tasks = []
    if part1:
        tasks.append(engine.excel_batch_scanner(
            userbot, part1, bot, sender_id, userbot_idx=0
        ))
    if part2 and userbot2:
        tasks.append(engine.excel_batch_scanner(
            userbot2, part2, bot, sender_id, userbot_idx=1
        ))
    elif part2:
        await bot.send_message(
            sender_id,
            "⚠️ 2-Userbot topilmadi. Qolgan kanallar 1-Userbot bilan skanerlanadi."
        )
        tasks.append(engine.excel_batch_scanner(
            userbot, part1 + part2, bot, sender_id, userbot_idx=0
        ))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ─────────────────────────────────────────────────────────────────────
# TUGMA: KUZATILADIGAN MUSIQALAR
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="🔔 Kuzatiladigan Musiqalar"))
async def btn_watch_music(event):
    if not await is_admin(event.sender_id):
        return
    watches = await music.get_watch_list()
    if watches:
        lines_txt = [f"🔔 **Kuzatiladigan musiqalar:** `{len(watches)}` ta\n"]
        for w_id, w_name, w_date in watches:
            lines_txt.append(f"  {w_id}. 🎵 `{w_name}` — {w_date}")
        buttons = []
        for w_id, w_name, w_date in watches:
            buttons.append([Button.inline(f"🗑 {w_id}. {w_name[:25]}", data=f"del_watch_{w_id}")])
        buttons.append([Button.inline("➕ Musiqa Qo\'shish", data="add_watch_music")])
        buttons.append([Button.inline("📋 Arxiv (Topilganlar)", data="watch_archive")])
        buttons.append([Button.inline("🔄 Bazani Qayta Skanerlash", data="watch_rescan")])
        await event.respond("\n".join(lines_txt), buttons=buttons)
    else:
        await event.respond(
            "🔔 **Kuzatiladigan musiqalar yo\'q**\n\n"
            "Musiqa qo\'shsangiz — darhol xabar beradi!",
            buttons=[
                [Button.inline("➕ Musiqa Qo\'shish", data="add_watch_music")],
                [Button.inline("📋 Arxiv (Topilganlar)", data="watch_archive")]
            ]
        )


@bot.on(events.CallbackQuery(pattern=b"del_watch_\\d+"))
async def del_watch_callback(event):
    if not await is_admin(event.sender_id):
        return
    w_id = int(event.data.decode().split("_")[-1])
    await music.delete_watch_music(w_id)
    await event.answer("✅ O\'chirildi!")
    watches = await music.get_watch_list()
    if watches:
        lines_txt = [f"🔔 **Kuzatiladigan musiqalar:** `{len(watches)}` ta\n"]
        for wid, wname, wdate in watches:
            lines_txt.append(f"  {wid}. 🎵 `{wname}` — {wdate}")
        buttons = []
        for wid, wname, wdate in watches:
            buttons.append([Button.inline(f"🗑 {wid}. {wname[:25]}", data=f"del_watch_{wid}")])
        buttons.append([Button.inline("➕ Musiqa Qo\'shish", data="add_watch_music")])
        await event.edit("\n".join(lines_txt), buttons=buttons)
    else:
        await event.edit(
            "🔔 Kuzatiladigan musiqalar yo\'q.",
            buttons=[[Button.inline("➕ Musiqa Qo\'shish", data="add_watch_music")]]
        )


@bot.on(events.CallbackQuery(pattern=b"add_watch_music"))
async def add_watch_callback(event):
    if not await is_admin(event.sender_id):
        return
    await event.answer()
    USER_STATES[event.sender_id] = {
        'state': 'waiting_watch_music',
        'name':  None
    }
    await event.respond(
        "🎵 **Kuzatiladigan musiqa qo\'shish**\n\n"
        "Avval musiqa nomini yozing (masalan: `Jakone - Mutny tip`):"
    )




@bot.on(events.NewMessage(func=lambda e: True))
async def watch_music_handler(event):
    if not await is_admin(event.sender_id):
        return
    state = USER_STATES.get(event.sender_id)
    if not isinstance(state, dict) or state.get('state') != 'waiting_watch_music':
        return

    # Avval nom so'raladi
    if state.get('name') is None:
        if not event.text:
            return
        state['name'] = event.text.strip()
        await event.respond(
            f"✅ Nom: `{state['name']}`\n\n"
            f"Endi musiqani yuboring 👇"
        )
        return

    # Keyin musiqa fayli
    if not (event.audio or event.voice or event.document):
        await event.respond("⚠️ Audio fayl yuboring!")
        return

    USER_STATES[event.sender_id] = None
    status = await event.respond("🎵 Musiqa tahlil qilinmoqda...")

    tmp_path = os.path.join(BASE_DIR, f"watch_{event.sender_id}.ogg")
    try:
        await event.download_media(file=tmp_path)
        fp, duration = await engine.music_mod.get_fingerprint_async(tmp_path)
        if not fp:
            await status.edit("❌ Fingerprint olishda xatolik. fpcalc.exe borligini tekshiring.")
            return
        await music.add_watch_music(fp, duration or 0, state['name'], event.sender_id)
        await status.edit(
            f"✅ **`{state['name']}`** kuzatuvga qo\'shildi!\n\n"
            f"Endi kimdir bu musiqani profil yoki kanalga qo\'ysa — darhol xabar beraman! 🔔"
        )
    except Exception as e:
        await status.edit(f"❌ Xatolik: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ─────────────────────────────────────────────────────────────────────
# REAL-TIME ALERT TIZIMI
# ─────────────────────────────────────────────────────────────────────

async def watch_alert_sender():
    """Kuzatiladigan musiqa yoki kalit so'z alert topilganda xabar yuboradi."""
    while True:
        item = await engine._WATCH_ALERTS.get()
        try:
            # Yangi format: ('alert', hit_dict)
            if isinstance(item, tuple) and len(item) == 2 and item[0] == 'alert':
                await process_alert_hits([item[1]])
                continue
            alert = item
            source_id = alert['source_id']
            source_type = alert['source_type']
            source_name = alert['source_name']

            # Kanal linki va -100 ID formatini tayyorlash
            extra = ""
            if source_type == 'kanal':
                ch_id_clean = str(source_id).lstrip('-')
                ch_id_fmt = f"-100{ch_id_clean}" if not str(source_id).startswith('-100') else source_id
                ch_link = ""
                try:
                    entity = await userbot.get_entity(int(source_id))
                    username = getattr(entity, 'username', None)
                    if username:
                        ch_link = f"https://t.me/{username}"
                except Exception as e:
                    engine._dbg("main.watch_alert_sender", e)
                if not ch_link:
                    try:
                        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as _db:
                            async with _db.execute(
                                "SELECT channel_id FROM hidden_channel_knocker "
                                "WHERE channel_id LIKE ? LIMIT 1",
                                (f"%{ch_id_clean}%",)
                            ) as _cur:
                                _row = await _cur.fetchone()
                        if _row and str(_row[0]).startswith('https://'):
                            ch_link = _row[0]
                    except Exception as e:
                        engine._dbg("main.watch_alert_sender", e)
                extra = (f"\n🔗 Link: {ch_link}\n🆔 Kanal ID: `{ch_id_fmt}`" if ch_link
                         else f"\n🆔 Kanal ID: `{ch_id_fmt}`")

            msg_text = (
                f"🚨 **KUZATILADIGAN MUSIQA TOPILDI!**\n\n"
                f"🎵 Musiqa: `{alert['watch_name']}`\n"
                f"{'👤' if source_type == 'profil' else '📢'} "
                f"{'Profil' if source_type == 'profil' else 'Kanal'}: "
                f"`{source_name}`\n"
                f"🆔 ID: `{source_id}`\n"
                f"🎯 O\'xshashlik: `{alert['score']}%`\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                + extra
            )

            # Barcha adminlarga xabar yuborish
            all_admins = set()
            all_admins.add(SUPER_ADMIN_ID)
            all_admins.update(EXTRA_ADMIN_IDS)
            async with db_mod.connect(db_mod.DB_NAME, timeout=30) as _db:
                async with _db.execute("SELECT admin_id FROM trusted_admins") as _cur:
                    for (aid,) in await _cur.fetchall():
                        all_admins.add(aid)

            try:
                await music.save_watch_alert_log(
                    alert['watch_name'], source_name, source_id,
                    source_type, alert['score']
                )
            except Exception as e:
                engine._dbg("main.watch_alert_sender", e)

            for admin_id in all_admins:
                try:
                    await bot.send_message(admin_id, msg_text)
                    await asyncio.sleep(0.1)  # Telegram 30 msg/s chegarasidan o'tmaslik
                except Exception as e:
                    engine._dbg("main.watch_alert_sender", e)
        except Exception as e:
            engine._dbg("main.watch_alert_sender", e)
        finally:
            engine._WATCH_ALERTS.task_done()

    # ── Alert xabarlari uchun handler ──────────────────────────────
    # (yuqoridagi loop dan keyin hech qachon yetib kelmaydi, lekin xavfsizlik uchun)


async def _handle_alert_queue_item(item):
    """Alert queue dan kelgan xabarni qayta ishlash."""
    try:
        kind, data = item
        if kind == 'alert':
            await process_alert_hits([data])
    except Exception as e:
        engine._dbg("main._handle_alert_queue_item", e)


@bot.on(events.NewMessage(pattern='/clean_tmp'))
async def clean_tmp(event):
    if not await is_admin(event.sender_id):
        return
    import glob
    files = glob.glob(os.path.join(BASE_DIR, "tmp_*.ogg"))
    count = 0
    for f in files:
        try:
            os.remove(f)
            count += 1
        except Exception as e:
            engine._dbg("main.clean_tmp", e)
    await event.respond(f"🗑 {count} ta vaqtinchalik fayl o\'chirildi.")


@bot.on(events.NewMessage(pattern='/check_media'))
async def check_media(event):
    if not await is_admin(event.sender_id):
        return
    # .env dagi birinchi manbadan oxirgi 20 ta xabarni tekshirish
    status = await event.respond("🔍 Tekshirilmoqda...")
    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            # Avval joined kanallardan qidirish
            async with db.execute(
                "SELECT channel_id FROM hidden_channel_knocker "
                "WHERE status='joined' LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            # Topilmasa open_channels dan
            if not row:
                async with db.execute(
                    "SELECT DISTINCT open_channels FROM users_memory_bank "
                    "WHERE open_channels IS NOT NULL AND open_channels != '' "
                    "AND open_channels != 'Yo\'q' LIMIT 1"
                ) as cur:
                    row = await cur.fetchone()
        
        if not row:
            await status.edit("❌ Manba topilmadi.")
            return
        
        source = row[0]
        entity = await userbot.get_entity(source)
        
        lines = ["📊 **Oxirgi 50 ta xabar tahlili:**\n"]
        audio_count = 0
        total = 0
        async for msg in userbot.iter_messages(entity, limit=50):
            total += 1
            if msg.audio or msg.voice or msg.document:
                media = msg.audio or msg.voice or msg.document
                mime = getattr(media, 'mime_type', 'unknown')
                size = getattr(media, 'size', 0)
                duration = getattr(media, 'duration', 0)
                lines.append(
                    f"`{msg.id}` | {mime} | {size//1024} KB | {duration}s"
                )
                audio_count += 1

        lines.append(f"\n📊 Jami: {total} xabar, {audio_count} ta media")
        await status.edit("\n".join(lines[:25]))
    except Exception as e:
        await status.edit(f"❌ Xatolik: {e}")


@bot.on(events.NewMessage(pattern=r'/rescan_channel (.+)'))
async def rescan_channel_cmd(event):
    if not await is_admin(event.sender_id):
        return
    target = _pm(event, 1).strip()
    status = await event.respond(f"🔄 `{target}` qayta skanerlanmoqda...")

    try:
        entity = await userbot.get_entity(target)
        channel_id = str(entity.id)
        channel_name = getattr(entity, 'title', target)

        # Progressni o'chirish — to'liq qayta skanerlash uchun
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            try:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS music_channel_progress "
                    "(channel_id TEXT PRIMARY KEY, last_msg_id INTEGER)"
                )
                await db.execute(
                    "DELETE FROM music_channel_progress WHERE channel_id=?",
                    (channel_id,)
                )
                await db.commit()
            except Exception as e:
                engine._dbg("main.rescan_channel_cmd", e)

        await status.edit(
            f"🎵 **`{channel_name}`** qayta skanerlanmoqda...\n"
            f"Jami xabarlar tekshiriladi. Biroz kuting."
        )

        asyncio.create_task(_do_rescan_channel(event.sender_id, entity, channel_id, channel_name, target))

    except Exception as e:
        await status.edit(f"❌ Xatolik: `{e}`")


async def _do_rescan_channel(sender_id, entity, channel_id, channel_name, target):
    """Kanalning barcha musiqalarini qayta skanerlash"""
    import music_scanner as music_mod_local
    await music_mod_local.init_music_db()
    BASE_DIR_LOCAL = os.path.dirname(os.path.abspath(__file__))
    count = 0
    total_audio = 0

    try:
        # Barcha audio xabarlarni yig'ish
        audio_msgs = []
        async for msg in userbot.iter_messages(entity, limit=None):
            if engine.is_music_file(msg):
                audio_msgs.append(msg)
        total_audio = len(audio_msgs)

        await bot.send_message(
            sender_id,
            f"🎵 `{channel_name}` da `{total_audio}` ta musiqa topildi. Skanerlanmoqda..."
        )

        async def _dl(m):
            tmp = os.path.join(BASE_DIR_LOCAL, f"tmp_rescan_{channel_id}_{m.id}.ogg")
            for attempt in range(3):
                try:
                    await m.download_media(file=tmp)
                    if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                        return (m.id, tmp)
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    if os.path.exists(tmp):
                        os.remove(tmp)
            return (m.id, None)

        BATCH = 5
        for i in range(0, len(audio_msgs), BATCH):
            batch = audio_msgs[i:i+BATCH]
            results = await asyncio.gather(*[_dl(m) for m in batch])
            for msg_id, tmp in results:
                if tmp and os.path.exists(tmp):
                    try:
                        fp, dur = await engine.music_mod.get_fingerprint_async(tmp)
                        if fp:
                            await music_mod_local.save_fingerprint(
                                channel_id, channel_name,
                                f"msg_{msg_id}", fp, dur or 0
                            )
                            count += 1
                    except Exception as e:
                        engine._dbg("main._dl", e)
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)

        await bot.send_message(
            sender_id,
            f"✅ **`{channel_name}`** qayta skanerlash yakunlandi!\n\n"
            f"🎵 Jami musiqa: `{total_audio}` ta\n"
            f"🎶 Fingerprint saqlandi: `{count}` ta"
        )

    except Exception as e:
        await bot.send_message(sender_id, f"❌ Xatolik: `{e}`")


@bot.on(events.NewMessage(pattern='/test_fpcalc'))
async def test_fpcalc(event):
    if not await is_admin(event.sender_id):
        return
    import subprocess, glob
    fpcalc_paths = [
        os.path.join(BASE_DIR, "fpcalc.exe"),
        os.path.join(BASE_DIR, "fpcalc"),
        "fpcalc.exe", "fpcalc"
    ]
    found_path = next((p for p in fpcalc_paths if os.path.exists(p)), None)
    if not found_path:
        await event.respond(
            "❌ **fpcalc.exe topilmadi!**\n" +
            "\n".join(f"`{p}`" for p in fpcalc_paths[:2])
        )
        return
    try:
        result = subprocess.run([found_path, "-version"], capture_output=True, text=True, timeout=10)
        await event.respond(
            f"✅ **fpcalc topildi!**\n"
            f"📁 Yo\'li: `{found_path}`\n"
            f"📋 Versiya: `{result.stdout.strip() or result.stderr.strip()}`"
        )
    except Exception as e:
        await event.respond(f"❌ fpcalc ishga tushmadi: `{e}`")


@bot.on(events.NewMessage(pattern=r'/check_profile (\d+)'))
async def check_profile_music(event):
    if not await is_admin(event.sender_id):
        return
    uid = int(_pm(event, 1))
    try:
        fi = await userbot(engine.GetFullUserRequest(uid))
        fu = fi.full_user
        fields = []
        for attr in ['saved_music', 'profile_song', 'profile_songs', 'music']:
            val = getattr(fu, attr, None)
            if val is not None:
                fields.append(f"`{attr}`: {type(val).__name__}")
        if fields:
            await event.respond(f"🎵 **Profil {uid} musiqa fieldlari:**\n\n" + "\n".join(fields))
        else:
            await event.respond(f"📭 Profil {uid} da musiqa field topilmadi")
    except Exception as e:
        await event.respond(f"❌ Xatolik: {e}")


@bot.on(events.NewMessage(pattern='/rescan_music'))
async def rescan_music_handler(event):
    if not await is_admin(event.sender_id):
        return
    status = await event.respond(
        "🎵 Barcha profillar musiqasi qayta skanerlanmoqda...\n"
        "Fon da ishlaydi."
    )
    asyncio.create_task(_rescan_all_profiles_music(event.sender_id, status))


async def _rescan_all_profiles_music(sender_id, status_msg):
    count = 0
    found = 0
    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            async with db.execute("SELECT DISTINCT user_id FROM users_memory_bank") as cur:
                users = await cur.fetchall()
        total = len(users)
        await status_msg.edit(f"🎵 Jami `{total}` ta profil tekshiriladi...")
        for (uid,) in users:
            try:
                fi = await engine._safe_api_call(lambda: userbot(engine.GetFullUserRequest(uid)))
                if fi is None:
                    count += 1
                    continue
                fu = fi.full_user
                music_docs = []
                for field in ['saved_music', 'profile_song', 'profile_songs', 'music']:
                    val = getattr(fu, field, None)
                    if val is None: continue
                    if isinstance(val, list): music_docs.extend(val)
                    else: music_docs.append(val)
                for idx, doc in enumerate(music_docs):
                    if hasattr(doc, 'document'): doc = doc.document
                    if not hasattr(doc, 'id'): continue
                    tmp_path = os.path.join(BASE_DIR, f"tmp_rescan_{uid}_{idx}.ogg")
                    try:
                        await userbot.download_media(doc, file=tmp_path)
                        if os.path.exists(tmp_path):
                            fp, duration = await engine.music_mod.get_fingerprint_async(tmp_path)
                            if fp:
                                already = await music.is_profile_music_saved(uid, fp)
                                if not already:
                                    await music.init_music_db()
                                    await music.save_fingerprint(str(uid), f"Profil: {uid}", f"profile_{uid}_{idx}", fp, duration or 0)
                                    found += 1
                    except Exception as e:
                        engine._dbg("main._rescan_all_profiles_music", e)
                    finally:
                        if os.path.exists(tmp_path): os.remove(tmp_path)
                count += 1
                if count % 100 == 0:
                    try:
                        await status_msg.edit(f"🎵 `{count}/{total}` tekshirildi | 🎶 Topildi: `{found}` ta")
                    except Exception as e:
                        engine._dbg("main._rescan_all_profiles_music", e)
            except Exception as e:
                engine._dbg("main._rescan_all_profiles_music", e)
            await asyncio.sleep(0.3)
        await bot.send_message(sender_id,
            f"✅ **Musiqa rescan yakunlandi!**\n\n"
            f"👥 Tekshirildi: `{count}` ta\n"
            f"🎶 Topildi: `{found}` ta")
        try: await status_msg.delete()
        except: pass
    except Exception as e:
        await bot.send_message(sender_id, f"❌ Xatolik: {e}")


@bot.on(events.NewMessage(pattern=r'/test_channel (.+)'))
async def test_channel_cmd(event):
    if not await is_admin(event.sender_id):
        return
    target = _pm(event, 1).strip()
    status = await event.respond(f"🔍 `{target}` tekshirilmoqda...")

    try:
        entity = await userbot.get_entity(target)
        channel_name = getattr(entity, 'title', target)

        total = 0
        audio_count = 0
        async for msg in userbot.iter_messages(entity, limit=None):
            total += 1
            if engine.is_music_file(msg):
                audio_count += 1

        await status.edit(
            f"📊 **`{channel_name}` tahlili:**\n\n"
            f"📨 Jami xabarlar: `{total}` ta\n"
            f"🎵 Musiqa fayllar: `{audio_count}` ta\n\n"
            f"Bot shu `{audio_count}` ta musiqani fingerprint qiladi."
        )
    except Exception as e:
        await status.edit(f"❌ Xatolik: {e}")


@bot.on(events.NewMessage(pattern='/check_knocker'))
async def check_knocker(event):
    if not await is_admin(event.sender_id):
        return
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM hidden_channel_knocker WHERE status='pending'"
        ) as cur:
            pending = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM hidden_channel_knocker WHERE status='pending' AND userbot_idx IS NULL"
        ) as cur:
            unassigned = (await cur.fetchone())[0]
        per_bot = []
        for idx in range(4):
            async with db.execute(
                "SELECT COUNT(*) FROM hidden_channel_knocker WHERE status='pending' AND userbot_idx=?",
                (idx,)
            ) as cur:
                cnt = (await cur.fetchone())[0]
                if cnt > 0:
                    per_bot.append((idx, cnt))
        async with db.execute(
            "SELECT channel_id, last_request_time, userbot_idx FROM hidden_channel_knocker "
            "WHERE status='pending' ORDER BY last_request_time ASC LIMIT 5"
        ) as cur:
            samples = await cur.fetchall()

    lines = [
        f"🔍 **Knocker holati:**\n",
        f"📥 Jami pending: `{pending}` ta",
    ]
    from datetime import datetime as _dt2
    today_str2 = _dt2.now().strftime("%Y-%m-%d")
    async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db2:
        today_sends = {}
        for idx, _ in per_bot:
            ts = (await (await _db2.execute(
                "SELECT COUNT(*) FROM hidden_channel_knocker WHERE userbot_idx=? AND last_request_time LIKE ?",
                (idx, today_str2 + "%")
            )).fetchone())[0]
            today_sends[idx] = ts
    for idx, cnt in per_bot:
        lines.append(f"🤖 Userbot{idx+1}: `{cnt}` kanal | Bugun: `{today_sends.get(idx,0)}` so'rovnoma")
    if unassigned:
        lines.append(f"⚠️ Tayinlanmagan: `{unassigned}` ta")
    lines += [
        f"⏱ Interval: `{engine.KNOCK_INTERVAL // 60}` daqiqa",
        f"⚡️ MONITORING_PAUSED: `{engine.MONITORING_PAUSED}`",
    ]
    if samples:
        lines.append(f"\n**Navbatdagi ({len(samples)} ta):**")
        for ch_id, last_req, ub_idx in samples:
            from datetime import datetime as dt
            elapsed = "—"
            if last_req:
                try:
                    diff = (dt.now() - dt.strptime(last_req, "%Y-%m-%d %H:%M")).total_seconds()
                    elapsed = f"{int(diff//3600)}s {int((diff%3600)//60)}d o\'tgan"
                except:
                    elapsed = last_req
            ub_label = f"UB{ub_idx+1}" if ub_idx is not None else "?"
            lines.append(f"  [{ub_label}] `{ch_id[:28]}` — {elapsed}")
    else:
        lines.append("\n✅ Navbatda pending kanal yo'q")

    await event.respond("\n".join(lines))


@bot.on(events.NewMessage(pattern='/reassign_knocker'))
async def reassign_knocker_cmd(event):
    """Barcha pending kanallarni N ta userbotga qayta taqsimlaydi."""
    if not await is_admin(event.sender_id):
        return
    n = len(engine._ALL_USERBOTS)
    if n == 0:
        await event.respond("❌ Userbotlar yuklanmagan.")
        return
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        rows = await (await db.execute(
            "SELECT rowid FROM hidden_channel_knocker WHERE status='pending'"
        )).fetchall()
        if not rows:
            await event.respond("📭 Pending kanallar yo'q.")
            return
        counts = [0] * n
        for i, (rowid,) in enumerate(rows):
            idx = i % n
            await db.execute(
                "UPDATE hidden_channel_knocker SET userbot_idx=? WHERE rowid=?",
                (idx, rowid)
            )
            counts[idx] += 1
        await db.commit()
    dist = " | ".join(f"UB{i+1}:{c}" for i, c in enumerate(counts))
    await event.respond(f"✅ {len(rows)} ta kanal qayta taqsimlandi:\n{dist}")


@bot.on(events.NewMessage(pattern='/test_knock'))
async def test_knock(event):
    if not await is_admin(event.sender_id):
        return
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT channel_id FROM hidden_channel_knocker WHERE status='pending' LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        await event.respond("❌ Pending kanal yo'q")
        return
    ch = row[0]
    await event.respond(f"🔄 Test: `{ch}` ga so'rovnoma yuborilmoqda...")
    try:
        if "/+" in ch:
            hash_part = ch.split("/+")[-1].rstrip("/")
        elif "joinchat/" in ch:
            hash_part = ch.split("joinchat/")[-1].rstrip("/")
        else:
            await event.respond(f"❌ Format noto'g'ri: `{ch}`")
            return
        from telethon.tl.functions.messages import ImportChatInviteRequest
        result = await userbot(ImportChatInviteRequest(hash=hash_part))
        await event.respond(f"✅ So'rovnoma yuborildi! Natija: `{type(result).__name__}`")
    except Exception as e:
        err = str(e).lower()
        if "already" in err or "request" in err:
            await event.respond(f"✅ So'rovnoma avval yuborilgan yoki qabul qilindi\n`{type(e).__name__}`")
        elif "flood" in err:
            await event.respond(f"⏳ FloodWait: `{e}`")
        elif "expired" in err or "invalid" in err:
            await event.respond(f"❌ Link eskirgan yoki noto'g'ri: `{type(e).__name__}`")
        else:
            await event.respond(f"❌ Noma'lum xato: `{type(e).__name__}: {e}`")


# ─────────────────────────────────────────────────────────────────────
# 🛡 HAVOLA TEKSHIRISH — Phishing/Scam aniqlovchi
# ─────────────────────────────────────────────────────────────────────

_PHISHING_WAIT = set()   # havola kutilayotgan foydalanuvchilar

@bot.on(events.NewMessage(pattern="🛡 Havola Tekshirish"))
async def btn_phishing(event):
    if not await is_admin(event.sender_id):
        return
    _PHISHING_WAIT.add(event.sender_id)
    await event.respond(
        "🛡 **Kiberxavfsizlik Tekshiruvi**\n\n"
        "Quyidagilardan birini yuboring:\n"
        "🔗 Shubhali havola yoki xabar (forward)\n"
        "📱 APK fayl — ruxsatlar, yashirin serverlar\n"
        "🎵 OGG fayl — niqoblangan fayl yoki yashirin ma'lumot\n\n"
        "**Havola tekshiruvi:**\n"
        "🦠 VirusTotal — 70+ antivirus bazasi\n"
        "☣️ URLhaus — malware/phishing bazasi\n"
        "🛡 SSL, WHOIS, yo'naltirish zanjiri\n"
        "🇺🇿 O'zbek brendlari taqlidi (Payme, Click, Humo...)\n"
        "📱 Telegram kanal/bot tahlili\n\n"
        "**APK tekshiruvi:**\n"
        "🔐 Xavfli ruxsatlar (SMS, mikrofon, kamera...)\n"
        "🌐 Ma'lumot uzatiladigan serverlar aniqlash\n"
        "🤖 Telegram bot API ga ma'lumot oqishi\n\n"
        "**OGG tekshiruvi:**\n"
        "🎭 Niqoblangan fayl aniqlash (EXE, APK...)\n"
        "🏷 Yashirin metadata tekshiruvi\n\n"
        "📊 Natija: 0–100 ballik xavf tizimi",
        buttons=[[Button.text("❌ Bekor Qilish")]]
    )

async def _run_phishing_check(sender_id, message):
    """Fon task: URL / APK / OGG fayllarni tahlil qilib natijani yuboradi."""
    try:
        # ── APK yoki OGG fayl yuborilganmi tekshirish ────────────────
        doc = getattr(message, 'document', None) or getattr(message, 'file', None)
        media = getattr(message, 'media', None)
        if media and hasattr(media, 'document'):
            doc = media.document

        if doc is not None:
            # Fayl nomi va turini aniqlash
            fname = ""
            if hasattr(doc, 'attributes'):
                for attr in doc.attributes:
                    if hasattr(attr, 'file_name'):
                        fname = attr.file_name or ""
                        break
            fname_lower = fname.lower()
            is_apk = fname_lower.endswith('.apk')
            is_ogg = fname_lower.endswith(('.ogg', '.oga', '.opus'))

            if is_apk or is_ogg:
                ftype = "APK" if is_apk else "OGG/Audio"
                await bot.send_message(sender_id,
                    f"⏳ **{ftype} fayl tahlil qilinmoqda...**\n"
                    f"📎 `{fname}`\n"
                    f"_(Yuklab olinmoqda...)_"
                )
                # Fayl nomi foydalanuvchidan keladi — Windows uchun xavfli
                # belgilarni (: * ? " < > | \ /) tozalaymiz, aks holda
                # download_media OSError beradi yoki path traversal bo'ladi.
                _safe_fname = re.sub(r'[^\w.\-]', '_', os.path.basename(fname)) or "file"
                tmp_path = os.path.join(BASE_DIR, f"_phish_tmp_{sender_id}_{_safe_fname}")
                try:
                    await bot.download_media(message, file=tmp_path)
                    if is_apk:
                        report, score = await phish_mod.analyze_apk(tmp_path)
                        header = "📱 **APK Tahlil Hisoboti:**\n\n"
                    else:
                        report, score = await phish_mod.analyze_ogg(tmp_path)
                        header = "🎵 **OGG/Audio Tahlil Hisoboti:**\n\n"
                    try:
                        await bot.send_message(sender_id, header + report, parse_mode='md')
                    except Exception:
                        await bot.send_message(sender_id, header + report)
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception as e:
                        engine._dbg("main._run_phishing_check", e)
                return
            # APK/OGG emas — URL tekshirishga o'tkaziladi (quyida)

        # ── Havola (URL) tekshirish ───────────────────────────────────
        urls = phish_mod.extract_urls_from_telethon_msg(message)
        context_w = phish_mod.check_message_context_telethon(message)

        if not urls and context_w:
            await bot.send_message(sender_id,
                "🚨 **Kiberxavfsizlik ogohlantirishlari:**\n\n"
                "Xabarda havola topilmadi, lekin shubhali belgilar aniqlandi:\n\n"
                + "\n".join(context_w)
                + "\n\n⚠️ _Bu xabarga ishonmang!_"
            )
            return

        if not urls:
            await bot.send_message(sender_id,
                "⚠️ Xabarda hech qanday havola topilmadi.\n"
                "Havola, APK yoki OGG fayl yuboring."
            )
            return

        await bot.send_message(sender_id,
            f"⏳ **{len(urls)} ta havola tahlil qilinmoqda...**\n"
            f"_(VirusTotal, URLhaus, SSL, WHOIS parallel tekshirilmoqda)_"
        )

        for url in list(urls)[:5]:
            report, score, probe_urls = await phish_mod.analyze_url(
                url, userbot=userbot, bot_client=userbot, context_w=context_w
            )
            if report:
                phish_mod.save_stat(url, score)
                text_out = (
                    "🚨 **Kiberxavfsizlik tahlil hisoboti:**\n\n"
                    + report
                    + "\n\n" + "—" * 20 + "\n"
                    "⚠️ _Xavf darajasi 50+ bo'lsa karta yoki parol kiritmang!_"
                )
                try:
                    await bot.send_message(sender_id, text_out, parse_mode='md')
                except Exception:
                    await bot.send_message(sender_id, text_out)

            if probe_urls:
                await bot.send_message(sender_id,
                    f"🤖 **Userbot {len(probe_urls)} ta yashirin havola topdi — tahlil boshlanmoqda...**"
                )
                for probe_url in probe_urls[:3]:
                    if probe_url in urls:
                        continue
                    p_report, p_score, _ = await phish_mod.analyze_url(probe_url, userbot=userbot)
                    if p_report:
                        phish_mod.save_stat(probe_url, p_score)
                        p_out = ("🤖 **Userbot topgan havola tahlili:**\n\n" + p_report
                                 + "\n\n⚠️ _Bot tugmasidagi havola — ayniqsa ehtiyot bo'ling!_")
                        try:
                            await bot.send_message(sender_id, p_out, parse_mode='md')
                        except Exception:
                            await bot.send_message(sender_id, p_out)
    except Exception as e:
        await bot.send_message(sender_id, f"❌ Tahlil xatosi: {e}")
    finally:
        await bot.send_message(sender_id, "✅ Tahlil yakunlandi.",
                               buttons=Button.clear())
        _PHISHING_WAIT.discard(sender_id)
        await bot.send_message(sender_id, "Asosiy menyu:", buttons=MAIN_KEYBOARD)


@bot.on(events.NewMessage(pattern="🔄 Botni Qayta Yuklash"))
async def btn_restart(event):
    if not await is_admin(event.sender_id):
        return
    await show_control_panel(event)


async def show_control_panel(event_or_msg, edit=False):
    """Nazorat panelini ko'rsatish"""
    r = engine._RESOURCE
    paused = engine.MONITORING_PAUSED
    music_paused = r['music_paused']
    profile_slow = r['profile_slow']
    heavy = r['heavy_scan']

    # Holat
    if paused:
        holat = "⏸ HAMMASI TO'XTATILGAN"
    elif heavy:
        holat = f"🔴 {r['current_task']} ishlayapti"
    else:
        holat = "🟢 NORMAL ISHLAYAPTI"

    navbat = _SCAN_QUEUE.qsize() if _SCAN_QUEUE else 0

    text = (
        f"🎛 **BOT NAZORAT PANELI**\n\n"
        f"⚙️ Holat: **{holat}**\n"
        f"📋 Navbat: `{navbat}` ta topshiriq\n\n"
        f"**Fon jarayonlar:**\n"
        f"{'⏸' if paused else '▶️'} Skanerlash\n"
        f"{'⏸' if music_paused else '▶️'} Musiqa tracker\n"
        f"{'🐢' if profile_slow else '▶️'} Profil tracker\n"
        f"{'⏸' if paused else '▶️'} So\'rovnoma tizimi\n"
    )

    buttons = [
        [Button.inline("🔄 Qayta Yuklash", data="bot_restart")],
        [
            Button.inline("⏸ Hammasini To'xtatish", data="ctrl_pause_all") if not paused
            else Button.inline("▶️ Hammasini Yoqish", data="ctrl_resume_all")
        ],
        [
            Button.inline("⏸ Musiqa Tracker", data="ctrl_pause_music") if not music_paused
            else Button.inline("▶️ Musiqa Tracker", data="ctrl_resume_music"),
            Button.inline("⏸ Profil Tracker", data="ctrl_pause_profile") if not profile_slow
            else Button.inline("▶️ Profil Tracker", data="ctrl_resume_profile"),
        ],
        [Button.inline("🗑 Navbatni Tozalash", data="ctrl_clear_queue")],
        [Button.inline("🔄 Yangilash", data="ctrl_refresh")],
    ]

    if edit:
        try:
            await event_or_msg.edit(text, buttons=buttons)
        except Exception as e:
            engine._dbg("main.show_control_panel", e)
    else:
        await event_or_msg.respond(text, buttons=buttons)


@bot.on(events.NewMessage(pattern=r"^/fix_links$"))
async def cmd_fix_links(event):
    if not await is_admin(event.sender_id):
        return
    status = await event.respond(
        "🔄 **Migratsiya boshlandi**\n"
        "`https://t.me/c/ID/1` → `https://t.me/username` formatga o'tkazilmoqda...\n"
        "Bu biroz vaqt olishi mumkin."
    )
    asyncio.create_task(_run_fix_links(event.sender_id, status))


async def _run_fix_links(sender_id, status_msg):
    try:
        updated, total, unique_res, prog_upd = await engine.migrate_pc_links(
            userbot, bot=bot, admin_id=sender_id
        )
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main._run_fix_links", e)
        await bot.send_message(
            sender_id,
            f"✅ **Migratsiya yakunlandi!**\n\n"
            f"📊 Tekshirilgan qatorlar: `{total}` ta\n"
            f"✏️ O'zgartirildi: `{updated}` ta\n"
            f"🔗 Unikal kanal IDlar hal qilindi: `{unique_res}` ta\n"
            f"🎵 Musiqa progress yangilandi: `{prog_upd}` ta"
        )
    except Exception as e:
        await bot.send_message(sender_id, f"❌ Xatolik: {e}")


@bot.on(events.CallbackQuery(pattern=b"bot_restart"))
async def bot_restart_cb(event):
    if not await is_admin(event.sender_id):
        return
    await event.answer("🔄 Qayta yuklanmoqda...")
    await event.edit("🔄 **Bot qayta yuklanmoqda...**")
    await asyncio.sleep(1)
    try:
        await userbot.disconnect()
        await bot.disconnect()
    except Exception as e:
        engine._dbg("main.bot_restart_cb", e)
    # os.execv Windowsda bo'sh joyli yo'l ('Новая папка') bilan ishonchsiz
    # va .session fayllari hali band bo'lishi mumkin. Yangi jarayon ochib,
    # eskisini to'liq yopamiz.
    import subprocess
    await asyncio.sleep(1)  # session DB qulflari bo'shashi uchun
    subprocess.Popen([sys.executable, *sys.argv])
    os._exit(0)


@bot.on(events.CallbackQuery(pattern=b"ctrl_pause_all"))
async def ctrl_pause_all(event):
    if not await is_admin(event.sender_id):
        return
    engine.MONITORING_PAUSED = True
    engine._RESOURCE['music_paused'] = True
    engine._RESOURCE['profile_slow'] = True
    await event.answer("⏸ Hammasi to'xtatildi!")
    await show_control_panel(event, edit=True)


@bot.on(events.CallbackQuery(pattern=b"ctrl_resume_all"))
async def ctrl_resume_all(event):
    if not await is_admin(event.sender_id):
        return
    engine.MONITORING_PAUSED = False
    engine._SCAN_COUNT = 0
    engine._RESOURCE['music_paused'] = False
    engine._RESOURCE['profile_slow'] = False
    engine._RESOURCE['heavy_scan'] = False
    engine._RESOURCE['current_task'] = None
    await event.answer("▶️ Hammasi yoqildi!")
    await show_control_panel(event, edit=True)


@bot.on(events.CallbackQuery(pattern=b"ctrl_pause_music"))
async def ctrl_pause_music(event):
    if not await is_admin(event.sender_id):
        return
    engine._RESOURCE['music_paused'] = True
    await event.answer("⏸ Musiqa tracker to'xtatildi!")
    await show_control_panel(event, edit=True)


@bot.on(events.CallbackQuery(pattern=b"ctrl_resume_music"))
async def ctrl_resume_music(event):
    if not await is_admin(event.sender_id):
        return
    engine._RESOURCE['music_paused'] = False
    await event.answer("▶️ Musiqa tracker yoqildi!")
    await show_control_panel(event, edit=True)


@bot.on(events.CallbackQuery(pattern=b"ctrl_pause_profile"))
async def ctrl_pause_profile(event):
    if not await is_admin(event.sender_id):
        return
    engine._RESOURCE['profile_slow'] = True
    await event.answer("🐢 Profil tracker sekinlashtirildi!")
    await show_control_panel(event, edit=True)


@bot.on(events.CallbackQuery(pattern=b"ctrl_resume_profile"))
async def ctrl_resume_profile(event):
    if not await is_admin(event.sender_id):
        return
    engine._RESOURCE['profile_slow'] = False
    await event.answer("▶️ Profil tracker tezlashtirildi!")
    await show_control_panel(event, edit=True)


@bot.on(events.CallbackQuery(pattern=b"ctrl_clear_queue"))
async def ctrl_clear_queue(event):
    if not await is_admin(event.sender_id):
        return
    count = _SCAN_QUEUE.qsize() if _SCAN_QUEUE else 0
    while _SCAN_QUEUE and not _SCAN_QUEUE.empty():
        try:
            _SCAN_QUEUE.get_nowait()
            _SCAN_QUEUE.task_done()
        except asyncio.QueueEmpty:
            break
    await event.answer(f"🗑 {count} ta topshiriq o'chirildi!")
    await show_control_panel(event, edit=True)


@bot.on(events.CallbackQuery(pattern=b"ctrl_refresh"))
async def ctrl_refresh(event):
    if not await is_admin(event.sender_id):
        return
    await event.answer("🔄 Yangilandi!")
    await show_control_panel(event, edit=True)


@bot.on(events.CallbackQuery(pattern=b"bot_pause"))
async def bot_pause_callback(event):
    if not await is_admin(event.sender_id):
        return
    engine.MONITORING_PAUSED = True
    await event.answer("⏸ To\'xtatildi!")
    await show_control_panel(event, edit=True)


@bot.on(events.CallbackQuery(pattern=b"bot_resume"))
async def bot_resume_callback(event):
    if not await is_admin(event.sender_id):
        return
    engine.MONITORING_PAUSED = False
    engine._SCAN_COUNT = 0
    await event.answer("▶️ Davom ettirildi!")
    await show_control_panel(event, edit=True)


# Skanerlash navbati — asyncio.Queue, main() da ishga tushiriladi
_SCAN_QUEUE: asyncio.Queue = None
_last_scan_end_time: float = 0.0   # oxirgi skan tugagan vaqt (Unix timestamp)
SCAN_COOLDOWN: int = 15 * 60       # skanerlashlar orasidagi minimal tanaffus (15 daqiqa)


async def scan_queue_runner():
    """Navbatdagi skanerlashlarni ketma-ket bajaradi (polling yo'q)."""
    while True:
        item = await _SCAN_QUEUE.get()
        try:
            # Oldingi skanerlash tugashini kutish
            while engine._SCAN_COUNT > 0 or engine.MONITORING_PAUSED:
                await asyncio.sleep(2)

            # 15 daqiqalik tanaffus tekshiruvi
            scan_type, sender_id, target = item
            elapsed = time.time() - _last_scan_end_time
            if elapsed < SCAN_COOLDOWN:
                wait_secs = int(SCAN_COOLDOWN - elapsed)
                mins = wait_secs // 60
                secs = wait_secs % 60
                await bot.send_message(
                    sender_id,
                    f"⏳ **Tanaffus vaqti**\n\n"
                    f"Keyingi skanerlashgacha: **{mins} daqiqa {secs} soniya**\n"
                    f"Telegram API cheklovidan himoya uchun kutilmoqda..."
                )
                await asyncio.sleep(wait_secs)
                await bot.send_message(sender_id, "✅ Tanaffus tugadi, skanerlash boshlanmoqda...")
            try:
                await bot.send_message(
                    sender_id,
                    f"▶️ Navbatdagi skanerlash boshlandi: `{target}`"
                )
                if scan_type == 'auto':
                    status = await bot.send_message(sender_id, "⏳ Skanerlash boshlandi, tur aniqlanmoqda...")
                    asyncio.create_task(run_auto_scan(sender_id, target, status))
                elif scan_type == 'msg':
                    fname  = f"Msg_OSINT_{datetime.now().strftime('%d_%H%M')}.xlsx"
                    fpath  = os.path.join(BASE_DIR, fname)
                    status = await bot.send_message(sender_id, "⏳ Yopiq guruh xabarlari o'qilmoqda...")
                    asyncio.create_task(run_msg_scan(sender_id, target, fpath, status))
                elif scan_type == 'group':
                    fname  = f"Group_OSINT_{datetime.now().strftime('%d_%H%M')}.xlsx"
                    fpath  = os.path.join(BASE_DIR, fname)
                    status = await bot.send_message(sender_id, "⏳ Skanerlash boshlandi...")
                    asyncio.create_task(run_background_scan(sender_id, target, fpath, status))
                elif scan_type == 'comment':
                    fname  = f"Comment_OSINT_{datetime.now().strftime('%d_%H%M')}.xlsx"
                    fpath  = os.path.join(BASE_DIR, fname)
                    status = await bot.send_message(sender_id, "⏳ Kanal commentariyalari o'qilmoqda...")
                    asyncio.create_task(run_comment_scan(sender_id, target, fpath, status))
                elif scan_type == 'keyword':
                    keyword, days, st = target
                    asyncio.create_task(run_keyword_search(sender_id, keyword, st, days))
            except Exception as e:
                print(f"scan_queue_runner xatosi: {e}")
        finally:
            _SCAN_QUEUE.task_done()


@bot.on(events.CallbackQuery(pattern=b"watch_archive"))
async def watch_archive_callback(event):
    if not await is_admin(event.sender_id):
        return
    await event.answer()
    logs = await music.get_watch_alerts_log(limit=1000)
    if not logs:
        await event.respond("📋 Arxiv bo\'sh — hali hech narsa topilmagan.")
        return

    # Excel tayyorlash
    wb = openpyxl.Workbook()
    sh = wb.active
    sh.title = "Topilganlar Arxivi"
    sh.append([
        "№", "Musiqa Nomi", "Manba Turi", "Kanal/Profil",
        "ID", "Ism", "Username", "Telefon", "Manba Guruh",
        "O'xshashlik %", "Topilgan Sana"
    ])

    # Barcha foydalanuvchi ma'lumotlarini bitta ulanishda oldindan yuklab olish
    all_src_ids = list({src_id for (_, _, src_id, _, _, _) in logs})
    user_data_map = {}
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        for _sid in all_src_ids:
            async with db.execute(
                "SELECT first_name, username, phone, group_link "
                "FROM users_memory_bank WHERE user_id=? LIMIT 1", (_sid,)
            ) as cur:
                _urow = await cur.fetchone()
            if _urow:
                user_data_map[_sid] = _urow

    for i, (w_name, src_name, src_id, src_type, score, found_date) in enumerate(logs, 1):
        manba = "👤 Profil" if src_type == "profil" else "📢 Kanal"

        fname = uname = phone = grp_link = ""
        _urow = user_data_map.get(src_id)
        if _urow:
            fname = _urow[0] or ""
            uname = ('@' + _urow[1]) if _urow[1] else ""
            phone = ('+' + _urow[2]) if _urow[2] else ""
            grp_link = _urow[3] or ""

        sh.append([
            i, w_name, manba, src_name, src_id,
            fname, uname, phone, grp_link,
            f"{score}%", found_date
        ])

    _loop = asyncio.get_event_loop()
    await _loop.run_in_executor(None, engine.apply_excel_styles, sh, len(logs))

    out_name = f"Watch_Arxiv_{datetime.now().strftime('%d_%H%M')}.xlsx"
    out_path = os.path.join(BASE_DIR, out_name)
    await _loop.run_in_executor(None, wb.save, out_path)

    await bot.send_file(
        event.sender_id, out_path,
        caption=(
            f"📋 **Topilganlar arxivi**\n\n"
            f"🎵 Jami: `{len(logs)}` ta topilgan\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
    )


@bot.on(events.CallbackQuery(pattern=rb"watch_archive_\d+"))
async def watch_archive_detail(event):
    if not await is_admin(event.sender_id):
        return
    w_id = int(event.data.decode().split("_")[-1])
    watches = await music.get_watch_list()
    watch = next((w for w in watches if w[0] == w_id), None)
    if not watch:
        await event.answer("Topilmadi")
        return
    logs = await music.get_watch_alerts_log(watch_name=watch[1], limit=20)
    lines = [f"📋 **`{watch[1]}` arxivi:** `{len(logs)}` ta\n"]
    for w_name, src_name, src_id, src_type, score, found_date in logs:
        icon = "👤" if src_type == "profil" else "📢"
        lines.append(f"{icon} `{src_name}` — {score}% — {found_date}")
    await event.respond("\n".join(lines) if lines else "📭 Bu musiqa hali topilmagan.")


@bot.on(events.NewMessage(pattern='/flood_stats'))
async def flood_stats_cmd(event):
    if not await is_admin(event.sender_id):
        return
    
    stats = engine._FLOOD_STATS
    if not stats:
        await event.respond("✅ Hozircha flood yo'q!")
        return

    # Saralash - eng ko'p flood bergani birinchi
    sorted_stats = sorted(stats.items(), key=lambda x: x[1]['count'], reverse=True)
    
    lines = ["📊 **FLOOD STATISTIKASI:**\n"]
    for func, data in sorted_stats:
        lines.append(
            f"🔴 **{func}**\n"
            f"   Soni: `{data['count']}` ta\n"
            f"   Jami: `{data['total_secs']}` sek\n"
            f"   Eng uzun: `{data['max_secs']}` sek\n"
        )
    
    lines.append("\n📁 To'liq log: `flood_log.txt`")
    await event.respond("\n".join(lines))


@bot.on(events.NewMessage(pattern='/flood_reset'))
async def flood_reset_cmd(event):
    if not await is_admin(event.sender_id):
        return
    engine._FLOOD_STATS.clear()
    # flood_log.txt ni ham tozalash
    log_path = os.path.join(BASE_DIR, 'flood_log.txt')
    if os.path.exists(log_path):
        open(log_path, 'w').close()
    await event.respond("✅ Flood statistikasi tozalandi!")


@bot.on(events.CallbackQuery(pattern=b"watch_rescan"))
async def watch_rescan_callback(event):
    if not await is_admin(event.sender_id):
        return
    await event.answer("🔄 Skanerlash boshlandi...")
    status = await bot.send_message(event.sender_id, "🔄 Bazadagi barcha fingerprintlar kuzatiladigan musiqalar bilan taqqoslanmoqda...")

    watches = await music.get_watch_list()
    if not watches:
        await status.edit("❌ Kuzatiladigan musiqalar yo\'q!")
        return

    total_found = 0
    async with db_mod.connect(music.MUSIC_DB, timeout=30) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS music_fingerprints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT, channel_name TEXT, file_name TEXT,
                fingerprint TEXT, duration REAL, added_date TEXT
            )
        """)
        async with db.execute(
            "SELECT channel_id, channel_name, file_name, fingerprint FROM music_fingerprints"
        ) as cur:
            all_fps = await cur.fetchall()

    # Watch fingerprintlarini pre-parse qilish — har biri 1 marta parse (15 ta)
    watch_fps_parsed = []
    async with db_mod.connect(music.MUSIC_DB, timeout=30) as db:
        async with db.execute("SELECT id, fingerprint FROM watch_fingerprints") as cur:
            for (w_id, w_fp) in await cur.fetchall():
                watch_obj = next((w for w in watches if w[0] == w_id), None)
                w_name = watch_obj[1] if watch_obj else str(w_id)
                try:
                    # numpy mavjud bo'lsa numpy array, aks holda list
                    arr = music.parse_fingerprint(w_fp)
                    watch_fps_parsed.append((w_id, w_name, arr))
                except Exception as e:
                    engine._dbg("main.watch_rescan_callback", e)

    await status.edit(
        f"🔍 `{len(all_fps)}` ta fingerprint `{len(watch_fps_parsed)}` ta musiqa bilan taqqoslanmoqda...\n"
        f"⚡️ Tezlashtirilgan batch rejim (bot muzlamaydi)"
    )

    # Barcha taqqoslashni thread'da bajarish — event loop bloklanmaydi
    _loop = asyncio.get_event_loop()
    matches = await _loop.run_in_executor(
        None,
        music.batch_compare_against_watches,
        watch_fps_parsed,
        all_fps
    )

    for ch_id, ch_name, fname, w_id, w_name, score_pct in matches:
        _src_type = 'profil' if (fname or '').startswith('profile_') else 'kanal'
        total_found += 1
        engine._WATCH_ALERTS.put_nowait({
            'admin_id':    event.sender_id,
            'watch_name':  w_name,
            'score':       score_pct,
            'source_name': ch_name or ch_id,
            'source_id':   ch_id,
            'source_type': _src_type
        })

    try:
        await status.edit(
            f"✅ **Skanerlash yakunlandi!**\n\n"
            f"🎵 Tekshirildi: `{len(all_fps)}` ta fingerprint\n"
            f"🚨 Topildi: `{total_found}` ta mos"
            + (f"\n\nXabarlar yuborilmoqda..." if total_found > 0 else "\n\nMos topilmadi.")
        )
    except Exception:
        await bot.send_message(
            event.sender_id,
            f"✅ **Skanerlash yakunlandi!**\n\n"
            f"🎵 Tekshirildi: `{len(all_fps)}` ta fingerprint\n"
            f"🚨 Topildi: `{total_found}` ta mos"
        )



@bot.on(events.NewMessage(pattern=r"/sync_cache"))
async def cmd_sync_cache(event):
    """Keshni qo'lda yangilash buyrug'i."""
    if not await is_admin(event.sender_id):
        return
    total, sources, last_sync = await engine.get_cache_stats()
    msg = await event.respond(
        f"🔄 **Kesh sinxronlash boshlandi...**\n"
        f"💾 Hozir: `{total:,}` xabar | `{sources}` manba\n"
        f"🕐 Oxirgi: {last_sync or 'hech qachon'}"
    )
    # Fon da ishga tushirish
    async def _do_sync():
        sources_set = set()
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            async with db.execute(
                "SELECT DISTINCT group_link FROM users_memory_bank "
                "WHERE group_link IS NOT NULL AND group_link != ''"
            ) as cur:
                for (link,) in await cur.fetchall():
                    sources_set.add(link)
            async with db.execute(
                "SELECT channel_id FROM hidden_channel_knocker WHERE status='joined'"
            ) as cur:
                for (ch_id,) in await cur.fetchall():
                    sources_set.add(ch_id)
        saved = 0
        for src in sources_set:
            try:
                saved += await engine.sync_source_messages(userbot, src, limit_days=90)
            except Exception as e:
                engine._dbg("main._do_sync", e)
            await asyncio.sleep(2)
        new_total, new_src, new_sync = await engine.get_cache_stats()
        try:
            await msg.edit(
                f"✅ **Kesh yangilandi!**\n"
                f"💾 Jami: `{new_total:,}` xabar | `{new_src}` manba\n"
                f"➕ Yangi saqlandi: `{saved}` ta\n"
                f"🕐 {new_sync}"
            )
        except Exception as e:
            engine._dbg("main._do_sync", e)
    asyncio.create_task(_do_sync())


async def run_id_search(sender_id: int, id_str: str, status_msg):
    """
    ID bo'yicha qidiruv:
    - Manfiy raqam → kanal ID → kanal nomi + linki
    - Musbat raqam → profil ID → u yozgan xabarlar + havolalar
    """
    try:
        num_id = int(id_str.strip())

        # ── KANAL ID (manfiy) ────────────────────────────────────────
        if num_id < 0:
            await status_msg.edit(f"🔍 Kanal `{num_id}` qidirilmoqda...")
            info, link = await engine.lookup_channel_by_id(userbot, num_id)
            if not info:
                await bot.send_message(
                    sender_id,
                    f"❌ `{num_id}` ID li kanal topilmadi.\n"
                    f"💡 Kanal bazada bo'lmasa yoki kirish imkoni yo'q bo'lsa topilmaydi."
                )
                return
            text = (
                f"📢 **Kanal ma'lumoti**\n\n"
                f"🆔 ID: `{num_id}`\n"
                f"📌 Nomi: **{info.get('title') or 'Nomalum'}**\n"
            )
            if info.get('username'):
                text += f"👤 Username: @{info['username']}\n"
            if info.get('members'):
                text += f"👥 A'zolar: `{info['members']:,}` ta\n"
            if link:
                text += f"\n🔗 **Havola:** {link}"
            await bot.send_message(sender_id, text)

        # ── PROFIL ID (musbat) ───────────────────────────────────────
        else:
            await status_msg.edit(f"🔍 Profil `{num_id}` qidirilmoqda...")
            profile, messages = await engine.lookup_user_by_id(num_id)

            if not profile and not messages:
                await bot.send_message(
                    sender_id,
                    f"❌ `{num_id}` ID li foydalanuvchi bazada topilmadi.\n"
                    f"💡 U hali skanerlangan guruhda bo'lmagan bo'lishi mumkin."
                )
                return

            # Profil bloki
            if profile:
                full_name = (profile['first_name'] + " " + profile['last_name']).strip() or "Nomsiz"
                un_part  = f"  @{profile['username']}" if profile['username'] else ""
                ph_part  = f"\n📱 Telefon: `{profile['phone']}`" if profile['phone'] else ""
                bio_part = f"\n📝 Bio: {profile['bio'][:150]}" if profile['bio'] else ""
                oc_part  = f"\n📢 Ochiq kanal: {profile['open_channels']}" if profile['open_channels'] and profile['open_channels'] not in ('', "Yo'q") else ""
                hh_part  = f"\n🔒 Maxfiy kanal: {profile['has_hidden']}" if profile['has_hidden'] and profile['has_hidden'] not in ('', '❌') else ""
                grp_list = "\n".join(f"  • {g}" for g in profile['groups'][:10]) if profile['groups'] else "  —"
                score, slabel, _ = engine.calculate_trust_score(profile, len(messages))
                profile_text = (
                    f"👤 **Profil ma'lumoti**\n"
                    f"🆔 ID: `{num_id}`\n"
                    f"📛 Ism: **{full_name}**{un_part}"
                    f"{ph_part}{bio_part}{oc_part}{hh_part}\n\n"
                    f"📋 **A'zo guruhlari ({len(profile['groups'])} ta):**\n{grp_list}\n\n"
                    f"💬 Xabarlar bazada: `{len(messages)}` ta\n"
                    f"🕐 Qo'shilgan: {profile['added_date']}\n"
                    f"🔐 **Trust Score: {score}/100 — {slabel}**\n"
                    f"{'─' * 30}"
                )
                # Profil rasmini yuborish
                photo_path = await engine.get_profile_photo(userbot, num_id)
                if photo_path and os.path.exists(photo_path):
                    try:
                        await bot.send_file(sender_id, photo_path, caption=profile_text)
                        os.remove(photo_path)
                    except Exception:
                        await bot.send_message(sender_id, profile_text)
                else:
                    await bot.send_message(sender_id, profile_text)

            if not messages:
                await bot.send_message(
                    sender_id,
                    "💬 Bu foydalanuvchi hech qaysi guruhda xabar yozmagan\n"
                    "(yoki xabarlar hali keshlanmagan)."
                )
                return

            # Xabarlar bloki
            chunk = f"💬 **Xabarlar ({len(messages)} ta):**\n\n"
            chunk_num = 1
            for r in messages:
                link_part = f"\n🔗 [O'tish]({r['link']})" if r.get('link') else ""
                line = (
                    f"📍 **{_md_escape(r['source_title'])}**  📅 {r['date']}\n"
                    f"💬 {_md_escape(r['text'])}"
                    f"{link_part}\n"
                    f"{'─' * 20}\n\n"
                )
                if len(chunk) + len(line) > 3800:
                    try:
                        await bot.send_message(sender_id, chunk, link_preview=False)
                    except Exception:
                        await bot.send_message(sender_id, chunk, parse_mode=None, link_preview=False)
                    await asyncio.sleep(0.4)
                    chunk = f"_(davomi {chunk_num + 1})_\n\n" + line
                    chunk_num += 1
                else:
                    chunk += line
            if chunk.strip():
                try:
                    await bot.send_message(sender_id, chunk, link_preview=False)
                except Exception:
                    await bot.send_message(sender_id, chunk, parse_mode=None, link_preview=False)

    except Exception as e:
        await bot.send_message(sender_id, _scan_err(e))
    finally:
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_id_search", e)


# ═════════════════════════════════════════════════════════════════════
# 🔬 TERGOV VOSITALARI
# ═════════════════════════════════════════════════════════════════════

@bot.on(events.NewMessage(pattern=re.compile(r'^(/tergov|🔬 Tergov Vositalari)$')))
async def btn_tergov(event):
    if not await is_admin(event.sender_id):
        return
    await event.respond(
        "🔬 **Tergov Vositalari**\nQuyidagi bo'limdan kerakli amalni tanlang:",
        buttons=[
            # Profil tahlili
            [Button.inline("🎯 Trust Score",       data=b"tv_trust"),
             Button.inline("📸 Profil Rasmi",       data=b"tv_photo")],
            [Button.inline("📅 Faollik Tarixi",     data=b"tv_timeline"),
             Button.inline("📊 Akkaunt Hayoti",     data=b"tv_lifecycle")],
            [Button.inline("✍️ Yozuv Uslubi",       data=b"tv_style"),
             Button.inline("🔄 Uslub Solishtirish", data=b"tv_compare")],
            [Button.inline("📝 O'zgarishlar",       data=b"tv_changes"),
             Button.inline("📄 To'liq Hisobot",     data=b"tv_evidence")],
            # Qidiruv
            [Button.inline("🔍 Username Qidiruv",   data=b"tv_lookup"),
             Button.inline("📞 Telefon Qidiruv",    data=b"tv_phone")],
            # Tahlil
            [Button.inline("👥 Umumiy A'zolar",     data=b"tv_common"),
             Button.inline("🤖 Koordinatsiya",       data=b"tv_coordinated")],
            [Button.inline("⏰ Bir Vaqtda Faol",    data=b"tv_temporal"),
             Button.inline("🗑 O'chirilgan Xabar",  data=b"tv_deleted")],
            [Button.inline("🕸 Tarmoq Xaritasi",    data=b"tv_network")],
            # Tergov ishi
            [Button.inline("➕ Yangi Tergov",       data=b"tv_case_new"),
             Button.inline("📋 Tergovlar Ro'yxati", data=b"tv_case_list")],
            [Button.inline("👤 Shaxs Qo'shish",     data=b"tv_case_add"),
             Button.inline("📄 Tergov Hisoboti",    data=b"tv_case_report")],
            [Button.inline("🗑 Tergovni O'chirish",  data=b"tv_case_del")],
        ]
    )


# ── Tergov vositalari: har bir tugma callback ─────────────────────────

_TV_PROMPTS = {
    "tv_trust":        ("🎯 Trust Score", "Tekshiriladigan shaxs ID sini yuboring:\n`123456789`"),
    "tv_photo":        ("📸 Profil Rasmi", "Shaxs ID sini yuboring:\n`123456789`"),
    "tv_timeline":     ("📅 Faollik Tarixi", "Shaxs ID sini yuboring:\n`123456789`"),
    "tv_lifecycle":    ("📊 Akkaunt Hayoti", "Shaxs ID sini yuboring:\n`123456789`"),
    "tv_style":        ("✍️ Yozuv Uslubi", "Shaxs ID sini yuboring:\n`123456789`"),
    "tv_compare":      ("🔄 Uslub Solishtirish", "Ikkita ID ni bo'sh joy bilan yuboring:\n`ID1 ID2`"),
    "tv_changes":      ("📝 O'zgarishlar", "Shaxs ID sini yuboring:\n`123456789`"),
    "tv_evidence":     ("📄 To'liq Hisobot", "Shaxs ID sini yuboring:\n`123456789`"),
    "tv_lookup":       ("🔍 Username Qidiruv", "Username yuboring:\n`@username`"),
    "tv_phone":        ("📞 Telefon Qidiruv", "Telefon raqam yuboring:\n`+998901234567`"),
    "tv_common":       ("👥 Umumiy A'zolar", "Ikkita guruh havolasini yuboring:\n`@guruh1 @guruh2`"),
    "tv_coordinated":  ("🤖 Koordinatsiya", "Guruh havolasini yuboring:\n`@guruh`"),
    "tv_deleted":      ("🗑 O'chirilgan Xabar", "Guruh havolasini yuboring:\n`@guruh`"),
    "tv_network":      ("🕸 Tarmoq Xaritasi", "IDlarni vergul bilan yuboring:\n`ID1,ID2,ID3`"),
    "tv_case_new":     ("➕ Yangi Tergov", "Tergov nomini yuboring:\n`Tergov nomi`"),
    "tv_case_add":     ("👤 Shaxs Qo'shish", "Tergov ID va shaxsni yuboring:\n`tergov_ID @username_yoki_ID`"),
    "tv_case_report":  ("📄 Tergov Hisoboti", "Tergov ID sini yuboring:\n`1`"),
    "tv_case_del":     ("🗑 Tergovni O'chirish", "O'chiriladigan tergov ID sini yuboring:\n`1`"),
}

@bot.on(events.CallbackQuery(pattern=b"tv_.*"))
async def tv_callback(event):
    if not await is_admin(event.sender_id):
        return
    action = event.data.decode()

    # tv_temporal — input kerak emas, to'g'ridan-to'g'ri ishga tushirish
    if action == "tv_temporal":
        await event.answer()
        status = await event.respond("⏳ Bir vaqtda faol akkauntlar hisoblanmoqda...")
        pairs = await engine.get_temporal_correlations()
        if not pairs:
            await status.edit("📭 Korrelyatsiya topilmadi.")
            return
        lines = ["⏰ **Bir vaqtda faol akkauntlar:**\n"]
        for uid1, uid2, overlap in pairs[:20]:
            lines.append(f"👤 `{uid1}` ↔ `{uid2}` — {overlap} ta mos soat")
        await status.edit("\n".join(lines))
        return

    # tv_case_list — input kerak emas
    if action == "tv_case_list":
        await event.answer()
        cases = await db_mod.get_investigations()
        if not cases:
            await event.respond("📁 Hozircha hech qanday tergov yo'q.\n➕ Yangi tergov ochish uchun tugmani bosing.")
            return
        lines = ["📁 **Tergovlar ro'yxati:**\n"]
        for c in cases:
            lines.append(f"`{c[0]}`. **{c[1]}** — {c[4][:10]}")
        await event.respond("\n".join(lines))
        return

    # Qolgan amallar — input kutish
    if action not in _TV_PROMPTS:
        await event.answer("Noma'lum amal", alert=True)
        return

    title, prompt = _TV_PROMPTS[action]
    USER_STATES[event.sender_id] = {'state': action}
    await event.answer()
    await event.respond(f"**{title}**\n\n{prompt}")


@bot.on(events.NewMessage())
async def tv_input_handler(event):
    """Tergov vositalari uchun input qabul qilish."""
    if not await is_admin(event.sender_id):
        return
    state = USER_STATES.get(event.sender_id)
    if not isinstance(state, dict):
        return
    action = state.get('state', '')
    if not action.startswith('tv_'):
        return

    text = (event.message.text or '').strip()
    if not text:
        return

    USER_STATES[event.sender_id] = None

    # Har bir amal uchun mavjud handler funksiyalarini chaqirish
    fake = event  # event o'zini ishlatamiz
    parts = text.split()

    if action == 'tv_trust':
        fake.message.text = f"/trust {text}"
        await cmd_trust(fake)

    elif action == 'tv_photo':
        fake.message.text = f"/photo {text}"
        await cmd_photo(fake)

    elif action == 'tv_timeline':
        fake.message.text = f"/timeline {text}"
        await cmd_timeline(fake)

    elif action == 'tv_lifecycle':
        fake.message.text = f"/lifecycle {text}"
        await cmd_lifecycle(fake)

    elif action == 'tv_style':
        fake.message.text = f"/style {text}"
        await cmd_style(fake)

    elif action == 'tv_compare':
        fake.message.text = f"/compare {text}"
        await cmd_compare_style(fake)

    elif action == 'tv_changes':
        fake.message.text = f"/changes {text}"
        await cmd_changes(fake)

    elif action == 'tv_evidence':
        fake.message.text = f"/evidence {text}"
        await cmd_evidence(fake)

    elif action == 'tv_lookup':
        fake.message.text = f"/lookup {text}"
        await cmd_lookup(fake)

    elif action == 'tv_phone':
        fake.message.text = f"/phone {text}"
        await cmd_phone(fake)

    elif action == 'tv_common':
        fake.message.text = f"/common {text}"
        await cmd_common(fake)

    elif action == 'tv_coordinated':
        fake.message.text = f"/coordinated {text}"
        await cmd_coordinated(fake)

    elif action == 'tv_deleted':
        fake.message.text = f"/deleted {text}"
        await cmd_deleted(fake)

    elif action == 'tv_network':
        fake.message.text = f"/network {text}"
        await cmd_network(fake)

    elif action == 'tv_case_new':
        fake.message.text = f"/case new {text}"
        await cmd_case_new(fake)

    elif action == 'tv_case_add':
        fake.message.text = f"/case add {text}"
        await cmd_case_add(fake)

    elif action == 'tv_case_report':
        fake.message.text = f"/case report {text}"
        await cmd_case_report(fake)

    elif action == 'tv_case_del':
        fake.message.text = f"/case del {text}"
        await cmd_case_del(fake)


# ─────────────────────────────────────────────────────────────────────
# 🚨 ALERT BOSHQARUVI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=re.compile(r'^(/alert|🚨 Alert Boshqaruvi)$')))
async def btn_alert_mgmt(event):
    if not await is_admin(event.sender_id):
        return
    alerts = await db_mod.list_alerts(event.sender_id)
    if not alerts:
        text = (
            "🚨 **Alert Boshqaruvi**\n\n"
            "Hozircha alert yo'q.\n\n"
            "Qo'shish: `/alert add <kalit_soz>`\n"
            "O'chirish: `/alert del <ID>`\n"
            "Ro'yxat: `/alert list`"
        )
    else:
        lines = ["🚨 **Alertlar ro'yxati:**\n"]
        for a in alerts:
            status = "✅" if a[3] else "⏸"
            grp = f" | {a[2]}" if a[2] else ""
            lines.append(f"{status} `{a[0]}` — **{a[1]}**{grp}")
        lines.append("\n`/alert add <kalit_soz>` — yangi qo'shish")
        lines.append("`/alert del <ID>` — o'chirish")
        text = "\n".join(lines)
    await event.respond(text)


@bot.on(events.NewMessage(pattern=r'/alert add (.+)'))
async def cmd_alert_add(event):
    if not await is_admin(event.sender_id):
        return
    keyword = _pm(event, 1).strip()
    aid = await db_mod.add_alert(event.sender_id, keyword)
    engine._invalidate_alerts_cache()
    await event.respond(f"✅ Alert qo'shildi!\n🆔 ID: `{aid}`\n🔑 Kalit so'z: `{keyword}`")

@bot.on(events.NewMessage(pattern=r'/alert del (\d+)'))
async def cmd_alert_del(event):
    if not await is_admin(event.sender_id):
        return
    aid = int(_pm(event, 1))
    await db_mod.delete_alert(aid)
    engine._invalidate_alerts_cache()
    await event.respond(f"❌ Alert `{aid}` o'chirildi.")

@bot.on(events.NewMessage(pattern='/alert list'))
async def cmd_alert_list(event):
    if not await is_admin(event.sender_id):
        return
    alerts = await db_mod.list_alerts(event.sender_id)
    if not alerts:
        await event.respond("Alertlar yo'q. `/alert add <so'z>` bilan qo'shing.")
        return
    lines = ["🚨 **Alertlaringiz:**\n"]
    for a in alerts:
        st = "✅ faol" if a[3] else "⏸ to'xtatilgan"
        lines.append(f"`{a[0]}` | **{a[1]}** | {st}")
    await event.respond("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
# #7 TRUST SCORE
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/trust (-?\d+)'))
async def cmd_trust(event):
    if not await is_admin(event.sender_id):
        return
    uid = int(_pm(event, 1))
    profile, messages = await engine.lookup_user_by_id(uid)
    if not profile and not messages:
        await event.respond(f"❌ `{uid}` ID bazada topilmadi.")
        return
    score, label, reasons = engine.calculate_trust_score(profile or {}, len(messages))
    bar = "█" * (score // 10) + "░" * (10 - score // 10)
    text = (
        f"🔐 **Trust Score: {uid}**\n\n"
        f"[{bar}] **{score}/100**\n"
        f"{label}\n\n"
        f"📊 **Ballar:**\n" +
        "\n".join(f"  {r}" for r in reasons)
    )
    await event.respond(text)


# ─────────────────────────────────────────────────────────────────────
# #8 O'ZGARISHLAR TARIXI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/changes (-?\d+)'))
async def cmd_changes(event):
    if not await is_admin(event.sender_id):
        return
    uid = int(_pm(event, 1))
    changes = await db_mod.get_user_change_log(uid)
    if not changes:
        await event.respond(f"📝 `{uid}` ID uchun o'zgarishlar yo'q (yoki hali kuzatilmagan).")
        return
    lines = [f"📝 **O'zgarishlar tarixi — ID {uid}** ({len(changes)} ta):\n"]
    for ch in changes[:30]:
        field_names = {'first_name': 'Ism', 'last_name': 'Familiya',
                       'username': 'Username', 'phone': 'Telefon', 'bio': 'Bio'}
        fname = field_names.get(ch[0], ch[0])
        lines.append(f"🕐 `{ch[3]}`")
        lines.append(f"   {fname}: `{ch[1] or '—'}` → `{ch[2] or '—'}`\n")
    await event.respond("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
# #10 USERNAME / TELEFON QIDIRUV
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/lookup (.+)'))
async def cmd_lookup(event):
    if not await is_admin(event.sender_id):
        return
    query = _pm(event, 1).strip()
    msg = await event.respond("🔍 Qidirilmoqda...")
    if query.startswith('@'):
        results = await engine.search_by_username(query)
        if not results:
            await msg.edit(f"❌ `{query}` username topilmadi.")
            return
        lines = [f"👤 **Username qidiruvi: {query}**\n"]
        for r in results[:5]:
            full = (r['first_name'] + ' ' + r['last_name']).strip() or 'Nomsiz'
            ph = f" | 📱{r['phone']}" if r['phone'] else ""
            lines.append(f"🆔 `{r['user_id']}` — **{full}** @{r['username']}{ph}")
            lines.append(f"   Guruhlar: {', '.join(r['groups'][:3])}")
        await msg.edit("\n".join(lines))
    elif query.startswith('+') or query[0].isdigit():
        result = await engine.search_by_phone(userbot, query)
        if not result:
            await msg.edit(f"❌ `{query}` telefon raqami topilmadi.")
            return
        full = (result['first_name'] + ' ' + result['last_name']).strip() or 'Nomsiz'
        src = result.get('source', '')
        lines = [
            f"📱 **Telefon qidiruvi: {query}**\n",
            f"🆔 ID: `{result['user_id']}`",
            f"📛 Ism: **{full}**",
            f"👤 Username: @{result['username']}" if result['username'] else "",
            f"📲 Manba: {src}",
        ]
        if result.get('groups'):
            lines.append(f"📋 Guruhlar: {', '.join(result['groups'][:3])}")
        await msg.edit("\n".join(l for l in lines if l))
    else:
        await msg.edit("⚠️ Format: `/lookup @username` yoki `/lookup +998901234567`")


# ─────────────────────────────────────────────────────────────────────
# #24 PROFIL RASMI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/photo (-?\d+)'))
async def cmd_photo(event):
    if not await is_admin(event.sender_id):
        return
    uid = int(_pm(event, 1))
    msg = await event.respond("📸 Rasm yuklanmoqda...")
    path = await engine.get_profile_photo(userbot, uid)
    if path and os.path.exists(path):
        await bot.send_file(event.sender_id, path, caption=f"📸 Profil rasmi — ID `{uid}`")
        os.remove(path)
        await msg.delete()
    else:
        await msg.edit(f"🔒 `{uid}` ID profil rasmi ko'rinmaydi (yopiq yoki yo'q).")


# ─────────────────────────────────────────────────────────────────────
# #25 TELEFON → PROFIL (alohida buyruq)
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/phone (.+)'))
async def cmd_phone(event):
    if not await is_admin(event.sender_id):
        return
    phone = _pm(event, 1).strip()
    msg = await event.respond("📱 Telegram da qidirilmoqda...")
    result = await engine.search_by_phone(userbot, phone)
    if not result:
        await msg.edit(f"❌ `{phone}` — Telegram da topilmadi.")
        return
    full = (result['first_name'] + ' ' + result['last_name']).strip() or 'Nomsiz'
    src_txt = "✅ Telegramdan topildi" if result.get('source') == 'telegram' else "✅ Bazadan topildi"
    text = (
        f"📱 **Telefon: {phone}**\n"
        f"{src_txt}\n\n"
        f"🆔 ID: `{result['user_id']}`\n"
        f"📛 Ism: **{full}**\n"
    )
    if result['username']:
        text += f"👤 Username: @{result['username']}\n"
    if result.get('groups'):
        text += f"📋 Guruhlar: {', '.join(result['groups'][:3])}\n"
    text += f"\n💡 `/trust {result['user_id']}` — trust score ko'rish"
    await msg.edit(text)


# ─────────────────────────────────────────────────────────────────────
# #12 FAOLLIK TIMELINE
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/timeline (-?\d+)'))
async def cmd_timeline(event):
    if not await is_admin(event.sender_id):
        return
    uid = int(_pm(event, 1))
    data = await engine.get_user_timeline(uid, days=90)
    if not data['timeline']:
        await event.respond(f"📅 `{uid}` ID uchun kesh da xabar yo'q.")
        return
    lines = [f"📅 **Faollik — ID {uid}** (so'nggi 90 kun)\n"]
    lines.append(f"Jami: `{data['total']}` xabar")
    lines.append(f"Birinchi: {data['first_msg']}")
    lines.append(f"Oxirgi:   {data['last_msg']}\n")
    lines.append("**Kunlik faollik:**")
    for d in data['timeline'][-20:]:
        bar = "▓" * min(d['count'], 20)
        lines.append(f"`{d['date']}` {bar} ({d['count']})")
    await event.respond("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
# #20 AKKAUNT HAYOTI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/lifecycle (-?\d+)'))
async def cmd_lifecycle(event):
    if not await is_admin(event.sender_id):
        return
    uid = int(_pm(event, 1))
    data = await engine.get_account_lifecycle(uid)
    if not data['monthly']:
        await event.respond(f"📊 `{uid}` ID uchun faollik ma'lumoti yo'q.")
        return
    lines = [f"📊 **Akkaunt hayoti — ID {uid}**\n"]
    lines.append(f"🕐 Birinchi ko'rilgan: {data['first_seen']}")
    lines.append(f"📝 O'zgarishlar soni: {data['change_count']}\n")
    if data['peak_month']:
        lines.append(f"📈 Eng faol oy: `{data['peak_month']}`")
    if data['peak_hour'] is not None:
        lines.append(f"🕐 Eng faol soat: `{data['peak_hour']}:00`\n")
    lines.append("**Oylik faollik:**")
    for m in data['monthly']:
        bar = "█" * min(m['count'] // 2, 25)
        lines.append(f"`{m['month']}` {bar} ({m['count']})")
    await event.respond("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
# #17 YOZUV USLUBI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/style (-?\d+)'))
async def cmd_style(event):
    if not await is_admin(event.sender_id):
        return
    uid = int(_pm(event, 1))
    style = await engine.analyze_writing_style(uid)
    if not style:
        await event.respond(f"✍️ `{uid}` ID uchun tahlil qilish uchun xabarlar yetarli emas.")
        return
    top = ", ".join(f"`{w}`({c})" for w, c in style['top_words'][:8])
    text = (
        f"✍️ **Yozuv uslubi — ID {uid}**\n\n"
        f"📨 Tahlil qilingan: `{style['msg_count']}` xabar\n"
        f"📏 O'rtacha uzunlik: `{style['avg_len']}` belgi / `{style['avg_words']}` so'z\n"
        f"😀 Emoji/xabar: `{style['emoji_ratio']}`\n"
        f"❓ Savol (%): `{style['question_pct']}` | ❗ Undov (%): `{style['exclaim_pct']}`\n"
        f"🔠 Katta harf (%): `{style['caps_ratio']}`\n\n"
        f"📊 **Top so'zlar:** {top}"
    )
    await event.respond(text)

@bot.on(events.NewMessage(pattern=r'/compare (-?\d+) (-?\d+)'))
async def cmd_compare_style(event):
    if not await is_admin(event.sender_id):
        return
    uid1 = int(_pm(event, 1))
    uid2 = int(_pm(event, 2))
    msg = await event.respond("✍️ Uslublar tahlil qilinmoqda...")
    similarity = await engine.compare_writing_styles(uid1, uid2)
    bar = "█" * (similarity // 10) + "░" * (10 - similarity // 10)
    if similarity >= 70:
        verdict = "⚠️ **Bir odam bo'lishi mumkin!**"
    elif similarity >= 50:
        verdict = "🟡 O'rtacha o'xshashlik"
    else:
        verdict = "🟢 Turli uslublar"
    text = (
        f"✍️ **Uslub solishtirish**\n"
        f"ID {uid1} vs ID {uid2}\n\n"
        f"[{bar}] **{similarity}%** o'xshashlik\n\n"
        f"{verdict}"
    )
    await msg.edit(text)


# ─────────────────────────────────────────────────────────────────────
# #15 CROSS-GROUP TAHLIL
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/common (.+?) (.+)'))
async def cmd_common(event):
    if not await is_admin(event.sender_id):
        return
    g1 = _pm(event, 1).strip()
    g2 = _pm(event, 2).strip()
    msg = await event.respond("🔍 Umumiy a'zolar qidirilmoqda...")
    members = await engine.get_common_members(g1, g2)
    if not members:
        await msg.edit(f"❌ `{g1}` va `{g2}` da umumiy a'zo topilmadi.")
        return
    lines = [f"👥 **Umumiy a'zolar: {len(members)} ta**\n{g1} ∩ {g2}\n"]
    for m in members[:30]:
        full = (m['first_name'] + ' ' + m['last_name']).strip() or 'Nomsiz'
        un = f" @{m['username']}" if m['username'] else ""
        ph = f" | {m['phone']}" if m['phone'] else ""
        lines.append(f"🆔 `{m['user_id']}` {full}{un}{ph}")
    if len(members) > 30:
        lines.append(f"\n... va yana {len(members)-30} ta")
    await msg.edit("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
# #16 KOORDINATSIYALANGAN XATTI-HARAKAT
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/coordinated (.+)'))
async def cmd_coordinated(event):
    if not await is_admin(event.sender_id):
        return
    group = _pm(event, 1).strip()
    msg = await event.respond("🔍 Tahlil qilinmoqda...")
    data = await engine.detect_coordinated_behavior(group)
    risk = data['risk_score']
    if risk >= 70:
        risk_label = "🔴 YUQORI XAVF"
    elif risk >= 40:
        risk_label = "🟡 O'RTA XAVF"
    else:
        risk_label = "🟢 PAST XAVF"
    text = (
        f"🕵️ **Koordinatsiya tahlili**\n{group}\n\n"
        f"👥 Jami a'zolar: `{data['total']}`\n"
        f"📵 Bio yo'q: `{data['no_bio_pct']}%`\n"
        f"📵 Telefon yo'q: `{data['no_phone_pct']}%`\n"
        f"🤖 Bir vaqtda qo'shilgan guruhlari: `{data['cluster_count']}`\n\n"
        f"⚠️ **Xavf darajasi: {risk}/100 — {risk_label}**"
    )
    if data['clusters']:
        text += f"\n\n**Shubhali klasterlar (birinchisi):**\n"
        for m in data['clusters'][0][:5]:
            full = (m[1] or '') + (' @' + m[2] if m[2] else '')
            text += f"  🆔 `{m[0]}` {full} — {m[5]}\n"
    await msg.edit(text)


# ─────────────────────────────────────────────────────────────────────
# #18 VAQT KORRELYATSIYASI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern='/temporal'))
async def cmd_temporal(event):
    if not await is_admin(event.sender_id):
        return
    msg = await event.respond("⏰ Vaqt korrelyatsiyasi hisoblanmoqda...")
    pairs = await engine.get_temporal_correlations(min_overlap=5)
    if not pairs:
        await msg.edit("⏰ Korrelyatsiya topilmadi (keshda yetarli xabar yo'q).")
        return
    lines = [f"⏰ **Bir vaqtda faol akkauntlar** (top {len(pairs)} juft):\n"]
    for uid1, uid2, overlap in pairs[:15]:
        lines.append(f"👤 `{uid1}` ↔ `{uid2}` — `{overlap}` umumiy soat")
        lines.append(f"   ⚠️ Bir odam bo'lishi mumkin — `/compare {uid1} {uid2}`")
    await msg.edit("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
# #19 O'CHIRILGAN XABARLAR
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/deleted (.+)'))
async def cmd_deleted(event):
    if not await is_admin(event.sender_id):
        return
    source = _pm(event, 1).strip()
    msgs = await engine.get_deleted_messages(source)
    if not msgs:
        await event.respond(f"🗑️ `{source}` da o'chirilgan xabar yo'q (yoki kuzatilmagan).")
        return
    lines = [f"🗑️ **O'chirilgan xabarlar — {source}** ({len(msgs)} ta):\n"]
    for m in msgs[:20]:
        un = f"@{m['username']}" if m['username'] else f"ID:{m['sender_id']}"
        link_part = f"\n   🔗 {m['link']}" if m['link'] else ""
        lines.append(f"📅 `{m['date']}` | {un}")
        lines.append(f"   💬 {m['text'][:150]}{link_part}\n")
    await event.respond("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
# #14 EVIDENCE PAKETI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/evidence (-?\d+)'))
async def cmd_evidence(event):
    if not await is_admin(event.sender_id):
        return
    uid = int(_pm(event, 1))
    msg = await event.respond(f"📄 `{uid}` uchun evidence hisoboti tayyorlanmoqda...")
    path = await engine.generate_evidence_report(uid)
    if path and os.path.exists(path):
        await bot.send_file(
            event.sender_id, path,
            caption=f"📄 **Evidence hisoboti — ID {uid}**\n{datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        os.remove(path)
        await msg.delete()
    else:
        await msg.edit(f"❌ `{uid}` ID bazada topilmadi.")


# ─────────────────────────────────────────────────────────────────────
# #23 TARMOQ XARITASI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/network (.+)'))
async def cmd_network(event):
    if not await is_admin(event.sender_id):
        return
    ids_raw = _pm(event, 1).strip()
    try:
        user_ids = [int(x.strip()) for x in ids_raw.split(',') if x.strip().lstrip('-').isdigit()]
    except Exception:
        await event.respond("⚠️ Format: `/network 123,456,789`")
        return
    if not user_ids:
        await event.respond("⚠️ ID lar vergul bilan ajrating: `/network 123,456,789`")
        return
    msg = await event.respond("🕸️ Tarmoq xaritasi yaratilmoqda...")
    path = await engine.generate_network_map(user_ids)
    if path and os.path.exists(path):
        await bot.send_file(
            event.sender_id, path,
            caption=f"🕸️ **Tarmoq xaritasi** — {len(user_ids)} ta shaxs\nBrauzerda oching"
        )
        os.remove(path)
        await msg.delete()
    else:
        await msg.edit("❌ Xarita yaratishda xatolik.")


# ─────────────────────────────────────────────────────────────────────
# #22 TERGOVCHI ISH MAYDONI
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r'/case new (.+)'))
async def cmd_case_new(event):
    if not await is_admin(event.sender_id):
        return
    name = _pm(event, 1).strip()
    inv_id = await db_mod.create_investigation(name, event.sender_id)
    await event.respond(
        f"📁 **Yangi tergov yaratildi!**\n"
        f"🆔 ID: `{inv_id}`\n"
        f"📌 Nom: {name}\n\n"
        f"Shaxs qo'shish: `/case add {inv_id} <user_id>`\n"
        f"Hisobot: `/case report {inv_id}`"
    )

@bot.on(events.NewMessage(pattern='/case list'))
async def cmd_case_list(event):
    if not await is_admin(event.sender_id):
        return
    invs = await db_mod.get_investigations(event.sender_id)
    if not invs:
        await event.respond("📁 Tergovlar yo'q. `/case new <nom>` bilan yarating.")
        return
    lines = ["📁 **Tergovlar ro'yxati:**\n"]
    for inv in invs:
        targets = await db_mod.get_investigation_targets(inv[0])
        lines.append(f"🆔 `{inv[0]}` — **{inv[1]}** ({len(targets)} shaxs) | {inv[4][:10]}")
    lines.append("\n`/case report <ID>` — hisobot olish")
    await event.respond("\n".join(lines))

@bot.on(events.NewMessage(pattern=r'/case add (\d+) (.+)'))
async def cmd_case_add(event):
    if not await is_admin(event.sender_id):
        return
    inv_id     = int(_pm(event, 1))
    target_val = _pm(event, 2).strip()
    ttype = 'channel' if target_val.lstrip('-').isdigit() and int(target_val) < 0 else 'user'
    await db_mod.add_investigation_target(inv_id, ttype, target_val)
    await event.respond(f"✅ `{target_val}` tergov `{inv_id}` ga qo'shildi.")

@bot.on(events.NewMessage(pattern=r'/case report (\d+)'))
async def cmd_case_report(event):
    if not await is_admin(event.sender_id):
        return
    inv_id = int(_pm(event, 1))
    msg = await event.respond("📄 Tergov hisoboti tayyorlanmoqda...")
    path = await engine.generate_investigation_report(inv_id)
    if path and os.path.exists(path):
        await bot.send_file(
            event.sender_id, path,
            caption=f"📄 **Tergov hisoboti #{inv_id}**"
        )
        os.remove(path)
        await msg.delete()
    else:
        await msg.edit(f"❌ Tergov `{inv_id}` topilmadi.")

@bot.on(events.NewMessage(pattern=r'/case del (\d+)'))
async def cmd_case_del(event):
    if not await is_admin(event.sender_id):
        return
    inv_id = int(_pm(event, 1))
    await db_mod.delete_investigation(inv_id)
    await event.respond(f"🗑️ Tergov `{inv_id}` o'chirildi.")


# ─────────────────────────────────────────────────────────────────────
# ALERT SENDER — yangi kesh xabarlarda alert tekshiruvi
# ─────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# 🕵️ TERGOV MA'LUMOTI — TO'LIQ PDF
# ─────────────────────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern="💬 Kamentariya Xisoboti"))
async def btn_tergov_malumot(event):
    if not await is_admin(event.sender_id):
        return
    USER_STATES[event.sender_id] = 'waiting_tergov_id'
    await event.respond(
        "💬 **Kamentariya Xisoboti — To'liq PDF Hisobot**\n\n"
        "Quyidagilardan birini kiriting:\n"
        "📱 **Telefon:**  `+998901234567`\n"
        "👤 **Username:** `@username`\n"
        "🆔 **Profil ID:** `123456789`\n\n"
        "📄 Bot barcha ma'lumotlarni to'plab PDF fayl yuboradi."
    )


async def run_tergov_pdf(sender_id: int, identifier: str, status_msg):
    try:
        result = await engine.generate_tergov_pdf(userbot, identifier)
        if result is None or (isinstance(result, tuple) and result[0] is None):
            err = result[1] if result else "Noma'lum xato"
            await bot.send_message(sender_id, f"❌ Topilmadi: {err}")
            return
        pdf_path, label = result
        if not pdf_path or not os.path.exists(pdf_path):
            await bot.send_message(sender_id, "❌ PDF yaratishda xatolik yuz berdi.")
            return
        size_kb = round(os.path.getsize(pdf_path) / 1024)
        await bot.send_file(
            sender_id, pdf_path,
            caption=(
                f"💬 **Kamentariya Xisoboti**\n"
                f"📋 {label}\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"📄 Hajmi: {size_kb} KB"
            )
        )
        os.remove(pdf_path)
    except Exception as e:
        await bot.send_message(sender_id, f"❌ PDF xatolik: {e}")
    finally:
        try:
            await status_msg.delete()
        except Exception as e:
            engine._dbg("main.run_tergov_pdf", e)


async def process_alert_hits(hits: list):
    for hit in hits:
        try:
            un = f"@{hit['sender_name']}" if hit['sender_name'] else f"ID:{hit['sender_id']}"
            link_part = f"\n🔗 {hit['link']}" if hit['link'] else ""
            text = (
                f"🚨 **ALERT ISHLADI!**\n"
                f"🔑 Kalit so'z: `{hit['keyword']}`\n"
                f"📍 Manba: {hit['source']}\n"
                f"👤 Kim: {un}\n"
                f"📅 Sana: {hit['date']}\n\n"
                f"💬 {hit['text'][:300]}"
                f"{link_part}"
            )
            await bot.send_message(hit['admin_id'], text, link_preview=False)
        except Exception as e:
            engine._dbg("main.process_alert_hits", e)


async def main():
    # asyncio.Queue lar event loop ichida yaratilishi kerak
    global _SCAN_QUEUE
    _SCAN_QUEUE = asyncio.Queue()

    await db_mod.init_db()
    await music.init_music_db()
    phish_mod.init_db()
    await userbot.start()
    await bot.start(bot_token=BOT_TOKEN)

    # N ta qo'shimcha userbotlarni ishga tushirish (USERBOT2..N)
    _active_extras = []
    for ub_extra in _EXTRA_USERBOTS:
        try:
            if ub_extra._ub_exists:
                await ub_extra.start()
            else:
                await ub_extra.start(phone=ub_extra._ub_phone)
            me_extra = await ub_extra.get_me()
            idx_extra = ub_extra._ub_idx + 1
            print(f"✅ Userbot{idx_extra} ishga tushdi: {me_extra.first_name} (+{me_extra.phone})")
            _active_extras.append(ub_extra)
        except Exception as e:
            idx_extra = ub_extra._ub_idx + 1
            print(f"[OGOHLANTIRISH] Userbot{idx_extra} ishga tushmadi: {e} — o'tkazib yuborilmoqda")

    # Faqat muvaffaqiyatli userbotlar ishlaydi
    _EXTRA_USERBOTS.clear()
    _EXTRA_USERBOTS.extend(_active_extras)
    # userbot2 ni yangilash (eski kod uchun)
    globals()['userbot2'] = _EXTRA_USERBOTS[0] if _EXTRA_USERBOTS else None

    # Elektr uzilishi qolgan vaqtinchalik fayllarni tozalash
    # Barcha prefikslar: tmp_, multi_, watch_ (audio), excel_upload_, _phish_tmp_
    import glob as _glob
    for _pat in ("tmp_*.ogg", "multi_*.ogg", "watch_*.ogg",
                 "excel_upload_*.xlsx", "_phish_tmp_*"):
        for _tmp in _glob.glob(os.path.join(BASE_DIR, _pat)):
            try:
                os.remove(_tmp)
            except Exception as e:
                engine._dbg("main.main", e)

    # Navbatlar — tasklar boshlanishidan oldin tayyor bo'lishi shart
    engine._WATCH_ALERTS        = asyncio.Queue()
    engine._JOINED_CHANNEL_QUEUE = asyncio.Queue()

    # Barcha userbotlar ro'yxati (bosh + qo'shimchalar)
    _all_userbots = [userbot] + _EXTRA_USERBOTS
    _n_ub = len(_all_userbots)

    # tg_scrapers ga global ro'yxatni uzatish
    engine._ALL_USERBOTS[:] = _all_userbots  # atomic replace — race condition yo'q

    # Kanallarni N userbotga qayta taqsimlash (n o'zgarganda)
    await db_mod.reassign_channels(_n_ub)

    # smart_channel_knocker — barcha userbotlar bilan (ichida taqsimlaydi)
    asyncio.create_task(engine.smart_channel_knocker(userbot, bot, SUPER_ADMIN_ID, extra_userbots=_EXTRA_USERBOTS))
    asyncio.create_task(engine.channel_join_watcher(userbot, bot, SUPER_ADMIN_ID, extra_userbots=_EXTRA_USERBOTS))

    # background_profile_tracker — har userbot o'z qismini oladi
    for _i, _ub in enumerate(_all_userbots):
        asyncio.create_task(engine.background_profile_tracker(_ub, ub_idx=_i, n_userbots=_n_ub))

    asyncio.create_task(engine.music_channel_tracker(userbot, userbot2))
    asyncio.create_task(watch_alert_sender())
    asyncio.create_task(scan_queue_runner())
    # Real vaqt handler: a'zo kanallarda yangi xabar → API chaqiruvsiz
    engine.setup_realtime_handlers(userbot, userbot2, bot=bot, admin_id=SUPER_ADMIN_ID)
    print("✅ Kiber-Stansiya OSINT Pro ishga tushdi!")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    _restart_delay = 30
    while True:
        try:
            asyncio.run(main())
            break  # Agar normal to'xtasa — chiqish
        except KeyboardInterrupt:
            print("\nBot to'xtatildi (Ctrl+C).")
            break
        except Exception as _e:
            print(f"\n[KRITIK] Bot kutilmagan xato bilan to'xtadi: {_e}")
            print(f"[INFO] {_restart_delay} soniyadan keyin qayta ishga tushadi...")
            time.sleep(_restart_delay)
            print("[INFO] Qayta ishga tushmoqda...")
