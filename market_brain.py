import yfinance as yf
import pandas as pd
import xlwings as xw
import time
import os

# CONFIGURATION
EXCEL_PATH = r"E:\Quant-Bridge\TradingBridge.xlsx"
TICKER = "SPY"

def get_market_data(symbol):
    stock = yf.Ticker(symbol)
    df = stock.history(period="5d", interval="5m")
    
    # Technicals
    close = df['Close'].iloc[-1]
    sma200 = df['Close'].rolling(200).mean().iloc[-1]
    
    # RSI Calculation
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs.iloc[-1]))
    
    # GEX Simulation (Zero Gamma approximation)
    # Pulls the strike with the highest Open Interest as the 'Wall'
    try:
        expiry = stock.options[0]
        opt = stock.option_chain(expiry)
        zero_g = opt.calls.loc[opt.calls['openInterest'].idxmax()]['strike']
    except:
        zero_g = close # Fallback if options data fails
        
    return close, sma200, rsi, zero_g

# Start Excel Bridge
if not os.path.exists(EXCEL_PATH):
    wb = xw.Book()
    wb.save(EXCEL_PATH)
else:
    wb = xw.Book(EXCEL_PATH)

sheet = wb.sheets[0]
print(f"Monitoring {TICKER}... Bridge Active at {EXCEL_PATH}")

while True:
    try:
        price, sma, rsi, gex = get_market_data(TICKER)
        
        # Confluence Logic
        if price > sma and price > gex and rsi > 50:
            signal = "LONG"
        elif price < sma and price < gex and rsi < 50:
            signal = "SHORT"
        else:
            signal = "WAIT"
            
        # Update Excel
        sheet.range("A1:C1").value = ["SYMBOL", "SIGNAL", "GEX_LEVEL"]
        sheet.range("A2").value = TICKER
        sheet.range("B2").value = signal
        sheet.range("C2").value = gex
        
        print(f"Update: {TICKER} | Signal: {signal} | GEX: {gex}")
        time.sleep(60)
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(10)