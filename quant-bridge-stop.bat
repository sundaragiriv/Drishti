@echo off
REM ============================================================================
REM  QUANT-BRIDGE — clean teardown. Stops scanner+dashboard.
REM  Does not touch EOD pipeline if it's running.
REM ============================================================================

title Quant-Bridge Stop
echo.
echo  Stopping Quant-Bridge scanner+dashboard...
echo.

powershell -NoProfile -Command ^
    "$found = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'signal_scanner.main|run_dashboard' };" ^
    "if ($found) {" ^
    "  $found | ForEach-Object { Write-Output \"  Stopping PID $($_.ProcessId): $($_.CommandLine.Substring(0, [Math]::Min(80, $_.CommandLine.Length)))...\"; Stop-Process -Id $_.ProcessId -Force -Confirm:$false }" ^
    "} else { Write-Output '  Nothing to stop.' }"

echo.
echo  Done. (EOD pipeline, if running, left alone.)
echo.
timeout /t 3 /nobreak >nul
