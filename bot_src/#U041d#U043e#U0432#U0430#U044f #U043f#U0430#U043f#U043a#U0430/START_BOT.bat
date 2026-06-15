@echo off
title KIBER STANSIYA OSINT PRO v3
color 0A
echo ============================================
echo   KIBER STANSIYA: INTEGRATED OSINT STATION
echo        TIZIM ISHGA TUSHMOQDA...
echo ============================================
echo.

:: Python borligini tekshirish
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python topilmadi!
    echo [!] Avval INSTALL.bat ni ishga tushiring!
    echo.
    pause
    exit
)

:: Kutubxonalar o'rnatilganligini tekshirish
python -c "import telethon, aiosqlite, openpyxl, dotenv" >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Kutubxonalar topilmadi!
    echo [!] Avval INSTALL.bat ni ishga tushiring!
    echo.
    pause
    exit
)

echo [PERSISTENCE] SQLite Ma'lumotlar bazasi tekshirilmoqda...
echo [SURVEILLANCE] Orqa fon monitoring agentlari yuklanmoqda...
echo.

python main.py
if %errorlevel% neq 0 (
    echo.
    echo [!] Xatolik yuz berdi!
    pause
)
