from datetime import datetime, date
from zoneinfo import ZoneInfo
import alpaca_client as ac
import strategy
import excel_client
import news_client
import alert_client
import state_client
import config


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def _now_et():
    return datetime.now(ZoneInfo("America/New_York"))


def _past_et(hhmm) -> bool:
    """True if the current ET time is at/after the given (hour, minute)."""
    now = _now_et()
    h, m = hhmm
    return (now.hour, now.minute) >= (h, m)


def _audit_buy(symbol, qty, price, asset_class, account, deployed_usd,
               exposure_cap_usd, is_fractionable):
    """Pre-trade audit gate. Returns (ok: bool, reason: str). Every buy must
    pass this before an order is submitted -- it re-checks, in one place, that
    the order is well-formed and within every risk limit, so a sizing bug
    upstream can't slip an out-of-policy trade through. Deliberately
    independent of how qty was computed."""
    if qty is None or qty <= 0:
        return False, "non-positive qty"
    if price is None or price <= 0:
        return False, "bad price"
    if not is_fractionable and qty != float(int(qty)):
        return False, "non-fractionable qty not a whole number"
    cost = qty * price
    # small tolerance for float noise on the dollar ceiling
    if cost > config.MAX_POSITION_USD * 1.0001:
        return False, f"position cost ${cost:,.2f} exceeds MAX_POSITION_USD ${config.MAX_POSITION_USD:,.2f}"
    if deployed_usd + cost > exposure_cap_usd * 1.0001:
        return False, f"would exceed exposure cap (${deployed_usd:,.2f}+${cost:,.2f} > ${exposure_cap_usd:,.2f})"
    try:
        buying_power = float(account.buying_power)
    except Exception:
        buying_power = None
    if buying_power is not None and cost > buying_power:
        return False, f"cost ${cost:,.2f} exceeds buying power ${buying_power:,.2f}"
    return True, "ok"


def _liquidate(symbol, pos, asset_class, reason, market_open, extended_hours_open, last_prices):
    """Close a single position now and log/alert it. Used by the intraday
    flatten and the account-drawdown stop. Returns realized P&L (or None)."""
    try:
        entry_price = float(pos.avg_entry_price)
        use_extended = asset_class == "stock" and not market_open and extended_hours_open
        ref = last_prices.get(symbol)
        if ref is None:
            ref = float(getattr(pos, "current_price", 0) or 0)
        if use_extended:
            order = ac.close_position(symbol, extended_hours=True,
                                       qty=abs(float(pos.qty)), ref_price=ref)
        else:
            order = ac.close_position(symbol)
        filled = ac.wait_for_fill(order.id)
        if filled.filled_avg_price is None:
            log(f"{symbol}: {reason} but close order did not fill (status={filled.status}), not logging.")
            return None
        exit_price = float(filled.filled_avg_price)
        qty = float(filled.filled_qty)
        pnl_usd = (exit_price - entry_price) * qty
        pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0
        log(f"{reason.upper()}: SELL {qty} {symbol} @ ${exit_price:.4f} | "
            f"P&L=${pnl_usd:,.2f} ({pnl_pct:.2f}%) | order_id={order.id}")
        excel_client.log_trade(symbol, asset_class, "sell", qty, exit_price,
                               reason, pnl_usd, pnl_pct, str(order.id))
        alert_client.send(f"SELL {symbol} ({reason})",
                          f"Sold {qty} {symbol} @ ${exit_price:.4f}\nP&L ${pnl_usd:,.2f} ({pnl_pct:.2f}%)\nReason: {reason}")
        return pnl_usd
    except Exception as e:
        log(f"{symbol}: liquidation error — {e}")
        return None


