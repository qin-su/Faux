"""Smart Faux Server — 独立 exe 启动入口（PyInstaller 打包用）。

打包/运行规则：
- .env、faux.db、日志均落在 exe 同目录（源码运行时为项目根目录）
- 首次运行若 .env 不存在，自动从同目录 .env.example 生成
- 未配置 FAUX_DB_PATH 时，数据库固定落在 exe 目录（避免 onefile 临时解压目录导致数据丢失）
"""

import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    """exe 所在目录（打包后）；源码运行时为项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def main() -> None:
    base = _base_dir()
    os.chdir(base)                     # 相对路径（./faux.db）落到 exe 目录
    sys.path.insert(0, str(base))      # 源码方式运行时保证 app 包可导入

    # 1) 必须在 import app.* 之前加载 .env（app.config 在 import 时读取环境变量）
    from dotenv import load_dotenv

    env_file = base / ".env"
    if not env_file.exists() and (base / ".env.example").exists():
        import shutil
        shutil.copy(base / ".env.example", env_file)
        print(f"[Smart Faux] 首次运行：已从 .env.example 生成 {env_file}")
    load_dotenv(env_file)

    # 2) 数据库默认落在 exe 目录（.env 显式配置优先）
    os.environ.setdefault("FAUX_DB_PATH", str(base / "faux.db"))

    # 3) 导入应用并启动（此时 app.config 读到正确配置）
    import uvicorn
    from app import config
    from app.main import app

    host = os.getenv("FAUX_HOST", "0.0.0.0")
    port = config.FAUX_PORT

    print("=" * 50)
    print("  Smart Faux Server (standalone)")
    print(f"  Dashboard : http://localhost:{port}/")
    print(f"  Swagger   : http://localhost:{port}/docs")
    print(f"  配置文件  : {env_file}")
    print(f"  SQLite    : {config.FAUX_DB_PATH}")
    print("  停止      : Ctrl+C 或关闭窗口")
    print("=" * 50)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
