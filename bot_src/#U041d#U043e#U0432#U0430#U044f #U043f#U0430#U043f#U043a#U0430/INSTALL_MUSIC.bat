@echo off
title Musiqa skanerlash - O'rnatish
color 0A
echo ============================================
echo   MUSIQA SKANERLASH - QOSHIMCHA O'RNATISH
echo ============================================
echo.
echo [*] ffmpeg va chromaprint yuklanmoqda...
echo.

:: ffmpeg yuklab olish
curl -L -o ffmpeg.zip https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip
echo [*] ffmpeg chiqarilmoqda...
powershell -Command "Expand-Archive -Path ffmpeg.zip -DestinationPath ffmpeg_tmp -Force"
:: fpcalc ni papkaga ko'chirish
for /r ffmpeg_tmp %%f in (fpcalc.exe) do copy "%%f" "fpcalc.exe"
for /r ffmpeg_tmp %%f in (ffmpeg.exe) do copy "%%f" "ffmpeg.exe"
rmdir /s /q ffmpeg_tmp
del ffmpeg.zip

echo.
echo ============================================
echo   [+] O'RNATISH YAKUNLANDI!
echo ============================================
echo.
echo Endi START.bat ni ishga tushiring.
pause
