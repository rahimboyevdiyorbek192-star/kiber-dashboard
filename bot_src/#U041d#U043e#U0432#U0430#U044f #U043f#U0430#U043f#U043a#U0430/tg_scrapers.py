# tg_scrapers.py
import re
import os
import asyncio
import random
import math
import urllib.parse
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import FormulaRule
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
from telethon.tl.types import InputPhoneContact
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import FloodWaitError, ChannelPrivateError, RpcCallFailError
from datetime import datetime
import aiosqlite
import tempfile
import database as db_mod
import music_scanner as music_mod

# Vaqtinchalik fayllar uchun papka — Windows/Linux ikkalisida ishlaydi
# (/tmp Windowsda yo'q, shuning uchun tempfile.gettempdir() ishlatamiz)
_TMP = tempfile.gettempdir()

SCANNER_PAUSED    = False
MONITORING_PAUSED = False
_SCAN_COUNT = 0
_WATCH_ALERTS = None  # asyncio.Queue() — main() da ishga tushiriladi
KNOCK_INTERVAL = 15 * 60   # Maxfiy kanal so'rovnoma oralig'i — 15 daqiqa

# N-userbot global ro'yxati — main() da to'ldiriladi
_ALL_USERBOTS: list = []   # [userbot, userbot2, ...]

def _get_n_userbots() -> int:
    return len(_ALL_USERBOTS) if _ALL_USERBOTS else 1

async def get_ub_for_channel(channel_link: str):
    """
    Kanal uchun biriktirilgan userbotni qaytaradi.
    Yangi kanal bo'lsa — avtomatik eng bo'sh userbotga biriktiradi.
    """
    n = _get_n_userbots()
    if n == 1 or not _ALL_USERBOTS:
        return _ALL_USERBOTS[0] if _ALL_USERBOTS else None
    idx = await db_mod.assign_channel(channel_link, n)
    if idx < len(_ALL_USERBOTS):
        return _ALL_USERBOTS[idx]
    return _ALL_USERBOTS[0]

async def auto_assign_channel(channel_link: str):
    """Yangi kanal topilganda avtomatik biriktirib qo'yadi."""
    n = _get_n_userbots()
    if n > 1 and channel_link:
        await db_mod.assign_channel(channel_link, n)

# Alert keshi — har xabar uchun DB ga bormaslik (60s TTL)
_alerts_cache = None
_alerts_cache_ts = 0.0

async def _get_alerts_cached():
    """Faol alertlarni 60 soniya keshlab beradi (DB yukini kamaytiradi)."""
    global _alerts_cache, _alerts_cache_ts
    import time as _t
    now = _t.monotonic()
    if _alerts_cache is not None and (now - _alerts_cache_ts) < 60:
        return _alerts_cache
    _alerts_cache    = await db_mod.get_active_alerts()
    _alerts_cache_ts = now
    return _alerts_cache

def _invalidate_alerts_cache():
    """Alert qo'shil/o'chirilganda keshni yangilash uchun."""
    global _alerts_cache_ts
    _alerts_cache_ts = 0.0

async def _check_batch_alerts(batch: list):
    """Keshga qo'shilgan xabarlar uchun alertlarni tekshirish (fon taskda)."""
    try:
        for msg_id, source, sender_id, sender_name, sender_un, text, msg_date in batch:
            if not text:
                continue
            hits = await check_message_alerts(msg_id, source, sender_id, sender_name, text, msg_date)
            if hits and _WATCH_ALERTS:
                for hit in hits:
                    await _WATCH_ALERTS.put(('alert', hit))
    except Exception as e:
        _dbg("_check_batch_alerts", e)

# Adaptiv flood tracker: flood kelsa avtomatik sekinlashadi
_FLOOD_PENALTY = 0.0   # qo'shimcha uyqu (soniyalarda), flood kelsa oshadi

# Userbot flood tracker: {id(userbot): unix_timestamp_until_flood_expires}
import time as _time_mod
from datetime import timedelta as _timedelta

# Telegram msg.date UTC bo'ladi — mahalliy vaqtga o'girish (O'zbekiston=UTC+5).
# .env da TIMEZONE_OFFSET=5 bilan sozlanadi.
try:
    _TZ_OFFSET = float(os.getenv("TIMEZONE_OFFSET", "5"))
except (TypeError, ValueError):
    _TZ_OFFSET = 5.0

def _fmt_date(dt):
    """msg.date (UTC) ni mahalliy vaqtda 'YYYY-MM-DD HH:MM' formatida qaytaradi."""
    if not dt:
        return ""
    return (dt + _timedelta(hours=_TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")

_UB_FLOOD_UNTIL: dict = {}
_PROCESSING_AUDIO: set = set()   # (channel_id, msg_id) — ikki userbot bir musiqani yuklamasligi uchun
_JOINED_CHANNEL_QUEUE: asyncio.Queue = None  # Ochilgan maxfiy kanallar navbati
_CURRENT_SCAN_CHANNEL: str = ""  # Hozir skanerlanyotgan kanal nomi

# ── Har userbot bir vaqtda nechta musiqa parallel yuklab oladi ──────────
# /yuklash N komandasi orqali o'zgartiriladi, faylga saqlanadi.
_MUSIC_PARALLEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_parallel.txt")

def _load_music_parallel() -> int:
    """Saqlangan parallel yuklash sonini o'qiydi (standart: 2)."""
    try:
        with open(_MUSIC_PARALLEL_FILE, "r") as f:
            n = int(f.read().strip())
            if 1 <= n <= 10:
                return n
    except Exception:
        pass
    return 2

MUSIC_PARALLEL: int = _load_music_parallel()

def set_music_parallel(n: int) -> int:
    """Parallel yuklash sonini o'rnatadi va faylga saqlaydi. 1–10 oralig'ida."""
    global MUSIC_PARALLEL
    n = max(1, min(10, int(n)))
    MUSIC_PARALLEL = n
    try:
        with open(_MUSIC_PARALLEL_FILE, "w") as f:
            f.write(str(n))
    except Exception as e:
        _dbg("set_music_parallel", e)
    return n


def _record_flood(seconds: float):
    """Flood kelganda penalty oshirish — keyingi so'rovlar sekinlashadi."""
    global _FLOOD_PENALTY
    _FLOOD_PENALTY = min(_FLOOD_PENALTY + seconds * 0.1, 30.0)

def _decay_flood():
    """Har muvaffaqiyatli so'rovda penalty ozayadi."""
    global _FLOOD_PENALTY
    if _FLOOD_PENALTY > 0:
        _FLOOD_PENALTY = max(0.0, _FLOOD_PENALTY - 0.05)

async def _safe_api_call(make_coro):
    """API chaqiruvni flood himoyasi bilan bajaradi.
    make_coro: callable, har safar yangi coroutine qaytaradi (lambda yoki partial).
    Flood wait semaphore TASHQARISIDA uxlaydi — boshqa tasklar bloklanmaydi.
    """
    for attempt in range(3):
        _decay_flood()
        extra = _FLOOD_PENALTY
        if extra > 0:
            await asyncio.sleep(min(extra, 10))
        try:
            return await asyncio.wait_for(make_coro(), timeout=30)
        except asyncio.TimeoutError:
            return None
        except FloodWaitError as e:
            _record_flood(e.seconds)
            log_flood("api_call", e.seconds)
            await asyncio.sleep(min(e.seconds + 2, 60))
            if attempt == 2:
                return None
        except Exception:
            return None
    return None

# ─── AQLLI RESURS MENEJERI ───────────────────────────────────────────
# Bot o'zi qaysi jarayon og'ir ekanini biladi va resurslarni taqsimlaydi
# Kanal musiqa skanerlash tugadimi?
_CHANNEL_MUSIC_DONE = False

# Flood statistikasi
_FLOOD_STATS = {}

def log_flood(func_name, seconds):
    """Flood statistikasini saqlash"""
    if func_name not in _FLOOD_STATS:
        _FLOOD_STATS[func_name] = {'count': 0, 'total_secs': 0, 'max_secs': 0}
    _FLOOD_STATS[func_name]['count'] += 1
    _FLOOD_STATS[func_name]['total_secs'] += seconds
    _FLOOD_STATS[func_name]['max_secs'] = max(_FLOOD_STATS[func_name]['max_secs'], seconds)
    
    # flood_log.txt ga yozish
    from datetime import datetime as _dt
    line = f"{_dt.now().strftime('%H:%M:%S')} | {func_name} | {seconds}s\n"
    try:
        import os as _os
        log_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'flood_log.txt')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception as e:
        _dbg("log_flood", e)
    print(f"[FLOOD] {func_name}: {seconds}s")

def _dbg(where, err):
    """Jim xatolarni error_log.txt ga yozadi — ishlashga ta'sir qilmaydi."""
    try:
        import os as _os
        from datetime import datetime as _dt
        line = f"{_dt.now().strftime('%Y-%m-%d %H:%M:%S')} | {where} | {type(err).__name__}: {err}\n"
        log_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'error_log.txt')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


_RESOURCE = {
    'heavy_scan':    False,   # Og'ir skanerlash (guruh, xabar, comment)
    'music_paused':  False,   # Musiqa tracker pauza
    'profile_slow':  False,   # Profil tracker sekin rejim
    'current_task':  None,    # Hozir nima ishlayapti
    'task_start':    None,    # Qachon boshlangan
}

def resource_start(task_name: str):
    """Og'ir jarayon boshlananda chaqiriladi."""
    _RESOURCE['heavy_scan']   = True
    _RESOURCE['music_paused'] = True
    _RESOURCE['profile_slow'] = True
    _RESOURCE['current_task'] = task_name
    _RESOURCE['task_start']   = datetime.now()
    print(f"[RESURS] {task_name} boshlandi — fon jarayonlar sekinlashtirildi")

def resource_stop(task_name: str):
    """Og'ir jarayon tugaganda chaqiriladi."""
    _RESOURCE['heavy_scan']   = False
    _RESOURCE['music_paused'] = False
    _RESOURCE['profile_slow'] = False
    _RESOURCE['current_task'] = None
    _RESOURCE['task_start']   = None
    print(f"[RESURS] {task_name} tugadi — fon jarayonlar davom ettirildi")

def resource_status() -> str:
    """Hozirgi resurs holati."""
    if _RESOURCE['heavy_scan']:
        started = _RESOURCE['task_start']
        elapsed = ""
        if started:
            secs = int((datetime.now() - started).total_seconds())
            elapsed = f" ({secs//60} daq {secs%60} sek)"
        return f"🔴 Og'ir jarayon: {_RESOURCE['current_task']}{elapsed}"
    return "🟢 Normal rejim"


# ─────────────────────────────────────────────────────────────────────
# YORDAMCHI FUNKSIYALAR
# ─────────────────────────────────────────────────────────────────────

def extract_exact_birth_date(text):
    if not text:
        return ""
    m = re.search(r'\b(\d{1,2}[.\/\-]\d{1,2}[.\/\-](?:19|20)\d{2})\b', text)
    if m:
        return m.group(1)
    years = re.findall(r'\b(19\d{2}|20[0-2]\d)\b', text)
    return years[0] if years else ""


def _fmt_birthday(bday):
    """
    Telegram profilidagi strukturaviy birthday (full_user.birthday) →
    'DD.MM' yoki 'DD.MM.YYYY'. Yo'q bo'lsa ''. Qo'shimcha API SHART EMAS —
    GetFullUserRequest javobida keladi.
    """
    try:
        if not bday:
            return ""
        d = getattr(bday, 'day', None)
        m = getattr(bday, 'month', None)
        y = getattr(bday, 'year', None)
        if d and m:
            s = f"{int(d):02d}.{int(m):02d}"
            if y:
                s += f".{int(y)}"
            return s
    except Exception:
        pass
    return ""


def _best_birth_date(full_user, bio):
    """Avval strukturaviy birthday (ishonchli), bo'lmasa bio matnidan."""
    s = _fmt_birthday(getattr(full_user, 'birthday', None)) if full_user else ""
    return s or extract_exact_birth_date(bio or "")


async def get_user_by_phone(userbot, phone: str):
    """
    Telefon raqam orqali foydalanuvchi ma'lumotlarini oladi.
    FloodWait kelsa — kutadi va qayta urinadi.
    Qaytaradi: (user, full_info) yoki (None, None)
    """
    imported_user_id = None
    try:
        # 1. Kontaktga saqlash — FloodWait bo'lsa kutib qayta urinish
        for attempt in range(3):
            try:
                result = await userbot(ImportContactsRequest([
                    InputPhoneContact(
                        client_id=0,
                        phone=f"+{phone}",
                        first_name="TempContact",
                        last_name=""
                    )
                ]))
                break
            except FloodWaitError as e:
                log_flood("get_user_by_phone", e.seconds)
                await asyncio.sleep(e.seconds + 5)
                if attempt == 2:
                    return None, None
            except Exception as e:
                print(f"get_user_by_phone xatosi ({phone}): {e}")
                return None, None

        if not result.users:
            return None, None

        user = result.users[0]
        imported_user_id = user.id

        # Kontaktlar orasida kichik pauza
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # 2. To'liq profil ma'lumotlarini olish
        try:
            full_info = await userbot(GetFullUserRequest(user.id))
        except FloodWaitError as e:
            log_flood("get_user_by_phone", e.seconds)
            await asyncio.sleep(min(e.seconds + 5, 300))
            try:
                full_info = await userbot(GetFullUserRequest(user.id))
            except Exception:
                full_info = None
        except Exception:
            full_info = None

        return user, full_info

    except Exception as e:
        print(f"get_user_by_phone xatosi ({phone}): {e}")
        return None, None
    finally:
        # 3. Kontaktni o'chirish (har doim)
        if imported_user_id:
            try:
                await userbot(DeleteContactsRequest(id=[imported_user_id]))
                await asyncio.sleep(1)
            except Exception as e:
                _dbg("get_user_by_phone", e)



def extract_bio_links(bio_text):
    """Bio dagi barcha Telegram havolalarini topadi."""
    if not bio_text:
        return []
    results = []
    seen = set()
    pattern = r'(?:https?://)?(?:t\.me|telegram\.me)(/[^\s\)\]>\"\']+)'
    for m in re.finditer(pattern, bio_text):
        path = m.group(1)
        if not path or path == '/':
            continue
        full_url = "https://t.me" + path
        if full_url in seen:
            continue
        seen.add(full_url)
        first = path.strip('/').split('/')[0]
        if first.startswith('+') or first in ('joinchat', 'addlist', 'c'):
            results.append(full_url)
        elif re.match(r'^[a-zA-Z0-9_]{3,}$', first):
            results.append("@" + first)
        else:
            results.append(full_url)
    return results


def extract_invite_links(bio_text):
    """
    Bio dan FAQAT maxfiy kanal invite linklarini ajratadi.
    t.me/+XXXX yoki t.me/joinchat/XXXX formatlar.
    """
    if not bio_text:
        return []
    results = []
    pattern = r'(?:https?://)?(?:t\.me|telegram\.me)(/(?:\+|joinchat/)[^\s\)\]>\"\']+)'
    for m in re.finditer(pattern, bio_text):
        path = m.group(1)
        full_url = "https://t.me" + path
        if full_url not in results:
            results.append(full_url)
    return results


async def safe_get_entity(userbot, target):
    """get_entity ni FloodWait bilan xavfsiz chaqirish."""
    from telethon.errors import FloodWaitError

    # tg://resolve?domain=cXXXXX → kanal ID ga aylantirish
    if isinstance(target, str):
        if 'tg://resolve' in target and 'domain=c' in target:
            try:
                cid = int(target.split('domain=c')[-1].split('&')[0].strip())
                target = int(f"-100{cid}")
            except Exception as e:
                _dbg("safe_get_entity", e)
        # https://t.me/c/XXXX/YYY → kanal ID
        elif 't.me/c/' in target:
            try:
                cid = int(target.split('t.me/c/')[1].split('/')[0])
                target = int(f"-100{cid}")
            except Exception as e:
                _dbg("safe_get_entity", e)

    # Userbot hali flood davrida bo'lsa — API chaqirmay darhol None
    _ub_key = id(userbot)
    _flood_exp = _UB_FLOOD_UNTIL.get(_ub_key, 0)
    if _flood_exp > _time_mod.time():
        remaining = int(_flood_exp - _time_mod.time())
        if remaining % 60 == 0:  # Har daqiqada bir marta log
            print(f"[FLOOD-SKIP] Userbot flood davri: yana {remaining}s")
        return None

    for attempt in range(3):
        try:
            return await userbot.get_entity(target)
        except FloodWaitError as e:
            log_flood("safe_get_entity", e.seconds)
            # Katta flood — userbotni bloklash va darhol qaytish
            if e.seconds > 60:
                _UB_FLOOD_UNTIL[_ub_key] = _time_mod.time() + e.seconds
                print(f"[FLOOD-LOCK] Userbot {e.seconds}s bloklandi. "
                      f"Qo'yib berilish vaqti: {e.seconds//60} daqiqa {e.seconds%60} soniya.")
                await asyncio.sleep(2)  # event loop ga nafs berish
                return None
            await asyncio.sleep(e.seconds + 2)
        except Exception:
            return None
    return None


async def send_join_request(userbot, invite_link):
    """
    t.me/+XXXX yoki t.me/joinchat/XXXX invite link orqali
    kanalga qo'shilish so'rovnomasi yuboradi.
    """
    import urllib.parse
    invite_link = urllib.parse.unquote(invite_link)
    try:
        if "/+" in invite_link:
            hash_part = invite_link.split("/+")[-1].rstrip("/")
        elif "joinchat/" in invite_link:
            hash_part = invite_link.split("joinchat/")[-1].rstrip("/")
        else:
            return False
        await userbot(ImportChatInviteRequest(hash=hash_part))
        return True
    except Exception as e:
        err = str(e).lower()
        if "already" in err or "request" in err:
            return True  # Avval yuborilgan — normal
        print(f"send_join_request xatosi ({invite_link}): {e}")
        return False


def validate_target(text: str) -> tuple[bool, str]:
    """
    Guruh/kanal linkini tekshiradi.
    Qaytaradi: (is_valid, cleaned_text)
    """
    if not text or len(text) > 512:
        return False, ""
    text = text.strip()
    # Ruxsat etilgan formatlar: @username, https://t.me/..., raqam
    import re
    if re.match(r'^@[a-zA-Z0-9_]{3,}$', text):
        return True, text
    if re.match(r'^https?://t\.me/', text):
        return True, text
    if re.match(r'^-?\d+$', text):
        return True, text
    if re.match(r'^[a-zA-Z0-9_]{3,}$', text):
        return True, text
    return False, text


def validate_keyword(text: str) -> tuple[bool, str]:
    """Kalit so'zni tekshiradi. Max 200 belgi."""
    if not text or len(text.strip()) == 0:
        return False, ""
    text = text.strip()
    if len(text) > 200:
        return False, text
    return True, text


# Skan davomida bir xil kanal ID ni qayta-qayta resolve qilmaslik uchun kesh
_pc_link_cache: dict = {}  # max 5000 ta, keyin tozalanadi

async def _pc_link_cached(ub, pc, chats=None) -> str:
    """
    personal_channel_id (pc) uchun havola — avval kesh (xotira + DB),
    topilmasa get_entity (faqat 1 marta) va keshga username bilan saqlaydi.
    chats: GetFullUserRequest javobidagi kanallar (fi.chats) — agar shu yerda
           shaxsiy kanal bo'lsa, ALOHIDA get_entity SHART EMAS (0 qo'shimcha API).
    Qaytaradi: https://t.me/username yoki https://t.me/c/{pc}/1
    """
    try:
        pc_int = int(pc)
    except Exception:
        pc_int = pc
    # 0. GetFullUser javobidagi chats dan olish (0 qo'shimcha API)
    if chats:
        for _ch in chats:
            if getattr(_ch, 'id', None) == pc_int:
                _uname = getattr(_ch, 'username', None)
                if _uname:
                    _link = f"https://t.me/{_uname}"
                    _pc_link_cache[pc_int] = _link
                    try:
                        _now = datetime.now().strftime("%Y-%m-%d %H:%M")
                        async with db_mod.connect(db_mod.DB_NAME, timeout=5) as _db:
                            await _db.execute(
                                "INSERT OR REPLACE INTO resolved_channel_ids "
                                "(channel_link, numeric_id, resolved_at) VALUES (?, ?, ?)",
                                (_link, f"-100{pc_int}", _now)
                            )
                            await _db.commit()
                    except Exception as e:
                        _dbg("_pc_link_cached", e)
                    return _link
                break
    # 1. Xotira keshi
    if pc_int in _pc_link_cache:
        return _pc_link_cache[pc_int]
    # 2. DB keshi — avval username-li havola saqlangan bo'lsa, API shart emas
    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=5) as _db:
            async with _db.execute(
                "SELECT channel_link FROM resolved_channel_ids WHERE numeric_id=?",
                (f"-100{pc_int}",)
            ) as _cur:
                _row = await _cur.fetchone()
        if _row and _row[0] and '/c/' not in _row[0]:
            _pc_link_cache[pc_int] = _row[0]
            return _row[0]
    except Exception as e:
        _dbg("_pc_link_cached", e)
    # 3. Keshda yo'q — bir martagina API so'rovi
    link = f"https://t.me/c/{pc_int}/1"
    try:
        ent   = await asyncio.wait_for(ub.get_entity(pc_int), timeout=8)
        uname = getattr(ent, 'username', None)
        if uname:
            link = f"https://t.me/{uname}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        async with db_mod.connect(db_mod.DB_NAME, timeout=5) as _db:
            await _db.execute(
                "INSERT OR REPLACE INTO resolved_channel_ids "
                "(channel_link, numeric_id, resolved_at) VALUES (?, ?, ?)",
                (link, f"-100{pc_int}", now_str)
            )
            await _db.commit()
    except Exception as e:
        _dbg("_pc_link_cached", e)
    if len(_pc_link_cache) > 5000:
        _pc_link_cache.clear()
    _pc_link_cache[pc_int] = link
    return link


async def _save_pc_id_to_cache(pc: int):
    """
    personal_channel_id ni resolved_channel_ids ga saqlaydi — API so'rovsiz.
    Fon task sifatida ishlaydi, skanerlashni sekinlashtirmaydi.
    """
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        async with db_mod.connect(db_mod.DB_NAME, timeout=5) as _db:
            await _db.execute(
                "INSERT OR REPLACE INTO resolved_channel_ids "
                "(channel_link, numeric_id, resolved_at) VALUES (?, ?, ?)",
                (f"https://t.me/c/{pc}/1", f"-100{pc}", now_str)
            )
            await _db.commit()
    except Exception as e:
        _dbg("_save_pc_id_to_cache", e)


async def _resolve_pc_link(ub, ch_id: int) -> str:
    """
    personal_channel_id ni to'g'ri havolaga aylantiradi.
    Kanal @username ga ega bo'lsa → https://t.me/username
    Bo'lmasa            fallback → https://t.me/c/{ch_id}/1
    Memory kesh bilan — bir skan davomida API qayta chaqirilmaydi.
    Faqat background_profile_tracker uchun — skanerlashda ishlatilmaydi.
    """
    if ch_id in _pc_link_cache:
        return _pc_link_cache[ch_id]
    link = f"https://t.me/c/{ch_id}/1"
    try:
        ent   = await asyncio.wait_for(ub.get_entity(ch_id), timeout=8)
        uname = getattr(ent, 'username', None)
        if uname:
            link = f"https://t.me/{uname}"
        # DB ga ham saqlash
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        async with db_mod.connect(db_mod.DB_NAME, timeout=5) as _db:
            await _db.execute(
                "INSERT OR REPLACE INTO resolved_channel_ids "
                "(channel_link, numeric_id, resolved_at) VALUES (?, ?, ?)",
                (link, f"-100{ch_id}", now_str)
            )
            await _db.commit()
    except Exception as e:
        _dbg("_resolve_pc_link", e)
    _pc_link_cache[ch_id] = link
    return link


async def resolve_personal_channel(userbot, ch_id):
    """
    Shaxsiy kanal linkini hal qiladi.
    Numeric ID ni resolved_channel_ids jadvaliga saqlaydi (keshlayd).
    Qaytaradi: (link, is_private)
    """
    try:
        ch_entity = await userbot.get_entity(ch_id)

        # Numeric ID ni kesh jadvaliga saqlash — a'zo bo'lmasdan ham olish mumkin
        if hasattr(ch_entity, 'id') and ch_entity.id:
            _eid = str(ch_entity.id).lstrip('-')
            _num_id = f"-100{_eid}" if not str(ch_entity.id).startswith('-100') else str(ch_entity.id)
            _link_key = str(ch_id)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            try:
                async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                    await _db.execute(
                        "INSERT OR REPLACE INTO resolved_channel_ids "
                        "(channel_link, numeric_id, resolved_at) VALUES (?, ?, ?)",
                        (_link_key, _num_id, now_str)
                    )
                    await _db.commit()
            except Exception as e:
                _dbg("resolve_personal_channel", e)

        ch_uname  = getattr(ch_entity, 'username', None)
        if ch_uname:
            return f"https://t.me/{ch_uname}", False
        try:
            full_ch = await userbot(GetFullChannelRequest(ch_entity))
            inv = getattr(full_ch.full_chat, 'exported_invite', None)
            if inv and getattr(inv, 'link', None):
                return inv.link, False
        except Exception as e:
            _dbg("resolve_personal_channel", e)
        ch_title = getattr(ch_entity, 'title', '')
        return ch_title or str(ch_id), False
    except ChannelPrivateError:
        return f"🔒 Maxfiy (ID:{ch_id})", True
    except Exception:
        return "", False


