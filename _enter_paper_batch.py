"""Place 10 new bracket orders on IBKR paper trading (port 7497)."""
import time
from math import floor
from ib_insync import IB, Stock

trades = [
    {"symbol": "AMAT", "qty": 28, "stop": 321.46, "target": 448.52},
    {"symbol": "VRT",  "qty": 40, "stop": 220.18, "target": 329.03},
    {"symbol": "STX",  "qty": 27, "stop": 319.16, "target": 514.65},
    {"symbol": "STT",  "qty": 80, "stop": 117.46, "target": 149.24},
    {"symbol": "JCI",  "qty": 72, "stop": 132.06, "target": 159.86},
    {"symbol": "OMC",  "qty": 117, "stop": 78.62, "target": 103.75},
    {"symbol": "JNJ",  "qty": 41, "stop": 237.48, "target": 264.85},
    {"symbol": "PEP",  "qty": 62, "stop": 156.37, "target": 182.81},
    {"symbol": "LMT",  "qty": 16, "stop": 625.73, "target": 761.36},
    {"symbol": "XOM",  "qty": 67, "stop": 141.32, "target": 171.07},
]

ib = IB()
print("Connecting to IBKR paper (port 7497)...")
try:
    ib.connect("127.0.0.1", 7497, clientId=26, timeout=10)
except Exception as e:
    print(f"7497 failed: {e}, trying 4002...")
    ib.connect("127.0.0.1", 4002, clientId=26, timeout=10)

accounts = ib.managedAccounts()
print(f"Account: {accounts}")
if not any(a.startswith("DU") or a.startswith("DF") for a in accounts):
    print("ERROR: Not a paper account! Aborting.")
    ib.disconnect()
    exit(1)

for t in trades:
    symbol = t["symbol"]
    qty = t["qty"]
    stop = t["stop"]
    target = t["target"]

    contract = Stock(symbol, "SMART", "USD")
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        print(f"\nERROR: Cannot qualify {symbol}")
        continue
    contract = qualified[0]

    print(f"\n{'='*60}")
    print(f"{symbol}: {qty} shares | stop=${stop:.2f} | target=${target:.2f}")

    bracket = ib.bracketOrder(
        action="BUY",
        quantity=qty,
        limitPrice=0,
        takeProfitPrice=round(target, 2),
        stopLossPrice=round(stop, 2),
    )
    parent, tp_order, sl_order = bracket

    parent.orderType = "MKT"
    parent.lmtPrice = 0
    parent.tif = "GTC"
    parent.outsideRth = True
    tp_order.tif = "GTC"
    tp_order.outsideRth = True
    sl_order.tif = "GTC"
    sl_order.outsideRth = True

    for order in bracket:
        ib.placeOrder(contract, order)
        print(f"  Placed: {order.action} {order.orderType} orderId={order.orderId}")

    ib.sleep(1)

# Wait for fills
print(f"\n{'='*60}")
print("Waiting for fills...")
ib.sleep(5)

positions = ib.positions()
print(f"\nPositions ({len(positions)}):")
for p in positions:
    print(f"  {p.contract.symbol}: {p.position} shares @ ${p.avgCost:.2f}")

orders = ib.openOrders()
print(f"\nOpen orders: {len(orders)}")

ib.disconnect()
print("\nDone!")
