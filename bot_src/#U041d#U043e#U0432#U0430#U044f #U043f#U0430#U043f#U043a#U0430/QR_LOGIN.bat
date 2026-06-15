@echo off
title KIBER STANSIYA - Userbot QR Login
color 0B
echo.
echo ╔══════════════════════════════════════════════════════╗
echo ║    KIBER STANSIYA — BARCHA USERBOT QR LOGIN          ║
echo ╚══════════════════════════════════════════════════════╝
echo.
echo [*] .env fayli tekshirilmoqda...

if not exist ".env" (
    echo [XATO] .env fayli topilmadi!
    echo        Avval INSTALL.bat ni ishga tushiring va .env ni to'ldiring.
    echo.
    pause
    exit /b 1
)

echo [OK] .env topildi.
echo.
echo [*] QR login boshlanmoqda...
echo     Har userbot uchun brauzer avtomatik ochiladi.
echo     QR ni skanerlang: Telegram - Sozlamalar - Qurilmalar - QR kod
echo.

python qr_login_all.py
if %errorlevel% neq 0 (
    python3 qr_login_all.py
)

echo.
pause
