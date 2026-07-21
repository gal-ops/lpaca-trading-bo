import os
from datetime import datetime
from openpyxl import Workbook, load_workbook
import config

TRADES_HEADERS = [
    "Timestamp", "Symbol", "Asset Class", "Side", "Qty", "Price", "Value ($)",
    "Reason", "Realized P&L ($)", "Realized P&L (%)", "Order ID",
]


def _ensure_workbook():
    if os.path.exists(config.EXCEL_FILE):
        wb = load_workbook(config.EXCEL_FILE)
    else:
        wb = Workbook()
        wb.remove(wb.active)

    if "Trades" not in wb.sheetnames:
        trades_ws = wb.create_sheet("Trades")
        trades_ws.append(TRADES_HEADERS)
    else:
        trades_ws = wb["Trades"]
        if [c.value for c in trades_ws[1]] != TRADES_HEADERS:
            for col, header in enumerate(TRADES_HEADERS, start=1):
                trades_ws.cell(row=1, column=col, value=header)

    if "Summary" not in wb.sheetnames:
        summary_ws = wb.create_sheet("Summary")
        summary_ws.append(["Metric", "Value"])
        summary_ws.append(["Total Trades", "=COUNTA(Trades!A2:A100000)"])
        summary_ws.append(["Wins", '=COUNTIF(Trades!I2:I100000,">0")'])
        summary_ws.append(["Losses", '=COUNTIF(Trades!I2:I100000,"<0")'])
        summary_ws.append(["Win Rate (%)", "=IFERROR(B3/(B3+B4)*100,0)"])
        summary_ws.append(["Total Realized P&L ($)", "=SUM(Trades!I2:I100000)"])

    return wb


def log_trade(symbol, asset_class, side, qty, price, reason,
               realized_pnl_usd=None, realized_pnl_pct=None, order_id=""):
    wb = _ensure_workbook()
    ws = wb["Trades"]
    value = qty * price
    ws.append([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        symbol,
        asset_class,
        side.upper(),
        qty,
        round(price, 6),
        round(value, 2),
        reason,
        round(realized_pnl_usd, 2) if realized_pnl_usd is not None else None,
        round(realized_pnl_pct, 4) if realized_pnl_pct is not None else None,
        order_id,
    ])
    wb.save(config.EXCEL_FILE)