def apply_excel_styles(ws, total_rows):
    """Excel faylni chiroyli formatlaydi.
    Ma'lumot qatorlari rangini Excel o'zi beradi (conditional formatting) —
    Python faqat bitta qoida yozadi, deyarli tezkor."""
    n_cols    = ws.max_column
    last_row  = ws.max_row
    last_col  = get_column_letter(n_cols)

    header_font  = Font(bold=True, color="FFFFFF", size=11)
    header_fill  = PatternFill("solid", fgColor="1F4E79")
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border  = Border(
        left=Side(style="thin"),  right=Side(style="thin"),
        top=Side(style="thin"),   bottom=Side(style="thin")
    )

    # 1. Sarlavha — to'liq stil (faqat 1 qator, tez)
    for col_num in range(1, n_cols + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = center_align
        cell.border    = thin_border

    # 2. Juft qatorlar — Excel o'zi rang beradi (conditional formatting, 1 qoida)
    # Ma'lumot qatori mavjud bo'lsagina (bo'sh varaqda A2:X1 noto'g'ri range bo'ladi)
    # Avval eski qoidalarni tozalaymiz — aks holda har saqlashda qoida to'planib to'qnashadi
    ws.conditional_formatting = ws.conditional_formatting.__class__()
    if last_row >= 2:
        data_range = f"A2:{last_col}{last_row}"
        even_fill = PatternFill(fill_type="solid", fgColor="DEEAF1")
        ws.conditional_formatting.add(
            data_range,
            FormulaRule(formula=["MOD(ROW(),2)=0"], fill=even_fill)
        )

    # 3. Ustun kengligi — sarlavhadan hisoblash, URL ustunlari keng
    url_keywords = ('link', 'url', 'kanal', 'guruh', 'manba', 'havola', 'bio')
    for col_num in range(1, n_cols + 1):
        header_val = str(ws.cell(row=1, column=col_num).value or '')
        base = len(header_val) + 8
        if any(k in header_val.lower() for k in url_keywords):
            width = max(base, 32)
        else:
            width = base
        ws.column_dimensions[get_column_letter(col_num)].width = min(width, 80)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 22


def apply_excel_styles_light(ws):
    """apply_excel_styles ga yo'naltiradi — eski chaqiruvlar ishlashi uchun."""
    apply_excel_styles(ws, ws.max_row - 1)

# ─────────────────────────────────────────────────────────────────────
# PARALLEL GURUH SKANERI — barcha userbot birgalikda (ochiq guruh)
# ─────────────────────────────────────────────────────────────────────

async def _ds_process_user(ub, user, target_group):
    """
    Bitta foydalanuvchini to'liq qayta ishlaydi (GetFullUserRequest + DB).
    Excel uchun ustun ro'yxati qaytaradi (№ siz) yoki None.
    MUHIM: GetFullUserRequest aynan SHU userbot bilan — access_hash o'zida.
    """
    # O'chirilgan akkaunt (deleted account) — API so'rovisiz aniqlanadi, o'tkazib yuboramiz
    if getattr(user, 'deleted', False):
        return None
    uid = user.id if hasattr(user, 'id') else user
    fn  = (getattr(user, 'first_name', '') or "") if hasattr(user, 'first_name') else ""
    ln  = (getattr(user, 'last_name', '') or "")  if hasattr(user, 'last_name') else ""
    un  = ("@" + user.username) if getattr(user, 'username', None) else ""
    ph  = (getattr(user, 'phone', '') or "")
    is_bot  = "✅" if getattr(user, 'bot', False) else "❌"
    is_prem = "✅" if getattr(user, 'premium', False) else "❌"
    bio = shaxsiy = maxfiy = ochiq = ""
    _bday = ""

    try:
        fi = await asyncio.wait_for(ub(GetFullUserRequest(uid)), timeout=20)
        fu  = fi.full_user
        bio = fu.about or ""
        _bday = _fmt_birthday(getattr(fu, 'birthday', None))
        inv = extract_invite_links(bio)
        if inv:
            maxfiy = ", ".join(inv)
            async with db_mod.connect(db_mod.DB_NAME, timeout=30) as _db:
                await _db.executemany(
                    "INSERT OR IGNORE INTO hidden_channel_knocker "
                    "(channel_id, creator_id, source_group) VALUES (?,?,?)",
                    [(lnk, uid, str(target_group)) for lnk in inv]
                )
                await _db.commit()
        al = extract_bio_links(bio)
        oc = [l for l in al if l.startswith('@') or
              ('t.me/' in l and '/+' not in l and 'joinchat' not in l)]
        ochiq = ", ".join(oc) if oc else ""
        pc = getattr(fu, 'personal_channel_id', None)
        if pc:
            shaxsiy = await _pc_link_cached(ub, pc, chats=getattr(fi, 'chats', None))
    except FloodWaitError as e:
        _record_flood(e.seconds)
        log_flood("_ds_process_user", e.seconds)
        await asyncio.sleep(min(e.seconds + 2, 120))
    except Exception as e:
        _dbg("_ds_process_user", e)

    if not fn and not ln and not un and not bio:
        return None

    url    = (f"https://t.me/{user.username}" if getattr(user, 'username', None)
              else f"tg://user?id={uid}")
    b_date = _bday or extract_exact_birth_date(bio)
    has_db = shaxsiy if shaxsiy else (maxfiy if maxfiy else "❌")
    await db_mod.save_user_to_bank(
        uid, str(target_group), fn, ln, un, ph, b_date, bio,
        ", ".join(extract_bio_links(bio)), has_db
    )
    return [fn, ln, un, uid, ("+" + ph) if ph else "",
            is_bot, is_prem, bio, shaxsiy, maxfiy, ochiq, url]


async def _deep_scan_parallel(all_bots, target_group, output_path, status_msg, scan_id):
    """
    Ochiq guruhni BARCHA userbot birgalikda skanerlaydi (a'zolik shart emas).
    1-bosqich: a'zolar user_id % N bo'yicha bo'linadi (har bot o'z ulushini).
    2-bosqich: xabar tarixi ID-oraliqlarga bo'linadi (har bot o'z oralig'ini).
    Har bot O'ZI o'qigani uchun access_hash o'zida — ma'lumot yo'qolmaydi.
    """
    n = len(all_bots)

    # Har userbot guruhni MUSTAQIL resolve qiladi (o'z access_hash i)
    entities = []
    for ub in all_bots:
        try:
            ent = await safe_get_entity(ub, target_group)
        except Exception:
            ent = None
        entities.append(ent)

    if entities[0] is None:
        raise Exception("Guruh topilmadi yoki Telegram cheklovi.")

    rows = []
    rows_lock = asyncio.Lock()
    seen = set()
    seen_lock = asyncio.Lock()

    try:
        await status_msg.edit(f"📋 **1-bosqich (parallel):** {n} ta userbot a'zolarni bo'lib oladi...")
    except Exception as e:
        _dbg("_deep_scan_parallel", e)

    # ── 1-BOSQICH: a'zolar (user_id % N bo'yicha bo'linadi) ──
    async def _phase1(k):
        ub  = all_bots[k]
        ent = entities[k]
        if ent is None:
            return
        sem   = asyncio.Semaphore(2)
        tasks = []
        try:
            async for u in ub.iter_participants(ent, aggressive=True):
                if (u.id % n) != k:
                    continue
                if getattr(u, 'bot', False) or getattr(u, 'deleted', False):
                    continue
                async with seen_lock:
                    if u.id in seen:
                        continue
                    seen.add(u.id)

                async def _do(uu):
                    async with sem:
                        await asyncio.sleep(0.5)
                        row = await _ds_process_user(ub, uu, target_group)
                        if row:
                            async with rows_lock:
                                rows.append(row)
                tasks.append(asyncio.create_task(_do(u)))
        except FloodWaitError as e:
            log_flood("parallel_participants", e.seconds)
            await asyncio.sleep(min(e.seconds + 5, 300))
        except Exception as e:
            _dbg("_deep_scan_parallel_p1", e)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    await asyncio.gather(*[_phase1(k) for k in range(n)], return_exceptions=True)
    _phase1_count = len(rows)

    try:
        await status_msg.edit(
            f"📨 **2-bosqich (parallel):** xabar tarixi {n} qismga bo'linmoqda...\n"
            f"(1-bosqichda: {_phase1_count} ta a'zo)"
        )
    except Exception as e:
        _dbg("_deep_scan_parallel", e)

    # ── 2-BOSQICH: xabar tarixi (ID-oraliqlarga bo'linadi) ──
    try:
        _last = await all_bots[0].get_messages(entities[0], limit=1)
        max_id = _last[0].id if _last else 0
    except Exception:
        max_id = 0

    if max_id > 0:
        chunk = max(1, max_id // n + 1)

        async def _phase2(k):
            ub  = all_bots[k]
            ent = entities[k]
            if ent is None:
                return
            lower  = k * chunk
            offset = (k + 1) * chunk + 1   # offset_id exclusive yuqori chegara
            sem    = asyncio.Semaphore(2)
            tasks  = []
            cache_batch = []
            try:
                async for msg in ub.iter_messages(ent, offset_id=offset, min_id=lower):
                    sid = msg.sender_id
                    if sid and sid > 0:
                        async with seen_lock:
                            is_new = sid not in seen
                            if is_new:
                                seen.add(sid)
                        if is_new:
                            sender = msg.sender
                            if sender and not getattr(sender, 'bot', False) and not getattr(sender, 'deleted', False):
                                async def _do(ss):
                                    async with sem:
                                        await asyncio.sleep(0.5)
                                        row = await _ds_process_user(ub, ss, target_group)
                                        if row:
                                            async with rows_lock:
                                                rows.append(row)
                                tasks.append(asyncio.create_task(_do(sender)))
                                # Xotira o'smasin — har 300 taskda tozalash
                                if len(tasks) >= 300:
                                    await asyncio.gather(*tasks, return_exceptions=True)
                                    tasks = []
                    # Matnli xabar — keshga
                    _txt = msg.text or getattr(msg, 'caption', None) or ""
                    if len(_txt) > 2:
                        sd = msg.sender
                        sn = su = ""
                        if sd and hasattr(sd, 'first_name'):
                            sn = ((sd.first_name or "") + " " + (sd.last_name or "")).strip()
                            su = getattr(sd, 'username', '') or ""
                        md = _fmt_date(msg.date)
                        cache_batch.append(
                            (msg.id, str(target_group), msg.sender_id or 0, sn, su, _txt[:2000], md)
                        )
                        if len(cache_batch) >= 500:
                            try:
                                async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                                    await _db.executemany(
                                        "INSERT OR IGNORE INTO messages_cache "
                                        "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                                        "VALUES (?,?,?,?,?,?,?)", cache_batch
                                    )
                                    await _db.commit()
                            except Exception as e:
                                _dbg("_deep_scan_parallel_p2", e)
                            cache_batch = []
            except FloodWaitError as e:
                log_flood("parallel_messages", e.seconds)
                await asyncio.sleep(min(e.seconds + 5, 300))
            except Exception as e:
                _dbg("_deep_scan_parallel_p2", e)
            if cache_batch:
                try:
                    async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                        await _db.executemany(
                            "INSERT OR IGNORE INTO messages_cache "
                            "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                            "VALUES (?,?,?,?,?,?,?)", cache_batch
                        )
                        await _db.commit()
                except Exception as e:
                    _dbg("_deep_scan_parallel_p2", e)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        await asyncio.gather(*[_phase2(k) for k in range(n)], return_exceptions=True)

    # ── Excel yozish (bir martada, oxirida) ──
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Guruh Skaner"
    ws.append([
        "№", "Ism", "Familiya", "Username", "Telegram ID",
        "Telefon", "Bot?", "Premium?", "Bio",
        "Shaxsiy Kanal", "Maxfiy Kanal", "Ochiq Kanal", "Profil havolasi"
    ])
    c = 0
    for r in rows:
        c += 1
        ws.append([c] + r)
    try:
        _fl = asyncio.get_running_loop()
        await _fl.run_in_executor(None, apply_excel_styles, ws, c)
        await _fl.run_in_executor(None, wb.save, output_path)
    except Exception as e:
        _dbg("_deep_scan_parallel", e)
    return c


# ─────────────────────────────────────────────────────────────────────
# GURUH SKANERI — bot.py dagi ub_members_process asosida
# ─────────────────────────────────────────────────────────────────────

async def deep_scan_group(userbot, target_group, output_path, status_msg,
                           resume_offset=0, resume_count=0, scan_id=None,
                           pre_entity=None, extra_userbots=None):
    """
    Guruh a'zolarini skanerlab Excel ga yozadi.
    Bio dagi t.me/+XXXX linklar — alohida "Maxfiy Kanal" ustuniga.
    Maxfiy kanallarga so'rovnoma yuboriladi va DB ga saqlanadi.
    extra_userbots: qo'shimcha userbot ro'yxati — GetFullUserRequest tezligi 2x+
    """
    global MONITORING_PAUSED, _SCAN_COUNT, _FLOOD_PENALTY

    # ── PARALLEL MARSHRUT: ochiq guruh + ko'p userbot ──
    # Ochiq guruhga a'zolik shart emas — har userbot mustaqil o'qiydi.
    # Yopiq guruh (invite link) bo'lsa — faqat a'zo userbot (pastdagi yo'l).
    # Rasmiy ro'yxat — takror userbot bo'lmasligi uchun
    _all_bots = list(_ALL_USERBOTS) if _ALL_USERBOTS else (
        [userbot] + [u for u in (extra_userbots or []) if u is not None]
    )
    _is_private = bool(re.search(r't\.me/\+|joinchat', str(target_group)))
    if len(_all_bots) > 1 and not _is_private and resume_offset == 0:
        _SCAN_COUNT += 1
        MONITORING_PAUSED = True
        _FLOOD_PENALTY = 0.0
        resource_start("Ochiq Guruh Skanerlash (parallel)")
        if scan_id is None:
            _sid = getattr(status_msg, 'chat_id', 0)
            scan_id = await db_mod.create_scan_session(str(target_group), output_path, _sid)
        try:
            _cnt = await _deep_scan_parallel(_all_bots, target_group, output_path, status_msg, scan_id)
            await db_mod.finish_scan_session(scan_id, status='done')
            return _cnt
        except Exception as _pe:
            await db_mod.finish_scan_session(scan_id, status='error')
            raise
        finally:
            _SCAN_COUNT -= 1
            if _SCAN_COUNT <= 0:
                _SCAN_COUNT = 0
                MONITORING_PAUSED = False
            resource_stop("Ochiq Guruh Skanerlash (parallel)")

    # ── BITTA USERBOT YO'LI (yopiq guruh yoki 1 userbot) ──
    # GetFullUserRequest uchun access_hash faqat o'qigan userbotda.
    _ub_pool = [userbot]
    _ub_count = len(_ub_pool)
    _ub_idx = 0

    _SCAN_COUNT += 1
    MONITORING_PAUSED = True
    _FLOOD_PENALTY = 0.0  # yangi skan — eski flood penaltyni nolga tushirish
    resource_start("Ochiq Guruh Skanerlash")

    if scan_id is None:
        sender_id = getattr(status_msg, 'chat_id', 0)
        scan_id = await db_mod.create_scan_session(str(target_group), output_path, sender_id)

    if resume_offset > 0 and os.path.exists(output_path):
        try:
            _loop = asyncio.get_running_loop()
            wb = await asyncio.wait_for(
                _loop.run_in_executor(None, openpyxl.load_workbook, output_path),
                timeout=30
            )
            ws = wb.active
        except Exception:
            resume_offset = 0
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Guruh Skaner"
            ws.append([
                "№", "Ism", "Familiya", "Username", "Telegram ID",
                "Telefon", "Bot?", "Premium?", "Bio",
                "Shaxsiy Kanal", "Maxfiy Kanal", "Ochiq Kanal", "Profil havolasi"
            ])
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Guruh Skaner"
        ws.append([
            "№", "Ism", "Familiya", "Username", "Telegram ID",
            "Telefon", "Bot?", "Premium?", "Bio",
            "Shaxsiy Kanal",    # personal_channel_id dan olingan
            "Maxfiy Kanal",     # Bio dagi t.me/+XXXX linklar
            "Ochiq Kanal",      # Bio dagi @username linklar
            "Profil havolasi"
        ])

    count   = resume_count
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    SAVE_EVERY = 50

    try:
        # pre_entity berilgan bo'lsa — qayta resolve qilinmaydi (ResolveUsernameRequest tejaladi)
        entity = pre_entity
        if entity is None:
            entity = await safe_get_entity(userbot, target_group)
        if entity is None:
            raise Exception("Guruh topilmadi yoki Telegram cheklovi. Biroz kutib qayta urining.")

        # --- 1-bosqich: a'zolar ro'yxati (aggressive=True) ---
        try:
            await status_msg.edit("📋 **1-bosqich:** A'zolar ro'yxati yuklanmoqda...")
        except Exception as e:
            _dbg("deep_scan_group", e)

        seen_ids = set()
        participants = []
        for _p_attempt in range(3):
            try:
                async for _u in userbot.iter_participants(entity, aggressive=True):
                    if _u.id not in seen_ids:
                        seen_ids.add(_u.id)
                        participants.append(_u)
                break  # muvaffaqiyatli tugadi
            except FloodWaitError as e:
                log_flood("iter_participants", e.seconds)
                _record_flood(e.seconds)
                await asyncio.sleep(min(e.seconds + 5, 300))
            except Exception:
                break

        phase1_count = len(participants)

        # --- 2-bosqich: xabar tarixi orqali qo'shimcha userlar ---
        try:
            await status_msg.edit(
                f"📨 **2-bosqich:** Xabarlar tarixidan qo'shimcha userlar izlanmoqda...\n"
                f"(1-bosqichda: {phase1_count} ta topildi)"
            )
        except Exception as e:
            _dbg("deep_scan_group", e)

        _msg2_count = 0
        _cache_batch = []
        _src_str = str(target_group)
        _iter_offset_id = 0
        _iter_done = False
        while not _iter_done:
            try:
                async for msg in userbot.iter_messages(
                    entity, limit=None,
                    offset_id=_iter_offset_id, reverse=False
                ):
                    if not msg.sender_id or msg.sender_id <= 0:
                        _iter_offset_id = msg.id
                        continue
                    _msg2_count += 1
                    _iter_offset_id = msg.id

                    if msg.sender_id not in seen_ids:
                        sender = msg.sender
                        if sender and not getattr(sender, 'bot', False) and not getattr(sender, 'deleted', False):
                            seen_ids.add(sender.id)
                            participants.append(sender)

                            # Topilgan zahoti to'liq ma'lumot olish va yozish
                            _fn  = sender.first_name or ""
                            _ln  = sender.last_name  or ""
                            _un  = ("@" + sender.username) if sender.username else ""
                            _ph  = sender.phone or ""
                            _bio = ""
                            _shaxsiy = ""
                            _maxfiy  = ""
                            _ochiq   = ""
                            _bday2   = ""
                            try:
                                await asyncio.sleep(0.5)
                                _ub2 = _ub_pool[_ub_idx % _ub_count]
                                _ub_idx += 1
                                fi = await asyncio.wait_for(
                                    _ub2(GetFullUserRequest(sender.id)), timeout=20
                                )
                                fu   = fi.full_user
                                _bio = fu.about or ""
                                _bday2 = _fmt_birthday(getattr(fu, 'birthday', None))
                                inv  = extract_invite_links(_bio)
                                if inv:
                                    _maxfiy = ", ".join(inv)
                                al  = extract_bio_links(_bio)
                                oc  = [l for l in al if l.startswith('@') or
                                       ('t.me/' in l and '/+' not in l and 'joinchat' not in l)]
                                _ochiq = ", ".join(oc) if oc else ""
                                pc  = getattr(fu, 'personal_channel_id', None)
                                if pc:
                                    _shaxsiy = await _pc_link_cached(userbot, pc, chats=getattr(fi, 'chats', None))
                                if inv:
                                    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as _db:
                                        await _db.executemany(
                                            "INSERT OR IGNORE INTO hidden_channel_knocker "
                                            "(channel_id, creator_id, source_group) VALUES (?,?,?)",
                                            [(lnk, sender.id, str(target_group)) for lnk in inv]
                                        )
                                        await _db.commit()
                            except FloodWaitError as e:
                                _record_flood(e.seconds)
                                log_flood("phase2_full_user", e.seconds)
                                await asyncio.sleep(min(e.seconds + 2, 120))
                            except Exception as e:
                                _dbg("deep_scan_group", e)

                            if _fn or _ln or _un or _bio:
                                _purl  = (f"https://t.me/{sender.username}" if sender.username
                                          else f"tg://user?id={sender.id}")
                                _bdate = _bday2 or extract_exact_birth_date(_bio)
                                count += 1
                                ws.append([
                                    count, _fn, _ln, _un, sender.id,
                                    ("+" + _ph) if _ph else "",
                                    "❌",
                                    "✅" if getattr(sender, "premium", False) else "❌",
                                    _bio, _shaxsiy, _maxfiy, _ochiq, _purl
                                ])
                                _has_db = _shaxsiy if _shaxsiy else (_maxfiy if _maxfiy else "❌")
                                await db_mod.save_user_to_bank(
                                    sender.id, str(target_group), _fn, _ln, _un,
                                    _ph, _bdate, _bio,
                                    ", ".join(extract_bio_links(_bio)),
                                    _has_db
                                )

                    # Matnli xabarlarni keshga yig'ish
                    _mc_text = msg.text or getattr(msg, 'caption', None) or ""
                    if len(_mc_text) > 2:
                        sender = msg.sender
                        s_id = getattr(sender, 'id', msg.sender_id) if sender else msg.sender_id
                        s_name = ""
                        s_un   = ""
                        if sender and hasattr(sender, 'first_name'):
                            s_name = ((sender.first_name or "") + " " + (sender.last_name or "")).strip()
                            s_un   = getattr(sender, 'username', '') or ""
                        elif sender and hasattr(sender, 'title'):
                            s_name = sender.title or ""
                            s_un   = getattr(sender, 'username', '') or ""
                        msg_dt = _fmt_date(msg.date)
                        _cache_batch.append((msg.id, _src_str, s_id, s_name, s_un, _mc_text[:2000], msg_dt))

                    # Har 1000 xabarda batch-insert
                    if len(_cache_batch) >= 1000:
                        try:
                            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                                await _db.executemany(
                                    "INSERT OR IGNORE INTO messages_cache "
                                    "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                                    "VALUES (?,?,?,?,?,?,?)",
                                    _cache_batch
                                )
                                await _db.commit()
                        except Exception as e:
                            _dbg("deep_scan_group", e)
                        _cache_batch = []

                    if _msg2_count % 2000 == 0:
                        try:
                            await status_msg.edit(
                                f"📨 **2-bosqich:** `{_msg2_count}` xabar ko'rildi\n"
                                f"👥 Yangi topilgan: `{len(participants) - phase1_count}` ta..."
                            )
                        except Exception as e:
                            _dbg("deep_scan_group", e)
                _iter_done = True  # barcha xabarlar muvaffaqiyatli o'qildi
            except FloodWaitError as e:
                log_flood("iter_messages_scan", e.seconds)
                await asyncio.sleep(min(e.seconds + 5, 300))
                # flood dan keyin davom etamiz (while loop qayta ishlaydi)
            except Exception as _msg_err:
                print(f"[deep_scan] iter_messages xatosi: {_msg_err}")
                _iter_done = True  # boshqa xatoda to'xtatamiz

        # Qolgan kesh batchni saqlash
        if _cache_batch:
            try:
                async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                    await _db.executemany(
                        "INSERT OR IGNORE INTO messages_cache "
                        "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                        "VALUES (?,?,?,?,?,?,?)",
                        _cache_batch
                    )
                    await _db.commit()
                asyncio.create_task(_check_batch_alerts(list(_cache_batch)))
            except Exception as e:
                _dbg("deep_scan_group", e)

        total = len(participants)
        _cached_msg_count = _msg2_count  # nechta xabar keshga yig'ildi

        try:
            await status_msg.edit(
                f"📨 **2-bosqich tugadi:** `{_cached_msg_count}` xabar keshga saqlandi\n"
                f"👥 Jami **{total}** ta unikal foydalanuvchi topildi\n"
                f"(a'zolar: {phase1_count} + xabar tarixi: {total - phase1_count})\n"
                f"🔍 Profillar tahlil qilinmoqda..."
            )
        except Exception as e:
            _dbg("deep_scan_group", e)

        # Ketma-ket oddiy loop — faqat Phase 1 a'zolari (Phase 2 da yozildi)
        work_list = [u for i, u in enumerate(participants[:phase1_count]) if i >= resume_offset]
        for _wi, user in enumerate(work_list):
            while SCANNER_PAUSED:
                await asyncio.sleep(1)

            uid = user.id
            bio = ""
            shaxsiy = ""
            maxfiy = ""
            ochiq = ""
            _bdayP = ""

            try:
                await asyncio.sleep(0.5)   # flood oldini olish — 2 userbot = har biri 1.0s/call
                _ub3 = _ub_pool[_ub_idx % _ub_count]
                _ub_idx += 1
                fi = await asyncio.wait_for(
                    _ub3(GetFullUserRequest(uid)), timeout=20
                )
                fu  = fi.full_user
                bio = fu.about or ""
                _bdayP = _fmt_birthday(getattr(fu, 'birthday', None))
                inv = extract_invite_links(bio)
                if inv:
                    maxfiy = ", ".join(inv)
                al = extract_bio_links(bio)
                oc = [l for l in al if l.startswith('@') or
                      ('t.me/' in l and '/+' not in l and 'joinchat' not in l)]
                ochiq = ", ".join(oc) if oc else ""
                pc = getattr(fu, 'personal_channel_id', None)
                if pc:
                    shaxsiy = await _pc_link_cached(userbot, pc, chats=getattr(fi, 'chats', None))
                if inv:
                    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as _db:
                        await _db.executemany(
                            "INSERT OR IGNORE INTO hidden_channel_knocker "
                            "(channel_id, creator_id, source_group) VALUES (?,?,?)",
                            [(lnk, uid, str(target_group)) for lnk in inv]
                        )
                        await _db.commit()
            except FloodWaitError as e:
                _record_flood(e.seconds)
                log_flood("deep_scan_group", e.seconds)
                await asyncio.sleep(min(e.seconds + 2, 120))
            except Exception as e:
                _dbg("deep_scan_group", e)

            first_name = user.first_name or ""
            last_name  = user.last_name  or ""
            uname      = ("@" + user.username) if user.username else ""
            phone      = user.phone or ""
            is_bot     = "✅" if user.bot else "❌"
            is_premium = "✅" if getattr(user, "premium", False) else "❌"

            if not first_name and not last_name and not uname and not bio:
                continue

            profile_url = (f"https://t.me/{user.username}" if user.username
                           else f"tg://user?id={uid}")
            b_date = _bdayP or extract_exact_birth_date(bio)

            count += 1
            ws.append([
                count, first_name, last_name, uname, uid,
                ("+" + phone) if phone else "",
                is_bot, is_premium, bio,
                shaxsiy, maxfiy, ochiq, profile_url
            ])

            has_db = shaxsiy if shaxsiy else (maxfiy if maxfiy else "❌")
            await db_mod.save_user_to_bank(
                uid, str(target_group), first_name, last_name, uname,
                phone, b_date, bio,
                ", ".join(extract_bio_links(bio)),
                has_db
            )

            # Har SAVE_EVERY da Excel ga yozish + progress saqlash
            if count > 0 and count % SAVE_EVERY == 0:
                _sv_loop = asyncio.get_running_loop()
                await _sv_loop.run_in_executor(None, wb.save, output_path)
                await db_mod.update_scan_progress(scan_id, resume_offset + _wi + 1, count)

            # Status yangilash — har 100 ta profilda
            if count % 100 == 0:
                try:
                    await status_msg.edit(
                        f"🔍 **Skanerlamoqda:** `{count}/{total}` ta profil..."
                    )
                except Exception as e:
                    _dbg("deep_scan_group", e)

    except Exception as e:
        await db_mod.finish_scan_session(scan_id, status='error')
        raise
    else:
        await db_mod.finish_scan_session(scan_id, status='done')
    finally:
        # Eng oxirgi saqlash — har doim bajariladi
        try:
            _fl = asyncio.get_running_loop()
            await _fl.run_in_executor(None, apply_excel_styles, ws, count)
            await _fl.run_in_executor(None, wb.save, output_path)
        except Exception as e:
            _dbg("deep_scan_group", e)
        _SCAN_COUNT -= 1
        if _SCAN_COUNT <= 0:
            _SCAN_COUNT = 0
            MONITORING_PAUSED = False
        resource_stop("Ochiq Guruh Skanerlash")

    return count


# ─────────────────────────────────────────────────────────────────────
# RESUME
# ─────────────────────────────────────────────────────────────────────

async def resume_pending_scans(userbot, bot, admin_id):
    pending = await db_mod.get_pending_scans()
    if not pending:
        return
    for scan_id, target_group, output_path, last_offset, total_count, sender_id in pending:
        # scan type ni aniqlash
        scan_type = 'group'  # default
        try:
            notify_id  = sender_id or admin_id
            status_msg = await bot.send_message(
                notify_id,
                f"♻️ **Tugallanmagan skanerlash davom ettirilmoqda!**\n"
                f"🏢 Guruh: `{target_group}`\n"
                f"📊 Oldindan: `{total_count}` ta\n"
                f"⏩ `{last_offset}` pozitsiyadan davom..."
            )
            asyncio.create_task(
                _resume_scan_task(userbot, bot, notify_id, target_group,
                                   output_path, last_offset, total_count,
                                   scan_id, status_msg)
            )
        except Exception as e:
            print(f"Resume xatosi: {e}")
            await db_mod.finish_scan_session(scan_id, status='error')


async def _resume_scan_task(userbot, bot, sender_id, target_group,
                             output_path, last_offset, total_count,
                             scan_id, status_msg):
    try:
        count = await deep_scan_group(
            userbot, target_group, output_path, status_msg,
            resume_offset=last_offset, resume_count=total_count, scan_id=scan_id
        )
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            await db.execute(
                "INSERT INTO archive_bin (file_name, file_path, created_date) VALUES (?, ?, ?)",
                (os.path.basename(output_path), output_path,
                 datetime.now().strftime("%Y-%m-%d"))
            )
            await db.commit()
        await bot.send_file(
            sender_id, output_path,
            caption=f"✅ Resume yakunlandi! Jami `{count}` ta profil."
        )
    except Exception as e:
        await bot.send_message(sender_id, f"❌ Resume xatolik: {e}")
    finally:
        try:
            await status_msg.delete()
        except Exception as e:
            _dbg("_resume_scan_task", e)


# ─────────────────────────────────────────────────────────────────────
# 24 SOATLIK MAXFIY KANAL MONITOR
# Faqat bio dagi t.me/+XXXX invite linklariga so'rovnoma yuboradi
# ─────────────────────────────────────────────────────────────────────

# Kunlik so'rovnoma hisoblagich
_daily_join_count = 0
_daily_join_date  = ""
MAX_DAILY_JOINS   = 288  # Kuniga maksimal so'rovnomalar soni


async def background_profile_tracker(userbot, ub_idx: int = 0, n_userbots: int = 1):
    """
    ub_idx=0, n_userbots=2 → faqat juft user_id lar (0,2,4,...)
    ub_idx=1, n_userbots=2 → faqat toq user_id lar (1,3,5,...)
    """
    batch_size = 50

    # Kanal musiqa skanerlash tugaguncha kutish
    global _CHANNEL_MUSIC_DONE
    label = f"[PROFIL-UB{ub_idx+1}]"
    print(f"{label} Kanal musiqalari tugashini kutmoqda...")
    while not _CHANNEL_MUSIC_DONE:
        await asyncio.sleep(30)
    print(f"{label} Kanal musiqalari tugadi — profil musiqasi boshlanadi")

    # Har userbot uchun alohida offset key
    _offset_key = f"profile_offset_{ub_idx}"
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS tracker_state "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        await db.commit()
        async with db.execute(
            "SELECT value FROM tracker_state WHERE key=?", (_offset_key,)
        ) as cur:
            row = await cur.fetchone()
            offset = int(row[0]) if row else 0

    while True:
        if MONITORING_PAUSED:
            await asyncio.sleep(15)
            continue
        try:
            async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                # bio/has_hidden ham partiya so'rovida olinadi — har user uchun
                # alohida SELECT ochilmaydi (DB ulanishlar kamayadi)
                if n_userbots > 1:
                    # Har userbot faqat o'z user_id larini oladi (modulo bo'yicha)
                    async with db.execute(
                        "SELECT user_id, bio, has_hidden FROM users_memory_bank "
                        "WHERE (CAST(user_id AS INTEGER) % ?) = ? "
                        "GROUP BY user_id ORDER BY user_id LIMIT ? OFFSET ?",
                        (n_userbots, ub_idx, batch_size, offset)
                    ) as cur:
                        users = await cur.fetchall()
                else:
                    async with db.execute(
                        "SELECT user_id, bio, has_hidden FROM users_memory_bank "
                        "GROUP BY user_id ORDER BY user_id LIMIT ? OFFSET ?",
                        (batch_size, offset)
                    ) as cur:
                        users = await cur.fetchall()

            if not users:
                offset = 0
                async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO tracker_state (key, value) VALUES (?, '0')",
                        (_offset_key,)
                    )
                    await db.commit()
                print(f"{label} Barcha profillar tekshirildi — 30 daqiqa kutilmoqda")
                await asyncio.sleep(1800)
                continue

            now_str    = datetime.now().strftime("%Y-%m-%d %H:%M")
            # GetFullUserRequest: Telegram ~80 req/min limit
            # 2 parallel + 1.5s sleep = ~1.3 req/s = 78 req/min — xavfsiz chegara
            _api_sem   = asyncio.Semaphore(2)
            # i3/i5 uchun: 3 parallel — CPU thrashing oldini oladi
            _dl_sem    = asyncio.Semaphore(3)

            async def _do_one_profile(uid, old_bio="", old_has_hidden=""):
                # Rate-limiting uyqu SEMAPHORE TASHQARISIDA — boshqa tasklar bloklanmaydi
                await asyncio.sleep(random.uniform(1.2, 1.8))
                try:
                    # old_bio / old_has_hidden partiya so'rovidan keladi
                    old_bio        = old_bio or ""
                    old_has_hidden = old_has_hidden or ""

                    # Semaphore FAQAT API chaqiruvi atrofida
                    async with _api_sem:
                        fi = await _safe_api_call(lambda: userbot(GetFullUserRequest(uid)))
                    if fi is None:
                        return
                    # O'chirilgan akkaunt (deleted account) — javobning o'zida keladi,
                    # qo'shimcha API so'rovi YO'Q. Bankdan o'chirib, o'tkazib yuboramiz.
                    _u_obj = (getattr(fi, 'users', None) or [None])[0]
                    if _u_obj is not None and getattr(_u_obj, 'deleted', False):
                        try:
                            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _ddb:
                                await _ddb.execute(
                                    "DELETE FROM users_memory_bank WHERE user_id=?", (uid,)
                                )
                                await _ddb.commit()
                        except Exception as e:
                            _dbg("delete_deleted_account", e)
                        return
                    bio = fi.full_user.about or ""

                    # Profil musiqasi — barcha 4 field, semaphore tashqarisida
                    try:
                        music_docs = []
                        for field in ['saved_music', 'profile_song', 'profile_songs', 'music']:
                            val = getattr(fi.full_user, field, None)
                            if val is None:
                                continue
                            if isinstance(val, list):
                                music_docs.extend(val)
                            else:
                                music_docs.append(val)

                        async def _proc_doc(idx, doc):
                            if hasattr(doc, 'document'):
                                doc = doc.document
                            if not hasattr(doc, 'id'):
                                return
                            # Audio nomi/ijrochisi → keshga (yuklamasdan, attributlardan)
                            try:
                                _title = _perf = ""
                                for _attr in getattr(doc, 'attributes', []) or []:
                                    if _attr.__class__.__name__ == 'DocumentAttributeAudio':
                                        _title = getattr(_attr, 'title', '') or ""
                                        _perf  = getattr(_attr, 'performer', '') or ""
                                        break
                                if _title or _perf:
                                    _meta = f"🎵 Profil musiqasi: {_perf} - {_title}".strip(" -")
                                    _now = datetime.now().strftime("%Y-%m-%d %H:%M")
                                    async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _mdb:
                                        await _mdb.execute(
                                            "INSERT OR IGNORE INTO messages_cache "
                                            "(msg_id, source, sender_id, sender_name, sender_username, text, msg_date) "
                                            "VALUES (?,?,?,?,?,?,?)",
                                            (doc.id, f"profile_music:{uid}", uid, "", "", _meta, _now)
                                        )
                                        await _mdb.commit()
                            except Exception as e:
                                _dbg("_proc_doc_meta", e)
                            tmp_music = os.path.join(
                                os.path.dirname(os.path.abspath(__file__)),
                                f"tmp_profile_{uid}_{idx}.ogg"
                            )
                            try:
                                # Yuklashdan OLDIN tekshiruv — doc.id bazada bormi?
                                _already_dl = False
                                try:
                                    async with db_mod.connect(music_mod.MUSIC_DB, timeout=5) as _cdb:
                                        async with _cdb.execute(
                                            "SELECT 1 FROM music_fingerprints "
                                            "WHERE channel_id=? AND file_name=?",
                                            (str(uid), f"profile_{uid}_{idx}")
                                        ) as _cc:
                                            if await _cc.fetchone():
                                                _already_dl = True
                                except Exception:
                                    pass
                                if _already_dl:
                                    return
                                async with _dl_sem:
                                    await userbot.download_media(doc, file=tmp_music)
                                if os.path.exists(tmp_music):
                                    fp, duration = await music_mod.get_fingerprint_async(tmp_music)
                                    if fp:
                                        await music_mod.init_music_db()
                                        already = await music_mod.is_profile_music_saved(uid, fp)
                                        if not already:
                                            await music_mod.save_fingerprint(
                                                str(uid), f"Profil: {uid}",
                                                f"profile_{uid}_{idx}", fp, duration or 0
                                            )
                                        hits = await music_mod.check_against_watch_list(fp)
                                        for hit in hits:
                                            if _WATCH_ALERTS is not None:
                                                _WATCH_ALERTS.put_nowait({
                                                'admin_id':    hit['admin_id'],
                                                'watch_name':  hit['watch_name'],
                                                'score':       hit['score'],
                                                'source_name': f"Profil: {uid}",
                                                'source_id':   str(uid),
                                                'source_type': 'profil'
                                            })
                            except Exception as e:
                                _dbg("_proc_doc", e)
                            finally:
                                if os.path.exists(tmp_music):
                                    os.remove(tmp_music)

                        if music_docs:
                            await asyncio.gather(
                                *[_proc_doc(i, d) for i, d in enumerate(music_docs)],
                                return_exceptions=True
                            )
                    except Exception as e:
                        _dbg("_proc_doc", e)

                    # has_hidden hisoblash
                    invite_links = extract_invite_links(bio)
                    has_hidden   = "❌"
                    if invite_links:
                        has_hidden = ", ".join(invite_links)
                        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                            await db.executemany(
                                "INSERT OR IGNORE INTO hidden_channel_knocker "
                                "(channel_id, creator_id, source_group) VALUES (?, ?, ?)",
                                [(inv_link, uid, "Monitoring") for inv_link in invite_links]
                            )
                            await db.commit()
                    elif getattr(fi.full_user, 'personal_channel_id', None):
                        ch_id = fi.full_user.personal_channel_id
                        has_hidden = await _pc_link_cached(userbot, ch_id, chats=getattr(fi, 'chats', None))

                    open_ch = ", ".join(extract_bio_links(bio))

                    bio_changed    = bio.strip() != old_bio.strip()
                    hidden_changed = has_hidden != old_has_hidden

                    if bio_changed or hidden_changed:
                        await db_mod.update_user_changes(uid, bio, open_ch, has_hidden)
                    else:
                        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                            await db.execute(
                                "UPDATE users_memory_bank SET bio=?, open_channels=? WHERE user_id=?",
                                (bio, open_ch, uid)
                            )
                            await db.commit()

                except Exception as e:
                    _dbg("_proc_doc", e)
                finally:
                    # Post-processing uyqu SEMAPHORE TASHQARISIDA — to'g'ri tezlik
                    if _RESOURCE['profile_slow']:
                        await asyncio.sleep(random.uniform(3, 6))
                    else:
                        await asyncio.sleep(random.uniform(1.5, 3))

            # 5 ta profil parallel skanerlash
            await asyncio.gather(
                *[asyncio.create_task(_do_one_profile(_u[0], _u[1], _u[2])) for _u in users],
                return_exceptions=True
            )

            offset += batch_size
            try:
                async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO tracker_state (key, value) VALUES (?, ?)",
                        (_offset_key, str(offset))
                    )
                    await db.commit()
            except Exception as e:
                _dbg("_proc_doc", e)
        except RpcCallFailError as e:
            print(f"{label} Telegram server xatosi (RpcCallFail): {e}. 60s kutilmoqda...")
            await asyncio.sleep(60)
            continue
        except Exception as e:
            print(f"{label} xato: {e}")
        await asyncio.sleep(60)


