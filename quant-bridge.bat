@echo off
REM ============================================================================
REM  QUANT-BRIDGE — single-click launcher.
REM  Brings up scanner + dashboard, opens the browser, prints instructions.
REM  Designed for hands-off operation during the 60-day prove-it window.
REM ============================================================================

setlocal enabledelayedexpansion
title Quant-Bridge Launcher
cd /d "%~dp0"

echo.
echo  ============================================================
echo   QUANT-BRIDGE                Day 0 hands-off launcher
echo  ============================================================
echo.

REM --- Python (project venv) check ----------------------------------------
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo  [FATAL] venv python not found at %PY%
    echo          Rebuild it:  py -3.12 -m venv .venv ^&^& .venv\Scripts\python -m pip install -e .
    pause
    exit /b 1
)

REM --- Optional: warn if network is down ----------------------------------
echo  [1/4] Probing network...
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'https://api.polygon.io/v3/reference/exchanges' -UseBasicParsing -TimeoutSec 5).StatusCode | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
    echo  [WARN] Network/HTTPS blocked. Likely Norton VPN ^/ Firewall.
    echo         Premarket data top-up will be SKIPPED.
    echo         FIX: Open Norton, disconnect VPN, then this launcher
    echo              can run premarket fresh on next bounce.
    set NETWORK_OK=0
) else (
    echo         Network OK.
    set NETWORK_OK=1
)

REM --- Optional: premarket if network is up and data is stale -------------
if "!NETWORK_OK!"=="1" (
    echo  [2/4] Running premarket data check ^(quiet^)...
    "%PY%" run_premarket.py --prices-only >logs\premarket_launcher.log 2>^&1
    if errorlevel 1 (
        echo         Premarket finished with warnings ^(see logs\premarket_launcher.log^).
    ) else (
        echo         Premarket OK.
    )
) else (
    echo  [2/4] Skipped premarket ^(no network^).
)

REM --- Kill any stale scanner/dashboard processes -------------------------
echo  [3/4] Stopping any stale instances...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'signal_scanner.main|run_dashboard' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }" >nul 2>nul
timeout /t 2 /nobreak >nul

REM --- Launch scanner + dashboard in its own visible window ---------------
echo  [4/4] Launching scanner + dashboard ^(new window, logs visible^)...
start "Quant-Bridge Scanner+Dashboard" powershell -NoExit -Command "cd '%CD%'; & '%PY%' -u -m signal_scanner.main --watchlist universe_master --ibkr-port 7497 --ignore-orphans"

REM --- Wait for dashboard, then open browser ------------------------------
echo         Waiting for dashboard to come up...
set /a tries=0
:waitloop
set /a tries+=1
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:8050/' -UseBasicParsing -TimeoutSec 2).StatusCode | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
    if !tries! geq 30 (
        echo         [WARN] Dashboard not responding after 60s. Check the scanner window.
        goto done
    )
    timeout /t 2 /nobreak >nul
    goto waitloop
)
echo         Dashboard live.

REM --- Open default browser ------------------------------------------------
start "" http://127.0.0.1:8050

:done
echo.
echo  ============================================================
echo   READY.  Dashboard: http://127.0.0.1:8050
echo   IBKR:   Start TWS/Gateway when ready — scanner auto-connects.
echo   STOP:   Run quant-bridge-stop.bat to shut down cleanly.
echo  ============================================================
echo.
echo  This window can be closed — the scanner runs in its own window.
echo.
pause
