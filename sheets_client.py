import json
import gspread
from google.oauth2.service_account import Credentials
import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TRADES_HEADERS = [
    "Timestamp", "Symbol", "Asset Class", "Side", "Qty", "Price", "Value ($)",
    "Reason", "Realized P&L ($)", "Realized P&L (%)", "Order ID",
]

_client = None
_sheet = None


def _get_sheet():
    global _client, _sheet
    if _sheet is not None:
        return _sheet

    creds_info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    _client = gspread.authorize(creds)
    _sheet = _client.open_by_key(config.GOOGLE_SHEET_ID)
    _ensure_tabs(_sheet)
    return _sheet


def _ensure_tabs(sheet):
    try:
        trades_ws = sheet.worksheet("Trades")
    except gspread.WorksheetNotFound:
        trades_ws = sheet.add_worksheet("Trades", rows=1000, cols=len(TRADES_HEADERS))
        trades_ws.append_row(TRADES_HEADERS)

    if trades_ws.row_values(1) != TRADES_HEADERS:
        trades_ws.update("A1", [TRADES_HEADERS])

    try:
        sheet.worksheet("Summary")
    except gspread.WorksheetNotFound:
        summary_ws = sheet.add_worksheet("Summary", rows=10, cols=2)
        summary_ws.update("A1", [
            ["Metric", "Value"],
            ["Total Trades", "=COUNTA(Trades!A2:A)"],
            ["Wins", "=COUNTIF(Trades!I2:I, \">0\")"],
            ["Losses", "=COUNTIF(Trades!I2:I, \"<0\")"],
            ["Win Rate (%)", "=IFERROR(B3/(B3+B4)*100, 0)"],
            ["Total Realized P&L ($)", "=SUM(Trades!I2:I)"],
        ])


def log_trade(symbol, asset_class, side, qty, price, reason,
               realized_pnl_usd=None, realized_pnl_pct=None, order_id=""):
    from datetime import datetime
    sheet = _get_sheet()
    ws = sheet.worksheet("Trades")
    value = qty * price
    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        symbol,
        asset_class,
        side.upper(),
        qty,
        round(price, 6),
        round(value, 2),
        reason,
        round(realized_pnl_usd, 2) if realized_pnl_usd is not None else "",
        round(realized_pnl_pct, 4) if realized_pnl_pct is not None else "",
        order_id,
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