# ─────────────────────────────────────────────────────────────────────
# XABAR SKANERLASH
# Guruh/kanal xabarlaridan yozgan odamlarni topadi
# A'zo bo'lmasa ham ochiq guruhda ishlaydi
# ─────────────────────────────────────────────────────────────────────

_SCAN_CHUNK = 2000   # har userbot bir tsiklda o'qiydigan xabar soni


async def _read_msg_chunk(ub, entity, add_offset: int, limit: int,
                          unique_users: dict, unique_ids: set,
                          src_str: str, cutoff=None) -> int:
    """add_offset dan boshlab limit ta xabar o'qiydi, foydalanuvchilarni yig'adi.
    cutoff berilsa — undan eski xabarga yetganda to'xtaydi (oxirgi N kun)."""
    from datetime import timezone as _tz
    count = 0
    local_cache = []
    try:
        async for msg in ub.iter_messages(entity, limit=limit,
                                          add_offset=add_offset):
            # Sana filtri — cutoff dan eski bo'lsa to'xtatish
            if cutoff and msg.date:
                _md = msg.date.replace(tzinfo=_tz.utc) if msg.date.tzinfo is None else msg.date
                if _md < cutoff:
                    break
            if msg.sender_id and msg.sender_id > 0:
                sender = msg.sender
                if sender and not getattr(sender, 'bot', False) and not getattr(sender, 'deleted', False) and hasattr(sender, 'first_name'):
                    if msg.sender_id not in unique_users:
                        unique_users[msg.sender_id] = sender
                else:
                    unique_ids.add(msg.sender_id)

            _mc_text = msg.text or getattr(msg, 'caption', None) or ""
            if len(_mc_text) > 2:
                sender = msg.sender
                s_id = getattr(sender, 'id', msg.sender_id or 0) if sender else (msg.sender_id or 0)
                s_name, s_un = "", ""
                if sender and hasattr(sender, 'first_name'):
                    s_name = ((sender.first_name or "") + " " + (sender.last_name or "")).strip()
                    s_un = getattr(sender, 'username', '') or ""
                elif sender and hasattr(sender, 'title'):
                    s_name = sender.title or ""
                    s_un = getattr(sender, 'username', '') or ""
                msg_dt = _fmt_date(msg.date)
                local_cache.append((msg.id, src_str, s_id, s_name, s_un, _mc_text[:2000], msg_dt))
                if len(local_cache) >= 300:
                    try:
                        async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                            await _db.executemany(
                                "INSERT OR IGNORE INTO messages_cache "
                                "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                                "VALUES (?,?,?,?,?,?,?)", local_cache
                            )
                            await _db.commit()
                        asyncio.create_task(_check_batch_alerts(list(local_cache)))
                    except Exception as e:
                        _dbg("_read_msg_chunk", e)
                    local_cache.clear()

            count += 1
    except Exception as e:
        print(f"[SCAN-CHUNK] offset={add_offset} xato: {e}")
    finally:
        if local_cache:
            try:
                async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                    await _db.executemany(
                        "INSERT OR IGNORE INTO messages_cache "
                        "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                        "VALUES (?,?,?,?,?,?,?)", local_cache
                    )
                    await _db.commit()
                asyncio.create_task(_check_batch_alerts(list(local_cache)))
            except Exception as e:
                _dbg("_read_msg_chunk", e)
    return count


async def scan_messages(userbot, target, output_path, status_msg, days=None,
                        resume_offset=0, resume_count=0, scan_id=None,
                        extra_userbot=None):
    """
    Guruh/kanal xabarlarini o'qib, yozgan foydalanuvchilarni skanerLaydi.
    """
    global _SCAN_COUNT, MONITORING_PAUSED, _FLOOD_PENALTY
    _SCAN_COUNT += 1
    MONITORING_PAUSED = True
    _FLOOD_PENALTY = 0.0
    _RESOURCE['music_paused'] = True
    _RESOURCE['profile_slow'] = True
    _RESOURCE['heavy_scan'] = True
    _RESOURCE['current_task'] = "Yopiq Guruh Skanerlash"
    _RESOURCE['task_start'] = datetime.now()

    if scan_id is None:
        sender_id_tmp = getattr(status_msg, 'sender_id', 0) or getattr(status_msg, 'chat_id', 0)
        scan_id = await db_mod.create_scan_session(str(target), output_path, sender_id_tmp)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        # Guruhga ulanish — a'zo bo'lmasa join qilish
        try:
            entity = await userbot.get_entity(target)
        except Exception:
            try:
                await userbot(JoinChannelRequest(target))
                await asyncio.sleep(1)
                entity = await userbot.get_entity(target)
            except Exception as e:
                raise Exception(f"Guruhga ulanib bo'lmadi: {e}")

        try:
            await status_msg.edit("📨 Xabarlar o'qilmoqda, foydalanuvchilar aniqlanmoqda...")
        except Exception as e:
            _dbg("scan_messages", e)

        # Xabar yozgan unikal foydalanuvchilarni yig'ish
        unique_users = {}  # user_id → user object

        # Sana filtri — "oxirgi N kun". offset_date ISHLATILMAYDI (Telethon uni
        # teskari talqin qiladi). Eng yangidan boshlab cutoff da to'xtatamiz.
        from datetime import timezone
        cutoff = None
        if days:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Xabarlardan foydalanuvchilarni yig'ish + keshga saqlash
        unique_ids = set()
        msg_count_tmp = 0
        _src_str = str(target)

        if extra_userbot is None:
            # Bitta userbot — oddiy rejim
            _cache_batch = []
            async for msg in userbot.iter_messages(entity, limit=None):
                # Sana filtri — cutoff dan eski bo'lsa to'xtatish
                if cutoff and msg.date:
                    _md = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date
                    if _md < cutoff:
                        break
                if msg.sender_id and msg.sender_id > 0:
                    sender = msg.sender
                    if sender and not getattr(sender, 'bot', False) and not getattr(sender, 'deleted', False) and hasattr(sender, 'first_name'):
                        if msg.sender_id not in unique_users:
                            unique_users[msg.sender_id] = sender
                    else:
                        unique_ids.add(msg.sender_id)
                _mc_text = msg.text or getattr(msg, 'caption', None) or ""
                if len(_mc_text) > 2:
                    sender = msg.sender
                    s_id = getattr(sender, 'id', msg.sender_id or 0) if sender else (msg.sender_id or 0)
                    s_name, s_un = "", ""
                    if sender and hasattr(sender, 'first_name'):
                        s_name = ((sender.first_name or "") + " " + (sender.last_name or "")).strip()
                        s_un = getattr(sender, 'username', '') or ""
                    elif sender and hasattr(sender, 'title'):
                        s_name = sender.title or ""
                        s_un = getattr(sender, 'username', '') or ""
                    msg_dt = _fmt_date(msg.date)
                    _cache_batch.append((msg.id, _src_str, s_id, s_name, s_un, _mc_text[:2000], msg_dt))
                    if len(_cache_batch) >= 300:
                        try:
                            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                                await _db.executemany(
                                    "INSERT OR IGNORE INTO messages_cache "
                                    "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                                    "VALUES (?,?,?,?,?,?,?)", _cache_batch
                                )
                                await _db.commit()
                            asyncio.create_task(_check_batch_alerts(list(_cache_batch)))
                        except Exception as e:
                            _dbg("scan_messages", e)
                        _cache_batch.clear()
                msg_count_tmp += 1
                if msg_count_tmp % 1000 == 0:
                    try:
                        await status_msg.edit(
                            f"📨 `{msg_count_tmp}` ta xabar o'qildi | "
                            f"👥 `{len(unique_users) + len(unique_ids)}` ta unikal..."
                        )
                    except Exception as e:
                        _dbg("scan_messages", e)
            if _cache_batch:
                try:
                    async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                        await _db.executemany(
                            "INSERT OR IGNORE INTO messages_cache "
                            "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                            "VALUES (?,?,?,?,?,?,?)", _cache_batch
                        )
                        await _db.commit()
                    asyncio.create_task(_check_batch_alerts(list(_cache_batch)))
                except Exception as e:
                    _dbg("scan_messages", e)
        else:
            # Ikki userbot — 2000 tadan navbatma-navbat, parallel
            # UB1: chunk 0 (0-1999), chunk 2 (4000-5999), ...
            # UB2: chunk 1 (2000-3999), chunk 3 (6000-7999), ...
            entity2 = entity
            try:
                try:
                    entity2 = await extra_userbot.get_entity(target)
                except Exception:
                    await extra_userbot(JoinChannelRequest(target))
                    await asyncio.sleep(1)
                    entity2 = await extra_userbot.get_entity(target)
            except Exception:
                pass  # UB2 kira olmasa UB1 davom etadi

            pair = 0
            while True:
                off1 = pair * _SCAN_CHUNK * 2
                off2 = off1 + _SCAN_CHUNK
                c1, c2 = await asyncio.gather(
                    _read_msg_chunk(userbot,       entity,  off1, _SCAN_CHUNK,
                                    unique_users, unique_ids, _src_str, cutoff),
                    _read_msg_chunk(extra_userbot, entity2, off2, _SCAN_CHUNK,
                                    unique_users, unique_ids, _src_str, cutoff),
                    return_exceptions=True
                )
                c1 = c1 if isinstance(c1, int) else 0
                c2 = c2 if isinstance(c2, int) else 0
                msg_count_tmp += c1 + c2
                try:
                    await status_msg.edit(
                        f"📨 `{msg_count_tmp}` ta xabar o'qildi (UB1+UB2) | "
                        f"👥 `{len(unique_users) + len(unique_ids)}` ta unikal..."
                    )
                except Exception as e:
                    _dbg("scan_messages", e)
                # Ikkalasi ham to'liq chunk o'qimagan → xabarlar tugadi
                if c1 < _SCAN_CHUNK and c2 < _SCAN_CHUNK:
                    break
                pair += 1

        # Sender None bo'lganlarni get_entity bilan olish
        missing = unique_ids - set(unique_users.keys())
        if missing:
            try:
                await status_msg.edit(
                    f"👥 `{len(missing)}` ta profil ma'lumoti olinmoqda..."
                )
            except Exception as e:
                _dbg("scan_messages", e)
            for uid in missing:
                try:
                    user = await userbot.get_entity(uid)
                    if user and not getattr(user, 'bot', False) and not getattr(user, 'deleted', False):
                        unique_users[uid] = user
                except FloodWaitError as e:
                    log_flood("scan_messages_get_entity", e.seconds)
                    await asyncio.sleep(min(e.seconds + 3, 300))
                    try:
                        user = await userbot.get_entity(uid)
                        if user and not getattr(user, 'bot', False) and not getattr(user, 'deleted', False):
                            unique_users[uid] = user
                    except Exception:
                        # Ma'lumot olib bo'lmasa ham ID bilan yozish
                        class MinimalUser:
                            def __init__(self, user_id):
                                self.id = user_id
                                self.first_name = ""
                                self.last_name = ""
                                self.username = None
                                self.phone = None
                                self.bot = False
                        unique_users[uid] = MinimalUser(uid)
                except Exception:
                    # Ma'lumot olib bo'lmasa ham ID bilan yozish
                    class MinimalUser:
                        def __init__(self, user_id):
                            self.id = user_id
                            self.first_name = ""
                            self.last_name = ""
                            self.username = None
                            self.phone = None
                            self.bot = False
                    unique_users[uid] = MinimalUser(uid)

        total = len(unique_users)
        try:
            await status_msg.edit(f"👥 {total} ta unikal foydalanuvchi topildi. Profillar tahlil qilinmoqda...")
        except Exception as e:
            _dbg("__init__", e)

        if not unique_users:
            raise Exception("Xabar yozgan foydalanuvchi topilmadi.")

        # Excel tayyorlash
        wb    = openpyxl.Workbook()
        sheet = wb.active
        sheet.title = "Xabar Skaneri"
        sheet.append([
            "№", "Ism", "Familiya", "Username", "Telegram ID",
            "Telefon", "Bio", "Bio Linklar",
            "Shaxsiy Kanal Linki", "Maxfiy Kanal Linki", "Profil havolasi"
        ])

        count    = 0
        SAVE_EVERY = 50

        for uid, user in unique_users.items():
            while SCANNER_PAUSED:
                await asyncio.sleep(1)

            count += 1
            if count % 20 == 0:
                try:
                    await status_msg.edit(
                        f"🔍 **Tahlil qilinmoqda:** `{count}/{total}` ta profil..."
                    )
                except Exception as e:
                    _dbg("__init__", e)

            f_name = user.first_name or ""
            l_name = user.last_name  or ""
            uname  = ("@" + user.username) if user.username else ""
            phone  = getattr(user, 'phone', '') or ""
            bio      = ""
            shaxsiy  = ""
            maxfiy   = ""
            _bdayM   = ""

            await asyncio.sleep(0.35)
            try:
                fi = await asyncio.wait_for(
                    userbot(GetFullUserRequest(uid)), timeout=20
                )
                bio = fi.full_user.about or ""
                _bdayM = _fmt_birthday(getattr(fi.full_user, 'birthday', None))
                inv_links = extract_invite_links(bio)
                if inv_links:
                    maxfiy = ", ".join(inv_links)
                    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                        await db.executemany(
                            "INSERT OR IGNORE INTO hidden_channel_knocker "
                            "(channel_id, creator_id, source_group) "
                            "VALUES (?, ?, ?)",
                            [(inv, uid, str(target)) for inv in inv_links]
                        )
                        await db.commit()
                ch_id = getattr(fi.full_user, 'personal_channel_id', None)
                if ch_id:
                    shaxsiy = await _pc_link_cached(userbot, ch_id, chats=getattr(fi, 'chats', None))
            except FloodWaitError as e:
                _record_flood(e.seconds)
                log_flood("scan_messages_user", e.seconds)
                await asyncio.sleep(min(e.seconds + 2, 120))
            except Exception as e:
                _dbg("__init__", e)

            # O'chirilgan hisob → o'tkazib yuborish
            if not f_name and not l_name and not uname and not bio:
                continue

            bio_links_str = ", ".join(extract_bio_links(bio)) if bio else ""
            p_link  = ("https://t.me/" + user.username) if user.username else f"tg://user?id={uid}"
            b_date  = _bdayM or extract_exact_birth_date(bio)
            has_db  = maxfiy if maxfiy else (shaxsiy if shaxsiy else "❌")

            sheet.append([
                count, f_name, l_name, uname, uid,
                ("+" + phone) if phone else "",
                bio, bio_links_str,
                shaxsiy, maxfiy, p_link
            ])

            # Bazaga saqlash
            await db_mod.save_user_to_bank(
                uid, str(target), f_name, l_name, uname,
                phone, b_date, bio, bio_links_str, has_db
            )

            if count % SAVE_EVERY == 0:
                # Bloklamaslik uchun ishchi oqimda (event loop band bo'lmaydi)
                _sv = asyncio.get_running_loop()
                await _sv.run_in_executor(None, apply_excel_styles, sheet, count)
                await _sv.run_in_executor(None, wb.save, output_path)

    except Exception as e:
        await db_mod.finish_scan_session(scan_id, status='error')
        raise e
    finally:
        _SCAN_COUNT -= 1
        if _SCAN_COUNT <= 0:
            _SCAN_COUNT = 0
            MONITORING_PAUSED = False
            _RESOURCE['music_paused'] = False
            _RESOURCE['profile_slow'] = False
            _RESOURCE['heavy_scan'] = False
            _RESOURCE['current_task'] = None
            _RESOURCE['task_start'] = None

    _sv = asyncio.get_running_loop()
    await _sv.run_in_executor(None, apply_excel_styles, sheet, count)
    await _sv.run_in_executor(None, wb.save, output_path)
    await db_mod.finish_scan_session(scan_id, status='done')
    return count



# ─────────────────────────────────────────────────────────────────────
# KANAL COMMENT SKANERLASH
# Kanal postlaridagi commentariyalardan foydalanuvchilarni topadi
# ─────────────────────────────────────────────────────────────────────

