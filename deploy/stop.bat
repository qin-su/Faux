@echo off
chcp 65001 >nul 2>&1
title Smart Faux Server - Stop
setlocal
cd /d "%~dp0"

:: 从 .env 读端口（缺省 8081）
set "PORT=8081"
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
        if /i "%%a"=="FAUX_PORT" set "PORT=%%b"
    )
)

echo [*] 查找并停止监听端口 %PORT% 的进程...
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /c:":%PORT% " ^| findstr /c:"LISTENING"') do (
    echo [*] 结束 PID %%p
    taskkill /f /pid %%p >nul 2>&1
    set FOUND=1
)
if "%FOUND%"=="0" echo [i] 未发现监听端口 %PORT% 的进程，无需停止
echo [OK] 完成
pause
