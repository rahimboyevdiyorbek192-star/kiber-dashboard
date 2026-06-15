@echo off
title KIBER STANSIYA - O'rnatish
color 0A
echo ============================================
echo   KIBER STANSIYA OSINT PRO - O'RNATISH
echo ============================================
echo.

:: Python borligini tekshirish
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python topilmadi. Yuklanmoqda...
    echo.
    :: Python 3.12 yuklab olish
    curl -o python_installer.exe https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe
    echo [*] Python o'rnatilmoqda...
    python_installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
    del python_installer.exe
    echo [+] Python o'rnatildi!
) else (
    echo [+] Python topildi!
)

echo.
echo [*] Kerakli kutubxonalar o'rnatilmoqda...
echo.

pip install telethon aiosqlite asyncpg openpyxl python-dotenv numpy reportlab requests python-whois PySocks --quiet
if %errorlevel% neq 0 (
    pip3 install telethon aiosqlite openpyxl python-dotenv numpy reportlab requests python-whois PySocks --quiet
)

echo [+] Asosiy kutubxonalar o'rnatildi!

echo.
echo [*] Havola tekshirish (Kiberxavfsizlik) kutubxonalari o'rnatilmoqda...
pip install androguard --quiet
if %errorlevel% neq 0 (
    echo [!] androguard o'rnatilmadi - ixtiyoriy, APK chuqur tahlilsiz ishlaydi
) else (
    echo [+] androguard o'rnatildi - APK DEX chuqur tahlil yoqildi!
)

echo.
echo [*] Ixtiyoriy: Playwright brauzer o'rnatilmoqda...
pip install playwright --quiet
playwright install chromium --quiet 2>nul
if %errorlevel% neq 0 (
    echo [!] Playwright o'rnatilmadi - ixtiyoriy, sahifa tahlilisiz ishlaydi
) else (
    echo [+] Playwright o'rnatildi - sahifada karta/parol shakli topiladi!
)

echo.
echo [*] .env fayli tekshirilmoqda...
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [+] .env fayli yaratildi (.env.example dan)
        echo.
        echo ============================================
        echo   [!] MUHIM: .env faylini oching va
        echo       o'z ma'lumotlaringizni kiriting:
        echo       - API_ID
        echo       - API_HASH
        echo       - BOT_TOKEN
        echo       - SUPER_ADMIN_ID
        echo ============================================
    ) else (
        echo [!] .env.example topilmadi - .env ni qo'lda yarating
    )
) else (
    echo [+] .env fayli mavjud!
)

echo.
echo ============================================
echo   [+] O'RNATISH MUVAFFAQIYATLI YAKUNLANDI!
echo ============================================
echo.
echo Keyingi qadam: .env faylini to'ldiring, keyin START_BOT.bat ni ishga tushiring.
echo.
pause