async def _comment_scan_parallel(all_bots, target, output_path, status_msg, scan_id):
    """
    Kanal komentariya (discussion guruh)ni BARCHA userbot birgalikda skanerlaydi.
    Har bot mustaqil: kanal → discussion ni topadi (o'z access_hash i),
    keyin xabar ID-oralig'ini bo'lib oladi. Ochiq kanal — a'zolik shart emas.
    """
    n = len(all_bots)

    # Har bot mustaqil: kanal → discussion guruh
    disc_entities = []
    for ub in all_bots:
        ent = None
        try:
            ch = await ub.get_entity(target)
            full_ch = await ub(GetFullChannelRequest(ch))
            linked_id = getattr(full_ch.full_chat, 'linked_chat_id', None)
            if linked_id:
                ent = await ub.get_entity(linked_id)
        except Exception as e:
            _dbg("_comment_scan_parallel", e)
        disc_entities.append(ent)

    if disc_entities[0] is None:
        raise Exception(
            "Bu kanalda comment bo'limi (discussion guruh) topilmadi yoki ulanib bo'lmadi."
        )

    rows = []
    rows_lock = asyncio.Lock()
    seen = set()
    seen_lock = asyncio.Lock()

    try:
        await status_msg.edit(f"💬 **Parallel komentariya:** {n} ta userbot bo'lib o'qimoqda...")
    except Exception as e:
        _dbg("_comment_scan_parallel", e)

    # Eng katta xabar ID si — oraliqlarga bo'lish uchun
    try:
        _last = await all_bots[0].get_messages(disc_entities[0], limit=1)
        max_id = _last[0].id if _last else 0
    except Exception:
        max_id = 0

    if max_id <= 0:
        raise Exception("Discussion guruhda xabar topilmadi.")

    chunk = max(1, max_id // n + 1)

    async def _worker(k):
        ub  = all_bots[k]
        ent = disc_entities[k]
        if ent is None:
            return
        lower  = k * chunk
        offset = (k + 1) * chunk + 1
        sem    = asyncio.Semaphore(2)
        tasks  = []
        cache_batch = []
        try:
            async for msg in ub.iter_messages(ent, offset_id=offset, min_id=lower):
                sid = msg.sender_id
                if sid and sid > 0:
                    async with seen_lock:
                        is_new = sid not in seen
                        if is_new:
                            seen.add(sid)
                    if is_new:
                        sender = msg.sender
                        if sender and not getattr(sender, 'bot', False) and not getattr(sender, 'deleted', False):
                            async def _do(ss):
                                async with sem:
                                    await asyncio.sleep(0.5)
                                    row = await _ds_process_user(ub, ss, target)
                                    if row:
                                        async with rows_lock:
                                            rows.append(row)
                            tasks.append(asyncio.create_task(_do(sender)))
                            if len(tasks) >= 300:
                                await asyncio.gather(*tasks, return_exceptions=True)
                                tasks = []
                _txt = msg.text or getattr(msg, 'caption', None) or ""
                if len(_txt) > 2:
                    sd = msg.sender
                    sn = su = ""
                    if sd and hasattr(sd, 'first_name'):
                        sn = ((sd.first_name or "") + " " + (sd.last_name or "")).strip()
                        su = getattr(sd, 'username', '') or ""
                    md = _fmt_date(msg.date)
                    cache_batch.append(
                        (msg.id, str(target), msg.sender_id or 0, sn, su, _txt[:2000], md)
                    )
                    if len(cache_batch) >= 500:
                        try:
                            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                                await _db.executemany(
                                    "INSERT OR IGNORE INTO messages_cache "
                                    "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                                    "VALUES (?,?,?,?,?,?,?)", cache_batch
                                )
                                await _db.commit()
                        except Exception as e:
                            _dbg("_comment_scan_parallel", e)
                        cache_batch = []
        except FloodWaitError as e:
            log_flood("parallel_comments", e.seconds)
            await asyncio.sleep(min(e.seconds + 5, 300))
        except Exception as e:
            _dbg("_comment_scan_parallel", e)
        if cache_batch:
            try:
                async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                    await _db.executemany(
                        "INSERT OR IGNORE INTO messages_cache "
                        "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                        "VALUES (?,?,?,?,?,?,?)", cache_batch
                    )
                    await _db.commit()
            except Exception as e:
                _dbg("_comment_scan_parallel", e)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    await asyncio.gather(*[_worker(k) for k in range(n)], return_exceptions=True)

    # Excel
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Komentariya Skaner"
    ws.append([
        "№", "Ism", "Familiya", "Username", "Telegram ID",
        "Telefon", "Bot?", "Premium?", "Bio",
        "Shaxsiy Kanal", "Maxfiy Kanal", "Ochiq Kanal", "Profil havolasi"
    ])
    c = 0
    for r in rows:
        c += 1
        ws.append([c] + r)
    try:
        _fl = asyncio.get_running_loop()
        await _fl.run_in_executor(None, apply_excel_styles, ws, c)
        await _fl.run_in_executor(None, wb.save, output_path)
    except Exception as e:
        _dbg("_comment_scan_parallel", e)
    return c, ch_title_safe(disc_entities[0])


def ch_title_safe(ent):
    return getattr(ent, 'title', 'Discussion') if ent else 'Discussion'


async def scan_channel_comments(userbot, target, output_path, status_msg,
                                resume_offset=0, resume_count=0, scan_id=None,
                                pre_entity=None):
    """
    Kanal postlarining comment qismidan (linked discussion guruh)
    yozgan foydalanuvchilarni topib skanerLaydi.

    Mantiq:
      1. Kanal linked_chat (discussion guruh) ni topadi
      2. Discussion guruhdagi barcha xabarlarni o'qiydi
      3. Unikal foydalanuvchilarni yig'adi
      4. Har birini GetFullUserRequest bilan skanerLaydi
    """
    global _SCAN_COUNT, MONITORING_PAUSED, _FLOOD_PENALTY

    # ── PARALLEL MARSHRUT: ochiq kanal + ko'p userbot ──
    _all_bots = list(_ALL_USERBOTS) if _ALL_USERBOTS else [userbot]
    _is_private = bool(re.search(r't\.me/\+|joinchat', str(target)))
    if len(_all_bots) > 1 and not _is_private and resume_offset == 0:
        _SCAN_COUNT += 1
        MONITORING_PAUSED = True
        _FLOOD_PENALTY = 0.0
        _RESOURCE['heavy_scan'] = True
        _RESOURCE['current_task'] = "Kanal Comment Skanerlash (parallel)"
        _RESOURCE['task_start'] = datetime.now()
        if scan_id is None:
            _sid = getattr(status_msg, 'chat_id', 0)
            scan_id = await db_mod.create_scan_session(str(target), output_path, _sid)
        try:
            _res = await _comment_scan_parallel(_all_bots, target, output_path, status_msg, scan_id)
            await db_mod.finish_scan_session(scan_id, status='done')
            return _res
        except Exception:
            await db_mod.finish_scan_session(scan_id, status='error')
            raise
        finally:
            _SCAN_COUNT -= 1
            if _SCAN_COUNT <= 0:
                _SCAN_COUNT = 0
                MONITORING_PAUSED = False
                _RESOURCE['heavy_scan'] = False
                _RESOURCE['current_task'] = None
                _RESOURCE['task_start'] = None

    _SCAN_COUNT += 1
    MONITORING_PAUSED = True
    _FLOOD_PENALTY = 0.0
    _RESOURCE['heavy_scan'] = True
    _RESOURCE['current_task'] = "Kanal Comment Skanerlash"
    _RESOURCE['task_start'] = datetime.now()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if scan_id is None:
        sender_id = getattr(status_msg, 'chat_id', 0)
        scan_id = await db_mod.create_scan_session(str(target), output_path, sender_id)

    try:
        # 1. Kanalga ulanish — pre_entity berilgan bo'lsa qayta resolve qilinmaydi
        try:
            channel = pre_entity or await userbot.get_entity(target)
        except Exception as e:
            raise Exception(f"Kanalga ulanib bo\'lmadi: {e}")

        try:
            await status_msg.edit("🔍 Kanalning discussion guruhi qidirilmoqda...")
        except Exception as e:
            _dbg("scan_channel_comments", e)

        # 2. Linked discussion guruhni topish
        discussion_group = None
        try:
            full_ch = await userbot(GetFullChannelRequest(channel))
            linked_id = getattr(full_ch.full_chat, 'linked_chat_id', None)
            if linked_id:
                discussion_group = await userbot.get_entity(linked_id)
        except Exception as e:
            _dbg("scan_channel_comments", e)

        if not discussion_group:
            raise Exception(
                "Bu kanalda comment bo'limi (discussion guruh) topilmadi.\n"
                "Kanal postlari ostida comment yozish imkoniyati yoqilmagan bo'lishi mumkin."
            )

        ch_title = getattr(channel, 'title', str(target))
        gr_title = getattr(discussion_group, 'title', 'Discussion')

        try:
            await status_msg.edit(
                f"✅ Discussion guruh topildi: **{gr_title}**\n"
                f"📨 Commentariyalar o'qilmoqda..."
            )
        except Exception as e:
            _dbg("scan_channel_comments", e)

        # 3. Discussion guruhdan xabar yozganlarni yig'ish + messages_cache ga yozish
        unique_users  = {}   # user_id → user object
        _msg_count    = 0
        _cache_batch  = []
        _src_str      = str(target)

        async for msg in userbot.iter_messages(discussion_group, limit=None):
            if not msg.sender_id:
                continue
            if msg.sender_id < 0:
                continue  # Kanal/guruh xabarlarini o'tkazib yuborish
            _msg_count += 1
            if msg.sender_id not in unique_users:
                sender = msg.sender
                if sender and not getattr(sender, 'bot', False) and not getattr(sender, 'deleted', False):
                    unique_users[msg.sender_id] = sender

            # Matnli xabarlarni keshga yig'ish
            _mc_text = msg.text or getattr(msg, 'caption', None) or ""
            if len(_mc_text) > 2:
                sender = msg.sender
                s_name = ""
                s_un   = ""
                if sender and hasattr(sender, 'first_name'):
                    s_name = ((sender.first_name or "") + " " + (sender.last_name or "")).strip()
                    s_un   = getattr(sender, 'username', '') or ""
                elif sender and hasattr(sender, 'title'):
                    s_name = sender.title or ""
                    s_un   = getattr(sender, 'username', '') or ""
                msg_dt = _fmt_date(msg.date)
                _cache_batch.append((
                    msg.id, _src_str, msg.sender_id,
                    s_name, s_un, _mc_text[:2000], msg_dt
                ))

            # Har 300 xabarda batch-insert
            if len(_cache_batch) >= 300:
                try:
                    async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                        await _db.executemany(
                            "INSERT OR IGNORE INTO messages_cache "
                            "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                            "VALUES (?,?,?,?,?,?,?)",
                            _cache_batch
                        )
                        await _db.commit()
                except Exception as e:
                    _dbg("scan_channel_comments", e)
                _cache_batch = []

            # Har 500 xabarda progress ko'rsatish
            if _msg_count % 500 == 0:
                try:
                    await status_msg.edit(
                        f"📨 **Xabarlar o'qilmoqda:** `{_msg_count}` ta ko'rildi\n"
                        f"👥 Hozircha topilgan: `{len(unique_users)}` ta unikal foydalanuvchi..."
                    )
                except Exception as e:
                    _dbg("scan_channel_comments", e)

        # Qolgan xabarlarni keshga yozish
        if _cache_batch:
            try:
                async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                    await _db.executemany(
                        "INSERT OR IGNORE INTO messages_cache "
                        "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                        "VALUES (?,?,?,?,?,?,?)",
                        _cache_batch
                    )
                    await _db.commit()
            except Exception as e:
                _dbg("scan_channel_comments", e)

        total = len(unique_users)
        if not total:
            raise Exception("Comment yozgan foydalanuvchi topilmadi.")

        try:
            await status_msg.edit(
                f"👥 **{total}** ta unikal foydalanuvchi topildi.\n"
                f"🔍 Profillar tahlil qilinmoqda..."
            )
        except Exception as e:
            _dbg("scan_channel_comments", e)

        # 4. Parallel profil skanerlash (2 parallel API + flood xavfsiz)
        wb    = openpyxl.Workbook()
        sheet = wb.active
        sheet.title = "Comment Skaneri"
        sheet.append([
            "№", "Ism", "Familiya", "Username", "Telegram ID",
            "Telefon", "Bio", "Bio Linklar",
            "Shaxsiy Kanal Linki", "Maxfiy Kanal Linki", "Profil havolasi"
        ])

        count      = 0
        SAVE_EVERY = 50

        # Natijalarni yig'ib, keyin tartib bilan yozish
        _cmt_sem  = asyncio.Semaphore(4)   # 4 parallel — _safe_api_call flood boshqaradi
        _progress = [0]                    # shared counter
        collected = {}                     # uid → row_data

        async def _scan_one(uid, user):
            while SCANNER_PAUSED:
                await asyncio.sleep(1)

            f_name = user.first_name or ""
            l_name = user.last_name  or ""
            uname  = ("@" + user.username) if user.username else ""
            phone  = getattr(user, 'phone', '') or ""
            bio    = ""
            shaxsiy = ""
            maxfiy  = ""
            _bdayC  = ""

            async with _cmt_sem:
                await asyncio.sleep(0.35)
                try:
                    fi = await asyncio.wait_for(
                        userbot(GetFullUserRequest(uid)), timeout=20
                    )
                    bio = fi.full_user.about or ""
                    _bdayC = _fmt_birthday(getattr(fi.full_user, 'birthday', None))
                    inv_links = extract_invite_links(bio)
                    if inv_links:
                        maxfiy = ", ".join(inv_links)
                        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as _db2:
                            await _db2.executemany(
                                "INSERT OR IGNORE INTO hidden_channel_knocker "
                                "(channel_id, creator_id, source_group) "
                                "VALUES (?, ?, ?)",
                                [(inv, uid, str(target)) for inv in inv_links]
                            )
                            await _db2.commit()
                    ch_id = getattr(fi.full_user, 'personal_channel_id', None)
                    # 777 va 1_000_000 dan kichik ID — Telegram tizimiy, real kanal emas.
                    if ch_id and int(ch_id) >= 1_000_000:
                        shaxsiy = await _pc_link_cached(userbot, ch_id, chats=getattr(fi, 'chats', None))
                except FloodWaitError as e:
                    _record_flood(e.seconds)
                    log_flood("scan_channel_comments", e.seconds)
                    await asyncio.sleep(min(e.seconds + 2, 120))
                except Exception as e:
                    _dbg("_scan_one", e)

            # O'chirilgan hisob → saqlamay o'tkazib yuborish
            if not f_name and not l_name and not uname and not bio:
                return

            bio_links_str = ", ".join(extract_bio_links(bio)) if bio else ""
            p_link  = (f"https://t.me/{user.username}" if user.username
                       else f"tg://user?id={uid}")
            b_date  = _bdayC or extract_exact_birth_date(bio)
            has_db  = maxfiy if maxfiy else (shaxsiy if shaxsiy else "❌")

            collected[uid] = (f_name, l_name, uname, phone, bio,
                              bio_links_str, shaxsiy, maxfiy, p_link, b_date, has_db)

            _progress[0] += 1
            if _progress[0] % 20 == 0:
                try:
                    await status_msg.edit(
                        f"🔍 **Tahlil:** `{_progress[0]}/{total}` ta profil..."
                    )
                except Exception as e:
                    _dbg("_scan_one", e)

        # Barcha userlarni parallel ishga tushirish
        await asyncio.gather(
            *[asyncio.create_task(_scan_one(uid, user))
              for uid, user in unique_users.items()],
            return_exceptions=True
        )

        # Natijalarni tartib bilan Excelga yozish
        for uid, user in unique_users.items():
            if uid not in collected:
                continue
            f_name, l_name, uname, phone, bio, bio_links_str, shaxsiy, maxfiy, p_link, b_date, has_db = collected[uid]
            count += 1
            sheet.append([
                count, f_name, l_name, uname, uid,
                ("+" + phone) if phone else "",
                bio, bio_links_str, shaxsiy, maxfiy, p_link
            ])
            await db_mod.save_user_to_bank(
                uid, str(target), f_name, l_name, uname,
                phone, b_date, bio, bio_links_str, has_db
            )
            if count % SAVE_EVERY == 0:
                # Bloklamaslik uchun ishchi oqimda (event loop band bo'lmaydi)
                _sv = asyncio.get_running_loop()
                await _sv.run_in_executor(None, apply_excel_styles, sheet, count)
                await _sv.run_in_executor(None, wb.save, output_path)

    except Exception as e:
        await db_mod.finish_scan_session(scan_id, status='error')
        raise e
    else:
        await db_mod.finish_scan_session(scan_id, status='done')
    finally:
        # Oxirgi saqlash — har doim bajariladi
        try:
            _fl2 = asyncio.get_running_loop()
            await _fl2.run_in_executor(None, apply_excel_styles, sheet, count)
            await _fl2.run_in_executor(None, wb.save, output_path)
        except Exception as e:
            _dbg("_scan_one", e)
        _SCAN_COUNT -= 1
        if _SCAN_COUNT <= 0:
            _SCAN_COUNT = 0
            MONITORING_PAUSED = False
        _RESOURCE['heavy_scan'] = False
        _RESOURCE['music_paused'] = False
        _RESOURCE['profile_slow'] = False
        _RESOURCE['current_task'] = None
        _RESOURCE['task_start'] = None

    return count, ch_title


# ─────────────────────────────────────────────────────────────────────
# KALIT SO'Z QIDIRUV
# Guruh/kanal xabarlaridan kalit so'z bo'yicha qidiradi
# ─────────────────────────────────────────────────────────────────────

async def search_keywords(userbot, target, keywords_str, status_msg, days=None):
    """
    Guruh/kanal xabarlaridan kalit so'zlarni qidiradi.
    keywords_str: "sotaman, telefon, uy" — vergul bilan ajratilgan
    Qaytaradi: natijalar ro'yxati [{name, username, user_id, date, text, source}]
    """
    # Kalit so'zlarni tayyorlash
    keywords = [k.strip().lower() for k in keywords_str.split(',') if k.strip()]
    if not keywords:
        return []

    results = []

    try:
        # Manba ga ulanish — safe_get_entity bilan flood himoyasi
        entity = await safe_get_entity(userbot, target)
        if entity is None:
            return []  # Kirish imkoni yo'q yoki flood — o'tkazib yuborish

        title = getattr(entity, 'title', str(target))

        try:
            await status_msg.edit(
                f"🔎 **`{', '.join(keywords)}`** qidirilmoqda...\n"
                f"📍 `{title}` xabarlari o'qilmoqda..."
            )
        except Exception as e:
            _dbg("search_keywords", e)

        msg_count = 0
        _src_str = str(target)
        _cache_batch = []

        # Sana filtri — "oxirgi N kun".
        # MUHIM: offset_date ISHLATILMAYDI. Telethon offset_date dan ESKI
        # xabarlardan boshlaydi (orqaga), shuning uchun "oxirgi N kun" buzilardi.
        # To'g'risi: eng yangidan boshlab, cutoff dan eski bo'lganda to'xtatamiz.
        from datetime import timezone
        cutoff = None
        if days:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async for msg in userbot.iter_messages(entity, limit=None):
            # Matn yo'q VA audio/musiqa ham yo'q — o'tkazib yuborish
            has_text  = bool(msg.text)
            has_audio = bool(msg.audio or msg.voice)
            if not has_text and not has_audio:
                continue

            # Sana filtri — cutoff dan eski bo'lsa to'xtatish
            if cutoff and msg.date:
                msg_dt = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date
                if msg_dt < cutoff:
                    break

            # Keshga saqlash (matnli xabarlar + caption)
            _mc_text = msg.text or getattr(msg, 'caption', None) or ""
            if len(_mc_text) > 2:
                sender = msg.sender
                s_id = getattr(sender, 'id', msg.sender_id or 0) if sender else (msg.sender_id or 0)
                s_name, s_un = "", ""
                if sender and hasattr(sender, 'first_name'):
                    s_name = ((sender.first_name or "") + " " + (sender.last_name or "")).strip()
                    s_un = getattr(sender, 'username', '') or ""
                elif sender and hasattr(sender, 'title'):
                    s_name = sender.title or ""
                    s_un = getattr(sender, 'username', '') or ""
                msg_dt_str = _fmt_date(msg.date)
                _cache_batch.append((msg.id, _src_str, s_id, s_name, s_un, _mc_text[:2000], msg_dt_str))
                if len(_cache_batch) >= 300:
                    try:
                        async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                            await _db.executemany(
                                "INSERT OR IGNORE INTO messages_cache "
                                "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                                "VALUES (?,?,?,?,?,?,?)", _cache_batch
                            )
                            await _db.commit()
                        asyncio.create_task(_check_batch_alerts(list(_cache_batch)))
                    except Exception as e:
                        _dbg("search_keywords", e)
                    _cache_batch.clear()

            msg_count += 1
            if msg_count % 500 == 0:
                try:
                    await status_msg.edit(
                        f"🔎 `{msg_count}` ta xabar ko'rildi | "
                        f"✅ Topildi: `{len(results)}` ta..."
                    )
                except Exception as e:
                    _dbg("search_keywords", e)

            # Qidiruv matni: caption + audio title + performer + fayl nomi
            search_parts = []
            if msg.text:
                search_parts.append(msg.text)
            if has_audio:
                audio = msg.audio or msg.voice
                if hasattr(audio, 'title') and audio.title:
                    search_parts.append(audio.title)
                if hasattr(audio, 'performer') and audio.performer:
                    search_parts.append(audio.performer)
                if msg.file and msg.file.name:
                    search_parts.append(msg.file.name)

            if not search_parts:
                continue

            search_text = " ".join(search_parts).lower()
            matched = [kw for kw in keywords if kw in search_text]
            if not matched:
                continue

            # Kim yozgan?

            sender = msg.sender
            if sender is None:
                try:
                    sender = await msg.get_sender()
                except FloodWaitError as fw:
                    await asyncio.sleep(fw.seconds + 3)
                    try:
                        sender = await msg.get_sender()
                    except Exception:
                        continue
                except Exception:
                    continue
            if not sender:
                continue
            if getattr(sender, 'bot', False) or getattr(sender, 'deleted', False):
                continue

            name     = ""
            username = ""
            uid      = getattr(sender, 'id', 0)

            if hasattr(sender, 'first_name'):
                name = (sender.first_name or "") + " " + (sender.last_name or "")
                name = name.strip()
                username = sender.username or ""
            elif hasattr(sender, 'title'):
                # Kanal/guruh nomi
                name = sender.title or ""

            # Natija matni: audio bo'lsa sarlavha + ijrochi, aks holda caption
            if has_audio:
                audio = msg.audio or msg.voice
                a_title = getattr(audio, 'title', '') or ''
                a_perf  = getattr(audio, 'performer', '') or ''
                f_name  = (msg.file.name if msg.file and msg.file.name else '')
                display = ""
                if a_title:
                    display = f"🎵 {a_title}"
                    if a_perf:
                        display += f" — {a_perf}"
                elif f_name:
                    display = f"🎵 {f_name}"
                else:
                    display = "🎵 Audio fayl"
                if msg.text:
                    display += f"\n💬 {msg.text[:100]}"
                text = display
            else:
                text = msg.text or ""
                if len(text) > 250:
                    text = text[:247] + "..."

            results.append({
                'name':     name,
                'username': username,
                'user_id':  uid,
                'date':     _fmt_date(msg.date),
                'text':     text,
                'source':   title,
                'matched':  ", ".join(matched)
            })

        if _cache_batch:
            try:
                async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                    await _db.executemany(
                        "INSERT OR IGNORE INTO messages_cache "
                        "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                        "VALUES (?,?,?,?,?,?,?)", _cache_batch
                    )
                    await _db.commit()
                asyncio.create_task(_check_batch_alerts(list(_cache_batch)))
            except Exception as e:
                _dbg("search_keywords", e)
            _cache_batch.clear()

        try:
            await status_msg.edit(
                f"✅ Qidiruv yakunlandi!\n"
                f"📊 Ko'rilgan xabarlar: `{msg_count}` ta\n"
                f"🎯 Topildi: `{len(results)}` ta"
            )
        except Exception as e:
            _dbg("search_keywords", e)

    except Exception as e:
        raise e

    return results


# ─────────────────────────────────────────────────────────────────────
# KANAL MUSIQA TRACKER
# Monitoring kanallaridagi audio xabarlarni skanerLaydi
# ─────────────────────────────────────────────────────────────────────

async def _scan_discussion_users_bg(userbot, discussion_id: int, source_link: str, userbot_idx: int = 0):
    """Musiqa kanal discussion guruhidagi foydalanuvchilarni fon rejimda skanerlaydi."""
    try:
        from telethon.tl.functions.users import GetFullUserRequest as _GetFullUserRequest
        from telethon.tl.types import PeerChannel as _PeerChannel
        _sem = asyncio.Semaphore(2)

        try:
            disc_entity = _PeerChannel(channel_id=discussion_id)
        except Exception:
            return

        unique_users = {}
        _cache_batch = []
        async for msg in userbot.iter_messages(disc_entity, limit=None):
            if not msg.sender_id or msg.sender_id < 0:
                continue
            # Xabar matnini keshga yozish (kalit so'z qidiruvi uchun)
            _txt = getattr(msg, 'message', None) or getattr(msg, 'text', None)
            if _txt:
                _snd = msg.sender
                _sname = ""
                _suname = ""
                if _snd:
                    _sname = (getattr(_snd, 'first_name', '') or '') + ' ' + (getattr(_snd, 'last_name', '') or '')
                    _sname = _sname.strip()
                    _suname = getattr(_snd, 'username', '') or ''
                _mdate = _fmt_date(getattr(msg, 'date', None))
                _cache_batch.append(
                    (msg.id, source_link, msg.sender_id, _sname, _suname, _txt, _mdate)
                )
                if len(_cache_batch) >= 200:
                    try:
                        async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                            await _db.executemany(
                                "INSERT OR IGNORE INTO messages_cache "
                                "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                                "VALUES (?,?,?,?,?,?,?)",
                                _cache_batch
                            )
                            await _db.commit()
                    except Exception as e:
                        _dbg("_scan_discussion_users_bg", e)
                    _cache_batch = []
            if msg.sender_id not in unique_users and msg.sender:
                if not getattr(msg.sender, 'bot', False) and not getattr(msg.sender, 'deleted', False):
                    unique_users[msg.sender_id] = msg.sender

        # Qolgan xabarlarni keshga yozish
        if _cache_batch:
            try:
                async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                    await _db.executemany(
                        "INSERT OR IGNORE INTO messages_cache "
                        "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                        "VALUES (?,?,?,?,?,?,?)",
                        _cache_batch
                    )
                    await _db.commit()
            except Exception as e:
                _dbg("_scan_discussion_users_bg", e)

        if not unique_users:
            return

        # DB da allaqachon bor foydalanuvchilarni filtrlash
        existing_ids = set()
        try:
            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                async with _db.execute(
                    "SELECT DISTINCT user_id FROM users_memory_bank WHERE group_link=?",
                    (source_link,)
                ) as _cur:
                    async for _row in _cur:
                        existing_ids.add(_row[0])
        except Exception as e:
            _dbg("_scan_discussion_users_bg", e)

        async def _process_user(uid, user):
            async with _sem:
                await asyncio.sleep(0.35)
                f_name = user.first_name or ""
                l_name = user.last_name  or ""
                uname  = ("@" + user.username) if user.username else ""
                phone  = getattr(user, 'phone', '') or ""
                bio    = ""
                shaxsiy = ""
                maxfiy  = ""
                _bdayD  = ""
                try:
                    fi = await asyncio.wait_for(
                        userbot(_GetFullUserRequest(uid)), timeout=20
                    )
                    bio = fi.full_user.about or ""
                    _bdayD = _fmt_birthday(getattr(fi.full_user, 'birthday', None))
                    inv_links = extract_invite_links(bio)
                    if inv_links:
                        maxfiy = ", ".join(inv_links)
                        try:
                            async with db_mod.connect(db_mod.DB_NAME, timeout=30) as _db:
                                await _db.executemany(
                                    "INSERT OR IGNORE INTO hidden_channel_knocker "
                                    "(channel_id, creator_id, source_group) VALUES (?,?,?)",
                                    [(inv, uid, source_link) for inv in inv_links]
                                )
                                await _db.commit()
                        except Exception as e:
                            _dbg("_process_user", e)
                    ch_id = getattr(fi.full_user, 'personal_channel_id', None)
                    # 777 va 1_000_000 dan kichik ID — Telegram tizimiy, real kanal emas.
                    if ch_id and int(ch_id) >= 1_000_000:
                        shaxsiy = await _pc_link_cached(userbot, ch_id, chats=getattr(fi, 'chats', None))
                        # Knocker ga QO'SHILMAYDI — musiqa skaneri urinib ko'radi,
                        # kira olmasa o'sha zahoti knocker ga qo'shiladi
                except FloodWaitError as e:
                    await asyncio.sleep(min(e.seconds + 2, 120))
                except Exception as e:
                    _dbg("_process_user", e)

                if not f_name and not l_name and not uname and not bio:
                    return

                bio_links_str = ", ".join(extract_bio_links(bio)) if bio else ""
                b_date  = _bdayD or extract_exact_birth_date(bio)
                has_db  = maxfiy if maxfiy else (shaxsiy if shaxsiy else "❌")
                try:
                    await db_mod.save_user_to_bank(
                        uid, source_link, f_name, l_name, uname,
                        phone, b_date, bio, bio_links_str, has_db
                    )
                except Exception as e:
                    _dbg("_process_user", e)

        tasks = [
            asyncio.create_task(_process_user(uid, user))
            for uid, user in unique_users.items()
            if uid not in existing_ids
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as e:
        _dbg("_process_user", e)

def _is_private_source(source: str) -> bool:
    """Maxfiy kanal: t.me/c/... yoki raqamli ID (-100XXXXX)."""
    s = str(source).strip()
    if 't.me/c/' in s:
        return True
    clean = s.lstrip('-')
    return clean.isdigit()


async def _music_process_one_source(userbot, source, userbot_idx=0):
    """Bitta kanalning musiqa xabarlarini skanerlaydi (music_channel_tracker uchun)."""
    while _RESOURCE['music_paused']:
        await asyncio.sleep(10)
    if MONITORING_PAUSED:
        return

    # ── Keshdan entity ID ni olish (get_entity chaqirmaslik uchun) ──────
    src_str = str(source).strip()
    entity = None
    channel_name = src_str
    channel_id = ""

    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
            async with _db.execute(
                "SELECT numeric_id, channel_name FROM resolved_channel_ids WHERE channel_link=?",
                (src_str,)
            ) as _cur:
                _row = await _cur.fetchone()
        if _row and _row[0]:
            try:
                from telethon.tl.types import PeerChannel as _PeerChannel
                # numeric_id ba'zan '-100XXXX' (marked) formatida saqlanadi.
                # PeerChannel esa toza (bare) musbat id talab qiladi — prefiksni olib tashlaymiz.
                _s = str(_row[0]).strip()
                if _s.startswith('-100'):
                    _s = _s[4:]
                else:
                    _s = _s.lstrip('-')
                _numeric = int(_s)
                entity = _PeerChannel(channel_id=_numeric)
                channel_id = str(_numeric)
                channel_name = _row[1] or src_str
            except Exception:
                entity = None
    except Exception as e:
        _dbg("_music_process_one_source", e)

    # Keshda yo'q — tekshir
    if entity is None:
        # 1. Invite havola → safe_get_entity CHAQIRMASDAN, to'g'ri hidden_channel_knocker ga
        is_invite = "t.me/+" in src_str or "t.me/joinchat/" in src_str
        if is_invite:
            try:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO hidden_channel_knocker "
                        "(channel_id, creator_id, source_group, last_request_time, userbot_idx) "
                        "VALUES (?, 0, 'Musiqa Tracker', ?, ?)",
                        (source, now_str, userbot_idx)
                    )
                    await db.commit()
            except Exception as e:
                _dbg("_music_process_one_source", e)
            return

        # 2. Numeric ID → PeerChannel (API so'rovisiz)
        _raw = src_str.lstrip('-')
        if _raw.isdigit():
            try:
                from telethon.tl.types import PeerChannel as _PeerChannel
                _numeric = int(src_str.lstrip('-'))
                entity = _PeerChannel(channel_id=_numeric)
                channel_id = str(_numeric)
            except Exception:
                entity = None

        # 3. @username → safe_get_entity (faqat bir marta, keshga yoziladi)
        if entity is None:
            try:
                entity = await safe_get_entity(userbot, source)
                if entity is None:
                    return
                channel_name = getattr(entity, 'title', src_str)
                channel_id = str(entity.id) if hasattr(entity, 'id') else ""
                if channel_id:
                    now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                    try:
                        async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                            await _db.execute(
                                "INSERT OR REPLACE INTO resolved_channel_ids "
                                "(channel_link, numeric_id, resolved_at, channel_name) VALUES (?,?,?,?)",
                                (src_str, channel_id, now_s, channel_name)
                            )
                            await _db.commit()
                    except Exception as e:
                        _dbg("_music_process_one_source", e)
            except Exception:
                return

    if not channel_id and hasattr(entity, 'id'):
        channel_id = str(entity.id)
    if not channel_id:
        return

    last_msg_id = 0
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        try:
            async with db.execute(
                "SELECT last_msg_id FROM music_channel_progress WHERE channel_id=?",
                (channel_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    last_msg_id = row[0] or 0
        except Exception:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS music_channel_progress "
                "(channel_id TEXT PRIMARY KEY, last_msg_id INTEGER)"
            )
            await db.commit()

    new_last_id = last_msg_id
    audio_count = [0]

    # reverse=True: eskidan yangi tomonga — svet o'chsa ham davom etish mumkin
    iter_kwargs = {"limit": None, "reverse": True}
    if last_msg_id > 0:
        iter_kwargs["min_id"] = last_msg_id

    BASE_DIR_LOCAL = os.path.dirname(os.path.abspath(__file__))
    import os as _os
    _cpu_count  = _os.cpu_count() or 2
    _fp_workers = max(2, _cpu_count // 2)        # Fizik yadro soni (HT ni hisobga olmaydi)
    _DL_SEM     = asyncio.Semaphore(MUSIC_PARALLEL)  # Parallel yuklab olish (I/O) — /yuklash N bilan sozlanadi
    _FP_SEM     = asyncio.Semaphore(_fp_workers) # Parallel fingerprint (fizik yadro)
    _ch_tasks   = set()

    async def _pipeline(m):
        _pkey = (channel_id, str(m.id))
        if _pkey in _PROCESSING_AUDIO:
            return
        try:
            async with db_mod.connect(music_mod.MUSIC_DB, timeout=5) as _mdb:
                async with _mdb.execute(
                    "SELECT 1 FROM music_fingerprints WHERE channel_id=? AND file_name=?",
                    (channel_id, f"msg_{m.id}")
                ) as _mc:
                    if await _mc.fetchone():
                        return
        except Exception as e:
            _dbg("_pipeline", e)

        _PROCESSING_AUDIO.add(_pkey)
        tmp_path = os.path.join(BASE_DIR_LOCAL, f"tmp_ch_{channel_id}_{m.id}.ogg")

        # 1. Yuklab olish (I/O — _DL_SEM bilan)
        async with _DL_SEM:
            ok = False
            for attempt in range(3):
                try:
                    await m.download_media(file=tmp_path)
                    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                        ok = True
                        break
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    if attempt < 2:
                        await asyncio.sleep(1)
            if not ok:
                _PROCESSING_AUDIO.discard(_pkey)
                return

        # 2. Fingerprint (CPU — _FP_SEM bilan, har yadro alohida)
        async with _FP_SEM:
            try:
                fp, duration = await music_mod.get_fingerprint_async(tmp_path)
                if fp:
                    await music_mod.save_fingerprint(
                        channel_id, channel_name,
                        f"msg_{m.id}", fp, duration or 0
                    )
                    audio_count[0] += 1
                    hits = await music_mod.check_against_watch_list(fp)
                    for hit in hits:
                        if _WATCH_ALERTS is not None:
                            _WATCH_ALERTS.put_nowait({
                                'admin_id':    hit['admin_id'],
                                'watch_name':  hit['watch_name'],
                                'score':       hit['score'],
                                'source_name': channel_name,
                                'source_id':   channel_id,
                                'source_type': 'kanal'
                            })
            except Exception as e:
                _dbg("_pipeline", e)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                _PROCESSING_AUDIO.discard(_pkey)

    _cache_batch = []
    _cache_src   = str(source)
    _msg_counter = 0
    _discussion_id = None  # Kanal discussion guruhi ID si
    _grp_members = []      # Guruh a'zolari → profil trackerga uzatish (INSERT OR IGNORE)

    try:
        async for msg in userbot.iter_messages(entity, **iter_kwargs):
            _msg_counter += 1
            # reverse=True: ID lar doim o'sib boradi
            new_last_id = msg.id

            # Discussion guruh ID sini birinchi uchraganda olish (0 extra API)
            if _discussion_id is None and msg.replies:
                _did = getattr(msg.replies, 'channel_id', None)
                if _did:
                    _discussion_id = _did
                    # Discussion ham o'sha userbotga biriktirilsin
                    disc_link = f"https://t.me/c/{_did}/1"
                    n = _get_n_userbots()
                    if n > 1:
                        try:
                            await db_mod.assign_channel(disc_link, n)
                            # Lekin biriktirilgan indeksni userbot_idx ga o'zgartir
                            async with db_mod.connect(db_mod.DB_NAME, timeout=5) as _ddb:
                                await _ddb.execute(
                                    "UPDATE channel_assignments SET userbot_idx=? WHERE channel_link=?",
                                    (userbot_idx, disc_link)
                                )
                                await _ddb.commit()
                        except Exception:
                            pass

            # Har 200 xabardan keyin event loop ga yield — flood emas, faqat boshqa tasklarga joy
            if _msg_counter % 200 == 0:
                await asyncio.sleep(0)

            if is_music_file(msg):
                t = asyncio.create_task(_pipeline(msg))
                _ch_tasks.add(t)
                t.add_done_callback(_ch_tasks.discard)

            # Guruh a'zosi (haqiqiy foydalanuvchi) → profil trackerga uzatish
            _snd = msg.sender
            if _snd and hasattr(_snd, 'first_name') and not getattr(_snd, 'bot', False) \
                    and not getattr(_snd, 'deleted', False) \
                    and getattr(_snd, 'id', 0) and (msg.sender_id or 0) > 0:
                _now = datetime.now().strftime("%Y-%m-%d %H:%M")
                _grp_members.append((
                    _snd.id, _cache_src,
                    _snd.first_name or "", _snd.last_name or "",
                    ("@" + _snd.username) if getattr(_snd, 'username', None) else "",
                    _now, _now
                ))

            if msg.text and len(msg.text) > 2:
                sender = msg.sender
                s_id   = getattr(sender, 'id', msg.sender_id or 0) if sender else (msg.sender_id or 0)
                s_name = ""
                s_un   = ""
                if sender and hasattr(sender, 'first_name'):
                    s_name = ((sender.first_name or "") + " " + (sender.last_name or "")).strip()
                    s_un   = getattr(sender, 'username', '') or ""
                msg_dt = _fmt_date(msg.date)
                _cache_batch.append((msg.id, _cache_src, s_id, s_name, s_un, msg.text[:2000], msg_dt))

            # A'zolarni partiya bilan yozish (INSERT OR IGNORE — mavjudni o'chirmaydi)
            if len(_grp_members) >= 300:
                try:
                    async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                        await _db.executemany(
                            "INSERT OR IGNORE INTO users_memory_bank "
                            "(user_id, group_link, first_name, last_name, username, added_date, last_updated) "
                            "VALUES (?,?,?,?,?,?,?)",
                            _grp_members
                        )
                        await _db.commit()
                except Exception as e:
                    _dbg("_music_grp_members", e)
                _grp_members = []

            if len(_cache_batch) >= 300:
                try:
                    async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                        await _db.executemany(
                            "INSERT OR IGNORE INTO messages_cache "
                            "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                            "VALUES (?,?,?,?,?,?,?)",
                            _cache_batch
                        )
                        await _db.commit()
                except Exception as e:
                    _dbg("_pipeline", e)
                _cache_batch = []

            # Har 500 xabardan keyin progress ni darhol bazaga yoz
            # (svet o'chsa ham davom etish uchun)
            if _msg_counter % 500 == 0:
                try:
                    async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                        await _db.execute(
                            "INSERT OR REPLACE INTO music_channel_progress "
                            "(channel_id, last_msg_id) VALUES (?,?)",
                            (channel_id, new_last_id)
                        )
                        await _db.commit()
                except Exception as e:
                    _dbg("_pipeline", e)
    except StopAsyncIteration:
        pass  # Bo'sh kanal — normal
    except Exception as _acc_err:
        _acc_str = str(_acc_err).lower()
        if any(x in _acc_str for x in ('private', 'forbidden', 'banned', 'not found')):
          try:
            _now = datetime.now().strftime("%Y-%m-%d %H:%M")
            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _kdb:
              await _kdb.execute(
                "INSERT OR IGNORE INTO hidden_channel_knocker "
                "(channel_id, creator_id, source_group, last_request_time) "
                "VALUES (?, 0, 'MusicScanner', ?)",
                (src_str, _now)
              )
              await _kdb.commit()
          except Exception as e:
            _dbg("_music_process_one_source", e)
        return
    if _cache_batch:
        try:
            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                await _db.executemany(
                    "INSERT OR IGNORE INTO messages_cache "
                    "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                    "VALUES (?,?,?,?,?,?,?)",
                    _cache_batch
                )
                await _db.commit()
        except Exception as e:
            _dbg("_pipeline", e)

    # Qolgan guruh a'zolarini yozish — profil tracker keyin boyitadi
    if _grp_members:
        try:
            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                await _db.executemany(
                    "INSERT OR IGNORE INTO users_memory_bank "
                    "(user_id, group_link, first_name, last_name, username, added_date, last_updated) "
                    "VALUES (?,?,?,?,?,?,?)",
                    _grp_members
                )
                await _db.commit()
        except Exception as e:
            _dbg("_music_grp_members", e)

    if _ch_tasks:
        await asyncio.gather(*_ch_tasks, return_exceptions=True)

    # Discussion guruhi topilgan bo'lsa — fon rejimda foydalanuvchilarni skanerlash
    if _discussion_id:
        asyncio.create_task(
            _scan_discussion_users_bg(userbot, _discussion_id, _cache_src, userbot_idx)
        )

    if new_last_id > last_msg_id:
        try:
            async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS music_channel_progress "
                    "(channel_id TEXT PRIMARY KEY, last_msg_id INTEGER)"
                )
                await db.execute(
                    "INSERT OR REPLACE INTO music_channel_progress "
                    "(channel_id, last_msg_id) VALUES (?, ?)",
                    (channel_id, new_last_id)
                )
                await db.commit()
        except Exception as e:
            print(f"Progress saqlash xatosi: {e}")

    await asyncio.sleep(random.uniform(1.5, 3.0))


async def _music_process_list(userbot, sources, userbot_idx=0):
    """Kanallar ro'yxatini bitta userbot bilan ketma-ket skanerlaydi."""
    label = f"UB{userbot_idx+1}"
    _ub_key = id(userbot)
    cursor_key = f"cursor_{label}"

    # Bot o'chib-yongan bo'lsa — qayerda to'xtaganini o'qi
    start_from = 0
    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
            async with _db.execute(
                "SELECT value FROM music_scan_state WHERE key=?", (cursor_key,)
            ) as _cur:
                _row = await _cur.fetchone()
            if _row:
                start_from = int(_row[0])
    except Exception:
        start_from = 0

    if start_from >= len(sources):
        start_from = 0  # Yangi tsikl boshlandi

    if start_from > 0:
        print(f"[MUSIQA-{label}] Davom etilmoqda: {start_from}/{len(sources)} kanaldan")

    for i, source in enumerate(sources):
        # Avvalgi to'xtash joyigacha o'tkazib yubor
        if i < start_from:
            continue

        # Event loop ga har 5 kanalda bir marta nafs berish
        if i % 5 == 0:
            await asyncio.sleep(0)

        # Cursor OLDIN saqlash — bot o'chsa ham keyingi kanaldan davom etadi
        try:
            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                await _db.execute(
                    "INSERT OR REPLACE INTO music_scan_state (key, value) VALUES (?,?)",
                    (cursor_key, str(i + 1))
                )
                await _db.commit()
        except Exception as e:
            _dbg("_music_process_list", e)

        # Userbot flood davrida bo'lsa — tugashini kut
        _flood_exp = _UB_FLOOD_UNTIL.get(_ub_key, 0)
        if _flood_exp > _time_mod.time():
            wait_sec = int(_flood_exp - _time_mod.time()) + 5
            print(f"[MUSIQA-{label}] Flood davri tugashini kutmoqda: {wait_sec}s...")
            await asyncio.sleep(wait_sec)

        global _CURRENT_SCAN_CHANNEL
        _CURRENT_SCAN_CHANNEL = str(source)
        try:
            await _music_process_one_source(userbot, source, userbot_idx)
        except FloodWaitError as e:
            wait = e.seconds
            log_flood("music_channel_tracker", wait)
            _UB_FLOOD_UNTIL[_ub_key] = _time_mod.time() + wait
            print(f"[MUSIQA-{label}] FloodWait {wait}s. Kutilmoqda...")
            await asyncio.sleep(min(wait, 3600))
        except Exception as e:
            print(f"[MUSIQA-{label}] Kanal xatosi ({source}): {e}")
            await asyncio.sleep(3)

        # Navbat endi alohida _secret_channel_queue_worker da ishlaydi

    # Tsikl tugadi — cursori tozalash (keyingi tsikl yangidan boshlansin)
    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
            await _db.execute("DELETE FROM music_scan_state WHERE key=?", (cursor_key,))
            await _db.commit()
    except Exception as e:
        _dbg("_music_process_list", e)


async def _secret_channel_queue_worker(all_bots):
    """
    Mustaqil navbat ishchi — maxfiy kanal ochildi signalini DARHOL qayta ishlaydi.
    MUHIM: maxfiy kanalni FAQAT a'zo bo'lgan userbot skanerlaydi
           (round-robin EMAS — boshqa userbot a'zo emas, o'qiy olmaydi).
    """
    global _CURRENT_SCAN_CHANNEL
    while True:
        try:
            if _JOINED_CHANNEL_QUEUE is None:
                await asyncio.sleep(5)
                continue
            jc = await _JOINED_CHANNEL_QUEUE.get()
        except Exception:
            await asyncio.sleep(5)
            continue

        jc_link = jc.get('link', '')
        jc_name = jc.get('name', jc_link)
        jc_bot  = jc.get('bot')
        jc_adm  = jc.get('admin_id')

        # A'zo bo'lgan userbotni ishlatamiz (navbatdan kelgan ub_idx)
        ub_idx_use = jc.get('ub_idx', 0)
        if ub_idx_use >= len(all_bots):
            ub_idx_use = 0
        ub = all_bots[ub_idx_use]

        print(f"[SECRET-WORKER] UB{ub_idx_use+1} → maxfiy kanal skanerlanmoqda: {jc_name}")
        _CURRENT_SCAN_CHANNEL = jc_link
        try:
            await _music_process_one_source(ub, jc_link, ub_idx_use)
            print(f"[SECRET-WORKER] Maxfiy kanal skaner tugadi: {jc_name}")
            if jc_bot and jc_adm:
                try:
                    await jc_bot.send_message(
                        jc_adm,
                        f"✅ **Maxfiy kanal skanerlandi!**\n\n"
                        f"📢 Kanal: `{jc_name}`\n"
                        f"🎵 Barcha musiqalar fingerprint qilindi"
                    )
                except Exception as e:
                    _dbg("_secret_channel_queue_worker", e)
        except Exception as _e:
            print(f"[SECRET-WORKER] Xato ({jc_name}): {_e}")


async def music_channel_tracker(userbot, userbot2=None):
    """
    Monitoring kanallaridagi barcha audio xabarlarni yuklab,
    fingerprint oladi va saqlaydi. Audio keyin o'chiriladi.
    Faqat yangi xabarlarni tekshiradi (oxirgi ID saqlanadi).
    userbot2 berilsa: maxfiy kanallar→userbot1, ochiq kanallar→ikkala userbot parallel.
    """
    await music_mod.init_music_db()

    # Maxfiy kanallar uchun mustaqil worker — music scan ni kutmaydi
    # N userbot: _ALL_USERBOTS dan to'liq ro'yxat olinadi
    _all_bots = _ALL_USERBOTS if _ALL_USERBOTS else (
        [userbot] + ([userbot2] if userbot2 else [])
    )
    asyncio.create_task(_secret_channel_queue_worker(_all_bots))

    while True:
        try:
            sources = await music_mod.get_all_sources()

            n_ub = _get_n_userbots()
            if n_ub == 1 or not _ALL_USERBOTS:
                # Faqat 1 userbot — eski usul
                for source in sources:
                    while _RESOURCE['music_paused']:
                        await asyncio.sleep(10)
                    if MONITORING_PAUSED:
                        await asyncio.sleep(5)
                        continue
                    try:
                        await _music_process_one_source(userbot, source, userbot_idx=0)
                    except Exception as e:
                        print(f"Kanal xatosi ({source}): {e}")
            else:
                # N userbot — har kanal o'z userbotiga biriktirilgan
                ub_lists = [[] for _ in range(n_ub)]
                for source in sources:
                    ss = str(source)
                    idx = await db_mod.get_channel_userbot(ss)
                    if idx is None:
                        # Yangi kanal — avtomatik biriktir
                        idx = await db_mod.assign_channel(ss, n_ub)
                    if idx < n_ub:
                        ub_lists[idx].append(source)
                    else:
                        ub_lists[0].append(source)

                log_parts = " | ".join(
                    f"UB{i+1}:{len(ub_lists[i])}" for i in range(n_ub)
                )
                print(f"[MUSIQA] {log_parts} ta kanal")

                await asyncio.gather(
                    *[
                        _music_process_list(_ALL_USERBOTS[i], ub_lists[i], userbot_idx=i)
                        for i in range(n_ub)
                        if ub_lists[i]
                    ],
                    return_exceptions=True
                )

        except RpcCallFailError as e:
            print(f"[MUSIQA] Telegram server xatosi (RpcCallFail): {e}. 60s kutilmoqda...")
            await asyncio.sleep(60)
            continue
        except FloodWaitError as e:
            wait = e.seconds
            log_flood("music_channel_tracker", wait)
            print(f"[MUSIQA] FloodWait {wait}s. Kutilmoqda...")
            await asyncio.sleep(min(wait, 3600))
            continue
        except Exception as e:
            print(f"music_channel_tracker xatosi: {e}")
            await asyncio.sleep(30)

        # Birinchi to'liq tsikl tugadi — profil tracker endi ishga tushishi mumkin.
        # MUHIM: bayroq True bo'lib QOLADI. Avval u darrov False ga qaytarilardi,
        # natijada har 30s tekshiradigan profil tracker True lahzasini ko'rmay,
        # umuman ishga tushmasdi.
        global _CHANNEL_MUSIC_DONE
        if not _CHANNEL_MUSIC_DONE:
            _CHANNEL_MUSIC_DONE = True
            print("[MUSIQA] Barcha kanal musiqalari skanerlandi — profil skaner ochildi.")
        print("[MUSIQA] Yangi tsikl boshlanmoqda...")


# ─────────────────────────────────────────────────────────────────────
# REAL VAQT HANDLER — userbot a'zo kanallarga yangi xabar kelsa
# API chaqiruvsiz, Telegram o'zi yuboradi
# ─────────────────────────────────────────────────────────────────────

async def _process_realtime_audio(userbot, msg, channel_id: str, channel_name: str):
    """Bitta yangi audio faylni yuklab fingerprint oladi (real vaqt)."""
    _key = (channel_id, str(msg.id))
    if _key in _PROCESSING_AUDIO:
        return
    _PROCESSING_AUDIO.add(_key)

    BASE_DIR_LOCAL = os.path.dirname(os.path.abspath(__file__))
    tmp_path = os.path.join(BASE_DIR_LOCAL, f"tmp_rt_{channel_id}_{msg.id}.ogg")
    try:
        # Bazada allaqachon saqlangan bo'lsa — yuklamasdan o'tkazib yuborish
        try:
            async with db_mod.connect(music_mod.MUSIC_DB, timeout=5) as _mdb:
                async with _mdb.execute(
                    "SELECT 1 FROM music_fingerprints WHERE channel_id=? AND file_name=?",
                    (channel_id, f"msg_{msg.id}")
                ) as _mc:
                    if await _mc.fetchone():
                        return
        except Exception as e:
            _dbg("_process_realtime_audio", e)

        await msg.download_media(file=tmp_path)
        if not (os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0):
            return
        fp, duration = await music_mod.get_fingerprint_async(tmp_path)
        if fp:
            await music_mod.save_fingerprint(
                channel_id, channel_name, f"msg_{msg.id}", fp, duration or 0
            )
            hits = await music_mod.check_against_watch_list(fp)
            for hit in hits:
                if _WATCH_ALERTS is not None:
                    _WATCH_ALERTS.put_nowait({
                        'admin_id':    hit['admin_id'],
                        'watch_name':  hit['watch_name'],
                        'score':       hit['score'],
                        'source_name': channel_name,
                        'source_id':   channel_id,
                        'source_type': 'realtime'
                    })
    except Exception as e:
        _dbg("_process_realtime_audio", e)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception as e:
                _dbg("_process_realtime_audio", e)
        _PROCESSING_AUDIO.discard(_key)


# Real-vaqt xabarlar uchun yozuv navbati — barcha userbotlardan keladigan
# xabarlar shu yagona navbatga tushadi, bitta yozuvchi ularni to'plab yozadi.
# Shu tufayli ko'p ulanish bir vaqtda yozmaydi → "database is locked" bo'lmaydi.
_RT_MSG_QUEUE = None
_RT_WRITER_TASK = None


async def _rt_writer_loop():
    """Navbatdagi real-vaqt xabarlarni to'plab (batch) bazaga yozadi."""
    global _RT_MSG_QUEUE
    while True:
        try:
            row = await _RT_MSG_QUEUE.get()
            batch = [row]
            # Navbatda yana bo'lsa — bir martada 300 tagacha to'playmiz
            try:
                while len(batch) < 300:
                    batch.append(_RT_MSG_QUEUE.get_nowait())
            except asyncio.QueueEmpty:
                pass
            try:
                async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                    await db.executemany(
                        "INSERT OR IGNORE INTO messages_cache "
                        "(msg_id,source,sender_id,sender_name,sender_username,text,msg_date) "
                        "VALUES (?,?,?,?,?,?,?)",
                        batch
                    )
                    await db.commit()
            except Exception as e:
                _dbg("_rt_writer_loop", e)
            await asyncio.sleep(0.3)  # kichik to'planish oynasi
        except Exception as e:
            _dbg("_rt_writer_loop", e)
            await asyncio.sleep(1)


async def _cache_realtime_message(msg, src_str: str):
    """Yangi xabarni yozuv navbatiga qo'yadi (real vaqt, 0 API, bloklamaydi)."""
    text = msg.text or getattr(msg, 'caption', None) or ""
    if len(text) < 2:
        return
    try:
        global _RT_MSG_QUEUE, _RT_WRITER_TASK
        # Navbat va yozuvchini ilk chaqiruvda ishga tushiramiz (lazy)
        if _RT_MSG_QUEUE is None:
            _RT_MSG_QUEUE = asyncio.Queue(maxsize=10000)
        if _RT_WRITER_TASK is None:
            _RT_WRITER_TASK = asyncio.create_task(_rt_writer_loop())

        sender = msg.sender
        s_id = getattr(sender, 'id', None) or msg.sender_id or 0
        s_name, s_un = "", ""
        if sender and hasattr(sender, 'first_name'):
            s_name = ((sender.first_name or "") + " " + (sender.last_name or "")).strip()
            s_un = getattr(sender, 'username', '') or ""
        elif sender and hasattr(sender, 'title'):
            s_name = sender.title or ""
            s_un = getattr(sender, 'username', '') or ""
        msg_dt = _fmt_date(msg.date)
        row = (msg.id, src_str, s_id, s_name, s_un, text[:2000], msg_dt)
        try:
            _RT_MSG_QUEUE.put_nowait(row)
        except asyncio.QueueFull:
            pass  # navbat to'lib ketsa, xabarni tashlaymiz (bot qotmaydi)
    except Exception as e:
        _dbg("_cache_realtime_message", e)


def setup_realtime_handlers(userbot, userbot2=None, bot=None, admin_id=None):
    """
    Userbotlarga real vaqt handlerlarini ulaydi:
      - NewMessage: audio → fingerprint, matn → kesh
      - UpdateChannel: maxfiy kanal ochildi → darhol xabar (0 API)
    """
    from telethon import events as _events
    from telethon.tl.types import UpdateChannel as _UpdateChannel

    async def _check_channel_opened(ub, channel_id: int, ub_idx: int):
        """UpdateChannel kelganda pending ro'yxatida tekshiradi (0 API)."""
        try:
            num_str  = str(channel_id)
            num_full = f"-100{channel_id}"
            async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _db:
                async with _db.execute(
                    "SELECT channel_id, creator_id, source_group "
                    "FROM hidden_channel_knocker "
                    "WHERE status='pending' AND userbot_idx=? "
                    "AND (numeric_id=? OR numeric_id=?)",
                    (ub_idx, num_str, num_full)
                ) as _cur:
                    row = await _cur.fetchone()
            if not row:
                return
            ch_link, creator_id, source_group = row
            try:
                entity = await ub.get_entity(channel_id)
            except Exception:
                return
            if bot and admin_id:
                await _notify_channel_joined(
                    ub, bot, admin_id, ub_idx,
                    entity, ch_link, ch_link, creator_id, source_group
                )
        except Exception as e:
            _dbg("_check_channel_opened", e)

    def _register_for(ub, ub_idx):
        """Bitta userbotga real vaqt handlerlarini ulaydi (0 API)."""

        @ub.on(_events.Raw(_UpdateChannel))
        async def _channel_update(update):
            asyncio.create_task(_check_channel_opened(ub, update.channel_id, ub_idx))

        @ub.on(_events.NewMessage)
        async def _msg_handler(event):
            try:
                msg = event.message
                # Faqat guruh/kanallarni kuzatamiz — shaxsiy (admin↔bot)
                # chatlar va userbotning o'z xabarlari keshga tushmasin.
                if getattr(event, 'is_private', False) or getattr(msg, 'out', False):
                    return
                chat = event.chat  # keshdan, 0 API
                chat_id = str(abs(event.chat_id or 0))
                chat_name = getattr(chat, 'title', chat_id) if chat else chat_id
                src_str = str(event.chat_id or chat_id)

                if is_music_file(msg):
                    asyncio.create_task(
                        _process_realtime_audio(ub, msg, chat_id, chat_name)
                    )
                await _cache_realtime_message(msg, src_str)
            except Exception as e:
                _dbg("_realtime_handler", e)

    # Barcha userbotlarga handler ulanadi (N userbot uchun)
    _bots = _ALL_USERBOTS if _ALL_USERBOTS else (
        [userbot] + ([userbot2] if userbot2 else [])
    )
    for _idx, _ub in enumerate(_bots):
        if _ub is not None:
            _register_for(_ub, _idx)


# ─────────────────────────────────────────────────────────────────────
# PARALLEL MUSIQA SKANERLASH — har a'zo uchun
# ─────────────────────────────────────────────────────────────────────

def is_music_file(msg):
    """Faqat audio/mpeg va audio/mp4 formatlarini qabul qiladi"""
    if msg.voice:
        return False
    if msg.audio:
        mime = (getattr(msg.audio, 'mime_type', '') or '').lower().strip()
        if not mime:
            return False
        return mime in (
            'audio/mpeg', 'audio/mp3', 'audio/mp4',
            'audio/wav', 'audio/x-wav',
            'audio/flac',
            'audio/m4a', 'audio/x-m4a',
        )
    return False

async def _notify_channel_joined(ub, bot, admin_id, idx, entity, ch_id_str, ch_id_raw, creator_id, source_group):
    """
    Kanal ochildi — atomik DB yangilash + admin xabari + musiqa skaner.
    WHERE status='pending' sharti orqali ikki marta xabar yuborilmaydi.
    """
    ch_name = getattr(entity, 'title', ch_id_str)
    numeric_id_str = ""
    if hasattr(entity, 'id') and entity.id:
        _eid = str(entity.id).lstrip('-')
        numeric_id_str = f"-100{_eid}" if not str(entity.id).startswith('-100') else str(entity.id)

    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        cur = await db.execute(
            "UPDATE hidden_channel_knocker SET status='joined', numeric_id=?, channel_id=? "
            "WHERE (channel_id=? OR channel_id=?) AND status='pending'",
            (numeric_id_str, ch_id_str, ch_id_str, ch_id_raw)
        )
        if cur.rowcount == 0:
            return  # Allaqachon 'joined' — ikkinchi xabar yubormaslik
        if creator_id:
            await db.execute(
                "UPDATE users_memory_bank SET has_hidden=? WHERE user_id=?",
                (ch_id_str, creator_id)
            )
        await db.commit()

    print(f"[WATCHER] 📨 Bot xabar yubormoqda → admin_id={admin_id}, kanal={ch_name}")
    try:
        await bot.send_message(
            admin_id,
            f"🔓 **MAXFIY KANALGA KIRISH OCHILDI!**\n\n"
            f"📢 Kanal: `{ch_name}`\n"
            f"🔗 Link: {ch_id_str}\n"
            f"🆔 ID: `{numeric_id_str}`\n"
            f"🏢 Manba: `{source_group}`\n"
            f"🤖 Userbot{idx + 1} orqali\n\n"
            f"🎵 Hozir skanerlanyapti: `{_CURRENT_SCAN_CHANNEL or '—'}`\n"
            f"⏳ U tugagach `{ch_name}` skanerlanadi..."
        )
        print(f"[WATCHER] ✅ Bot xabari yuborildi → {admin_id}")
    except Exception as _e:
        print(f"[WATCHER] ❌ Bot xabari YUBORILMADI → admin_id={admin_id} | Xato: {_e}")
    # Kanalni A'ZO bo'lgan userbotga doimiy biriktirish (chalkashmasin)
    try:
        n = _get_n_userbots()
        if n > 1:
            for _link in (ch_id_str, numeric_id_str):
                if not _link:
                    continue
                async with db_mod.connect(db_mod.DB_NAME, timeout=10) as _adb:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                    await _adb.execute(
                        "INSERT OR REPLACE INTO channel_assignments "
                        "(channel_link, userbot_idx, assigned_at) VALUES (?, ?, ?)",
                        (_link, idx, now_str)
                    )
                    await _adb.commit()
    except Exception as e:
        _dbg("_notify_channel_joined", e)

    # Navbatga A'ZO userbot indeksi bilan qo'shiladi (round-robin EMAS)
    if _JOINED_CHANNEL_QUEUE is not None:
        _JOINED_CHANNEL_QUEUE.put_nowait({
            'link':     ch_id_str,
            'name':     ch_name,
            'bot':      bot,
            'admin_id': admin_id,
            'ub_idx':   idx,   # ← qaysi userbot a'zo bo'lgani
        })
    print(f"[WATCHER] ✅ Kanal ochildi (UB{idx+1} ga biriktirildi): {ch_name}")


async def _get_channel_dialog_ids(ub) -> set:
    """Userbot a'zo bo'lgan barcha kanal ID larini set qaytaradi."""
    ids = set()
    try:
        async for dialog in ub.iter_dialogs(limit=500):
            if hasattr(dialog.entity, 'id'):
                ids.add(dialog.entity.id)
    except Exception as e:
        _dbg("_get_channel_dialog_ids", e)
    return ids


async def _match_new_channel_to_pending(ub, bot, admin_id, idx, channel_id):
    """
    Dialog diff orqali topilgan yangi kanal IDni pending ro'yxatidagi
    invite link bilan moslashtiradi.
    """
    try:
        entity = await ub.get_entity(channel_id)
        if entity is None:
            return

        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            async with db.execute(
                "SELECT rowid, channel_id, creator_id, source_group "
                "FROM hidden_channel_knocker WHERE status='pending' AND userbot_idx=?",
                (idx,)
            ) as cur:
                pending = await cur.fetchall()

        from telethon.tl.functions.messages import CheckChatInviteRequest as _CCIR
        for _, ch_id_raw, creator_id, source_group in pending:
            import urllib.parse
            ch_id_str = urllib.parse.unquote(ch_id_raw)
            if "/+" not in ch_id_str and "joinchat/" not in ch_id_str:
                continue
            hash_part = ch_id_str.split("/+")[-1].rstrip("/") if "/+" in ch_id_str else ch_id_str.split("joinchat/")[-1].rstrip("/")
            try:
                info = await ub(_CCIR(hash=hash_part))
                if hasattr(info, 'chat') and info.chat.id == channel_id:
                    await _notify_channel_joined(ub, bot, admin_id, idx, entity, ch_id_str, ch_id_raw, creator_id, source_group)
                    return
            except Exception as e:
                if "already" in str(e).lower() or "member" in str(e).lower():
                    # Shu link bo'lishi ehtimoli katta
                    await _notify_channel_joined(ub, bot, admin_id, idx, entity, ch_id_str, ch_id_raw, creator_id, source_group)
                    return
            await asyncio.sleep(0.3)
    except Exception as e:
        print(f"[WATCHER] Moslashtirish xatosi (ch={channel_id}): {e}")


async def channel_join_watcher(userbot, bot, admin_id, extra_userbots=None):
    """
    Maxfiy kanallar ochilishini kuzatadi — 2 ta usul:
    - Real-time: UpdateChannel Raw event (0 API)
    - Har 15 daqiqa: CheckChatInviteRequest (smart_channel_knocker)
    iter_dialogs poll olib tashlandi — faqat flood beradi.
    """
    # Bu funksiya endi faqat ishga tushganligini bildiradi.
    # Asosiy ish: setup_realtime_handlers → _check_channel_opened (UpdateChannel)
    #             smart_channel_knocker (har 15 daqiqa CheckChatInviteRequest)
    print("[WATCHER] Kanal kuzatuv ishga tushdi (real-time + 15 daqiqa so'rovnoma)")


async def _distribute_channels(n: int):
    """
    Pending kanallarni n ta userbotga taqsimlaydi.
    MUHIM: so'rovnoma yuborilgan kanal (last_request_time bor) hech qachon
    boshqa userbotga ko'chirilmaydi — faqat NULL bo'lganlar tayinlanadi.
    """
    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            # 0. So'rovnoma YUBORILMAGAN kanallarni NULL ga qaytaramiz —
            #    shunda ular barcha N userbotga teng qayta taqsimlanadi.
            #    (last_request_time bo'sh = hech kim ulanmagan -> ko'chirish xavfsiz)
            await db.execute(
                "UPDATE hidden_channel_knocker SET userbot_idx=NULL "
                "WHERE status='pending' "
                "AND (last_request_time IS NULL OR last_request_time='')"
            )

            # 1. Hozirgi taqsimotni olish (faqat NULL larni tayinlaymiz)
            counts = [0] * n
            async with db.execute(
                "SELECT userbot_idx, COUNT(*) FROM hidden_channel_knocker "
                "WHERE status='pending' AND userbot_idx IS NOT NULL GROUP BY userbot_idx"
            ) as cur:
                for row in await cur.fetchall():
                    idx = int(row[0])
                    if 0 <= idx < n:
                        counts[idx] = row[1]
                    else:
                        # Egasi YO'QOLGAN kanallar (userbot o'chirilgan, idx >= n) → NULL.
                        # So'rovnoma yuborilgan bo'lsa ham ko'chiriladi, chunki
                        # o'sha userbot endi yo'q — aks holda kanal hech qachon ishlanmaydi.
                        await db.execute(
                            "UPDATE hidden_channel_knocker SET userbot_idx=NULL "
                            "WHERE status='pending' AND userbot_idx=?",
                            (row[0],)
                        )

            # 2. REBALANCE YO'Q — so'rovnoma yuborilgan kanallar o'z UB ida qoladi
            #    Faqat yangi (NULL) kanallar tayinlanadi

            # 3. NULL kanallarni eng bo'sh UB ga tayinlash
            async with db.execute(
                "SELECT rowid FROM hidden_channel_knocker "
                "WHERE status='pending' AND userbot_idx IS NULL ORDER BY rowid"
            ) as cur:
                unassigned = [r[0] for r in await cur.fetchall()]

            if not unassigned:
                await db.commit()
                return

            if unassigned:
                params = []
                for rowid in unassigned:
                    min_idx = counts.index(min(counts))
                    params.append((min_idx, rowid))
                    counts[min_idx] += 1
                await db.executemany(
                    "UPDATE hidden_channel_knocker SET userbot_idx=? WHERE rowid=?",
                    params
                )

            await db.commit()
            print(f"[KNOCKER] {len(unassigned)} ta yangi kanal taqsimlandi: {counts}")
    except Exception as e:
        print(f"[KNOCKER] Taqsimlashda xato: {e}")


async def smart_channel_knocker(userbot, bot, admin_id, extra_userbots=None):
    """
    Har 15 daqiqada HAR USERBOT uchun:
    1. Kanallar teng taqsimlangan — har userbot faqat o'zinikidan 1 tasiga so'rovnoma yuboradi
    2. Navbat bo'yicha sikl: 140 kanalga yuborib bo'lgandan so'ng boshidan qaytadi
    3. Kirish ochildi → o'sha userbot musiqa skanerlaydi + admin ga xabar
    extra_userbots: [userbot2, ...] — qo'shimcha userbotlar
    """
    all_bots = [userbot] + [u for u in (extra_userbots or []) if u is not None]
    n = len(all_bots)

    # Barcha tayinlanmagan kanallarni taqsimlash
    await _distribute_channels(n)

    await asyncio.sleep(300)

    while True:
        await asyncio.sleep(KNOCK_INTERVAL)
        if MONITORING_PAUSED:
            continue

        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

            # Yangi qo'shilgan kanallarni taqsimlash
            await _distribute_channels(n)

            for idx, ub in enumerate(all_bots):
                # Bu userbot uchun navbatdagi 1 ta kanal (faqat o'zinikidan)
                async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                    async with db.execute(
                        "SELECT channel_id, creator_id, source_group, last_request_time "
                        "FROM hidden_channel_knocker WHERE status='pending' AND userbot_idx=? "
                        "ORDER BY last_request_time ASC LIMIT 1",
                        (idx,)
                    ) as cur:
                        row = await cur.fetchone()

                if not row:
                    continue

                ch_id_raw, creator_id, source_group, last_req = row
                # URL-encoded linklar (%2B → +) ni decode qilish
                import urllib.parse
                ch_id_str = urllib.parse.unquote(ch_id_raw)
                if MONITORING_PAUSED:
                    break

                # 1. Kirish ochildimi tekshirish
                joined = False
                entity = None
                try:
                    entity = await ub.get_entity(ch_id_str)
                    joined = True
                except Exception as e:
                    _dbg("smart_channel_knocker", e)

                if not joined and ("/+" in ch_id_str or "joinchat/" in ch_id_str):
                    try:
                        from telethon.tl.functions.messages import CheckChatInviteRequest
                        if "/+" in ch_id_str:
                            hash_part = ch_id_str.split("/+")[-1].rstrip("/")
                        else:
                            hash_part = ch_id_str.split("joinchat/")[-1].rstrip("/")
                        invite_info = await ub(CheckChatInviteRequest(hash=hash_part))
                        if hasattr(invite_info, 'chat'):
                            joined = True
                            entity = invite_info.chat
                    except Exception as e:
                        err = str(e).lower()
                        if "already" in err or "member" in err:
                            joined = True
                            try:
                                result = await ub(ImportChatInviteRequest(hash_part))
                                if hasattr(result, 'chats') and result.chats:
                                    entity = result.chats[0]
                            except Exception as e2:
                                err2 = str(e2).lower()
                                if "already" in err2 or "member" in err2:
                                    # a'zo — to'g'ridan-to'g'ri get_entity
                                    try:
                                        entity = await ub.get_entity(ch_id_str)
                                    except Exception as e:
                                        _dbg("smart_channel_knocker", e)
                                else:
                                    joined = False

                if joined and entity is not None:
                    await _notify_channel_joined(ub, bot, admin_id, idx, entity, ch_id_str, ch_id_raw, creator_id, source_group)
                    continue

                # 2. So'rovnoma yuborish
                print(f"[KNOCKER] UB{idx+1} urinmoqda: {ch_id_str[:50]}")
                sent = await send_join_request(ub, ch_id_str)
                print(f"[KNOCKER] UB{idx+1} natija: sent={sent}")
                if sent:
                    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                        await db.execute(
                            "UPDATE hidden_channel_knocker SET last_request_time=?, userbot_idx=?, channel_id=? WHERE channel_id=?",
                            (now_str, idx, ch_id_str, ch_id_raw)
                        )
                        await db.commit()
                    print(f"[KNOCKER] UB{idx+1} DB yangilandi ✓")

        except Exception as e:
            if 'FloodWait' in str(type(e).__name__):
                wait = getattr(e, 'seconds', 60)
                log_flood("smart_channel_knocker", wait)
            print(f"smart_channel_knocker xatosi: {e}")


async def sync_source_messages(userbot, source: str, limit_days: int = 90):
    """
    Bir manbaning yangi xabarlarini lokal bazaga saqlaydi.
    Faqat oxirgi sinxronlashdan keyingi xabarlarni oladi.
    """
    import database as db_mod
    from datetime import timezone, timedelta

    entity = await safe_get_entity(userbot, source)
    if entity is None:
        return 0

    # Oxirgi saqlangan xabar ID sini olish
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT last_msg_id FROM source_sync_state WHERE source=?", (source,)
        ) as cur:
            row = await cur.fetchone()
        last_id = row[0] if row else 0

    # Sana chegarasi — birinchi marta bo'lsa limit_days ga qadar
    offset_date = None
    if last_id == 0:
        offset_date = datetime.now(timezone.utc) - timedelta(days=limit_days)

    saved = 0
    new_last_id = last_id
    msg_counter = 0

    try:
        async for msg in userbot.iter_messages(
            entity,
            limit=None,
            min_id=last_id,
            offset_date=offset_date,
            reverse=True
        ):
            if not msg or not msg.id:
                continue

            # Faqat matnli / audio xabarlar (caption ham)
            text = ""
            if msg.text:
                text = msg.text
            elif getattr(msg, 'caption', None):
                text = msg.caption
            elif msg.audio or msg.voice:
                parts = []
                audio = msg.audio or msg.voice
                if hasattr(audio, 'title') and audio.title:
                    parts.append(audio.title)
                if hasattr(audio, 'performer') and audio.performer:
                    parts.append(audio.performer)
                if msg.file and msg.file.name:
                    parts.append(msg.file.name)
                if msg.message:
                    parts.append(msg.message)
                text = " | ".join(parts)

            if not text:
                continue

            sender = msg.sender
            s_id   = getattr(sender, 'id', 0) if sender else 0
            s_name = ""
            s_un   = ""
            if sender and hasattr(sender, 'first_name'):
                s_name = ((sender.first_name or "") + " " + (sender.last_name or "")).strip()
                s_un   = sender.username or ""
            elif sender and hasattr(sender, 'title'):
                s_name = sender.title or ""

            msg_date = _fmt_date(msg.date)

            async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                try:
                    await db.execute(
                        "INSERT OR IGNORE INTO messages_cache "
                        "(msg_id, source, sender_id, sender_name, sender_username, text, msg_date) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (msg.id, source, s_id, s_name, s_un, text, msg_date)
                    )
                    await db.commit()
                    saved += 1
                except Exception as e:
                    _dbg("sync_source_messages", e)

            if msg.id > new_last_id:
                new_last_id = msg.id

            msg_counter += 1
            # Har 100 xabarda 2 sekund nafas — flood oldini olish
            if msg_counter % 100 == 0:
                await asyncio.sleep(2)
            else:
                await asyncio.sleep(0.03)

    except FloodWaitError as e:
        # Flood bo'lsa — holatni saqlash va chiqish (keyingi sinxronda davom etadi)
        log_flood("sync_source_messages", e.seconds)
        if e.seconds <= 120:
            await asyncio.sleep(e.seconds + 2)
    except Exception as e:
        _dbg("sync_source_messages", e)

    # Sinxron holatini yangilash
    if new_last_id > last_id:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            await db.execute(
                "INSERT OR REPLACE INTO source_sync_state (source, last_msg_id, last_synced) "
                "VALUES (?, ?, ?)",
                (source, new_last_id, now_str)
            )
            await db.commit()

    return saved


async def search_keywords_local(keyword_str: str, days: int = None):
    """
    Lokal messages_cache dan kalit so'z qidiradi.
    FTS5 VA LIKE ikkalasini ishlatib, id bo'yicha deduplikatsiya qiladi.
    Telegram API ga murojaat qilmaydi.
    Qaytaradi: natijalar ro'yxati [{name, username, user_id, date, text, source, matched}]
    """
    import database as db_mod
    from datetime import timedelta

    keywords = [k.strip().lower() for k in keyword_str.split(',') if k.strip()]
    if not keywords:
        return []

    date_param: list = []
    fts_date_clause  = ""
    like_date_clause = ""
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        fts_date_clause  = "AND m.msg_date >= ?"
        like_date_clause = "AND msg_date >= ?"
        date_param = [cutoff]

    seen_ids: set = set()
    combined_rows: list = []

    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        # 1. FTS5 — tez indeks qidiradi
        try:
            fts_terms = " OR ".join(f'"{kw}"' for kw in keywords)
            fts_query = f"""
                SELECT m.id, m.sender_id, m.sender_name, m.sender_username,
                       m.text, m.msg_date, m.source
                FROM messages_fts f
                JOIN messages_cache m ON m.id = f.rowid
                WHERE messages_fts MATCH ?
                {fts_date_clause}
                ORDER BY m.msg_date DESC
                LIMIT 500
            """
            async with db.execute(fts_query, [fts_terms] + date_param) as cur:
                for row in await cur.fetchall():
                    if row[0] not in seen_ids:
                        seen_ids.add(row[0])
                        combined_rows.append(row[1:])
        except Exception as e:
            _dbg("search_keywords_local", e)

        # 2. LIKE — messages_cache dagi BARCHA satrlarni qidiradi
        #    (FTS5 indeksida bo'lmagan eski ma'lumotlarni ham topadi)
        like_clauses = " OR ".join(["LOWER(text) LIKE ?" for _ in keywords])
        like_params  = [f"%{kw}%" for kw in keywords]
        like_query = f"""
            SELECT id, sender_id, sender_name, sender_username,
                   text, msg_date, source
            FROM messages_cache
            WHERE ({like_clauses})
            {like_date_clause}
            ORDER BY msg_date DESC
            LIMIT 500
        """
        async with db.execute(like_query, like_params + date_param) as cur:
            for row in await cur.fetchall():
                if row[0] not in seen_ids:
                    seen_ids.add(row[0])
                    combined_rows.append(row[1:])

    results = []
    for (s_id, s_name, s_un, text, msg_date, source) in combined_rows:
        search_text = (text or "").lower()
        matched = [kw for kw in keywords if kw in search_text]
        if not matched:
            continue
        display = text or ""
        if len(display) > 300:
            display = display[:297] + "..."
        results.append({
            'name':     s_name or "",
            'username': s_un   or "",
            'user_id':  s_id   or 0,
            'date':     msg_date or "",
            'text':     display,
            'source':   source or "",
            'matched':  ", ".join(matched)
        })

    return results


async def get_cache_stats():
    """Kesh statistikasi."""
    import database as db_mod
    async with db_mod.connect(db_mod.DB_NAME, timeout=15) as db:
        async with db.execute("SELECT COUNT(*) FROM messages_cache") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(DISTINCT source) FROM messages_cache") as cur:
            sources = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT MAX(last_synced) FROM source_sync_state"
        ) as cur:
            last_sync = (await cur.fetchone())[0]
    return total, sources, last_sync


