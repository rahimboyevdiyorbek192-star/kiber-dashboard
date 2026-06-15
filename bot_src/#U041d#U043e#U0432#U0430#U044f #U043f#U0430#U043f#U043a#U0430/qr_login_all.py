"""
qr_login_all.py — Barcha userbotlar uchun QR kod orqali kirish
1-userbotdan boshlab, N-userbotgacha ketma-ket kiradi.
Har biri uchun brauzerda QR ochiladi — skanerlaysiz, tayyor!
"""
import asyncio
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from telethon import TelegramClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_userbot_configs():
    """
    .env dan barcha userbotlarni o'qiydi.
    1-userbot: API_ID, API_HASH, session=userbot_session
    2-userbot: USERBOT2_API_ID, USERBOT2_API_HASH, session=userbot2_session
    ...
    """
    configs = []

    # 1-userbot
    api_id   = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    if api_id and api_hash:
        configs.append({
            "num":      1,
            "api_id":   int(api_id),
            "api_hash": api_hash,
            "session":  os.path.join(BASE_DIR, "userbot_session"),
            "label":    "1-USERBOT (asosiy)",
        })
    else:
        print("[XATO] .env da API_ID va API_HASH topilmadi!")
        sys.exit(1)

    # 2-userbot va undan yuqori
    i = 2
    while True:
        phone_key    = f"USERBOT{i}_PHONE"
        api_id_key   = f"USERBOT{i}_API_ID"
        api_hash_key = f"USERBOT{i}_API_HASH"
        session_path = os.path.join(BASE_DIR, f"userbot{i}_session")

        phone    = os.getenv(phone_key, "").strip()
        ub_id    = os.getenv(api_id_key, "").strip()
        ub_hash  = os.getenv(api_hash_key, "").strip()
        exists   = os.path.exists(session_path + ".session")

        # Na phone, na session — to'xtaymiz
        if not phone and not exists and not ub_id:
            break

        if not ub_id or not ub_hash:
            print(f"[O'TKAZIB] USERBOT{i}: API_ID yoki API_HASH yo'q — o'tkazilmoqda")
            i += 1
            continue

        configs.append({
            "num":      i,
            "api_id":   int(ub_id),
            "api_hash": ub_hash,
            "session":  session_path,
            "label":    f"{i}-USERBOT",
        })
        i += 1

    return configs


async def login_one(cfg):
    """Bitta userbot uchun QR login."""
    num     = cfg["num"]
    label   = cfg["label"]
    session = cfg["session"]

    client = TelegramClient(session, cfg["api_id"], cfg["api_hash"])
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[✅] {label}: allaqachon kirgan — {me.first_name} (+{me.phone})")
        await client.disconnect()
        return True

    print()
    print("=" * 55)
    print(f"  {label} — QR KOD ORQALI KIRISH")
    print("=" * 55)

    qr = await client.qr_login()

    qr_img = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={qr.url}"
    print(f"\n  QR havola: {qr_img}\n")

    # HTML fayl yaratish va brauzerda ochish
    html_path = os.path.join(BASE_DIR, f"_qr_userbot{num}.html")
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{label} QR Login</title></head>
<body style="text-align:center;font-family:Arial;padding:40px;background:#1a1a2e">
  <h2 style="color:#2AABEE">{label} — Telegram QR Login</h2>
  <img src="{qr_img}" style="border:4px solid #2AABEE;border-radius:10px;margin:20px"/>
  <p style="color:#ccc;font-size:18px">
    Telegram → <b>Sozlamalar</b> → <b>Qurilmalar</b> → <b>QR kod skanerlash</b>
  </p>
  <p style="color:#888;font-size:13px">QR 60 soniya amal qiladi.</p>
</body>
</html>"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    try:
        import webbrowser
        webbrowser.open(html_path)
        print(f"[OK] Brauzer ochildi — {label} QR ni skanerlang!")
    except Exception:
        print(f"[INFO] Yuqoridagi havolani brauzerda oching.")

    print(f"\nKutilmoqda (60 soniya)...")
    print(f"Telegram → Sozlamalar → Qurilmalar → QR kod skanerlash\n")

    try:
        await qr.wait(60)
        me = await client.get_me()
        print(f"\n[✅] {label} muvaffaqiyatli kirdi: {me.first_name} (+{me.phone})")
        session_name = os.path.basename(session)
        print(f"[✅] {session_name}.session saqlandi.\n")
        await client.disconnect()
        try:
            os.remove(html_path)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"\n[XATO] {label}: {e}")
        print(f"Qayta urinish uchun — QR_LOGIN.bat ni qayta ishga tushiring.\n")
        await client.disconnect()
        try:
            os.remove(html_path)
        except Exception:
            pass
        return False


async def main():
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║    KIBER STANSIYA — BARCHA USERBOT QR LOGIN          ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    configs = get_userbot_configs()
    print(f"[INFO] Topilgan userbot soni: {len(configs)} ta")
    print()

    ok = 0
    fail = 0
    for cfg in configs:
        result = await login_one(cfg)
        if result:
            ok += 1
        else:
            fail += 1
        if cfg != configs[-1]:
            print("Keyingisiga o'tilmoqda...")
            await asyncio.sleep(2)

    print()
    print("═" * 55)
    print(f"  NATIJA: {ok} ta muvaffaqiyatli | {fail} ta xato")
    print("═" * 55)
    if ok > 0:
        print(f"\n[✅] Endi START_BOT.bat ni ishga tushiring!")
    if fail > 0:
        print(f"[!]  Xatoliklarni tuzatib, QR_LOGIN.bat ni qayta ishga tushiring.")
    print()


asyncio.run(main())
