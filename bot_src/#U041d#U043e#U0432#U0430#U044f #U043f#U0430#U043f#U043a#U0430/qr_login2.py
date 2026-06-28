"""
qr_login2.py — Ikkinchi userbot uchun QR kod orqali kirish
Bir marta ishga tushiring, QR ni skanerlang, tayyor!
Keyin main.py ni ishga tushiring.
"""
import asyncio
import os
from telethon import TelegramClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

API_ID   = int(os.getenv("USERBOT2_API_ID",  os.getenv("API_ID",  "0")))
API_HASH = os.getenv("USERBOT2_API_HASH", os.getenv("API_HASH", ""))

if not API_ID or not API_HASH:
    print("[XATO] .env faylida API_ID va API_HASH topilmadi!")
    exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION  = os.path.join(BASE_DIR, "userbot2_session")


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[OK] Userbot2 session allaqachon mavjud: {me.first_name} (+{me.phone})")
        await client.disconnect()
        return

    print("=" * 50)
    print("  IKKINCHI USERBOT — QR KOD ORQALI KIRISH")
    print("=" * 50)
    print()

    qr = await client.qr_login()

    # QR ni terminalda ham ko'rsatish (oddiy URL)
    print("Quyidagi havolani brauzerda oching (QR rasm):")
    qr_img = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={qr.url}"
    print(f"\n  {qr_img}\n")

    # HTML fayl yaratish
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Userbot2 QR Login</title></head>
<body style="text-align:center;font-family:Arial;padding:40px;background:#1a1a2e">
  <h2 style="color:#2AABEE">Ikkinchi Userbot — Telegram QR Login</h2>
  <img src="{qr_img}" style="border:4px solid #2AABEE;border-radius:10px;margin:20px"/>
  <p style="color:#ccc;font-size:18px">
    Telegram ilovangiz → <b>Sozlamalar</b> → <b>Qurilmalar</b> → <b>QR kod skanerlash</b>
  </p>
  <p style="color:#888;font-size:13px">
    QR 60 soniya amal qiladi. Vaqt o'tsa — skriptni qayta ishga tushiring.
  </p>
</body>
</html>"""

    html_path = os.path.join(BASE_DIR, "qr_login2.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    try:
        import webbrowser
        webbrowser.open(html_path)
        print("[OK] Brauzer ochildi — QR ni skanerlang!")
    except Exception:
        print(f"[INFO] Brauzer ochilmadi. Yuqoridagi havolani qo'lda oching.")

    print("\nKutilmoqda (60 soniya)...")
    print("Telegram → Sozlamalar → Qurilmalar → QR kod skanerlash\n")

    try:
        await qr.wait(60)
        me = await client.get_me()
        print(f"\n[OK] Muvaffaqiyat! {me.first_name} (+{me.phone}) tizimga kirdi.")
        print(f"[OK] userbot2_session saqlandi.")
        print("\nEndi main.py ni ishga tushiring — 2 userbot bilan ishlaydi!")
    except Exception as e:
        print(f"\n[XATO] Vaqt o'tdi yoki xato: {e}")
        print("Qayta urinib ko'ring: python qr_login2.py")

    await client.disconnect()

    # HTML faylni tozalash
    try:
        os.remove(html_path)
    except Exception:
        pass


asyncio.run(main())