# ─────────────────────────────────────────────────────────────────────
# ID BO'YICHA QIDIRUV
# ─────────────────────────────────────────────────────────────────────

def _make_msg_link(source: str, msg_id: int) -> str:
    """Xabar havolasini yaratish."""
    if not source or not msg_id:
        return ""
    s = source.strip()
    # t.me/username/msg_id
    if 't.me/' in s and '+' not in s and 'joinchat' not in s:
        uname = s.split('t.me/')[-1].split('/')[0].rstrip('/')
        if uname:
            return f"https://t.me/{uname}/{msg_id}"
    if s.startswith('@'):
        return f"https://t.me/{s[1:]}/{msg_id}"
    # Numeric channel ID
    if s.lstrip('-').isdigit():
        num = abs(int(s))
        if num > 1000000000:
            return f"https://t.me/c/{num}/{msg_id}"
    return ""


async def lookup_channel_by_id(userbot, channel_id: int):
    """
    Kanal ID si bo'yicha kanal ma'lumotlari va havola qaytaradi.
    Avval DB dan, keyin Telegram API dan qidiradi.
    """
    import database as db_mod

    # 1. Lokal DB dan qidirish
    abs_id = str(abs(channel_id))
    async with db_mod.connect(db_mod.DB_NAME, timeout=15) as db:
        async with db.execute(
            "SELECT channel_id, numeric_id FROM hidden_channel_knocker "
            "WHERE numeric_id=? OR channel_id=?",
            (abs_id, str(channel_id))
        ) as cur:
            row = await cur.fetchone()

    if row:
        ch_link = row[0]
        return {'title': ch_link, 'username': None, 'members': None}, ch_link

    # 2. resolved_channel_ids dan
    async with db_mod.connect(db_mod.DB_NAME, timeout=15) as db:
        async with db.execute(
            "SELECT channel_link FROM resolved_channel_ids WHERE numeric_id=?",
            (abs_id,)
        ) as cur:
            row2 = await cur.fetchone()
    if row2:
        return {'title': row2[0], 'username': None, 'members': None}, row2[0]

    # 3. Telegram API dan (bir marta so'rov)
    try:
        entity = await safe_get_entity(userbot, channel_id)
        if entity is None:
            return None, None
        title   = getattr(entity, 'title', str(channel_id))
        uname   = getattr(entity, 'username', None)
        members = getattr(entity, 'participants_count', None)
        if uname:
            link = f"https://t.me/{uname}"
        else:
            link = f"https://t.me/c/{abs(channel_id)}"
        return {'title': title, 'username': uname, 'members': members}, link
    except Exception:
        return None, None


