@echo off
rem ============================================================
rem  upsbridge Windows 启动脚本（交互运行，配置在 %APPDATA%）
rem  双击即可启动；直接关窗口退出。
rem ============================================================
setlocal
cd /d "%~dp0.."

set "CONF=%APPDATA%\upsbridge\config.json"
set "PORT=28190"

rem 优先 py 启动器，其次 python；都找不到就报错退出
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app\target\upsbridge\upsbridge.py --config "%CONF%" --port %PORT%
  goto :done
)
where python >nul 2>nul
if %errorlevel%==0 (
  python app\target\upsbridge\upsbridge.py --config "%CONF%" --port %PORT%
  goto :done
)
echo [upsbridge] 找不到 Python。请安装 Python 3.11+ 并勾选 "Add python.exe to PATH"。
echo 下载：https://www.python.org/downloads/
pause
exit /b 1

:done
endlocal
