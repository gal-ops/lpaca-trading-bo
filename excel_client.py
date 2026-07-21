import os
from datetime import datetime
from openpyxl import Workbook, load_workbook
from openpyxl.chart import LineChart, BarChart, PieChart, DoughnutChart, Reference
import config

TRADES_HEADERS = [
    "Timestamp", "Symbol", "Asset Class", "Side", "Qty", "Price", "Value ($)",
    "Reason", "Realized P&L ($)", "Realized P&L (%)", "Order ID",
    "Cumulative P&L ($)",
]

# Fixed row ranges used for chart/formula references so they keep working
# as more trades get appended, without having to rebuild charts each run.
MAX_ROWS = 100000
SYMBOL_TABLE_MAX_ROWS = 200
SYMBOL_TABLE_START_ROW = 2


def _ensure_trades_sheet(wb):
    if "Trades" not in wb.sheetnames:
        ws = wb.create_sheet("Trades")
        ws.append(TRADES_HEADERS)
    else:
        ws = wb["Trades"]
        if [c.value for c in ws[1]] != TRADES_HEADERS:
            for col, header in enumerate(TRADES_HEADERS, start=1):
                ws.cell(row=1, column=col, value=header)
    return ws


def _ensure_summary_sheet(wb):
    if "Summary" in wb.sheetnames:
        return wb["Summary"]
    ws = wb.create_sheet("Summary")
    ws.append(["Metric", "Value"])
    ws.append(["Total Trades", f"=COUNTA(Trades!A2:A{MAX_ROWS})"])
    ws.append(["Wins", f'=COUNTIF(Trades!I2:I{MAX_ROWS},">0")'])
    ws.append(["Losses", f'=COUNTIF(Trades!I2:I{MAX_ROWS},"<0")'])
    ws.append(["Win Rate (%)", "=IFERROR(B3/(B3+B4)*100,0)"])
    ws.append(["Total Realized P&L ($)", f"=SUM(Trades!I2:I{MAX_ROWS})"])
    return ws


def _ensure_charts_sheet(wb):
    is_new = "Charts" not in wb.sheetnames
    ws = wb["Charts"] if not is_new else wb.create_sheet("Charts")

    if is_new:
        ws["A1"] = "Symbol"
        ws["B1"] = "Total Realized P&L ($)"
        ws["D1"] = "Asset Class"
        ws["E1"] = "Trade Count"

        # --- Line chart: cumulative P&L trend over every trade ---
        line = LineChart()
        line.title = "Cumulative P&L Over Time"
        line.y_axis.title = "P&L ($)"
        line.x_axis.title = "Trade #"
        line.style = 12
        data = Reference(wb["Trades"], min_col=12, min_row=1, max_row=MAX_ROWS)
        line.add_data(data, titles_from_data=True)
        line.width, line.height = 24, 10
        ws.add_chart(line, "H2")

        # --- Pie chart: win vs loss distribution ---
        pie = PieChart()
        pie.title = "Win / Loss Distribution"
        cats = Reference(wb["Summary"], min_col=1, min_row=3, max_row=4)
        data = Reference(wb["Summary"], min_col=2, min_row=3, max_row=4)
        pie.add_data(data, titles_from_data=False)
        pie.set_categories(cats)
        pie.width, pie.height = 12, 10
        ws.add_chart(pie, "H22")

        # --- Bar chart: realized P&L by symbol ---
        bar = BarChart()
        bar.type = "col"
        bar.title = "Realized P&L by Symbol"
        bar.y_axis.title = "P&L ($)"
        bar.x_axis.title = "Symbol"
        data = Reference(ws, min_col=2, min_row=1, max_row=SYMBOL_TABLE_MAX_ROWS + 1)
        cats = Reference(ws, min_col=1, min_row=2, max_row=SYMBOL_TABLE_MAX_ROWS + 1)
        bar.add_data(data, titles_from_data=True)
        bar.set_categories(cats)
        bar.width, bar.height = 24, 10
        ws.add_chart(bar, "A22")

        # --- Doughnut chart: trades by asset class (stock vs crypto) ---
        donut = DoughnutChart()
        donut.title = "Trades by Asset Class"
        data = Reference(ws, min_col=5, min_row=1, max_row=3)
        cats = Reference(ws, min_col=4, min_row=2, max_row=3)
        donut.add_data(data, titles_from_data=True)
        donut.set_categories(cats)
        donut.width, donut.height = 12, 10
        ws.add_chart(donut, "A40")

    return ws


def _refresh_chart_tables(wb):
    """Recompute the small aggregation tables the bar/doughnut charts read from."""
    trades_ws = wb["Trades"]
    charts_ws = wb["Charts"]

    symbol_pnl = {}
    asset_class_counts = {}
    for row in trades_ws.iter_rows(min_row=2, values_only=True):
        symbol, asset_class, pnl = row[1], row[2], row[8]
        if asset_class:
            asset_class_counts[asset_class] = asset_class_counts.get(asset_class, 0) + 1
        if pnl not in (None, ""):
            symbol_pnl[symbol] = symbol_pnl.get(symbol, 0) + float(pnl)

    for r in range(SYMBOL_TABLE_START_ROW, SYMBOL_TABLE_START_ROW + SYMBOL_TABLE_MAX_ROWS):
        charts_ws.cell(row=r, column=1, value=None)
        charts_ws.cell(row=r, column=2, value=None)

    for i, (symbol, total) in enumerate(sorted(symbol_pnl.items(), key=lambda kv: -kv[1])):
        if i >= SYMBOL_TABLE_MAX_ROWS:
            break
        r = SYMBOL_TABLE_START_ROW + i
        charts_ws.cell(row=r, column=1, value=symbol)
        charts_ws.cell(row=r, column=2, value=round(total, 2))

    for i, asset_class in enumerate(["stock", "crypto"]):
        charts_ws.cell(row=2 + i, column=4, value=asset_class)
        charts_ws.cell(row=2 + i, column=5, value=asset_class_counts.get(asset_class, 0))


def _ensure_workbook():
    if os.path.exists(config.EXCEL_FILE):
        wb = load_workbook(config.EXCEL_FILE)
    else:
        wb = Workbook()
        wb.remove(wb.active)

    _ensure_trades_sheet(wb)
    _ensure_summary_sheet(wb)
    _ensure_charts_sheet(wb)
    return wb


def log_trade(symbol, asset_class, side, qty, price, reason,
               realized_pnl_usd=None, realized_pnl_pct=None, order_id=""):
    wb = _ensure_workbook()
    ws = wb["Trades"]
    value = qty * price

    prev_cumulative = ws.cell(row=ws.max_row, column=12).value if ws.max_row >= 2 else 0
    cumulative = (prev_cumulative or 0) + (realized_pnl_usd or 0)

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
        round(cumulative, 2),
    ])

    _refresh_chart_tables(wb)
    wb.save(config.EXCEL_FILE)
