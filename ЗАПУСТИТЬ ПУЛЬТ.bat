@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Pult Podslushano MEZ
.venv\Scripts\python.exe vk_admin_bot.py
pause
