@echo off
title QuantBridge Scanner + Dashboard
echo ========================================
echo  QUANT-BRIDGE SCANNER + DASHBOARD
echo  Port 7497, ClientId 20
echo  Close this window to stop everything
echo ========================================
echo.
cd /d E:\Quant-Bridge
python -m signal_scanner --watchlist universe_master --ibkr-port 7497
pause
