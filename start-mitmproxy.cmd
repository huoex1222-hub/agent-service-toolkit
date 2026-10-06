@echo off
REM Start mitmweb chained in front of Clash, to capture the agent service's outbound
REM traffic. Double-click, or run from any terminal. See NETWORK_CAPTURE.md.
REM
REM NOTE: keep this file ASCII-only. cmd.exe parses .cmd files in the system ANSI
REM codepage (GBK on a Chinese Windows), so UTF-8 Chinese text here turns into
REM garbage commands. The Chinese explanation lives in NETWORK_CAPTURE.md.
setlocal

REM uv tool installs its shims under the user profile; add them to PATH
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where mitmweb >nul 2>&1
if errorlevel 1 (
    echo [x] mitmweb not found. Install it first:  uv tool install mitmproxy
    pause
    exit /b 1
)

REM Port guard: 8888 / 8088 in use usually means a mitmweb is already running
netstat -ano | findstr /r /c:":8888 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [x] Port 8888 is already in use - another mitmweb is probably running.
    echo     Close it first, or change the ports here and in .env.
    pause
    exit /b 1
)

echo.
echo   mitmproxy starting ...
echo.
echo     proxy      127.0.0.1:8888   (this is what .env HTTP_PROXY / HTTPS_PROXY point at)
echo     web UI     127.0.0.1:8088
echo     flows      %USERPROFILE%\.mitmproxy\flows.mitm
echo.
echo   BEFORE you stop this program, set the proxy in .env back to 127.0.0.1:7890,
echo   otherwise the agent service cannot reach anything at all.
echo.
echo   Press Ctrl+C to stop.
echo.

mitmweb --mode upstream:http://127.0.0.1:7890 --listen-host 127.0.0.1 --listen-port 8888 --web-host 127.0.0.1 --web-port 8088 --web-open-browser -w "%USERPROFILE%\.mitmproxy\flows.mitm"

echo.
echo mitmweb exited. Remember to point .env back at 127.0.0.1:7890.
pause
