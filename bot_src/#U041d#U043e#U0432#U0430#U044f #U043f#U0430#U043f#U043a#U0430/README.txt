╔══════════════════════════════════════════════╗
║      KIBER STANSIYA OSINT PRO v3             ║
║      O'RNATISH VA ISHLATISH YO'RIQNOMASI     ║
╚══════════════════════════════════════════════╝

BIRINCHI MARTA O'RNATISH:
─────────────────────────
1. INSTALL.bat ni o'ng tugma → "Administrator sifatida ishga tushirish"
   (Python va kutubxonalar avtomatik o'rnatiladi)

2. .env faylini oching va ma'lumotlarni to'ldiring:
   API_ID=sizning_api_id
   API_HASH=sizning_api_hash
   BOT_TOKEN=sizning_bot_token
   SUPER_ADMIN_ID=sizning_telegram_id
   ADMIN_IDS=qo'shimcha_adminlar (ixtiyoriy)

3. START_BOT.bat ni o'ng tugma → "Administrator sifatida ishga tushirish"

KEYINGI SAFAR:
──────────────
Faqat START_BOT.bat ni ishga tushiring.

.ENV FAYLINI QAYERDAN OLISH:
─────────────────────────────
API_ID va API_HASH: https://my.telegram.org
BOT_TOKEN: @BotFather dan
SUPER_ADMIN_ID: @userinfobot ga /start yuboring

MUHIM:
──────
• Papkadan tashqariga ko'chirmang — barcha fayllar bir joyda bo'lishi kerak
• Birinchi ishga tushirishda userbot uchun telefon raqam so'raladi
• Internet va VPN kerak bo'lishi mumkin

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YANGI USERBOT QO'SHISH (N-USERBOT TIZIMI):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Bot cheksiz userbot bilan ishlaydi.
Har yangi userbot qo'shilishi bilan ish avtomatik bo'linadi
va bot shunchalik tezroq ishlaydi.

USUL 1 — .env orqali (oddiy):
──────────────────────────────
.env fayliga qo'shing:

   # 2-userbot
   USERBOT2_PHONE=+998901234567

   # 3-userbot (ixtiyoriy)
   USERBOT3_PHONE=+998901234568

   # 4-userbot (ixtiyoriy)
   USERBOT4_PHONE=+998901234569

Agar har bir userbot uchun alohida API_ID/HASH bo'lsa:
   USERBOT2_API_ID=12345678
   USERBOT2_API_HASH=abcdef1234567890abcdef1234567890

Bo'lmasa — asosiy API_ID/HASH ishlatiladi (odatda yetarli).

USUL 2 — Session fayl orqali (tavsiya):
────────────────────────────────────────
1. qr_login2.py ni ishga tushiring:
   python qr_login2.py

2. QR kodni skanerlang (Telegram → Sozlamalar → Qurilmalar → QR)

3. userbot2_session.session fayli yaratiladi

4. Botni qayta yoqing — avtomatik taniydi.

Keyingi userbot uchun: userbot3_session.session, userbot4_session.session...

QANCHA USERBOT — SHUNCHA TEZLIK:
──────────────────────────────────
   1 userbot  → standart tezlik
   2 userbot  → 2× tez (profil yig'ish, maxfiy kanal)
   3 userbot  → 3× tez
   5 userbot  → 5× tez
   (Flood xavfi OSHMAYDI — har biri o'z tezligida ishlaydi)