def run_bot():
    market_open = ac.is_market_open()
    extended_hours_open = ac.is_extended_hours_session()
    if not market_open and extended_hours_open:
        log("Stock market is in extended hours — stock orders will use LIMIT "
            "orders, not market orders (Alpaca requirement outside core session).")
    elif not market_open:
        log("Stock market is closed (outside core and extended hours) — evaluating crypto only.")

    account = ac.get_account()
    positions = ac.get_positions()
    num_positions = len(positions)

    log(f"Account equity: ${float(account.equity):,.2f} | "
        f"Buying power: ${float(account.buying_power):,.2f} | "
        f"Open positions: {num_positions}")

    # Account-level daily-loss kill switch: stop opening NEW positions once
    # today's P&L drops past EITHER the percentage OR the dollar threshold
    # (whichever trips first), using Alpaca's own last_equity (yesterday's
    # close) as the baseline -- no state needs tracking across the bot's
    # stateless runs. Existing positions still get managed normally below
    # (stop-loss/take-profit/news exits keep running) -- this only blocks
    # adding new risk on a bad day.
    equity = float(account.equity)
    last_equity = float(account.last_equity)
    daily_pnl_usd = equity - last_equity
    daily_pnl_pct = daily_pnl_usd / last_equity if last_equity else 0.0
    daily_loss_breached = (daily_pnl_pct <= -config.MAX_DAILY_LOSS_PCT
                           or daily_pnl_usd <= -config.MAX_DAILY_LOSS_USD)
    log(f"Daily P&L: {daily_pnl_pct*100:+.2f}% (${daily_pnl_usd:,.2f}) vs yesterday's close "
        f"(limits: -{config.MAX_DAILY_LOSS_PCT*100:.1f}% / -${config.MAX_DAILY_LOSS_USD:,.0f})"
        + (" — DAILY LOSS LIMIT HIT, no new positions this cycle." if daily_loss_breached else ""))

    signals = {}
    asset_classes = {}
    last_prices = {}
    fractionable = {}

    # ---- Account-level drawdown stop (high-water mark, persisted) ----
    today_et = _now_et().date().isoformat()
    st = state_client.load()
    hwm = max(float(st.get("high_water_mark", 0.0)), equity)
    drawdown_pct = (equity - hwm) / hwm if hwm else 0.0
    already_halted_today = st.get("halt_date") == today_et
    drawdown_triggered = (config.ACCOUNT_DRAWDOWN_STOP_PCT > 0
                          and drawdown_pct <= -config.ACCOUNT_DRAWDOWN_STOP_PCT
                          and not already_halted_today)
    log(f"Account high-water mark: ${hwm:,.2f} | drawdown {drawdown_pct*100:+.2f}% "
        f"(stop at -{config.ACCOUNT_DRAWDOWN_STOP_PCT*100:.0f}%)")

    if drawdown_triggered:
        log(f"ACCOUNT DRAWDOWN STOP FIRED: {drawdown_pct*100:.2f}% below high-water mark "
            f"— liquidating entire book to cash, resuming next day.")
        alert_client.send("ACCOUNT DRAWDOWN STOP",
                          f"Equity ${equity:,.2f} is {drawdown_pct*100:.2f}% below high-water mark "
                          f"${hwm:,.2f}. Liquidating all positions to cash; trading resumes next day.")
        for sym, pos in list(positions.items()):
            acl = "crypto" if ("/" in sym or sym.endswith("USD")) else "stock"
            if acl == "stock" and not market_open and not extended_hours_open:
                continue  # can't sell stocks with the market fully closed; next session will
            _liquidate(sym, pos, acl, "account drawdown stop", market_open, extended_hours_open, last_prices)
        fresh = ac.get_account()
        st["high_water_mark"] = float(fresh.equity)  # reset baseline post-liquidation
        st["halt_date"] = today_et
        state_client.save(st)
        positions = ac.get_positions()
        num_positions = len(positions)
    else:
        st["high_water_mark"] = hwm  # persist the rolling maximum
        state_client.save(st)

    # ---- Intraday flatten / late-day gate (stocks only; crypto is 24/7-exempt) ----
    flatten_stocks_now = (config.INTRADAY_FLATTEN_STOCKS and market_open
                          and _past_et(config.FLATTEN_BEFORE_CLOSE_ET))
    block_stock_new = market_open and _past_et(config.STOP_NEW_STOCK_BUYS_AFTER_ET)
    block_new_entries = daily_loss_breached or drawdown_triggered or already_halted_today

    # Full real Alpaca-tradable universe, fetched live -- not a hardcoded
    # watchlist. Crypto is always scanned; stocks are scanned during core OR
    # extended hours (order placement below branches on which one applies).
    stock_session_open = market_open or extended_hours_open
    crypto_symbols = ac.get_tradable_symbols("crypto")
    stock_symbols = ac.get_tradable_symbols("stock") if stock_session_open else {}
    fractionable.update(crypto_symbols)
    fractionable.update(stock_symbols)
    log(f"Universe this cycle: {len(stock_symbols)} stocks, {len(crypto_symbols)} crypto pairs.")

    for asset_class, symbols in (("stock", stock_symbols), ("crypto", crypto_symbols)):
        if not symbols:
            continue
        bars_by_symbol = ac.get_bars_batch(list(symbols), asset_class)
        buys = sells = 0
        for symbol in symbols:
            bars = bars_by_symbol.get(symbol)
            if not bars:
                continue
            try:
                result = strategy.compute_signals(bars)
            except Exception as e:
                log(f"{symbol}: error evaluating — {e}")
                continue
            asset_classes[symbol] = asset_class
            last_prices[symbol] = bars[-1].close
            if result["signal"] == "hold":
                continue  # don't flood the log with thousands of holds
            signals[symbol] = result
            buys += result["signal"] == "buy"
            sells += result["signal"] == "sell"
            log(f"{symbol} ({asset_class}): signal={result['signal']} | {result['reason']}")
        log(f"{asset_class}: {len(bars_by_symbol)} symbols had bar data, "
            f"{buys} buy signal(s), {sells} sell signal(s).")

    # --- SELL first, to free up capital/slots before considering buys ---
    for symbol, result in list(signals.items()):
        if result["signal"] != "sell" or symbol not in positions:
            continue
        try:
            pos = positions[symbol]
            entry_price = float(pos.avg_entry_price)
            asset_class = asset_classes[symbol]

            if asset_class == "stock" and not market_open and extended_hours_open:
                order = ac.close_position(symbol, extended_hours=True,
                                           qty=abs(float(pos.qty)), ref_price=last_prices[symbol])
            else:
                order = ac.close_position(symbol)
            filled = ac.wait_for_fill(order.id)
            if filled.filled_avg_price is None:
                log(f"{symbol}: sell order did not fill (status={filled.status}), not logging.")
                continue
            exit_price = float(filled.filled_avg_price)
            qty = float(filled.filled_qty)

            pnl_usd = (exit_price - entry_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0

            log(f"SELL {qty} {symbol} @ ${exit_price:.4f} | P&L=${pnl_usd:,.2f} ({pnl_pct:.2f}%) | order_id={order.id}")
            excel_client.log_trade(
                symbol, asset_classes[symbol], "sell", qty, exit_price,
                result["reason"], pnl_usd, pnl_pct, str(order.id),
            )
            alert_client.send(f"SELL {symbol} (signal)",
                              f"Sold {qty} {symbol} @ ${exit_price:.4f}\n"
                              f"P&L ${pnl_usd:,.2f} ({pnl_pct:.2f}%)\n{result['reason']}")
            num_positions -= 1
        except Exception as e:
            log(f"{symbol}: sell error — {e}")

    # --- BUY: research news, rank competing candidates, fill available slots ---
    available_slots = config.MAX_POSITIONS - num_positions
    if available_slots > 0 and block_new_entries:
        why = []
        if daily_loss_breached:
            why.append("daily-loss limit")
        if drawdown_triggered or already_halted_today:
            why.append("account drawdown stop")
        log("Skipping all new entries this cycle: " + ", ".join(why) + ".")
    if available_slots > 0 and not block_new_entries:
        buy_signals = {s: r for s, r in signals.items()
                        if s not in positions and r["signal"] == "buy"}
        log(f"{len(buy_signals)} raw buy candidate(s) across the scanned universe.")

        # Total-exposure ceiling: sum of current position market values, and
        # the hard cap it may not exceed. Enforced per-buy below so the bot
        # always leaves a cash cushion instead of deploying the whole account
        # into one correlated basket (the cause of the 2026-07-22 drawdown).
        deployed_usd = sum(abs(float(getattr(p, "market_value", 0) or 0)) for p in positions.values())
        exposure_cap_usd = equity * config.MAX_TOTAL_EXPOSURE_PCT
        log(f"Current exposure: ${deployed_usd:,.0f} of ${exposure_cap_usd:,.0f} cap "
            f"({config.MAX_TOTAL_EXPOSURE_PCT*100:.0f}% of equity).")

        # Pre-rank by technical score and cap before spending a news-API
        # call per candidate -- bounds news volume even on a market-wide
        # rally with hundreds of simultaneous signals, while staying far
        # larger than MAX_POSITIONS so selection quality isn't compromised.
        top_candidates = dict(
            sorted(buy_signals.items(), key=lambda kv: kv[1]["score"], reverse=True)
            [:config.NEWS_CANDIDATE_CAP]
        )

        for symbol, result in top_candidates.items():
            news = news_client.get_news_sentiment(symbol)
            result["news_score"] = news["score"]
            result["score"] += news["score"] * 2
            result["reason"] += f'; news: "{news["headline"]}" (sentiment={news["score"]:+d})'
            log(f"{symbol}: news sentiment={news['score']:+d} | \"{news['headline']}\"")

        top_candidates = {s: r for s, r in top_candidates.items() if r["news_score"] > config.NEWS_VETO_SCORE}

        ranked = strategy.rank_candidates(top_candidates, available_slots)
        for symbol in ranked:
            try:
                result = signals[symbol]
                price = last_prices[symbol]
                asset_class = asset_classes[symbol]
                # Intraday: no new STOCK entries late in the day (they'd only
                # be flattened at the close). Crypto is exempt (24/7).
                if asset_class == "stock" and block_stock_new:
                    log(f"{symbol}: past intraday stock-entry cutoff, skipping new stock buy.")
                    continue
                qty = strategy.calculate_qty(account, price, asset_class)
                # Absolute-dollar ceiling per position, on top of the % sizing.
                if price > 0 and qty * price > config.MAX_POSITION_USD:
                    qty = config.MAX_POSITION_USD / price
                if not fractionable.get(symbol, True):
                    # Some equities don't support fractional shares -- floor to
                    # a whole share (confirmed live: IRE/CALC/CRMX/RIOX failed).
                    qty = float(int(qty))

                # Pre-trade audit gate -- the mandated "double-check every
                # parameter before the trade" step. Independent re-validation
                # of qty, price, fractionability, dollar cap, exposure cap and
                # buying power. If ANY check fails, the order is not submitted.
                ok, why = _audit_buy(symbol, qty, price, asset_class, account,
                                     deployed_usd, exposure_cap_usd, fractionable.get(symbol, True))
                if not ok:
                    log(f"{symbol}: pre-trade audit blocked buy — {why}.")
                    if "exposure cap" in why:
                        break  # book is full; stop scanning further candidates
                    continue
                order_cost = qty * price
                if asset_class == "stock" and not market_open and extended_hours_open:
                    order = ac.place_market_order(symbol, qty, "buy", asset_class,
                                                    extended_hours=True, ref_price=price)
                else:
                    order = ac.place_market_order(symbol, qty, "buy", asset_class)
                filled = ac.wait_for_fill(order.id)
                if filled.filled_avg_price is None:
                    log(f"{symbol}: buy order did not fill (status={filled.status}), not logging.")
                    continue
                fill_price = float(filled.filled_avg_price)
                fill_qty = float(filled.filled_qty)
                log(f"BUY {fill_qty} {symbol} @ ${fill_price:.4f} | order_id={order.id}")
                excel_client.log_trade(
                    symbol, asset_classes[symbol], "buy", fill_qty, fill_price,
                    result["reason"], order_id=str(order.id),
                )
                alert_client.send(f"BUY {symbol}",
                                  f"Bought {fill_qty} {symbol} @ ${fill_price:.4f} "
                                  f"(${fill_qty*fill_price:,.2f})\n{result['reason']}")
                num_positions += 1
                deployed_usd += fill_qty * fill_price  # keep exposure running total current
                account = ac.get_account()  # refresh buying power for next candidate
            except Exception as e:
                log(f"{symbol}: buy error — {e}")

    # --- Intraday flatten: close ALL stock positions before the close so
    # nothing is held overnight (8A). Crypto is exempt (24/7). ---
    if flatten_stocks_now:
        cur = ac.get_positions()
        stock_syms = [s for s in cur if not ("/" in s or s.endswith("USD"))]
        if stock_syms:
            log(f"Intraday flatten: closing {len(stock_syms)} stock position(s) before the close.")
            for s in stock_syms:
                _liquidate(s, cur[s], "stock", "intraday flatten before close",
                           market_open, extended_hours_open, last_prices)

    # --- Stop-loss / take-profit sweep, plus a news-driven protective exit,
    # over whatever remains open ---
    positions = ac.get_positions()
    for symbol, pos in positions.items():
        try:
            asset_class = asset_classes.get(symbol, "crypto" if "/" in symbol or symbol.endswith("USD") else "stock")
            # Stocks can be managed during core OR extended hours now; only
            # skip entirely outside both. Crypto is always 24/7.
            if asset_class == "stock" and not market_open and not extended_hours_open:
                continue
            use_extended = asset_class == "stock" and not market_open and extended_hours_open

            pnl_pct = float(pos.unrealized_plpc)
            sl_pct = strategy.stop_loss_pct(asset_class)
            tp_pct = strategy.take_profit_pct(asset_class)
            reason = None
            if pnl_pct <= -sl_pct:
                reason = f"stop-loss hit at {pnl_pct*100:.2f}%"
            elif pnl_pct >= tp_pct:
                reason = f"take-profit hit at {pnl_pct*100:.2f}%"

            # News research on every held position, not just buy candidates:
            # strongly negative news is an independent protective exit, using
            # the same veto threshold applied on entry. Good news never
            # overrides a stop-loss/take-profit already decided above —
            # safety first, same principle as the buy/sell conflict rule.
            if reason is None:
                news = news_client.get_news_sentiment(symbol)
                log(f"{symbol}: held-position news sentiment={news['score']:+d} | \"{news['headline']}\"")
                if news["score"] <= config.NEWS_VETO_SCORE:
                    reason = f'news turned bearish: "{news["headline"]}" (sentiment={news["score"]:+d})'

            if reason:
                entry_price = float(pos.avg_entry_price)
                if use_extended:
                    order = ac.close_position(symbol, extended_hours=True,
                                               qty=abs(float(pos.qty)), ref_price=float(pos.current_price))
                else:
                    order = ac.close_position(symbol)
                filled = ac.wait_for_fill(order.id)
                if filled.filled_avg_price is None:
                    log(f"{symbol}: {reason} but close order did not fill (status={filled.status}), not logging.")
                    continue
                exit_price = float(filled.filled_avg_price)
                qty = float(filled.filled_qty)

                pnl_usd = (exit_price - entry_price) * qty
                actual_pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0
                log(f"{reason.upper()}: closing {symbol} | P&L=${pnl_usd:,.2f} | order_id={order.id}")
                excel_client.log_trade(
                    symbol, asset_class, "sell", qty, exit_price,
                    reason, pnl_usd, actual_pnl_pct, str(order.id),
                )
                alert_client.send(f"SELL {symbol} ({reason})",
                                  f"Sold {qty} {symbol} @ ${exit_price:.4f}\n"
                                  f"P&L ${pnl_usd:,.2f} ({actual_pnl_pct:.2f}%)\nReason: {reason}")
        except Exception as e:
            log(f"{symbol} position check error: {e}")

    # ---- Full account snapshot, every cycle regardless of whether a trade
    # happened -- the Excel workbook's EquityLog/Positions tabs are the
    # continuous record; Trades only captures fills. ----
    try:
        final_account = ac.get_account()
        final_positions = ac.get_positions()
        excel_client.log_snapshot(final_account, final_positions, asset_classes,
                                  extra={"high_water_mark": st["high_water_mark"]})
    except Exception as e:
        log(f"snapshot logging error: {e}")


def _enforce_paper_live_gate():
    """Refuse to run in a mismatched/unsafe paper-vs-live configuration.
    While LIVE_TRADING is False, a live-looking endpoint in .env must never
    be honored -- alpaca_client already forces paper, and this refuses to
    start so the mismatch is loud rather than silent. Going live is a
    deliberate act: LIVE_TRADING must be explicitly True."""
    endpoint_is_paper = "paper" in (config.BASE_URL or "").lower()
    if config.LIVE_TRADING:
        log("################################################################")
        log("# LIVE_TRADING = True — trading a REAL funded account.         #")
        log("# Real money is at risk. Position/daily-loss dollar caps apply. #")
        log("################################################################")
        if endpoint_is_paper:
            log("NOTE: LIVE_TRADING is True but ALPACA_BASE_URL still points at the "
                "paper endpoint — orders will hit the paper account.")
    else:
        if not endpoint_is_paper:
            raise SystemExit(
                "REFUSING TO START: LIVE_TRADING is False but ALPACA_BASE_URL looks "
                "live (" + str(config.BASE_URL) + "). Set the paper endpoint, or set "
                "LIVE_TRADING = True in config.py deliberately if you truly intend to "
                "trade real money.")
        log("Mode: PAPER trading (LIVE_TRADING = False).")


def test_connection():
    _enforce_paper_live_gate()
    log("Testing Alpaca connection...")
    account = ac.get_account()
    log(f"Connected! Account ID: {account.id}")
    log(f"  Status:        {account.status}")
    log(f"  Equity:        ${float(account.equity):,.2f}")
    log(f"  Cash:          ${float(account.cash):,.2f}")
    log(f"  Buying Power:  ${float(account.buying_power):,.2f}")
    clock = ac.trading_client.get_clock()
    log(f"  Market open:   {clock.is_open}")
    log(f"  Next open:     {clock.next_open}")
    log(f"  Next close:    {clock.next_close}")
    log("Connection test passed.")


if __name__ == "__main__":
    import time
    import schedule

    test_connection()
    log("Bot starting. Universe: full Alpaca-tradable stock + crypto universe, fetched live each cycle.")
    log("Running strategy every 5 minutes...")

    run_bot()  # run immediately on start

    schedule.every(5).minutes.do(run_bot)

    while True:
        schedule.run_pending()
        time.sleep(30)
