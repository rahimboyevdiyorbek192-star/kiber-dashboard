# database.py
import os
import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME  = os.path.join(BASE_DIR, "cyber_station.db")


@asynccontextmanager
async def connect(db_path=None, timeout=30):
    """Yagona ulanish helperi — HAR BIR connection uchun busy_timeout
    o'rnatadi. Aks holda 'database is locked' xatosi darrov chiqadi
    (busy_timeout per-connection sozlama, defaulti 0)."""
    async with aiosqlite.connect(db_path or DB_NAME, timeout=timeout) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("PRAGMA synchronous=NORMAL")
        yield db

async def init_db():
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA cache_size=-32000")
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users_memory_bank (
                user_id     INTEGER,
                group_link  TEXT,
                first_name  TEXT,
                last_name   TEXT,
                username    TEXT,
                phone       TEXT,
                birth_date  TEXT,
                bio         TEXT,
                open_channels TEXT,
                has_hidden  TEXT,
                added_date  TEXT,
                last_updated TEXT,
                PRIMARY KEY (user_id, group_link)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trusted_admins (
                admin_id INTEGER PRIMARY KEY
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS hidden_channel_knocker (
                channel_id        TEXT PRIMARY KEY,
                creator_id        INTEGER,
                source_group      TEXT,
                last_request_time TEXT,
                status            TEXT DEFAULT 'pending',
                numeric_id        TEXT
            )
        """)
        try:
            await db.execute("ALTER TABLE hidden_channel_knocker ADD COLUMN numeric_id TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE hidden_channel_knocker ADD COLUMN userbot_idx INTEGER DEFAULT NULL")
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS archive_bin (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name    TEXT,
                file_path    TEXT,
                created_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scan_resume (
                scan_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                target_group TEXT,
                output_path  TEXT,
                last_offset  INTEGER DEFAULT 0,
                total_count  INTEGER DEFAULT 0,
                sender_id    INTEGER,
                status       TEXT DEFAULT 'running',
                started_at   TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS resolved_channel_ids (
                channel_link TEXT PRIMARY KEY,
                numeric_id   TEXT,
                resolved_at  TEXT,
                channel_name TEXT
            )
        """)
        try:
            await db.execute("ALTER TABLE resolved_channel_ids ADD COLUMN channel_name TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users_memory_bank ADD COLUMN added_date TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users_memory_bank ADD COLUMN last_updated TEXT")
        except Exception:
            pass
        try:
            await db.execute(
                "UPDATE scan_resume SET status='error' WHERE status='running'"
            )
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages_cache (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id          INTEGER NOT NULL,
                source          TEXT NOT NULL,
                sender_id       INTEGER,
                sender_name     TEXT,
                sender_username TEXT,
                text            TEXT,
                msg_date        TEXT,
                cached_at       TEXT DEFAULT (datetime('now')),
                is_deleted      INTEGER DEFAULT 0,
                UNIQUE(msg_id, source)
            )
        """)
        try:
            await db.execute("ALTER TABLE messages_cache ADD COLUMN is_deleted INTEGER DEFAULT 0")
        except Exception:
            pass
        # FTS5 — tez to'liq matn qidirish (content= messages_cache ga bog'langan)
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(
                text,
                sender_name,
                sender_username,
                source,
                content='messages_cache',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 1'
            )
        """)
        # FTS avtomatik yangilanishi uchun triggerlar
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS mc_ai AFTER INSERT ON messages_cache BEGIN
                INSERT INTO messages_fts(rowid, text, sender_name, sender_username, source)
                VALUES (new.id, new.text, new.sender_name, new.sender_username, new.source);
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS mc_ad AFTER DELETE ON messages_cache BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, text, sender_name, sender_username, source)
                VALUES ('delete', old.id, old.text, old.sender_name, old.sender_username, old.source);
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS mc_au AFTER UPDATE ON messages_cache BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, text, sender_name, sender_username, source)
                VALUES ('delete', old.id, old.text, old.sender_name, old.sender_username, old.source);
                INSERT INTO messages_fts(rowid, text, sender_name, sender_username, source)
                VALUES (new.id, new.text, new.sender_name, new.sender_username, new.source);
            END
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_mc_date   ON messages_cache(msg_date)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_mc_source ON messages_cache(source)"
        )
        # Tezlik uchun qo'shimcha indekslar — tez-tez qidiriladigan ustunlar
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_mc_sender ON messages_cache(sender_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_hck_status ON hidden_channel_knocker(status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_umb_group ON users_memory_bank(group_link)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS source_sync_state (
                source      TEXT PRIMARY KEY,
                last_msg_id INTEGER DEFAULT 0,
                last_synced TEXT
            )
        """)

        # ── YANGI JADVALLAR ──────────────────────────────────────────────

        # #8 O'zgarishlar tarixi
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_change_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                old_value  TEXT,
                new_value  TEXT,
                changed_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ucl_user ON user_change_log(user_id)"
        )

        # #9 Alert tizimi
        await db.execute("""
            CREATE TABLE IF NOT EXISTS keyword_alerts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id      INTEGER NOT NULL,
                keyword       TEXT NOT NULL,
                target_groups TEXT DEFAULT '',
                is_active     INTEGER DEFAULT 1,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alert_hits (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id  INTEGER,
                msg_id    INTEGER,
                source    TEXT,
                sender_id INTEGER,
                hit_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(alert_id, msg_id, source)
            )
        """)

        # #22 Tergovchi ish maydoni
        await db.execute("""
            CREATE TABLE IF NOT EXISTS investigations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                creator_id INTEGER,
                notes      TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS investigation_targets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                inv_id       INTEGER NOT NULL,
                target_type  TEXT NOT NULL,
                target_value TEXT NOT NULL,
                notes        TEXT DEFAULT '',
                added_at     TEXT DEFAULT (datetime('now'))
            )
        """)

        # Musiqa skaneri cursor jadvali (bot o'chsa-yonsa davom etish uchun)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS music_scan_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Kanal → userbot biriktirilishi jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_assignments (
                channel_link TEXT PRIMARY KEY,
                userbot_idx  INTEGER DEFAULT 0,
                assigned_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ca_ub ON channel_assignments(userbot_idx)"
        )

        try:
            await db.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass
        await db.commit()

    # FTS5 indeks bo'sh bo'lsa — background rebuild (bir martalik)
    import asyncio
    asyncio.create_task(_fts_rebuild_if_needed())


async def _fts_rebuild_if_needed():
    """FTS5 indexi bo'sh bo'lsa mavjud messages_cache dan bir martalik rebuild."""
    import asyncio
    await asyncio.sleep(5)  # DB to'liq ochilsin
    try:
        async with connect(DB_NAME, timeout=120) as db:
            # FTS5 da yozuv bormi?
            async with db.execute("SELECT rowid FROM messages_fts LIMIT 1") as cur:
                fts_row = await cur.fetchone()
            if fts_row is not None:
                return  # Allaqachon to'ldirilgan
            # messages_cache da ma'lumot bormi?
            async with db.execute("SELECT COUNT(*) FROM messages_cache") as cur:
                mc_count = (await cur.fetchone())[0]
            if mc_count == 0:
                return
            print(f"[FTS5] {mc_count:,} ta xabar indekslanmoqda...")
            await db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            await db.commit()
            print(f"[FTS5] Indeks tayyor ({mc_count:,} ta xabar).")
    except Exception as e:
        print(f"[FTS5] Rebuild xatosi: {e}")


# ─────────────────────────────────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────────────────────────────────

async def add_admin(admin_id: int):
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute(
            "INSERT OR IGNORE INTO trusted_admins (admin_id) VALUES (?)", (admin_id,)
        )
        await db.commit()

async def remove_admin(admin_id: int):
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute("DELETE FROM trusted_admins WHERE admin_id=?", (admin_id,))
        await db.commit()

async def is_admin(admin_id: int, super_admin_id: int) -> bool:
    if admin_id == super_admin_id:
        return True
    async with connect(DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT 1 FROM trusted_admins WHERE admin_id=?", (admin_id,)
        ) as cur:
            return await cur.fetchone() is not None

async def get_all_admins():
    async with connect(DB_NAME, timeout=30) as db:
        async with db.execute("SELECT admin_id FROM trusted_admins ORDER BY admin_id") as cur:
            return await cur.fetchall()

async def update_user_changes(user_id: int, bio: str, open_channels: str, has_hidden: str):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute(
            "UPDATE users_memory_bank "
            "SET bio=?, open_channels=?, has_hidden=?, last_updated=? "
            "WHERE user_id=?",
            (bio, open_channels, has_hidden, now_str, user_id)
        )
        await db.commit()

async def save_user_to_bank(user_id, group_link, f_name, l_name, uname,
                             phone, b_date, bio, o_chan, h_hidden):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    async with connect(DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT added_date, first_name, last_name, username, phone, bio "
            "FROM users_memory_bank WHERE user_id=? AND group_link=?",
            (user_id, group_link)
        ) as cur:
            existing = await cur.fetchone()
        added = now_str if not existing else (existing[0] or now_str)

        # O'zgarishlarni loglash
        if existing:
            changes = []
            old_vals = {'first_name': existing[1], 'last_name': existing[2],
                        'username': existing[3], 'phone': existing[4], 'bio': existing[5]}
            new_vals = {'first_name': f_name, 'last_name': l_name,
                        'username': uname, 'phone': phone, 'bio': bio}
            for field, old_v in old_vals.items():
                new_v = new_vals[field]
                if (old_v or '') != (new_v or '') and (old_v or new_v):
                    changes.append((user_id, field, old_v or '', new_v or '', now_str))
            if changes:
                await db.executemany(
                    "INSERT INTO user_change_log (user_id, field_name, old_value, new_value, changed_at) "
                    "VALUES (?,?,?,?,?)",
                    changes
                )

        await db.execute("""
            INSERT OR REPLACE INTO users_memory_bank
            (user_id, group_link, first_name, last_name, username,
             phone, birth_date, bio, open_channels, has_hidden, added_date, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, group_link, f_name, l_name, uname,
              phone, b_date, bio, o_chan, h_hidden, added, now_str))
        await db.commit()


