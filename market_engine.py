import yfinance as yf
import pandas as pd
import xlwings as xw
import time
import os

# --- SETTINGS ---
EXCEL_PATH = r"E:\Quant-Bridge\TradingBridge.xlsx"
SYMBOL = "AAPL"  # Change this to whatever ticker you want to track

def get_signals(ticker):
    # Fetch 5-minute data
    stock = yf.Ticker(ticker)
    df = stock.history(period="2d", interval="5m")
    
    if df.empty: return "WAIT", 0.0
    
    close = df['Close'].iloc[-1]
    sma200 = df['Close'].rolling(200).mean().iloc[-1]
    
    # RSI Calculation
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rsi = 100 - (100 / (1 + (gain / loss))).iloc[-1]
    
    # GEX Simulation: Using the strike with highest Open Interest as the 'Wall'
    try:
        opt = stock.option_chain(stock.options[0])
        gex_wall = opt.calls.loc[opt.calls['openInterest'].idxmax()]['strike']
    except:
        gex_wall = sma200 # Fallback

    # Logic: All must align
    if close > sma200 and close > gex_wall and rsi > 50:
        return "LONG", gex_wall
    elif close < sma200 and close < gex_wall and rsi < 50:
        return "SHORT", gex_wall
    else:
        return "WAIT", gex_wall

# --- START BRIDGE ---
print("Connecting to Excel...")
wb = xw.Book(EXCEL_PATH)
sheet = wb.sheets[0]

print(f"Engine Running for {SYMBOL}. Watch your Excel sheet update...")

while True:
    try:
        signal, gex = get_signals(SYMBOL)
        
        # This pushes the data into Excel cells A2, B2, and C2
        sheet.range("A1").value = ["SYMBOL", "SIGNAL", "GEX_WALL"]
        sheet.range("A2").value = [SYMBOL, signal, gex]
        
        print(f"[{time.strftime('%H:%M:%S')}] {SYMBOL}: {signal} | Wall: {gex}")
        time.sleep(20) # Updates every 20 seconds
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(5)