async def lookup_user_by_id(user_id: int):
    """
    Profil ID bo'yicha to'liq qidiruv:
    1. users_memory_bank — a'zo guruhlar + profil ma'lumotlari (xabar yozmagan bo'lsa ham)
    2. messages_cache    — yozgan xabarlar + havolalar
    """
    import database as db_mod

    # 1. Profil va guruhlar
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            """
            SELECT MAX(first_name), MAX(last_name), MAX(username), MAX(phone),
                   MAX(bio), MAX(open_channels), MAX(has_hidden),
                   GROUP_CONCAT(DISTINCT group_link), MIN(added_date), MAX(last_updated)
            FROM users_memory_bank WHERE user_id=?
            """,
            (user_id,)
        ) as cur:
            pr = await cur.fetchone()

    # 2. Xabarlar
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT msg_id, source, sender_name, sender_username, text, msg_date "
            "FROM messages_cache WHERE sender_id=? ORDER BY msg_date DESC",
            (user_id,)
        ) as cur:
            msg_rows = await cur.fetchall()

    profile = None
    if pr and any(pr):
        fn, ln, un, ph, bio, oc, hh, grps, added, updated = pr
        profile = {
            'first_name':    fn or "",
            'last_name':     ln or "",
            'username':      un or "",
            'phone':         ph or "",
            'bio':           bio or "",
            'open_channels': oc or "",
            'has_hidden':    hh or "",
            'groups':        [g.strip() for g in (grps or "").split(',') if g.strip()],
            'added_date':    added or "",
            'last_updated':  updated or "",
        }

    messages = []
    for msg_id, source, name, username, text, msg_date in msg_rows:
        link = _make_msg_link(source, msg_id)
        src_title = source or ""
        if 't.me/' in src_title:
            src_title = src_title.split('t.me/')[-1].rstrip('/')
        elif src_title.startswith('@'):
            src_title = src_title[1:]
        messages.append({
            'msg_id':       msg_id,
            'source':       source,
            'source_title': src_title,
            'name':         name or "",
            'username':     username or "",
            'text':         (text or "")[:300],
            'date':         msg_date or "",
            'link':         link,
        })

    return profile, messages


async def lookup_user_messages(user_id: int):
    _, messages = await lookup_user_by_id(user_id)
    return messages


# ═════════════════════════════════════════════════════════════════════
# YANGI FUNKSIYALAR — TERGOV VA SCAM ANIQLASH
# ═════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────
# #7 TRUST SCORE
# ─────────────────────────────────────────────────────────────────────

def calculate_trust_score(profile: dict, msg_count: int = 0) -> tuple:
    score = 0
    reasons = []
    if profile.get('phone'):
        score += 25; reasons.append("+25 telefon")
    if profile.get('bio') and len(profile['bio']) > 3:
        score += 15; reasons.append("+15 bio")
    if profile.get('username'):
        score += 10; reasons.append("+10 username")
    if profile.get('open_channels') and profile['open_channels'] not in ('', "Yo'q", 'Yoq'):
        score += 10; reasons.append("+10 ochiq kanal")
    if len(profile.get('groups', [])) >= 3:
        score += 10; reasons.append("+10 3+ guruh")
    if msg_count > 0:
        score += 10; reasons.append("+10 xabar yozgan")
    if msg_count > 50:
        score += 5; reasons.append("+5 faol")
    if profile.get('has_hidden') and profile['has_hidden'] not in ('', '❌', 'Yoq', "Yo'q"):
        score -= 20; reasons.append("-20 maxfiy kanal")
    if profile.get('added_date'):
        try:
            added = datetime.strptime(profile['added_date'][:10], '%Y-%m-%d')
            days = (datetime.now() - added).days
            if days > 365:
                score += 15; reasons.append("+15 eski akkaunt")
            elif days > 90:
                score += 8; reasons.append("+8 90+ kun")
        except Exception as e:
            _dbg("calculate_trust_score", e)
    score = max(0, min(100, score))
    if score >= 70:
        label = "🟢 Ishonchli"
    elif score >= 40:
        label = "🟡 O'rta"
    else:
        label = "🔴 Shubhali"
    return score, label, reasons


# ─────────────────────────────────────────────────────────────────────
# #10 USERNAME / TELEFON BO'YICHA QIDIRUV
# ─────────────────────────────────────────────────────────────────────

async def search_by_username(username: str) -> list:
    import database as db_mod
    uname = username.lstrip('@').lower().strip()
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            """
            SELECT DISTINCT user_id, MAX(first_name), MAX(last_name), username,
                   MAX(phone), MAX(bio), GROUP_CONCAT(DISTINCT group_link), MAX(added_date)
            FROM users_memory_bank
            WHERE LOWER(username)=?
            GROUP BY user_id
            LIMIT 20
            """,
            (uname,)
        ) as cur:
            rows = await cur.fetchall()
    results = []
    for r in rows:
        results.append({
            'user_id':    r[0],
            'first_name': r[1] or '',
            'last_name':  r[2] or '',
            'username':   r[3] or '',
            'phone':      r[4] or '',
            'bio':        r[5] or '',
            'groups':     [g.strip() for g in (r[6] or '').split(',') if g.strip()],
            'added_date': r[7] or '',
        })
    return results

async def search_by_phone(userbot, phone: str) -> dict:
    import database as db_mod
    from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
    from telethon.tl.types import InputPhoneContact

    # Avval bazadan qidirish
    clean = phone.strip().replace(' ', '').replace('-', '')
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            """
            SELECT DISTINCT user_id, MAX(first_name), MAX(last_name), MAX(username),
                   phone, MAX(bio), GROUP_CONCAT(DISTINCT group_link), MAX(added_date)
            FROM users_memory_bank
            WHERE phone LIKE ?
            GROUP BY user_id LIMIT 5
            """,
            (f'%{clean[-9:]}%',)
        ) as cur:
            db_rows = await cur.fetchall()

    db_result = None
    if db_rows:
        r = db_rows[0]
        db_result = {
            'user_id':    r[0],
            'first_name': r[1] or '',
            'last_name':  r[2] or '',
            'username':   r[3] or '',
            'phone':      r[4] or '',
            'bio':        r[5] or '',
            'groups':     [g.strip() for g in (r[6] or '').split(',') if g.strip()],
            'added_date': r[7] or '',
            'source':     'baza',
        }
        return db_result

    # Telegram ImportContacts orqali qidirish
    try:
        result = await userbot(ImportContactsRequest([
            InputPhoneContact(client_id=0, phone=clean, first_name='X', last_name='')
        ]))
        if result.users:
            u = result.users[0]
            try:
                await userbot(DeleteContactsRequest(id=[u.id]))
            except Exception as e:
                _dbg("search_by_phone", e)
            return {
                'user_id':    u.id,
                'first_name': u.first_name or '',
                'last_name':  u.last_name or '',
                'username':   u.username or '',
                'phone':      getattr(u, 'phone', phone) or phone,
                'bio':        '',
                'groups':     [],
                'added_date': '',
                'source':     'telegram',
            }
    except Exception as e:
        _dbg("search_by_phone", e)
    return None