# ─────────────────────────────────────────────────────────────────────
# SCAN RESUME
# ─────────────────────────────────────────────────────────────────────

async def create_scan_session(target_group, output_path, sender_id):
    async with connect(DB_NAME, timeout=30) as db:
        cur = await db.execute(
            "INSERT INTO scan_resume (target_group, output_path, last_offset, "
            "total_count, sender_id, status, started_at) VALUES (?,?,0,0,?,?,?)",
            (target_group, output_path, sender_id, 'running',
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        await db.commit()
        return cur.lastrowid

async def update_scan_progress(scan_id, last_offset, total_count):
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute(
            "UPDATE scan_resume SET last_offset=?, total_count=? WHERE scan_id=?",
            (last_offset, total_count, scan_id)
        )
        await db.commit()

async def finish_scan_session(scan_id, status='done'):
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute(
            "UPDATE scan_resume SET status=? WHERE scan_id=?", (status, scan_id)
        )
        await db.commit()

async def get_pending_scans():
    async with connect(DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT scan_id, target_group, output_path, last_offset, "
            "total_count, sender_id FROM scan_resume WHERE status='running'"
        ) as cur:
            return await cur.fetchall()


# ─────────────────────────────────────────────────────────────────────
# #8 O'ZGARISHLAR TARIXI
# ─────────────────────────────────────────────────────────────────────

async def get_user_change_log(user_id: int, limit: int = 50):
    async with connect(DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT field_name, old_value, new_value, changed_at "
            "FROM user_change_log WHERE user_id=? "
            "ORDER BY changed_at DESC LIMIT ?",
            (user_id, limit)
        ) as cur:
            return await cur.fetchall()


# ─────────────────────────────────────────────────────────────────────
# #9 ALERT TIZIMI
# ─────────────────────────────────────────────────────────────────────

async def add_alert(admin_id: int, keyword: str, target_groups: str = '') -> int:
    async with connect(DB_NAME, timeout=30) as db:
        cur = await db.execute(
            "INSERT INTO keyword_alerts (admin_id, keyword, target_groups) VALUES (?,?,?)",
            (admin_id, keyword.lower().strip(), target_groups)
        )
        await db.commit()
        return cur.lastrowid

async def list_alerts(admin_id: int = None):
    async with connect(DB_NAME, timeout=30) as db:
        if admin_id:
            async with db.execute(
                "SELECT id, keyword, target_groups, is_active, created_at "
                "FROM keyword_alerts WHERE admin_id=? ORDER BY id",
                (admin_id,)
            ) as cur:
                return await cur.fetchall()
        async with db.execute(
            "SELECT id, keyword, target_groups, is_active, created_at "
            "FROM keyword_alerts ORDER BY id"
        ) as cur:
            return await cur.fetchall()

async def delete_alert(alert_id: int):
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute("DELETE FROM keyword_alerts WHERE id=?", (alert_id,))
        await db.commit()

async def toggle_alert(alert_id: int, is_active: int):
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute(
            "UPDATE keyword_alerts SET is_active=? WHERE id=?", (is_active, alert_id)
        )
        await db.commit()

async def get_active_alerts():
    async with connect(DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT id, admin_id, keyword, target_groups "
            "FROM keyword_alerts WHERE is_active=1"
        ) as cur:
            return await cur.fetchall()

async def check_and_record_alert_hit(alert_id: int, msg_id: int, source: str, sender_id: int) -> bool:
    async with connect(DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT 1 FROM alert_hits WHERE alert_id=? AND msg_id=? AND source=?",
            (alert_id, msg_id, source)
        ) as cur:
            if await cur.fetchone():
                return False
        try:
            await db.execute(
                "INSERT INTO alert_hits (alert_id, msg_id, source, sender_id) VALUES (?,?,?,?)",
                (alert_id, msg_id, source, sender_id)
            )
            await db.commit()
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────────
# #22 TERGOVCHI ISH MAYDONI
# ─────────────────────────────────────────────────────────────────────

async def create_investigation(name: str, creator_id: int) -> int:
    async with connect(DB_NAME, timeout=30) as db:
        cur = await db.execute(
            "INSERT INTO investigations (name, creator_id) VALUES (?,?)",
            (name, creator_id)
        )
        await db.commit()
        return cur.lastrowid

async def get_investigations(creator_id: int = None):
    async with connect(DB_NAME, timeout=30) as db:
        if creator_id:
            async with db.execute(
                "SELECT id, name, creator_id, notes, created_at "
                "FROM investigations WHERE creator_id=? ORDER BY id DESC",
                (creator_id,)
            ) as cur:
                return await cur.fetchall()
        async with db.execute(
            "SELECT id, name, creator_id, notes, created_at "
            "FROM investigations ORDER BY id DESC"
        ) as cur:
            return await cur.fetchall()

async def add_investigation_target(inv_id: int, target_type: str, target_value: str, notes: str = '') -> int:
    async with connect(DB_NAME, timeout=30) as db:
        cur = await db.execute(
            "INSERT INTO investigation_targets (inv_id, target_type, target_value, notes) "
            "VALUES (?,?,?,?)",
            (inv_id, target_type, str(target_value), notes)
        )
        await db.commit()
        return cur.lastrowid

async def get_investigation_targets(inv_id: int):
    async with connect(DB_NAME, timeout=30) as db:
        async with db.execute(
            "SELECT id, target_type, target_value, notes, added_at "
            "FROM investigation_targets WHERE inv_id=? ORDER BY added_at",
            (inv_id,)
        ) as cur:
            return await cur.fetchall()

async def delete_investigation(inv_id: int):
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute("DELETE FROM investigation_targets WHERE inv_id=?", (inv_id,))
        await db.execute("DELETE FROM investigations WHERE id=?", (inv_id,))
        await db.commit()

async def update_investigation_notes(inv_id: int, notes: str):
    async with connect(DB_NAME, timeout=30) as db:
        await db.execute(
            "UPDATE investigations SET notes=? WHERE id=?", (notes, inv_id)
        )
        await db.commit()


# ─────────────────────────────────────────────────────────────────────
# KANAL → USERBOT BIRIKTIRILISHI
# ─────────────────────────────────────────────────────────────────────

async def get_channel_userbot(channel_link: str) -> int | None:
    """Kanal qaysi userbotga biriktirilganini qaytaradi. Yo'q bo'lsa None."""
    async with connect(DB_NAME, timeout=10) as db:
        async with db.execute(
            "SELECT userbot_idx FROM channel_assignments WHERE channel_link=?",
            (channel_link,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None

async def assign_channel(channel_link: str, n_userbots: int) -> int:
    """
    Kanalni eng kam yukli userbotga biriktiradi.
    Allaqachon biriktirilgan bo'lsa — o'zgartirmaydi.
    Qaytaradi: biriktirilgan userbot idx.
    """
    async with connect(DB_NAME, timeout=10) as db:
        # Allaqachon bor?
        async with db.execute(
            "SELECT userbot_idx FROM channel_assignments WHERE channel_link=?",
            (channel_link,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            return row[0]

        # Har userbotdagi kanal soni
        counts = [0] * n_userbots
        async with db.execute(
            "SELECT userbot_idx, COUNT(*) FROM channel_assignments GROUP BY userbot_idx"
        ) as cur:
            for ub_idx, cnt in await cur.fetchall():
                if 0 <= ub_idx < n_userbots:
                    counts[ub_idx] = cnt

        min_idx = counts.index(min(counts))
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        await db.execute(
            "INSERT OR IGNORE INTO channel_assignments (channel_link, userbot_idx, assigned_at) "
            "VALUES (?, ?, ?)",
            (channel_link, min_idx, now_str)
        )
        await db.commit()
        return min_idx

async def get_userbot_channels(userbot_idx: int) -> list:
    """Userbotga biriktirilgan barcha kanallar ro'yxatini qaytaradi."""
    async with connect(DB_NAME, timeout=10) as db:
        async with db.execute(
            "SELECT channel_link FROM channel_assignments WHERE userbot_idx=? ORDER BY assigned_at",
            (userbot_idx,)
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

async def reassign_channels(n_userbots: int):
    """
    Userbot soni o'zgarganda kanallarni MOSLASHTIRADI.
    MUHIM: allaqachon biriktirilgan kanal o'z userbotida QOLADI
           (maxfiy kanal faqat o'z egasi ko'ra oladi — ko'chirilmaydi).
    Faqat:
      - egasi yo'qolgan kanallar (userbot_idx >= n) qayta tarqatiladi
      - yangilari assign_channel orqali eng bo'sh UB ga ketadi
    """
    async with connect(DB_NAME, timeout=30) as db:
        # 1. Hozirgi taqsimotni hisoblash (faqat n ichidagilar)
        counts = [0] * n_userbots
        orphans = []
        async with db.execute(
            "SELECT channel_link, userbot_idx FROM channel_assignments ORDER BY assigned_at"
        ) as cur:
            for ch, idx in await cur.fetchall():
                if idx is not None and 0 <= idx < n_userbots:
                    counts[idx] += 1            # o'z joyida qoladi
                else:
                    orphans.append(ch)          # egasi yo'q (userbot o'chirilgan)

        # 2. Faqat egasiz kanallarni eng bo'sh UB ga bering
        for ch in orphans:
            min_idx = counts.index(min(counts))
            await db.execute(
                "UPDATE channel_assignments SET userbot_idx=? WHERE channel_link=?",
                (min_idx, ch)
            )
            counts[min_idx] += 1
        await db.commit()
        if orphans:
            print(f"[ASSIGN] {len(orphans)} ta egasiz kanal qayta tarqatildi: {counts}")
