"""Place 3 bracket orders on IBKR paper trading (port 7497)."""
import time
from math import floor
from ib_insync import IB, Stock

ib = IB()
print("Connecting to IBKR paper (port 7497)...")
try:
    ib.connect("127.0.0.1", 7497, clientId=25, timeout=10)
except Exception as e:
    print(f"7497 failed: {e}, trying 4002...")
    ib.connect("127.0.0.1", 4002, clientId=25, timeout=10)

accounts = ib.managedAccounts()
print(f"Account: {accounts}")

# Quick account check
for av in ib.accountSummary():
    if av.tag in ("NetLiquidation", "BuyingPower", "TotalCashValue"):
        print(f"  {av.tag}: ${float(av.value):,.2f}")

# Our 3 trades
trades = [
    {"symbol": "WELL", "stop": 198.62, "target": 227.42},
    {"symbol": "MS",   "stop": 151.58, "target": 193.63},
    {"symbol": "IDXX", "stop": 599.80, "target": 712.05},
]

TARGET_NOTIONAL = 10000

for t in trades:
    symbol = t["symbol"]
    stop = t["stop"]
    target = t["target"]

    contract = Stock(symbol, "SMART", "USD")
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        print(f"\nERROR: Cannot qualify {symbol}")
        continue
    contract = qualified[0]

    # Get current price
    ticker = ib.reqMktData(contract)
    ib.sleep(2)
    price = ticker.marketPrice()
    if price != price or price <= 0:
        price = ticker.close or ticker.last or 0
    ib.cancelMktData(contract)

    if price <= 0:
        print(f"\n{symbol}: No price, skipping")
        continue

    # Sizing: ~$10K per trade
    if price < 10:
        qty = 1000
    else:
        qty = floor(TARGET_NOTIONAL / price)
    qty = max(qty, 1)

    print(f"\n{'='*60}")
    print(f"{symbol}: {qty} shares @ ${price:.2f} (~${qty*price:,.0f})")
    print(f"  MKT entry | stop=${stop:.2f} | target=${target:.2f}")

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

    ib.sleep(2)

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
for o in orders:
    print(f"  {o.action} {o.orderType} qty={o.totalQuantity} lmt={o.lmtPrice} aux={o.auxPrice}")

ib.disconnect()
print("\nDone!")