# ─────────────────────────────────────────────────────────────────────
# #12 FOYDALANUVCHI FAOLLIK TIMELINE
# ─────────────────────────────────────────────────────────────────────

async def get_user_timeline(user_id: int, days: int = 30) -> dict:
    import database as db_mod
    from datetime import timedelta
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            """
            SELECT DATE(msg_date) as day, COUNT(*) as cnt, GROUP_CONCAT(DISTINCT source)
            FROM messages_cache
            WHERE sender_id=? AND msg_date >= ?
            GROUP BY day ORDER BY day
            """,
            (user_id, since)
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute(
            "SELECT COUNT(*) FROM messages_cache WHERE sender_id=?", (user_id,)
        ) as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT MIN(msg_date), MAX(msg_date) FROM messages_cache WHERE sender_id=?",
            (user_id,)
        ) as cur:
            span = await cur.fetchone()
    timeline = [{'date': r[0], 'count': r[1], 'sources': r[2]} for r in rows]
    return {'timeline': timeline, 'total': total,
            'first_msg': span[0] if span else None,
            'last_msg':  span[1] if span else None}


# ─────────────────────────────────────────────────────────────────────
# #15 CROSS-GROUP TAHLIL — UMUMIY A'ZOLAR
# ─────────────────────────────────────────────────────────────────────

async def get_common_members(group1: str, group2: str) -> list:
    import database as db_mod
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            """
            SELECT a.user_id, a.first_name, a.last_name, a.username, a.phone
            FROM users_memory_bank a
            JOIN users_memory_bank b ON a.user_id = b.user_id
            WHERE a.group_link=? AND b.group_link=?
            ORDER BY a.first_name
            """,
            (group1, group2)
        ) as cur:
            rows = await cur.fetchall()
    return [{'user_id': r[0], 'first_name': r[1] or '', 'last_name': r[2] or '',
             'username': r[3] or '', 'phone': r[4] or ''} for r in rows]


# ─────────────────────────────────────────────────────────────────────
# #16 KOORDINATSIYALANGAN XATTI-HARAKAT
# ─────────────────────────────────────────────────────────────────────

async def detect_coordinated_behavior(group_link: str, hours: int = 48) -> dict:
    import database as db_mod
    from datetime import timedelta

    # 1. Bir vaqtda qo'shilgan akkauntlar (added_date bo'yicha)
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            """
            SELECT user_id, first_name, username, phone, bio, added_date
            FROM users_memory_bank
            WHERE group_link=? AND added_date IS NOT NULL
            ORDER BY added_date
            """,
            (group_link,)
        ) as cur:
            members = await cur.fetchall()

    # Vaqt oynasida guruhlash
    clusters = []
    if members:
        window_secs = hours * 3600
        i = 0
        while i < len(members):
            cluster = [members[i]]
            j = i + 1
            try:
                t0 = datetime.strptime(members[i][5][:16], '%Y-%m-%d %H:%M')
            except Exception:
                i += 1
                continue
            while j < len(members):
                try:
                    tj = datetime.strptime(members[j][5][:16], '%Y-%m-%d %H:%M')
                    if (tj - t0).total_seconds() <= window_secs:
                        cluster.append(members[j])
                        j += 1
                    else:
                        break
                except Exception:
                    j += 1
            if len(cluster) >= 5:
                clusters.append(cluster)
            i = j if j > i else i + 1

    # 2. Bio o'xshashligi — bo'sh bio li akkauntlar
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            """
            SELECT COUNT(*) FROM users_memory_bank
            WHERE group_link=? AND (bio IS NULL OR bio='')
            """,
            (group_link,)
        ) as cur:
            no_bio_count = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users_memory_bank WHERE group_link=?", (group_link,)
        ) as cur:
            total_count = (await cur.fetchone())[0]
        async with db.execute(
            """
            SELECT COUNT(*) FROM users_memory_bank
            WHERE group_link=? AND (phone IS NULL OR phone='')
            """,
            (group_link,)
        ) as cur:
            no_phone_count = (await cur.fetchone())[0]

    no_bio_pct   = round(no_bio_count * 100 / max(total_count, 1))
    no_phone_pct = round(no_phone_count * 100 / max(total_count, 1))
    risk_score = 0
    if no_bio_pct > 70:   risk_score += 30
    if no_phone_pct > 80: risk_score += 20
    if clusters:          risk_score += min(len(clusters) * 10, 50)
    risk_score = min(risk_score, 100)

    return {
        'total':         total_count,
        'clusters':      clusters[:5],
        'cluster_count': len(clusters),
        'no_bio_pct':    no_bio_pct,
        'no_phone_pct':  no_phone_pct,
        'risk_score':    risk_score,
    }


# ─────────────────────────────────────────────────────────────────────
# #17 YOZUV USLUBI TAHLILI
# ─────────────────────────────────────────────────────────────────────

async def analyze_writing_style(user_id: int) -> dict:
    import database as db_mod
    import re as _re
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT text FROM messages_cache WHERE sender_id=? AND is_deleted=0 "
            "AND text IS NOT NULL LIMIT 500",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return None
    texts = [r[0] for r in rows if r[0]]
    total_chars  = sum(len(t) for t in texts)
    total_words  = sum(len(t.split()) for t in texts)
    avg_len      = round(total_chars / max(len(texts), 1))
    avg_words    = round(total_words / max(len(texts), 1))
    emoji_count  = sum(len(_re.findall(r'[\U0001F000-\U0001FFFF]', t)) for t in texts)
    question_cnt = sum(t.count('?') for t in texts)
    exclaim_cnt  = sum(t.count('!') for t in texts)
    caps_ratio   = round(sum(1 for t in texts for c in t if c.isupper()) /
                         max(total_chars, 1) * 100)
    # Top so'zlar
    all_words = []
    for t in texts:
        for w in t.lower().split():
            w = _re.sub(r'[^\w]', '', w)
            if len(w) > 3:
                all_words.append(w)
    word_freq = {}
    for w in all_words:
        word_freq[w] = word_freq.get(w, 0) + 1
    top_words = sorted(word_freq.items(), key=lambda x: -x[1])[:10]
    return {
        'msg_count':   len(texts),
        'avg_len':     avg_len,
        'avg_words':   avg_words,
        'emoji_ratio': round(emoji_count / max(len(texts), 1), 1),
        'question_pct': round(question_cnt * 100 / max(len(texts), 1)),
        'exclaim_pct':  round(exclaim_cnt * 100 / max(len(texts), 1)),
        'caps_ratio':   caps_ratio,
        'top_words':    top_words,
    }

async def compare_writing_styles(user_id1: int, user_id2: int) -> int:
    s1 = await analyze_writing_style(user_id1)
    s2 = await analyze_writing_style(user_id2)
    if not s1 or not s2:
        return 0
    score = 100
    diff_len   = abs(s1['avg_len']   - s2['avg_len'])
    diff_words = abs(s1['avg_words'] - s2['avg_words'])
    diff_emoji = abs(s1['emoji_ratio'] - s2['emoji_ratio'])
    diff_caps  = abs(s1['caps_ratio'] - s2['caps_ratio'])
    score -= min(diff_len // 5, 25)
    score -= min(diff_words * 3, 20)
    score -= min(int(diff_emoji * 10), 15)
    score -= min(diff_caps // 2, 15)
    # Umumiy so'zlar
    words1 = {w for w, _ in s1['top_words']}
    words2 = {w for w, _ in s2['top_words']}
    common = len(words1 & words2)
    score += common * 3
    return max(0, min(100, score))


# ─────────────────────────────────────────────────────────────────────
# #18 VAQT KORRELYATSIYASI
# ─────────────────────────────────────────────────────────────────────

async def get_temporal_correlations(min_overlap: int = 5, limit: int = 20) -> list:
    import database as db_mod
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        # Har bir foydalanuvchi qaysi soatlarda faol
        async with db.execute(
            """
            SELECT sender_id, CAST(strftime('%H', msg_date) AS INTEGER) as hour, COUNT(*) as cnt
            FROM messages_cache
            WHERE sender_id IS NOT NULL AND is_deleted=0
            GROUP BY sender_id, hour
            HAVING cnt >= 2
            """,
        ) as cur:
            rows = await cur.fetchall()
    # {user_id: set of active hours}
    user_hours = {}
    for sender_id, hour, _ in rows:
        if sender_id not in user_hours:
            user_hours[sender_id] = set()
        user_hours[sender_id].add(hour)
    # Juftliklar
    users = list(user_hours.keys())
    pairs = []
    for i in range(len(users)):
        for j in range(i + 1, len(users)):
            overlap = len(user_hours[users[i]] & user_hours[users[j]])
            if overlap >= min_overlap:
                pairs.append((users[i], users[j], overlap))
    pairs.sort(key=lambda x: -x[2])
    return pairs[:limit]


# ─────────────────────────────────────────────────────────────────────
# #19 O'CHIRILGAN XABARLARNI KUZATISH
# ─────────────────────────────────────────────────────────────────────

async def get_deleted_messages(source: str, limit: int = 50) -> list:
    import database as db_mod
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            """
            SELECT msg_id, sender_id, sender_name, sender_username, text, msg_date
            FROM messages_cache
            WHERE source=? AND is_deleted=1
            ORDER BY msg_date DESC LIMIT ?
            """,
            (source, limit)
        ) as cur:
            rows = await cur.fetchall()
    return [{'msg_id': r[0], 'sender_id': r[1], 'name': r[2] or '',
             'username': r[3] or '', 'text': (r[4] or '')[:300],
             'date': r[5] or '', 'link': _make_msg_link(source, r[0])} for r in rows]

async def mark_deleted_messages(source: str, current_msg_ids: set):
    import database as db_mod
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT msg_id FROM messages_cache WHERE source=? AND is_deleted=0",
            (source,)
        ) as cur:
            cached_ids = {r[0] for r in await cur.fetchall()}
    deleted_ids = cached_ids - current_msg_ids
    if deleted_ids:
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            await db.executemany(
                "UPDATE messages_cache SET is_deleted=1 WHERE msg_id=? AND source=?",
                [(mid, source) for mid in deleted_ids]
            )
            await db.commit()
    return len(deleted_ids)


# ─────────────────────────────────────────────────────────────────────
# #20 AKKAUNT HAYOTI TAHLILI
# ─────────────────────────────────────────────────────────────────────

async def get_account_lifecycle(user_id: int) -> dict:
    import database as db_mod
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        # Oylik faollik
        async with db.execute(
            """
            SELECT strftime('%Y-%m', msg_date) as month, COUNT(*) as cnt
            FROM messages_cache
            WHERE sender_id=? AND is_deleted=0
            GROUP BY month ORDER BY month
            """,
            (user_id,)
        ) as cur:
            monthly = await cur.fetchall()
        # Soatlik faollik
        async with db.execute(
            """
            SELECT CAST(strftime('%H', msg_date) AS INTEGER) as hour, COUNT(*) as cnt
            FROM messages_cache
            WHERE sender_id=?
            GROUP BY hour ORDER BY hour
            """,
            (user_id,)
        ) as cur:
            hourly = await cur.fetchall()
        # O'zgarishlar soni
        async with db.execute(
            "SELECT COUNT(*) FROM user_change_log WHERE user_id=?", (user_id,)
        ) as cur:
            change_count = (await cur.fetchone())[0]
        # Birinchi ko'rinish
        async with db.execute(
            "SELECT MIN(added_date) FROM users_memory_bank WHERE user_id=?", (user_id,)
        ) as cur:
            first_seen = (await cur.fetchone())[0]
        # Birinchi va oxirgi xabar
        async with db.execute(
            "SELECT MIN(msg_date), MAX(msg_date) FROM messages_cache WHERE sender_id=? AND is_deleted=0",
            (user_id,)
        ) as cur:
            span = await cur.fetchone()
        first_msg = span[0] if span and span[0] else None
        last_msg  = span[1] if span and span[1] else None

    if not monthly:
        peak_hour = None
        peak_month = None
    else:
        peak_month = max(monthly, key=lambda x: x[1])[0] if monthly else None
        peak_hour  = max(hourly,  key=lambda x: x[1])[0] if hourly  else None

    return {
        'monthly':      [{'month': r[0], 'count': r[1]} for r in monthly],
        'hourly':       [{'hour': r[0],  'count': r[1]} for r in hourly],
        'peak_month':   peak_month,
        'peak_hour':    peak_hour,
        'change_count': change_count,
        'first_seen':   first_seen or '',
        'first_msg':    first_msg,
        'last_msg':     last_msg,
    }


# ─────────────────────────────────────────────────────────────────────
# #24 PROFIL RASMI
# ─────────────────────────────────────────────────────────────────────

async def get_profile_photo(userbot, user_id: int) -> str:
    try:
        from telethon.tl.functions.photos import GetUserPhotosRequest
        photos = await userbot(GetUserPhotosRequest(
            user_id=user_id, offset=0, max_id=0, limit=1
        ))
        if photos.photos:
            path = f"{_TMP}/tg_photo_{user_id}.jpg"
            await userbot.download_media(photos.photos[0], file=path)
            return path
    except Exception as e:
        _dbg("get_profile_photo", e)
    return None


# ─────────────────────────────────────────────────────────────────────
# #14 EVIDENCE PAKETI
# ─────────────────────────────────────────────────────────────────────

async def generate_evidence_report(user_id: int, userbot=None) -> str:
    import database as db_mod
    profile, messages = await lookup_user_by_id(user_id)
    changes          = await db_mod.get_user_change_log(user_id)
    lifecycle        = await get_account_lifecycle(user_id)
    style            = await analyze_writing_style(user_id)
    score, label, _  = calculate_trust_score(profile or {}, len(messages))

    lines = []
    sep = "=" * 60
    lines.append(sep)
    lines.append("TERGOV HISOBOTI — KIBER-STANSIYA OSINT PRO")
    lines.append(f"Sana: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Profil ID: {user_id}")
    lines.append(sep)
    lines.append("")

    if profile:
        full_name = (profile['first_name'] + " " + profile['last_name']).strip() or "Nomsiz"
        lines.append("[ PROFIL MA'LUMOTI ]")
        lines.append(f"Ism:             {full_name}")
        lines.append(f"Username:        @{profile['username']}" if profile['username'] else "Username:        —")
        lines.append(f"Telefon:         {profile['phone']}" if profile['phone'] else "Telefon:         Yopiq")
        lines.append(f"Bio:             {profile['bio'][:200]}" if profile['bio'] else "Bio:             —")
        lines.append(f"Ochiq kanal:     {profile['open_channels']}" if profile['open_channels'] else "Ochiq kanal:     —")
        lines.append(f"Maxfiy kanal:    {profile['has_hidden']}" if profile['has_hidden'] and profile['has_hidden'] not in ('❌', '') else "Maxfiy kanal:    Yo'q")
        lines.append(f"Birinchi ko'rilgan: {profile['added_date']}")
        lines.append(f"So'nggi yangilanish: {profile['last_updated']}")
        lines.append("")
        lines.append(f"[ A'ZO GURUHLAR — {len(profile['groups'])} ta ]")
        for g in profile['groups']:
            lines.append(f"  • {g}")
        lines.append("")
    else:
        lines.append("[ PROFIL: bazada topilmadi ]")
        lines.append("")

    lines.append(f"[ TRUST SCORE: {score}/100 — {label} ]")
    lines.append("")

    if changes:
        lines.append(f"[ O'ZGARISHLAR TARIXI — {len(changes)} ta ]")
        for ch in changes:
            lines.append(f"  {ch[3]}  {ch[0]}: '{ch[1]}' → '{ch[2]}'")
        lines.append("")

    if lifecycle['monthly']:
        lines.append("[ OYLIK FAOLLIK ]")
        for m in lifecycle['monthly']:
            bar = "█" * min(m['count'], 40)
            lines.append(f"  {m['month']}: {bar} ({m['count']})")
        if lifecycle['peak_hour'] is not None:
            lines.append(f"  Eng faol soat: {lifecycle['peak_hour']}:00")
        lines.append("")

    if style:
        lines.append("[ YOZUV USLUBI ]")
        lines.append(f"  O'rtacha xabar uzunligi: {style['avg_len']} belgi, {style['avg_words']} so'z")
        lines.append(f"  Emoji/xabar: {style['emoji_ratio']}")
        lines.append(f"  Savol (%): {style['question_pct']}  Undov (%): {style['exclaim_pct']}")
        lines.append(f"  Katta harf (%): {style['caps_ratio']}")
        top = ", ".join(f"{w}({c})" for w, c in style['top_words'][:5])
        lines.append(f"  Tez-tez ishlatiladigan so'zlar: {top}")
        lines.append("")

    if messages:
        lines.append(f"[ XABARLAR — {len(messages)} ta ]")
        for i, m in enumerate(messages):
            lines.append(f"  [{i+1}] {m['date']} | {m['source_title']}")
            lines.append(f"      {m['text'][:150]}")
            if m.get('link'):
                lines.append(f"      Havola: {m['link']}")
        lines.append("")

    lines.append(sep)
    lines.append("HISOBOT TUGADI")
    lines.append(sep)

    report_text = "\n".join(lines)
    path = f"{_TMP}/evidence_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(report_text)
    return path


# ─────────────────────────────────────────────────────────────────────
# #22 TERGOV — HISOBOT
# ─────────────────────────────────────────────────────────────────────

async def generate_investigation_report(inv_id: int) -> str:
    import database as db_mod
    invs = await db_mod.get_investigations()
    inv  = next((i for i in invs if i[0] == inv_id), None)
    if not inv:
        return None
    targets = await db_mod.get_investigation_targets(inv_id)

    lines = []
    sep = "=" * 60
    lines.append(sep)
    lines.append(f"TERGOV: {inv[1]}")
    lines.append(f"Yaratilgan: {inv[4]}")
    if inv[3]:
        lines.append(f"Izohlar: {inv[3]}")
    lines.append(sep)
    lines.append("")

    user_ids = [int(t[2]) for t in targets if t[1] == 'user' and str(t[2]).lstrip('-').isdigit()]
    channel_ids = [t[2] for t in targets if t[1] == 'channel']

    for uid in user_ids:
        profile, messages = await lookup_user_by_id(uid)
        score, label, _   = calculate_trust_score(profile or {}, len(messages))
        lines.append(f"[ SHAXS ID: {uid} ]")
        if profile:
            full = (profile['first_name'] + " " + profile['last_name']).strip()
            lines.append(f"  Ism: {full}")
            if profile['username']:
                lines.append(f"  Username: @{profile['username']}")
            if profile['phone']:
                lines.append(f"  Telefon: {profile['phone']}")
            lines.append(f"  Guruhlar: {', '.join(profile['groups'][:5])}")
        lines.append(f"  Trust Score: {score}/100 — {label}")
        lines.append(f"  Xabarlar: {len(messages)} ta")
        # Umumiy guruhlar boshqa shaxslar bilan
        if len(user_ids) > 1:
            profile_groups = set(profile['groups']) if profile else set()
            for uid2 in user_ids:
                if uid2 == uid:
                    continue
                p2, _ = await lookup_user_by_id(uid2)
                if p2:
                    common = profile_groups & set(p2['groups'])
                    if common:
                        lines.append(f"  Umumiy guruh {uid2} bilan: {', '.join(list(common)[:3])}")
        lines.append("")

    for ch in channel_ids:
        lines.append(f"[ KANAL: {ch} ]")
        lines.append("")

    lines.append(sep)
    path = f"{_TMP}/investigation_{inv_id}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    with open(path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    return path


# ─────────────────────────────────────────────────────────────────────
# #23 TARMOQ TOPOLOGIYASI (HTML)
# ─────────────────────────────────────────────────────────────────────

