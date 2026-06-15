import asyncio
import webbrowser
import os
from telethon import TelegramClient

# config.py dan o'qish
try:
    exec(open('config.py').read())
except:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        API_ID = int(os.getenv('API_ID'))
        API_HASH = os.getenv('API_HASH')
    except:
        print("[XATO] API_ID va API_HASH topilmadi!")
        exit(1)

async def main():
    client = TelegramClient('userbot_session', API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        print("[OK] Session allaqachon mavjud!")
        await client.disconnect()
        return

    print("QR kod orqali kirish...")
    qr = await client.qr_login()

    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={qr.url}"
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Telegram QR Login</title></head>
<body style="text-align:center;font-family:Arial;padding:40px">
  <h2>Telegram ilovangiz bilan skanerlang</h2>
  <img src="{qr_url}" style="border:4px solid #2AABEE;border-radius:10px"/>
  <p style="color:gray">Telegram → Sozlamalar → Qurilmalar → QR kod skanerlash</p>
  <p style="font-size:12px;color:#ccc">Sahifani yangilash kerak bo'lsa qayta ishga tushiring</p>
</body>
</html>"""

    with open('qr_login.html', 'w', encoding='utf-8') as f:
        f.write(html)

    webbrowser.open('qr_login.html')
    print("[OK] Brauzerda QR kod ochildi!")
    print("Telegram ilovangizda: Sozlamalar → Qurilmalar → QR kod skanerlash")
    print("Kutilmoqda (60 soniya)...")

    try:
        await qr.wait(60)
        print("\n[OK] Muvaffaqiyat! userbot_session saqlandi.")
        print("Endi python main.py ni ishga tushiring.")
    except Exception as e:
        print(f"\n[XATO] Vaqt o'tdi yoki xato: {e}")
        print("Qayta ishga tushiring: python qr_login.py")

    await client.disconnect()

asyncio.run(main())
