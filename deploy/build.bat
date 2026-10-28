@echo off
chcp 65001 >nul 2>&1
title Smart Faux Server - Build
setlocal
cd /d "%~dp0"

echo ========================================
echo   Smart Faux Server 一键打包
echo ========================================
echo.

echo [1/4] 检查 Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [X] 未找到 Python，请先安装 Python 3.10+ 并加入 PATH
    pause
    exit /b 1
)
python --version

echo.
echo [2/4] 检查项目依赖（fastapi/uvicorn 等）...
python -c "import fastapi, uvicorn, dotenv, pika" >nul 2>&1
if errorlevel 1 (
    echo [*] 安装项目依赖...
    python -m pip install -r ..\requirements.txt
    if errorlevel 1 ( echo [X] 依赖安装失败 & pause & exit /b 1 )
)

echo.
echo [3/4] 检查 PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [*] 安装 PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 ( echo [X] PyInstaller 安装失败 & pause & exit /b 1 )
)

echo.
echo [4/4] 打包中（首次约 1-2 分钟）...
python -m PyInstaller faux.spec --noconfirm --clean --distpath . --workpath .build_work
if errorlevel 1 ( echo [X] 打包失败 & pause & exit /b 1 )

rd /s /q ".build_work" >nul 2>&1

:: 拷贝配置模板（若 deploy 下尚无）
if not exist ".env.example" copy /y "..\.env.example" ".env.example" >nul

echo.
echo ========================================
echo   [OK] 打包完成: %CD%\faux-server.exe
echo   使用: 双击 start.bat（或直接运行 exe）
echo ========================================
pause