async def generate_network_map(user_ids: list) -> str:
    import database as db_mod
    import json as _json

    nodes = []
    edges = []
    seen_nodes = set()
    group_nodes = set()

    for uid in user_ids[:30]:
        profile, _ = await lookup_user_by_id(uid)
        if not profile:
            continue
        label = (profile['first_name'] or str(uid))[:20]
        score, s_label, _ = calculate_trust_score(profile)
        color = '#e74c3c' if score < 40 else ('#f39c12' if score < 70 else '#2ecc71')
        if uid not in seen_nodes:
            nodes.append({'id': str(uid), 'label': label, 'color': color,
                          'title': f"ID:{uid} | Score:{score}"})
            seen_nodes.add(uid)
        for g in profile['groups']:
            if g not in group_nodes:
                nodes.append({'id': g, 'label': g[:20], 'color': '#3498db',
                              'shape': 'diamond', 'title': g})
                group_nodes.add(g)
            edges.append({'from': str(uid), 'to': g})

    nodes_json = _json.dumps(nodes)
    edges_json = _json.dumps(edges)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Network Map — OSINT Pro</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>body{{margin:0;background:#1a1a2e}}
#net{{width:100%;height:100vh}}
</style></head><body>
<div id="net"></div>
<script>
var nodes=new vis.DataSet({nodes_json});
var edges=new vis.DataSet({edges_json});
var container=document.getElementById('net');
var options={{
  nodes:{{font:{{color:'#fff'}},size:20}},
  edges:{{color:'#555',arrows:'to'}},
  physics:{{stabilization:true}},
  background:{{color:'#1a1a2e'}}
}};
new vis.Network(container,{{nodes:nodes,edges:edges}},options);
</script></body></html>"""

    path = f"{_TMP}/network_map_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    return path


# ─────────────────────────────────────────────────────────────────────
# ALERT TEKSHIRUVI — xabar cache ga qo'shilganda
# ─────────────────────────────────────────────────────────────────────

async def check_message_alerts(msg_id: int, source: str, sender_id: int,
                                sender_name: str, text: str, msg_date: str) -> list:
    import database as db_mod
    if not text:
        return []
    text_lower = text.lower()
    alerts = await _get_alerts_cached()
    hits = []
    for alert_id, admin_id, keyword, target_groups in alerts:
        if keyword not in text_lower:
            continue
        if target_groups:
            allowed = [g.strip() for g in target_groups.split(',')]
            if not any(g in source for g in allowed):
                continue
        is_new = await db_mod.check_and_record_alert_hit(alert_id, msg_id, source, sender_id or 0)
        if is_new:
            hits.append({
                'admin_id':    admin_id,
                'keyword':     keyword,
                'msg_id':      msg_id,
                'source':      source,
                'sender_id':   sender_id,
                'sender_name': sender_name or '',
                'text':        text[:300],
                'date':        msg_date,
                'link':        _make_msg_link(source, msg_id),
            })
    return hits


# ═════════════════════════════════════════════════════════════════════
# TERGOV MA'LUMOTI — TO'LIQ PDF HISOBOT
# ═════════════════════════════════════════════════════════════════════

async def resolve_identifier_to_uid(userbot, identifier: str):
    """
    Telefon, @username yoki ID dan user_id ni aniqlash.
    Returns: (user_id, source_info)
    """
    import database as db_mod
    ident = identifier.strip()

    # Numeric ID (musbat)
    if ident.lstrip('-').isdigit():
        num = int(ident)
        if num > 0:
            return num, f"ID: {num}"
        else:
            return None, "Kanal ID (manfiy)"

    # @username
    if ident.startswith('@'):
        uname = ident[1:].lower()
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            async with db.execute(
                "SELECT DISTINCT user_id FROM users_memory_bank WHERE LOWER(username)=? LIMIT 1",
                (uname,)
            ) as cur:
                row = await cur.fetchone()
        if row:
            return row[0], f"@{uname} (bazadan)"
        # Telegram dan qidirish
        try:
            entity = await userbot.get_entity(ident)
            return entity.id, f"@{uname} (Telegramdan)"
        except Exception:
            return None, "Username topilmadi"

    # Telefon raqam
    if ident.startswith('+') or (len(ident) >= 9 and ident[0].isdigit()):
        result = await search_by_phone(userbot, ident)
        if result:
            return result['user_id'], f"Telefon: {ident}"
        return None, "Telefon topilmadi"

    return None, "Noma'lum format"


async def generate_tergov_pdf(userbot, identifier: str) -> str:
    """
    Telefon / @username / ID bo'yicha to'liq PDF tergov hisoboti.
    """
    import database as db_mod

    BASE = os.path.dirname(os.path.abspath(__file__))
    FONT_PATH = os.path.join(BASE, 'DejaVuSans.ttf')

    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                     TableStyle, Image, HRFlowable, PageBreak)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfbase import pdfmetrics

    def _valid_img(path):
        """Rasmni PIL bilan tekshiradi — yaroqsiz bo'lsa False qaytaradi."""
        try:
            from PIL import Image as PILImage
            if not path or not os.path.exists(path) or os.path.getsize(path) < 100:
                return False
            with PILImage.open(path) as im:
                im.verify()
            return True
        except Exception:
            return False
    from reportlab.pdfbase.ttfonts import TTFont

    # Font ro'yxatdan o'tkazish
    if 'DejaVu' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('DejaVu', FONT_PATH))
        pdfmetrics.registerFont(TTFont('DejaVu-Bold', FONT_PATH))

    # ── 1. Identifikatordan user_id ni aniqlash ──────────────────────
    user_id, id_source = await resolve_identifier_to_uid(userbot, identifier)
    if not user_id:
        return None, id_source

    # ── 2. Ma'lumotlar to'plash ──────────────────────────────────────
    profile, messages  = await lookup_user_by_id(user_id)
    changes            = await db_mod.get_user_change_log(user_id)
    lifecycle          = await get_account_lifecycle(user_id)
    style_data         = await analyze_writing_style(user_id)
    score, slabel, reasons = calculate_trust_score(profile or {}, len(messages))

    # Profil rasmlari
    photo_paths = []
    try:
        from telethon.tl.functions.photos import GetUserPhotosRequest
        photos_result = await userbot(GetUserPhotosRequest(
            user_id=user_id, offset=0, max_id=0, limit=5
        ))
        for i, ph in enumerate(photos_result.photos[:5]):
            p = f"{_TMP}/pdf_photo_{user_id}_{i}.jpg"
            try:
                await userbot.download_media(ph, file=p)
                if os.path.exists(p):
                    photo_paths.append(p)
            except Exception as e:
                _dbg("_valid_img", e)
    except Exception as e:
        _dbg("_valid_img", e)

    # Musiqa xabarlari
    music_msgs = []
    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            async with db.execute(
                """
                SELECT msg_id, source, text, msg_date
                FROM messages_cache
                WHERE sender_id=? AND (
                    text LIKE '%🎵%' OR text LIKE '%🎶%' OR text LIKE '%mp3%'
                    OR text LIKE '%musiqa%' OR text LIKE '%music%' OR text LIKE '%audio%'
                    OR text LIKE '%song%' OR text LIKE '%track%'
                )
                ORDER BY msg_date DESC LIMIT 30
                """,
                (user_id,)
            ) as cur:
                music_msgs = await cur.fetchall()
    except Exception as e:
        _dbg("_valid_img", e)

    # Guruh statistikasi
    group_stats = {}
    for m in messages:
        src = m['source_title'] or m['source']
        group_stats[src] = group_stats.get(src, 0) + 1

    # ── 3. PDF yaratish ──────────────────────────────────────────────
    pdf_path = f"{_TMP}/tergov_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    _now_str  = datetime.now().strftime('%Y-%m-%d %H:%M')

    # ── Sahifa footer (faqat pastki chiziq + qizil matn) ─────────────
    def _draw_page(canv, doc_obj):
        canv.saveState()
        pw, ph = A4
        canv.setStrokeColor(colors.HexColor('#dddddd'))
        canv.setLineWidth(0.5)
        canv.line(15*mm, 14*mm, pw - 15*mm, 14*mm)
        canv.restoreState()

    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        rightMargin=18*mm, leftMargin=18*mm,
        topMargin=18*mm, bottomMargin=22*mm,
        onFirstPage=_draw_page,
        onLaterPages=_draw_page,
    )

    styles = getSampleStyleSheet()

    def S(text, size=10, bold=False, color=colors.black, align='LEFT'):
        style = ParagraphStyle(
            name=f's{size}{bold}{align}',
            fontName='DejaVu',
            fontSize=size,
            textColor=color,
            alignment={'LEFT': 0, 'CENTER': 1, 'RIGHT': 2, 'JUSTIFY': 4}.get(align, 0),
            leading=size * 1.4,
            spaceAfter=2,
        )
        safe = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return Paragraph(safe, style)

    def HR():
        return HRFlowable(width="100%", thickness=0.5, color=colors.grey, spaceAfter=4)

    import re as _re

    _tc_style = ParagraphStyle(
        name='tc',
        fontName='DejaVu',
        fontSize=8,
        leading=10,
        splitLongWords=True,
        wordWrap='LTR',
    )
    _tc_hdr = ParagraphStyle(
        name='tc_hdr',
        fontName='DejaVu',
        fontSize=9,
        leading=11,
        textColor=colors.white,
        splitLongWords=True,
    )

    def TC(text, header=False):
        """Table Cell — matn katakda wrap bo'ladi."""
        safe = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return Paragraph(safe, _tc_hdr if header else _tc_style)

    def _plain(cell):
        """TC yoki string dan oddiy matnni olish."""
        if hasattr(cell, 'text'):
            return _re.sub(r'<[^>]+>', '', cell.text)
        return str(cell) if cell is not None else ''

    def _auto_col_widths(data, total_w):
        """
        Har bir ustun kengligi: o'sha ustundagi eng uzun mazmun (50 belgi)
        asosida o'lchanadi. Katta ustunlar 40% dan oshmasligi ta'minlanadi.
        Jami aniq total_w ga to'g'rilanadi.
        """
        if not data:
            return []
        n = max(len(row) for row in data)
        PAD = 10  # pt — chap/o'ng padding

        natural = []
        for j in range(n):
            col_max = PAD * 2 + 6  # minimal
            for row in data:
                if j < len(row):
                    txt = _plain(row[j])
                    # Birinchi qator (ko'p qatorli matnda sarlavhadan kenglikni olish)
                    first = txt.split('\n')[0][:60]
                    w = pdfmetrics.stringWidth(first, 'DejaVu', 8) + PAD * 2
                    col_max = max(col_max, w)
            natural.append(col_max)

        # Har bir ustun 42% dan ko'p joy olmasin
        cap = total_w * 0.42
        capped = [min(w, cap) for w in natural]

        # Jami kenglikka proportsional moslashtirish
        total = sum(capped)
        scale = total_w / total if total > 0 else 1
        return [w * scale for w in capped]

    def TBL(data, col_widths=None, header_bg=colors.HexColor('#2c3e50')):
        widths = col_widths if col_widths is not None else _auto_col_widths(data, W)
        t = Table(data, colWidths=widths, repeatRows=1)
        style = TableStyle([
            ('FONTNAME',    (0,0), (-1,-1), 'DejaVu'),
            ('FONTSIZE',    (0,0), (-1,-1), 8),
            ('BACKGROUND',  (0,0), (-1,0),  header_bg),
            ('TEXTCOLOR',   (0,0), (-1,0),  colors.white),
            ('FONTSIZE',    (0,0), (-1,0),  9),
            ('ALIGN',       (0,0), (-1,-1), 'LEFT'),
            ('VALIGN',      (0,0), (-1,-1), 'TOP'),
            ('GRID',        (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1),
             [colors.HexColor('#f8f9fa'), colors.white]),
            ('TOPPADDING',  (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING', (0,0), (-1,-1), 4),
            ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ])
        t.setStyle(style)
        return t

    story = []
    W = 174*mm  # usable width (A4 - 2*18mm margins)

    # ── MUQOVA ───────────────────────────────────────────────────────
    story.append(Spacer(1, 10*mm))
    # Profil rasmi (agar bor bo'lsa)
    if photo_paths and _valid_img(photo_paths[0]):
        try:
            img = Image(photo_paths[0], width=40*mm, height=40*mm)
            img.hAlign = 'CENTER'
            story.append(img)
            story.append(Spacer(1, 3*mm))
        except Exception as e:
            _dbg("TBL", e)

    story.append(S("HISOBOT", size=22, align='CENTER', color=colors.HexColor('#2c3e50')))
    story.append(Spacer(1, 3*mm))
    story.append(HR())

    full_name = ""
    if profile:
        full_name = (profile['first_name'] + " " + profile['last_name']).strip() or "Nomsiz"
    story.append(S(f"Shaxs: {full_name or identifier}", size=16, align='CENTER', color=colors.HexColor('#e74c3c')))
    story.append(S(f"ID: {user_id}  |  {id_source}", size=10, align='CENTER', color=colors.HexColor('#555')))
    story.append(S(f"Sana: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
                   f"Trust Score: {score}/100 — {slabel}", size=10, align='CENTER'))
    story.append(HR())
    story.append(Spacer(1, 5*mm))

    # ── 1. PROFIL MA'LUMOTI ───────────────────────────────────────────
    story.append(S("1. PROFIL MA'LUMOTI", size=13, color=colors.HexColor('#2c3e50')))
    story.append(HR())
    if profile:
        data = [[TC("Maydon", header=True), TC("Qiymat", header=True)]]
        rows = [
            (TC("To'liq ism"),          TC(full_name)),
            (TC("Username"),            TC(f"@{profile['username']}" if profile['username'] else "—")),
            (TC("Telefon"),             TC(profile['phone'] or "Yopiq")),
            (TC("Bio"),                 TC(profile['bio'][:200] if profile['bio'] else "—")),
            (TC("Ochiq kanal"),         TC(profile['open_channels'] or "—")),
            (TC("Maxfiy kanal"),        TC(profile['has_hidden'] if profile['has_hidden'] not in ('', '❌') else "Yo'q")),
            (TC("Birinchi ko'rilgan"),  TC(profile['added_date'] or "—")),
            (TC("So'nggi yangilangan"), TC(profile['last_updated'] or "—")),
            (TC("A'zo guruhlar"),       TC(f"{len(profile['groups'])} ta")),
            (TC("Yozilgan xabarlar"),   TC(f"{len(messages)} ta")),
        ]
        data.extend(rows)
        story.append(TBL(data))
    else:
        story.append(S("Profil bazada topilmadi.", color=colors.red))
    story.append(Spacer(1, 5*mm))

    # ── 2. TRUST SCORE ────────────────────────────────────────────────
    story.append(S("2. TRUST SCORE (ISHONCHLILIK BALI)", size=13, color=colors.HexColor('#2c3e50')))
    story.append(HR())
    score_color = colors.HexColor('#27ae60') if score >= 70 else (
        colors.HexColor('#f39c12') if score >= 40 else colors.HexColor('#e74c3c'))
    story.append(S(f"Ball: {score}/100 — {slabel}", size=14, color=score_color))
    if reasons:
        for r in reasons:
            story.append(S(f"  • {r}", size=9, color=colors.HexColor('#555')))
    story.append(Spacer(1, 5*mm))

    # ── 3. A'ZO GURUHLAR / KANALLAR ───────────────────────────────────
    if profile and profile['groups']:
        story.append(S("3. A'ZO GURUHLAR / KANALLAR", size=13, color=colors.HexColor('#2c3e50')))
        story.append(HR())
        story.append(S(f"Jami: {len(profile['groups'])} ta"))
        data = [[TC("#", header=True), TC("Guruh / Kanal", header=True), TC("Xabarlar soni", header=True)]]
        for i, g in enumerate(profile['groups'], 1):
            cnt = group_stats.get(g.split('t.me/')[-1].rstrip('/'), 0)
            if cnt == 0:
                cnt = group_stats.get(g, 0)
            data.append([TC(str(i)), TC(g), TC(str(cnt) if cnt else "—")])
        story.append(TBL(data))
        story.append(Spacer(1, 5*mm))

    # ── 4. XABAR STATISTIKASI ─────────────────────────────────────────
    story.append(S("4. XABAR STATISTIKASI", size=13, color=colors.HexColor('#2c3e50')))
    story.append(HR())
    if lifecycle['monthly']:
        story.append(S(f"Birinchi xabar: {lifecycle['first_msg'] or '—'}"))
        story.append(S(f"Oxirgi xabar:  {lifecycle['last_msg'] or '—'}"))
        if lifecycle['peak_month']:
            story.append(S(f"Eng faol oy:   {lifecycle['peak_month']}"))
        if lifecycle['peak_hour'] is not None:
            story.append(S(f"Eng faol soat: {lifecycle['peak_hour']}:00"))
        story.append(Spacer(1, 3*mm))
        # Oylik grafik (matn ko'rinishida)
        data = [[TC("Oy", header=True), TC("Xabarlar", header=True), TC("Grafik", header=True)]]
        for m in lifecycle['monthly']:
            bar = "█" * min(m['count'] // max(1, max(x['count'] for x in lifecycle['monthly']) // 20), 20)
            data.append([TC(m['month']), TC(str(m['count'])), TC(bar)])
        story.append(TBL(data))
    else:
        story.append(S("Kesh da xabar statistikasi yo'q."))
    story.append(Spacer(1, 5*mm))

    # ── 5. YOZUV USLUBI ───────────────────────────────────────────────
    if style_data:
        story.append(S("5. YOZUV USLUBI TAHLILI", size=13, color=colors.HexColor('#2c3e50')))
        story.append(HR())
        data = [[TC("Ko'rsatkich", header=True), TC("Qiymat", header=True)]]
        data.extend([
            (TC("Tahlil qilingan xabarlar"), TC(str(style_data['msg_count']))),
            (TC("O'rtacha uzunlik (belgi)"), TC(str(style_data['avg_len']))),
            (TC("O'rtacha so'zlar soni"),   TC(str(style_data['avg_words']))),
            (TC("Emoji / xabar"),           TC(str(style_data['emoji_ratio']))),
            (TC("Savol xabarlari (%)"),     TC(str(style_data['question_pct']))),
            (TC("Undov xabarlari (%)"),     TC(str(style_data['exclaim_pct']))),
            (TC("Katta harf (%)"),          TC(str(style_data['caps_ratio']))),
            (TC("Top so'zlar"),             TC(", ".join(f"{w}({c})" for w, c in style_data['top_words'][:8]))),
        ])
        story.append(TBL(data))
        story.append(Spacer(1, 5*mm))

    # ── 6. O'ZGARISHLAR TARIXI ────────────────────────────────────────
    if changes:
        story.append(S("6. O'ZGARISHLAR TARIXI", size=13, color=colors.HexColor('#2c3e50')))
        story.append(HR())
        field_names = {'first_name': 'Ism', 'last_name': 'Familiya',
                       'username': 'Username', 'phone': 'Telefon', 'bio': 'Bio'}
        data = [[TC("Sana", header=True), TC("Maydon", header=True),
                 TC("Eski", header=True), TC("Yangi", header=True)]]
        for ch in changes[:30]:
            data.append([TC(ch[3][:16]), TC(field_names.get(ch[0], ch[0])),
                         TC((ch[1] or '—')[:50]), TC((ch[2] or '—')[:50])])
        story.append(TBL(data))
        story.append(Spacer(1, 5*mm))

    # ── 7. MUSIQA XABARLARI ───────────────────────────────────────────
    if music_msgs:
        story.append(S("7. MUSIQA / MEDIA XABARLARI", size=13, color=colors.HexColor('#2c3e50')))
        story.append(HR())
        data = [[TC("Sana", header=True), TC("Manba", header=True), TC("Xabar", header=True)]]
        for mm_row in music_msgs[:20]:
            src_title = (mm_row[1] or '').split('t.me/')[-1].rstrip('/')[:30]
            data.append([
                TC((mm_row[3] or '')[:16]),
                TC(src_title),
                TC((mm_row[2] or '')[:100])
            ])
        story.append(TBL(data))
        story.append(Spacer(1, 5*mm))

    # ── 8. BARCHA XABARLAR ────────────────────────────────────────────
    story.append(PageBreak())
    story.append(S("8. BARCHA YOZILGAN XABARLAR", size=13, color=colors.HexColor('#2c3e50')))
    story.append(HR())
    if messages:
        story.append(S(f"Jami: {len(messages)} ta xabar — barchasi qo'shilgan"))
        story.append(Spacer(1, 3*mm))
        data = [[TC("#", header=True), TC("Sana", header=True), TC("Manba", header=True),
                 TC("Xabar matni", header=True), TC("Havola", header=True)]]
        for i, m in enumerate(messages, 1):
            link_val = m.get('link') or '—'
            if link_val != '—' and len(link_val) > 45:
                link_val = link_val[:45] + '…'
            data.append([
                TC(str(i)),
                TC((m['date'] or '')[:16]),
                TC((m['source_title'] or '')[:30]),
                TC((m['text'] or '')[:120]),
                TC(link_val),
            ])
        # Jami: 7+25+32+72+38 = 174mm
        story.append(TBL(data))
    else:
        story.append(S("Bu foydalanuvchi skanerlangan guruhlarda matnli xabar yozmagan yoki xabar tarixi mavjud emas."))
    story.append(Spacer(1, 5*mm))

    # ── 9. PROFIL RASMLARI ────────────────────────────────────────────
    if photo_paths:
        story.append(PageBreak())
        story.append(S("9. PROFIL RASMLARI", size=13, color=colors.HexColor('#2c3e50')))
        story.append(HR())
        story.append(S(f"Topilgan rasmlar: {len(photo_paths)} ta"))
        story.append(Spacer(1, 5*mm))
        # Rasmlarni 2 ustun qilib joylash
        img_row = []
        for i, ph in enumerate(photo_paths):
            if not _valid_img(ph):
                continue
            try:
                img = Image(ph, width=80*mm, height=80*mm)
                img_row.append(img)
                if len(img_row) == 2:
                    t = Table([img_row], colWidths=[90*mm, 90*mm])
                    story.append(t)
                    story.append(Spacer(1, 3*mm))
                    img_row = []
            except Exception as e:
                _dbg("TBL", e)
        if img_row:
            t = Table([img_row + [""]], colWidths=[90*mm, 90*mm])
            story.append(t)

    # ── 10. YAKUNIY XULOSA ────────────────────────────────────────────
    story.append(PageBreak())
    story.append(S("10. YAKUNIY XULOSA", size=13, color=colors.HexColor('#2c3e50')))
    story.append(HR())
    data = [[TC("Ko'rsatkich", header=True), TC("Qiymat", header=True)]]
    data.extend([
        (TC("Tekshirilgan shaxs"),      TC(full_name or identifier)),
        (TC("Telegram ID"),             TC(str(user_id))),
        (TC("Trust Score"),             TC(f"{score}/100 — {slabel}")),
        (TC("Jami guruhlar"),           TC(f"{len(profile['groups']) if profile else 0} ta")),
        (TC("Jami xabarlar (keshda)"),  TC(f"{len(messages)} ta")),
        (TC("O'zgarishlar soni"),       TC(f"{len(changes)} ta")),
        (TC("Musiqa xabarlari"),        TC(f"{len(music_msgs)} ta")),
        (TC("Profil rasmlari"),         TC(f"{len(photo_paths)} ta")),
        (TC("Hisobot yaratildi"),       TC(datetime.now().strftime("%Y-%m-%d %H:%M"))),
    ])
    story.append(TBL(data))
    story.append(Spacer(1, 10*mm))
    story.append(HRFlowable(width="100%", thickness=1,
                             color=colors.HexColor('#bbbbbb'),
                             spaceAfter=6, spaceBefore=4))
    # PDF ni saqlash
    doc.build(story)

    # Vaqtinchalik rasmlarni tozalash
    for ph in photo_paths:
        try:
            os.remove(ph)
        except Exception as e:
            _dbg("TBL", e)

    return pdf_path, f"{full_name or identifier} — ID {user_id}"


# ─────────────────────────────────────────────────────────────────────
# EXCEL BATCH SKANERLASH
# ─────────────────────────────────────────────────────────────────────

def _is_invite_link(ch: str) -> bool:
    """t.me/+hash yoki t.me/joinchat/hash formatini aniqlaydi."""
    return '/+' in ch or 'joinchat/' in ch


def _extract_invite_hash(ch: str) -> str:
    """Invite linkdan hash qismini ajratib oladi."""
    if '/+' in ch:
        return ch.split('/+')[-1].rstrip('/').split('?')[0]
    if 'joinchat/' in ch:
        return ch.split('joinchat/')[-1].rstrip('/').split('?')[0]
    return ch


def _normalize_channel_link(ch: str) -> str:
    """
    t.me/c/NUMERIC_ID/MSG_ID  →  -100NUMERIC_ID
    t.me/c/NUMERIC_ID          →  -100NUMERIC_ID
    Boshqa formatlar o'zgarmaydi.
    """
    ch = ch.strip()
    # https://t.me/c/1234567890/5  yoki  t.me/c/1234567890
    if 't.me/c/' in ch:
        after = ch.split('t.me/c/')[-1].rstrip('/')
        numeric_id = after.split('/')[0].split('?')[0]
        if numeric_id.isdigit():
            return f"-100{numeric_id}"
    return ch


async def _excel_join_channel(userbot, ch: str):
    """
    Kanalga qo'shiladi va (entity, scan_target) juftini qaytaradi.
    scan_target — scan_channel_comments ga beriladigan identifikator.
    Muvaffaqiyatsiz bo'lsa — exception chiqaradi.
    """
    ch = _normalize_channel_link(ch)
    is_invite = _is_invite_link(ch)

    # 1. Avval oddiy entity resolve — ko'pincha a'zo bo'lgan kanallar shunday topiladi
    try:
        entity = await userbot.get_entity(ch)
        ch_id  = getattr(entity, 'id', None)
        scan_t = f"-100{ch_id}" if ch_id else ch
        return entity, scan_t
    except Exception as e:
        _dbg("_excel_join_channel", e)

    # 2. Invite link: ImportChatInviteRequest
    if is_invite:
        hash_part = _extract_invite_hash(ch)
        try:
            updates = await userbot(ImportChatInviteRequest(hash=hash_part))
            # Yangi qo'shilgan chat updates.chats[0] ichida
            if getattr(updates, 'chats', None):
                entity = updates.chats[0]
                ch_id  = getattr(entity, 'id', None)
                scan_t = f"-100{ch_id}" if ch_id else ch
                return entity, scan_t
            # updates.chat_invite — faqat ma'lumot, a'zo qilinmagan (so'rov yuborildi)
            # Shu holda entity yo'q, scan qilolmaymiz — lekin exception chiqarmaymiz
            raise Exception("So'rov yuborildi, kanal ochilishini kuting")
        except Exception as inv_e:
            err_l = str(inv_e).lower()
            if 'already' in err_l or 'participant' in err_l:
                # A'zo bo'lib, invite link orqali resolve ish bermadi.
                # Invite hash dan kanal ID ni aniqlab bo'lmaydi — 300 ta dialogni
                # yuklab o'tirish behuda edi (olib tashlandi).
                raise Exception("Allaqachon a'zo, lekin kanal ID aniqlanmadi — @username yoki ID yuboring")
            raise inv_e

    # 3. Ochiq kanal / guruh: JoinChannelRequest
    await userbot(JoinChannelRequest(ch))
    await asyncio.sleep(5)
    entity = await userbot.get_entity(ch)
    ch_id  = getattr(entity, 'id', None)
    scan_t = f"-100{ch_id}" if ch_id else ch
    return entity, scan_t


# ─────────────────────────────────────────────────────────────────────
# BAZADAGI ESKI t.me/c/ID/1 LINKLARNI @USERNAME GA O'GIRISH
# ─────────────────────────────────────────────────────────────────────

_PC_RESOLVE_CACHE: dict = {}   # ch_id (int) → resolved link (str)
_PC_LINK_RE = re.compile(r'https?://t\.me/c/(\d+)(?:/\d+)?')


async def _resolve_one_pc_id(userbot, ch_id: int, original: str) -> str:
    """ch_id ni username ga aylantiradi. Cache ishlatadi."""
    if ch_id in _PC_RESOLVE_CACHE:
        return _PC_RESOLVE_CACHE[ch_id]
    try:
        ent   = await asyncio.wait_for(userbot.get_entity(ch_id), timeout=8)
        uname = getattr(ent, 'username', None)
        link  = f"https://t.me/{uname}" if uname else original
    except Exception:
        link = original
    _PC_RESOLVE_CACHE[ch_id] = link
    return link


async def migrate_pc_links(userbot, bot=None, admin_id=None) -> tuple:
    """
    users_memory_bank.has_hidden va open_channels ustunlaridagi
    https://t.me/c/NUMERIC_ID/1  →  https://t.me/username
    formatiga o'tkazadi.
    Bir marta ishlatiladigan migratsiya funksiyasi.
    Qaytaradi: (updated_rows, total_rows, unique_resolved)
    """
    # 1. Barcha t.me/c/ bo'lgan qatorlarni olish
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT user_id, group_link, has_hidden, open_channels "
            "FROM users_memory_bank "
            "WHERE has_hidden LIKE '%t.me/c/%' OR open_channels LIKE '%t.me/c/%'"
        ) as cur:
            rows = await cur.fetchall()

    total   = len(rows)
    updated = 0
    unique_resolved = 0

    for idx, (uid, grp, has_hidden, open_channels) in enumerate(rows):

        # ── has_hidden ───────────────────────────────────────────────
        new_has_hidden = has_hidden or ""
        changed = False
        for m in _PC_LINK_RE.finditer(has_hidden or ""):
            ch_id    = int(m.group(1))
            old_link = m.group(0)
            if ch_id not in _PC_RESOLVE_CACHE:
                unique_resolved += 1
                await asyncio.sleep(0.35)  # flood oldini olish
            new_link = await _resolve_one_pc_id(userbot, ch_id, old_link)
            if new_link != old_link:
                new_has_hidden = new_has_hidden.replace(old_link, new_link)
                changed = True

        # ── open_channels ────────────────────────────────────────────
        new_open = open_channels or ""
        for m in _PC_LINK_RE.finditer(open_channels or ""):
            ch_id    = int(m.group(1))
            old_link = m.group(0)
            if ch_id not in _PC_RESOLVE_CACHE:
                unique_resolved += 1
                await asyncio.sleep(0.35)
            new_link = await _resolve_one_pc_id(userbot, ch_id, old_link)
            if new_link != old_link:
                new_open = new_open.replace(old_link, new_link)
                changed = True

        if changed:
            async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                await db.execute(
                    "UPDATE users_memory_bank "
                    "SET has_hidden=?, open_channels=? "
                    "WHERE user_id=? AND group_link=?",
                    (new_has_hidden, new_open, uid, grp)
                )
                await db.commit()
            updated += 1

        # Progress xabari har 100 qatorda
        if bot and admin_id and (idx + 1) % 100 == 0:
            try:
                await bot.send_message(
                    admin_id,
                    f"🔄 Migratsiya: `{idx + 1}/{total}` qator tekshirildi | "
                    f"✅ O'zgartirildi: `{updated}` ta"
                )
            except Exception as e:
                _dbg("migrate_pc_links", e)

    # music_channel_progress da ham eski linklarni yangilash
    progress_updated = 0
    try:
        async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
            async with db.execute(
                "SELECT source FROM music_channel_progress "
                "WHERE source LIKE '%t.me/c/%'"
            ) as cur:
                old_sources = await cur.fetchall()
        for (old_src,) in old_sources:
            m = _PC_LINK_RE.search(old_src)
            if m:
                ch_id    = int(m.group(1))
                new_src  = _PC_RESOLVE_CACHE.get(ch_id)
                if new_src and new_src != old_src:
                    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
                        # Yangi source allaqachon bor bo'lsa — eskisini o'chirish
                        async with db.execute(
                            "SELECT 1 FROM music_channel_progress WHERE source=?",
                            (new_src,)
                        ) as cur2:
                            exists = await cur2.fetchone()
                        if exists:
                            await db.execute(
                                "DELETE FROM music_channel_progress WHERE source=?",
                                (old_src,)
                            )
                        else:
                            await db.execute(
                                "UPDATE music_channel_progress SET source=? WHERE source=?",
                                (new_src, old_src)
                            )
                        await db.commit()
                    progress_updated += 1
    except Exception as e:
        _dbg("migrate_pc_links", e)

    return updated, total, unique_resolved, progress_updated


async def excel_batch_scanner(userbot, channels: list, bot, admin_id: int,
                               userbot_idx: int = 0):
    """
    Excel fayldan olingan kanallar ro'yxatini ketma-ket skanerleydi.
    Qo'llab-quvvatlanadigan format:
      @username | t.me/username | t.me/+invite_hash | t.me/joinchat/hash | -100ID
    Har kanal orasida 15 daqiqa to'xtaydi.
    Kanalda discussion guruh (kamentariya) bo'lsa — foydalanuvchilar skanerlanadi.
    """
    ub_label = f"Userbot{userbot_idx + 1}"
    total    = len(channels)
    done = skipped = errors = 0

    for i, ch in enumerate(channels, 1):
        ch = str(ch).strip()
        if not ch or ch.lower() in ('none', 'nan', ''):
            continue

        tag = f"🤖 **{ub_label}** `[{i}/{total}]`\n🔗 `{ch}`"

        try:
            # 1. Kanalga kirish va entity olish
            entity    = None
            scan_tgt  = ch
            joined_ok = False

            try:
                entity, scan_tgt = await _excel_join_channel(userbot, ch)
                joined_ok = True
            except Exception as je:
                err_msg = str(je)
                if 'So\'rov yuborildi' in err_msg or 'invite' in err_msg.lower():
                    await bot.send_message(
                        admin_id,
                        f"📨 {tag}\nYopiq kanal — qo'shilish so'rovi yuborildi\n"
                        f"Kanal ochilsa avtomatik skanerlanydi (knock tizimi orqali)"
                    )
                else:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ {tag}\nKirish imkonsiz: `{err_msg[:100]}` → o'tkazib yuborildi"
                    )
                skipped += 1
                if i < total:
                    next_ch = str(channels[i]).strip() if i < len(channels) else "—"
                    await bot.send_message(
                        admin_id,
                        f"⏳ **{ub_label}** | Keyingi: `{next_ch}`\n15 daqiqa... ({i}/{total})"
                    )
                    await asyncio.sleep(15 * 60)
                continue

            if entity is None or not joined_ok:
                skipped += 1
                if i < total:
                    await asyncio.sleep(15 * 60)
                continue

            ch_title = getattr(entity, 'title', ch)

            # 2. Skanerlash — scan_channel_comments o'zi discussion guruh borligini tekshiradi
            ch_clean = re.sub(r'[^\w]', '_', ch)[:30]
            fpath    = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                f"excelbatch_{admin_id}_ub{userbot_idx}_{i}_{ch_clean}.xlsx"
            )

            await bot.send_message(
                admin_id,
                f"🔍 {tag}\n**{ch_title}** skanerlanyapti..."
            )

            try:
                count, ch_title_r = await scan_channel_comments(
                    userbot, scan_tgt, fpath, status_msg=None
                )
                done += 1
                txt = (
                    f"✅ {tag}\n**{ch_title_r}**: `{count}` ta profil yozildi"
                    if count > 0 else
                    f"📭 {tag}\n**{ch_title_r}**: kanalda hech kim comment yozmagan"
                )
                await bot.send_message(admin_id, txt)
                if os.path.exists(fpath) and count > 0:
                    try:
                        await bot.send_file(
                            admin_id, fpath,
                            caption=f"📊 {ch_title_r} — Excel batch scan"
                        )
                    except Exception as e:
                        _dbg("excel_batch_scanner", e)

            except Exception as se:
                err_msg = str(se)
                if "discussion guruh" in err_msg or "comment bo'limi" in err_msg:
                    await bot.send_message(
                        admin_id,
                        f"📭 {tag}\n**{ch_title}** — kamentariya bo'limi yo'q → o'tkazib yuborildi"
                    )
                    skipped += 1
                else:
                    await bot.send_message(
                        admin_id,
                        f"❌ {tag}\nSkanerlashda xatolik: `{err_msg[:120]}`"
                    )
                    errors += 1

        except FloodWaitError as fw:
            await bot.send_message(
                admin_id,
                f"⏳ **{ub_label}** FloodWait: `{fw.seconds}` soniya kutilmoqda..."
            )
            await asyncio.sleep(fw.seconds + 60)
            continue
        except Exception as e:
            await bot.send_message(
                admin_id,
                f"❌ {tag}\nXatolik: `{type(e).__name__}: {str(e)[:100]}`"
            )
            errors += 1

        # Keyingi kanal oldidan 15 daqiqa kutish (oxirgi kanaldan keyin kutmaymiz)
        if i < total:
            next_ch = str(channels[i]).strip() if i < len(channels) else "—"
            await bot.send_message(
                admin_id,
                f"⏳ **{ub_label}** | Keyingi: `{next_ch}`\n"
                f"15 daqiqa kutilmoqda... ({i}/{total})"
            )
            await asyncio.sleep(15 * 60)

    await bot.send_message(
        admin_id,
        f"🏁 **{ub_label}** — Barcha **{total}** ta kanal ko'rib chiqildi!\n"
        f"✅ Skanerlandi: `{done}` | 📭 O'tkazildi: `{skipped}` | ❌ Xato: `{errors}`"
    )
