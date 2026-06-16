# music_scanner.py — Kanal musiqa skanerlash va taqqoslash
import os
import asyncio
import random
import aiosqlite
import database as db_mod
from datetime import datetime
from collections import defaultdict

# Numpy optimallashtirish — o'rnatilgan bo'lsa 100x tezroq taqqoslash
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    print("[MUSIQA] numpy topilmadi — Python rejimida ishlaydi (sekinroq). "
          "'pip install numpy' bilan o'rnating.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUSIC_DB  = os.path.join(BASE_DIR, "music_fingerprints.db")

_SCANNING_LOCK = asyncio.Lock()   # coroutine-safe skanerlash holati
SCANNING = False                  # tashqi ko'rish uchun (faqat o'qish)
_DB_INITIALIZED = False


# ─────────────────────────────────────────────────────────────────────
# MUSIQA BAZASI
# ─────────────────────────────────────────────────────────────────────

async def init_music_db():
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    _DB_INITIALIZED = True
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA cache_size=-32000")
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS music_fingerprints (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  TEXT,
                channel_name TEXT,
                file_name   TEXT,
                fingerprint TEXT,
                duration    REAL,
                added_date  TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scanned_channels (
                channel_id  TEXT PRIMARY KEY,
                scanned_at  TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watch_fingerprints (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                fingerprint TEXT,
                duration    REAL,
                admin_id    INTEGER,
                added_date  TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watch_alerts_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_name  TEXT,
                source_name TEXT,
                source_id   TEXT,
                source_type TEXT,
                score       REAL,
                found_date  TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_mfp_channel ON music_fingerprints(channel_id)"
        )
        await db.commit()


async def save_fingerprint(channel_id, channel_name, file_name, fingerprint, duration):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        async with db_mod.connect(MUSIC_DB, timeout=30) as db:
            await db.execute(
                "INSERT INTO music_fingerprints "
                "(channel_id, channel_name, file_name, fingerprint, duration, added_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (channel_id, channel_name, file_name, fingerprint, duration, now)
            )
            await db.commit()
    except Exception as e:
        print(f"save_fingerprint xatosi: {e}")


async def mark_channel_scanned(channel_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        await db.execute(
            "INSERT OR REPLACE INTO scanned_channels (channel_id, scanned_at) VALUES (?, ?)",
            (str(channel_id), now)
        )
        await db.commit()


async def is_channel_scanned(channel_id):
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        async with db.execute(
            "SELECT 1 FROM scanned_channels WHERE channel_id=?", (str(channel_id),)
        ) as cur:
            return await cur.fetchone() is not None


async def get_stats():
    await init_music_db()
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        total_fp    = (await (await db.execute("SELECT COUNT(*) FROM music_fingerprints")).fetchone())[0]
        total_ch    = (await (await db.execute("SELECT COUNT(DISTINCT channel_id) FROM music_fingerprints")).fetchone())[0]
    return total_fp, total_ch


# ─────────────────────────────────────────────────────────────────────
# FINGERPRINT OLISH
# ─────────────────────────────────────────────────────────────────────

async def get_fingerprint_async(audio_path):
    """Async wrapper — event loopni bloklamaydi (thread poolda ishlaydi)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_fingerprint, audio_path)


def get_fingerprint(audio_path):
    """
    Audio fayldan fingerprint oladi.
    chromaprint (fpcalc) yordamida.
    """
    import subprocess
    import sys

    # fpcalc ni bot papkasidan qidirish
    fpcalc_path = os.path.join(BASE_DIR, "fpcalc.exe")
    if not os.path.exists(fpcalc_path):
        fpcalc_path = os.path.join(BASE_DIR, "fpcalc")
    if not os.path.exists(fpcalc_path):
        fpcalc_path = "fpcalc"  # PATH dan qidirish

    try:
        result = subprocess.run(
            [fpcalc_path, "-raw", "-length", "0", audio_path],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return None, None
        fp       = None
        duration = None
        for line in result.stdout.strip().split('\n'):
            if line.startswith("FINGERPRINT="):
                fp = line.split("=", 1)[1]
            elif line.startswith("DURATION="):
                duration = float(line.split("=", 1)[1])
        return fp, duration
    except Exception as e:
        print(f"Fingerprint xatosi: {e}")
        return None, None


def compare_fingerprints(fp1, fp2):
    """
    AcoustID XOR bit taqqoslash.
    Numpy mavjud bo'lsa 30-70x tezroq ishlaydi.
    fp1, fp2: comma-separated string YOKI pre-parsed array.
    """
    try:
        arr1 = parse_fingerprint(fp1) if isinstance(fp1, str) else fp1
        arr2 = parse_fingerprint(fp2) if isinstance(fp2, str) else fp2
        return compare_fp_arrays(arr1, arr2)
    except Exception:
        return 0.0


def parse_fingerprint(fp_str):
    """
    Fingerprint string → array.
    Numpy mavjud bo'lsa uint32 ndarray, aks holda Python list.
    Bu funksiya bir marta chaqiriladi — taqqoslashda qayta parse QILINMAYDI.
    """
    if _HAS_NUMPY:
        # np.fromstring(sep=...) eskirgan (NumPy 2.x da olib tashlanmoqda).
        # Buning o'rniga to'g'ridan-to'g'ri massivga aylantiramiz.
        if not fp_str:
            return np.empty(0, dtype=np.uint32)
        return np.array(fp_str.split(','), dtype=np.int64).astype(np.uint32)
    return list(map(int, fp_str.split(',')))


def compare_fp_arrays(arr1, arr2):
    """
    Pre-parsed arraylarni tezkor XOR taqqoslash.
    Numpy: ~2-5μs, Python: ~139μs (30-70x farq).
    """
    if _HAS_NUMPY and isinstance(arr1, np.ndarray):
        min_len = min(len(arr1), len(arr2))
        if min_len == 0:
            return 0.0
        a = arr1[:min_len]
        b = arr2[:min_len]
        xor = np.bitwise_xor(a, b)
        # uint32 → uint8 (4 bayt per element) → bitlarni sanash
        diff_bits = int(np.unpackbits(xor.view(np.uint8)).sum())
        total_bits = min_len * 32
        return (total_bits - diff_bits) / total_bits
    else:
        # Python fallback
        return _compare_nums_fast(arr1, arr2)


def compare_fingerprints_sliding(fp1, fp2, window=100):
    """
    Sliding window taqqoslash — kesilgan/offset qo'shiqlarni ham topadi.
    fp1: qidirilayotgan musiqa (query)
    fp2: bazadagi musiqa (database)
    """
    try:
        nums1 = list(map(int, fp1.split(',')))
        nums2 = list(map(int, fp2.split(',')))
        if not nums1 or not nums2:
            return 0.0
        best_score = 0.0
        len1, len2 = len(nums1), len(nums2)
        win = min(window, len1, len2)
        step = max(1, win // 2)
        for off2 in range(0, len2 - win + 1, step):
            chunk2 = nums2[off2:off2 + win]
            for off1 in range(0, min(len1, win * 2) - win + 1, step):
                chunk1 = nums1[off1:off1 + win]
                if len(chunk1) < win or len(chunk2) < win:
                    continue
                if _HAS_NUMPY:
                    a = np.array(chunk1, dtype=np.uint32)
                    b = np.array(chunk2, dtype=np.uint32)
                    xor = np.bitwise_xor(a, b)
                    diff_bits = int(np.unpackbits(xor.view(np.uint8)).sum())
                    total_bits = win * 32
                    score = (total_bits - diff_bits) / total_bits
                else:
                    total_bits = 0
                    matching_bits = 0
                    for a, b in zip(chunk1, chunk2):
                        xor = a ^ b
                        diff = bin(xor & 0xFFFFFFFF).count('1')
                        total_bits += 32
                        matching_bits += (32 - diff)
                    score = matching_bits / total_bits if total_bits > 0 else 0.0
                if score > best_score:
                    best_score = score
                    if best_score >= 0.95:
                        return best_score
        return best_score
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────
# BARCHA MANBALARDAN KANAL/GURUH LISTINI OLISH
# ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# LSH INDEKS (tez qidirish uchun)
# ─────────────────────────────────────────────────────────────────────

# Band-based LSH: fingerprint 10 ta bandga (har biri 32 bit) bo'linadi
# Har bir band hash → kandidatlar ro'yxati
_LSH_BANDS = 10
_LSH_BAND_WIDTH = 32  # bits per band = 1 uint32 element

_lsh_index: "defaultdict[tuple, list]" = defaultdict(list)


def build_lsh_index(fps_parsed_list):
    """
    LSH indeksini quriladi.
    fps_parsed_list: [(parsed_array, metadata), ...]
      metadata — ixtiyoriy, candidate ro'yxatida qaytariladi.
    Band-based LSH: B=10 band, har band W=32 bit (1 uint32 element).
    Fingerprint uzunligi yetarli bo'lmasa mavjud elementlar ishlatiladi.
    """
    global _lsh_index
    _lsh_index = defaultdict(list)
    for parsed_arr, metadata in fps_parsed_list:
        if _HAS_NUMPY:
            arr = parsed_arr if isinstance(parsed_arr, np.ndarray) else np.array(parsed_arr, dtype=np.uint32)
            length = len(arr)
        else:
            arr = parsed_arr
            length = len(arr)
        for band_idx in range(_LSH_BANDS):
            start = band_idx * _LSH_BAND_WIDTH
            if start >= length:
                break
            end = min(start + _LSH_BAND_WIDTH, length)
            if _HAS_NUMPY:
                band_val = tuple(arr[start:end].tolist())
            else:
                band_val = tuple(arr[start:end])
            key = (band_idx, band_val)
            _lsh_index[key].append((parsed_arr, metadata))


def lsh_candidates(query_fp):
    """
    Query fingerprint uchun LSH kandidatlarini qaytaradi.
    query_fp: parsed array (np.ndarray yoki list).
    Qaytaradi: (parsed_arr, metadata) juftliklari set'i (indeks bo'yicha).
    """
    if _HAS_NUMPY:
        arr = query_fp if isinstance(query_fp, np.ndarray) else np.array(query_fp, dtype=np.uint32)
        length = len(arr)
    else:
        arr = query_fp
        length = len(arr)

    seen_ids = set()
    candidates = []
    for band_idx in range(_LSH_BANDS):
        start = band_idx * _LSH_BAND_WIDTH
        if start >= length:
            break
        end = min(start + _LSH_BAND_WIDTH, length)
        if _HAS_NUMPY:
            band_val = tuple(arr[start:end].tolist())
        else:
            band_val = tuple(arr[start:end])
        key = (band_idx, band_val)
        for item in _lsh_index.get(key, []):
            item_id = id(item[0])
            if item_id not in seen_ids:
                seen_ids.add(item_id)
                candidates.append(item)
    return candidates


def _compare_nums_fast(nums1, nums2):
    """Pre-parsed ro'yxatlarni tezkor XOR taqqoslash."""
    min_len = min(len(nums1), len(nums2))
    if min_len == 0:
        return 0.0
    total_bits = 0
    matching_bits = 0
    for a, b in zip(nums1[:min_len], nums2[:min_len]):
        xor = a ^ b
        diff_bits = bin(xor & 0xFFFFFFFF).count('1')
        total_bits += 32
        matching_bits += (32 - diff_bits)
    return matching_bits / total_bits if total_bits > 0 else 0.0


def batch_compare_against_watches(watch_fps_parsed, all_fps_raw, threshold=0.65):
    """
    Barcha fingerprintlarni kuzatiladigan musiqalar bilan BITTA thread'da taqqoslaydi.
    watch_fps_parsed: [(w_id, w_name, parsed_array), ...]
    all_fps_raw:      [(ch_id, ch_name, fname, fp_str), ...]
    Qaytaradi: [(ch_id, ch_name, fname, w_id, w_name, score_pct), ...]

    Numpy: 14912×15 = 223,680 taqqoslash ~1-3 soniyada (thread'da, bot muzlamaydi).
    Python fallback: ~30 soniya.
    LSH indeks: 1M fingerprintda ham 1000 kabi tez ishlaydi.
    """
    results = []
    # DB fingerprintlarini BIR MARTA parse qilish (numpy yoki list)
    db_parsed = []
    for ch_id, ch_name, fname, fp_str in all_fps_raw:
        try:
            arr = parse_fingerprint(fp_str)
            db_parsed.append((ch_id, ch_name, fname, arr))
        except Exception:
            continue

    # LSH indeksini DB fingerprintlari bo'yicha quriladi
    build_lsh_index([(arr, (ch_id, ch_name, fname)) for ch_id, ch_name, fname, arr in db_parsed])

    for w_id, w_name, arr1 in watch_fps_parsed:
        # LSH orqali kandidatlar olish
        candidates = lsh_candidates(arr1)
        if candidates:
            for arr2, (ch_id, ch_name, fname) in candidates:
                score = compare_fp_arrays(arr1, arr2)
                if score >= threshold:
                    results.append((ch_id, ch_name, fname, w_id, w_name, round(score * 100, 1)))
        else:
            # LSH kandidat topilmasa — to'liq skanerlash (fallback)
            for ch_id, ch_name, fname, arr2 in db_parsed:
                score = compare_fp_arrays(arr1, arr2)
                if score >= threshold:
                    results.append((ch_id, ch_name, fname, w_id, w_name, round(score * 100, 1)))
    return results


async def get_all_sources():
    """
    users_memory_bank + hidden_channel_knocker dan
    barcha unikal manbalarni qaytaradi.
    """
    await init_music_db()
    sources = set()
    async with db_mod.connect(db_mod.DB_NAME, timeout=30) as db:
        # 1. users_memory_bank dan guruh linklarni
        async with db.execute(
            "SELECT DISTINCT group_link FROM users_memory_bank "
            "WHERE group_link IS NOT NULL AND group_link != ''"
        ) as cur:
            for (link,) in await cur.fetchall():
                sources.add(link)

        # 2. hidden_channel_knocker dan joined kanallar
        async with db.execute(
            "SELECT channel_id FROM hidden_channel_knocker WHERE status='joined'"
        ) as cur:
            for (ch_id,) in await cur.fetchall():
                sources.add(ch_id)

        # 3. Shaxsiy/ochiq kanallar (open_channels ustuni)
        # @username, t.me/username, https://t.me/username — barchasi qabul qilinadi
        async with db.execute(
            "SELECT DISTINCT open_channels FROM users_memory_bank "
            "WHERE open_channels IS NOT NULL AND open_channels != ? "
            "AND open_channels != ?", ("", "Yo'q")
        ) as cur:
            for (ch,) in await cur.fetchall():
                for link in ch.split(','):
                    link = link.strip()
                    if link and (link.startswith('http') or
                                 link.startswith('@') or
                                 link.startswith('t.me/')):
                        sources.add(link)

        # 4. Has_hidden: shaxsiy kanal linki yoki maxfiy kanal
        async with db.execute(
            "SELECT DISTINCT has_hidden FROM users_memory_bank "
            "WHERE has_hidden IS NOT NULL AND has_hidden != '' "
            "AND has_hidden != '❌' AND has_hidden NOT LIKE '%Maxfiy%'"
        ) as cur:
            for (ch,) in await cur.fetchall():
                for link in ch.split(','):
                    link = link.strip()
                    if link and (link.startswith('http') or
                                 't.me/' in link or
                                 link.startswith('@') or
                                 link.lstrip('-').isdigit()):
                        sources.add(link)

    return list(sources)


# ─────────────────────────────────────────────────────────────────────
# FON SKANERLASH
# ─────────────────────────────────────────────────────────────────────

def _is_private_source(source: str) -> bool:
    """Maxfiy kanal: t.me/c/... yoki raqamli ID (-100XXXXX)."""
    s = str(source).strip()
    if 't.me/c/' in s:
        return True
    clean = s.lstrip('-')
    return clean.isdigit()


async def _scan_source_list(userbot, sources, shared, status_msg, total_sources):
    """Bitta userbot bilan kanallar ro'yxatini skanerlaydi. shared — umumiy hisoblagich."""
    for source in sources:
        if not SCANNING:
            break
        if await is_channel_scanned(source):
            continue
        try:
            try:
                entity = await userbot.get_entity(source)
            except Exception:
                continue

            channel_name = getattr(entity, 'title', str(source))

            if status_msg:
                async with shared['lock']:
                    done = shared['scanned']
                try:
                    await status_msg.edit(
                        f"🎵 **Musiqa skanerlash:**\n"
                        f"📊 `{done+1}/{total_sources}` kanal\n"
                        f"📢 `{channel_name}`\n"
                        f"🎶 Jami audio: `{shared['audio']}` ta"
                    )
                except Exception:
                    pass

            async for msg in userbot.iter_messages(entity, limit=500):
                if not SCANNING:
                    break
                if not msg.audio and not msg.voice:
                    continue
                tmp_path = os.path.join(BASE_DIR, f"tmp_audio_{msg.id}.ogg")
                try:
                    await msg.download_media(file=tmp_path)
                    fp, duration = await get_fingerprint_async(tmp_path)
                    if fp:
                        file_name = f"{channel_name}_{msg.id}"
                        await save_fingerprint(
                            str(entity.id), channel_name,
                            file_name, fp, duration or 0
                        )
                        async with shared['lock']:
                            shared['audio'] += 1
                except Exception as e:
                    print(f"Audio xatosi: {e}")
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                await asyncio.sleep(random.uniform(0.5, 1.5))

            await mark_channel_scanned(source)
            async with shared['lock']:
                shared['scanned'] += 1

        except Exception as e:
            print(f"Kanal xatosi ({source}): {e}")

        await asyncio.sleep(random.uniform(1, 3))


async def scan_all_channels(userbot, bot, admin_id, status_msg=None, userbot2=None):
    """
    Barcha manbalardan audio fayllarni yuklab, fingerprint oladi va saqlaydi.
    userbot2 berilsa: maxfiy kanallar→userbot1, ochiq kanallar→ikkala parallel.
    """
    global SCANNING
    if _SCANNING_LOCK.locked():
        return
    async with _SCANNING_LOCK:
        SCANNING = True
        await init_music_db()
        sources = await get_all_sources()
        total_sources = len(sources)
        shared = {'audio': 0, 'scanned': 0, 'lock': asyncio.Lock()}

        try:
            if userbot2 is None:
                await _scan_source_list(userbot, sources, shared, status_msg, total_sources)
            else:
                already_private = [s for s in sources if _is_private_source(str(s))]
                public          = [s for s in sources if not _is_private_source(str(s))]

                mid  = (len(public) + 1) // 2
                pub1 = public[:mid]
                pub2 = public[mid:]

                await asyncio.gather(
                    _scan_source_list(userbot,  already_private + pub1, shared, status_msg, total_sources),
                    _scan_source_list(userbot2, pub2,                   shared, None,        total_sources),
                    return_exceptions=True
                )
        finally:
            SCANNING = False

    return shared['scanned'], shared['audio']


# ─────────────────────────────────────────────────────────────────────
# MUSIQA QIDIRISH
# ─────────────────────────────────────────────────────────────────────

async def search_music(audio_path, threshold=0.65):
    """
    Berilgan audio faylni bazadagi fingerprint lar bilan taqqoslaydi.
    Mos kelganlarni qaytaradi.
    LSH indeks mavjud bo'lsa — faqat kandidatlar taqqoslanadi (tez).
    """
    await init_music_db()

    # Berilgan audio fingerprint (thread pool — event loop bloklanmaydi)
    fp_query, duration = await get_fingerprint_async(audio_path)
    if not fp_query:
        raise Exception(
            "Fingerprint olishda xatolik.\n"
            "ffmpeg va fpcalc o'rnatilganini tekshiring."
        )

    results = []
    BATCH_SIZE = 500
    # Query fingerprint bir marta parse qilinadi — loop tashqarisida
    arr_query_parsed = parse_fingerprint(fp_query)

    def _best_score(arr_db, fp_db_str):
        """Avval tez (oddiy), yetmasa sliding."""
        s1 = compare_fp_arrays(arr_query_parsed, arr_db)
        if s1 >= threshold:
            return s1
        return compare_fingerprints_sliding(fp_query, fp_db_str)

    # LSH indeks qurilgan bo'lsa — tez yo'l
    if _lsh_index:
        candidates = lsh_candidates(arr_query_parsed)
        if candidates:
            for arr_db, (ch_id, ch_name, fname, dur) in candidates:
                score = _best_score(arr_db, fp_query)
                if score >= threshold:
                    results.append({
                        'channel_id':   ch_id,
                        'channel_name': ch_name,
                        'file_name':    fname,
                        'score':        round(score * 100, 1),
                        'duration':     dur
                    })
            results.sort(key=lambda x: x['score'], reverse=True)
            return results

    # LSH indeks yo'q yoki bo'sh — to'liq skanerlash (fallback)
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        offset = 0
        while True:
            async with db.execute(
                "SELECT channel_id, channel_name, file_name, fingerprint, duration "
                "FROM music_fingerprints LIMIT ? OFFSET ?",
                (BATCH_SIZE, offset)
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                break
            for ch_id, ch_name, fname, fp_db, dur in rows:
                arr_db = parse_fingerprint(fp_db)
                score = _best_score(arr_db, fp_db)
                if score >= threshold:
                    results.append({
                        'channel_id':   ch_id,
                        'channel_name': ch_name,
                        'file_name':    fname,
                        'score':        round(score * 100, 1),
                        'duration':     dur
                    })
            offset += BATCH_SIZE

    # O'xshashlik bo'yicha saralash
    results.sort(key=lambda x: x['score'], reverse=True)
    return results


async def stop_scanning():
    global SCANNING
    SCANNING = False


async def add_watch_music(fingerprint, duration, name, admin_id):
    """Kuzatiladigan musiqa qo'shish."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        await db.execute(
            "INSERT INTO watch_fingerprints (name, fingerprint, duration, admin_id, added_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, fingerprint, duration, admin_id, now)
        )
        await db.commit()


async def get_watch_list():
    """Kuzatiladigan musiqalar ro'yxati."""
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        async with db.execute(
            "SELECT id, name, added_date FROM watch_fingerprints ORDER BY id DESC"
        ) as cur:
            return await cur.fetchall()


async def delete_watch_music(music_id):
    """Kuzatiladigan musiqani o'chirish."""
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        await db.execute("DELETE FROM watch_fingerprints WHERE id=?", (music_id,))
        await db.commit()


async def check_against_watch_list(fingerprint, threshold=0.65):
    """
    Yangi fingerprint ni kuzatiladigan musiqalar bilan taqqoslaydi.
    fingerprint: string yoki pre-parsed array.
    """
    await init_music_db()
    results = []
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        async with db.execute(
            "SELECT id, name, fingerprint, admin_id FROM watch_fingerprints"
        ) as cur:
            watches = await cur.fetchall()

    # Query fingerprint bir marta parse qilinadi
    arr_query = parse_fingerprint(fingerprint) if isinstance(fingerprint, str) else fingerprint

    fp_query_str = fingerprint if isinstance(fingerprint, str) else None
    for w_id, w_name, w_fp, admin_id in watches:
        arr_watch = parse_fingerprint(w_fp)
        score = compare_fp_arrays(arr_query, arr_watch)
        if score < threshold and fp_query_str:
            score = compare_fingerprints_sliding(fp_query_str, w_fp)
        if score >= threshold:
            results.append({
                'watch_id':   w_id,
                'watch_name': w_name,
                'admin_id':   admin_id,
                'score':      round(score * 100, 1)
            })
    return results


async def save_watch_alert_log(watch_name, source_name, source_id, source_type, score):
    """Topilgan musiqa arxivga saqlanadi."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        await db.execute(
            "INSERT INTO watch_alerts_log "
            "(watch_name, source_name, source_id, source_type, score, found_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (watch_name, source_name, source_id, source_type, score, now)
        )
        await db.commit()


async def get_watch_alerts_log(watch_name=None, limit=50):
    """Arxivdan topilganlarni olish."""
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        if watch_name:
            async with db.execute(
                "SELECT watch_name, source_name, source_id, source_type, score, found_date "
                "FROM watch_alerts_log WHERE watch_name=? "
                "ORDER BY found_date DESC LIMIT ?",
                (watch_name, limit)
            ) as cur:
                return await cur.fetchall()
        else:
            async with db.execute(
                "SELECT watch_name, source_name, source_id, source_type, score, found_date "
                "FROM watch_alerts_log ORDER BY found_date DESC LIMIT ?",
                (limit,)
            ) as cur:
                return await cur.fetchall()


async def is_profile_music_saved(user_id, fingerprint):
    """Profil musiqasi allaqachon bazada borligini tekshiradi."""
    await init_music_db()
    async with db_mod.connect(MUSIC_DB, timeout=30) as db:
        async with db.execute(
            "SELECT fingerprint FROM music_fingerprints "
            "WHERE channel_id=? AND file_name LIKE 'profile_%'",
            (str(user_id),)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return False
    for (saved_fp,) in rows:
        score = compare_fingerprints(saved_fp, fingerprint)
        if score >= 0.65:
            return True
    return False
