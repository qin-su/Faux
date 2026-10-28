# Smart Faux Server — 独立部署包

打包版 Mock 服务（单文件 exe，目标机器**无需安装 Python**）。

## 文件清单

| 文件 | 说明 |
|------|------|
| `faux-server.exe` | 可执行服务（约 22MB，含全部依赖 + Dashboard；由 `build.bat` 生成，不入库） |
| `start.bat` | 启动器：拉起 exe + 3 秒后自动打开 Dashboard |
| `stop.bat` | 停止器：结束监听端口的进程 |
| `.env` / `.env.example` | 配置文件（首次运行 exe 时自动从模板生成） |
| `build.bat` | 一键重新打包（本机有 Python 时使用） |
| `launcher.py` / `faux.spec` | 打包源文件 |

## 使用

```bat
:: 1. （可选）编辑 .env 修改端口 / MQ 配置
:: 2. 启动
start.bat          :: 或直接双击 faux-server.exe

:: 3. 访问
http://localhost:8081/        :: Dashboard
http://localhost:8081/docs    :: Swagger

:: 4. 停止：关闭窗口 / Ctrl+C，或运行
stop.bat
```

## 运行规则

- `.env`、`faux.db`（SQLite）都生成/保存在 **exe 同目录**，迁移时整个目录拷走即可
- 首次运行若没有 `.env`，自动从 `.env.example` 生成
- 端口由 `.env` 中 `FAUX_PORT` 控制（默认 8081，改后 `start.bat` 打开的浏览器地址自动跟随）
- 控制台窗口显示运行日志，**关闭窗口即停止服务**

## 常见问题

| 现象 | 处理 |
|------|------|
| 启动报 `error while attempting to bind ... 10048` | 端口被占用：改 `.env` 的 `FAUX_PORT`，或用 `stop.bat` / netstat 找到占用来源 |
| 双击 exe 闪退 | 在 cmd 中运行 `faux-server.exe` 查看报错；常见为端口占用或 `.env` 格式错误 |
| 杀毒软件拦截 | PyInstaller onefile 误报属常见现象，加白名单即可 |
| 需要改代码后重打包 | 本机装 Python 3.10+，在 deploy 目录运行 `build.bat` |

## 与源码版的关系

- 功能与本仓库源码版完全一致（同一份代码打包）
- 源码开发用根目录 `run.bat`（支持 --reload）；部署/分发用本目录 exe
