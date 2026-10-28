@echo off
chcp 65001 >nul 2>&1
title Smart Faux Server
setlocal
cd /d "%~dp0"

if not exist "faux-server.exe" (
    echo [X] 未找到 faux-server.exe，请先运行 build.bat 打包
    pause
    exit /b 1
)

:: 从 .env 读端口（缺省 8081）
set "PORT=8081"
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
        if /i "%%a"=="FAUX_PORT" set "PORT=%%b"
    )
)

echo [*] Smart Faux Server 启动中（端口 %PORT%）...
echo [*] 3 秒后自动打开 Dashboard（http://localhost:%PORT%/）
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:%PORT%/"

faux-server.exe
pause
