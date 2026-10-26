@echo off
chcp 65001 >nul 2>&1
title Smart Faux Server

cd /d "%~dp0"

echo ========================================
echo   Smart Faux Server
echo ========================================
echo.

:: 检查 .env
if not exist ".env" (
    echo [!] 未找到 .env，从 .env.example 复制...
    copy .env.example .env >nul
    echo [i] 请编辑 .env 填入 ESB 连接信息后重新启动
)

:: 检查 Python
where python >nul 2>&1
if errorlevel 1 (
    echo [X] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

:: 检查依赖
python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo [*] 安装依赖...
    pip install -r requirements.txt
)

echo [*] 启动 Smart Faux Server (端口 8081)...
echo [*] Web UI:    http://localhost:8081/
echo [*] Swagger:  http://localhost:8081/docs
echo [*] 按 Ctrl+C 停止
echo.

python -m uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload

pause
