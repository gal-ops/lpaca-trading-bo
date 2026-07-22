from datetime import datetime
import alpaca_client as ac
import strategy
import excel_client
import news_client
import config


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def _universe(market_open: bool):
    """Yields (symbol, asset_class) pairs to evaluate this cycle."""
    if market_open:
        for symbol in config.STOCK_WATCHLIST:
            yield symbol, "stock"
    for symbol in config.CRYPTO_WATCHLIST:
        yield symbol, "crypto"


def run_bot():
    market_open = ac.is_market_open()
    if not market_open:
        log("Stock market is closed — evaluating crypto only.")

    account = ac.get_account()
    positions = ac.get_positions()
    num_positions = len(positions)

    log(f"Account equity: ${float(account.equity):,.2f} | "
        f"Buying power: ${float(account.buying_power):,.2f} | "
        f"Open positions: {num_positions}")

    signals = {}
    asset_classes = {}
    last_prices = {}

    for symbol, asset_class in _universe(market_open):
        try:
            bars = ac.get_bars(symbol, asset_class)
            if not bars:
                log(f"{symbol}: no bar data, skipping.")
                continue
            result = strategy.compute_signals(bars)
            signals[symbol] = result
            asset_classes[symbol] = asset_class
            last_prices[symbol] = bars[-1].close
            log(f"{symbol} ({asset_class}): signal={result['signal']} | {result['reason']}")
        except Exception as e:
            log(f"{symbol}: error fetching/evaluating — {e}")

    # --- SELL first, to free up capital/slots before considering buys ---
    for symbol, result in list(signals.items()):
        if result["signal"] != "sell" or symbol not in positions:
            continue
        try:
            pos = positions[symbol]
            entry_price = float(pos.avg_entry_price)

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
            num_positions -= 1
        except Exception as e:
            log(f"{symbol}: sell error — {e}")

    # --- BUY: research news, rank competing candidates, fill available slots ---
    available_slots = config.MAX_POSITIONS - num_positions
    if available_slots > 0:
        buy_signals = {s: r for s, r in signals.items()
                        if s not in positions and r["signal"] == "buy"}

        # Free news research: fold headline sentiment into each candidate's
        # score, and veto candidates with clearly bad news even if the
        # technical signal says buy.
        for symbol, result in buy_signals.items():
            news = news_client.get_news_sentiment(symbol)
            result["news_score"] = news["score"]
            result["score"] += news["score"] * 2
            result["reason"] += f'; news: "{news["headline"]}" (sentiment={news["score"]:+d})'
            log(f"{symbol}: news sentiment={news['score']:+d} | \"{news['headline']}\"")

        buy_signals = {s: r for s, r in buy_signals.items() if r["news_score"] > -2}

        ranked = strategy.rank_candidates(buy_signals, available_slots)
        for symbol in ranked:
            try:
                result = signals[symbol]
                price = last_prices[symbol]
                qty = strategy.calculate_qty(account, price, asset_classes[symbol])
                if qty <= 0.000001:
                    log(f"{symbol}: insufficient buying power, skipping.")
                    continue
                order = ac.place_market_order(symbol, qty, "buy", asset_classes[symbol])
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
                num_positions += 1
                account = ac.get_account()  # refresh buying power for next candidate
            except Exception as e:
                log(f"{symbol}: buy error — {e}")

    # --- Stop-loss / take-profit sweep over whatever remains open ---
    positions = ac.get_positions()
    for symbol, pos in positions.items():
        try:
            asset_class = asset_classes.get(symbol, "crypto" if "/" in symbol or symbol.endswith("USD") else "stock")
            # Regular stock market orders can't execute outside market hours —
            # attempting one just produces a canceled order. Crypto is 24/7.
            if asset_class == "stock" and not market_open:
                continue

            pnl_pct = float(pos.unrealized_plpc)
            reason = None
            if pnl_pct <= -config.STOP_LOSS_PCT:
                reason = f"stop-loss hit at {pnl_pct*100:.2f}%"
            elif pnl_pct >= config.TAKE_PROFIT_PCT:
                reason = f"take-profit hit at {pnl_pct*100:.2f}%"

            if reason:
                entry_price = float(pos.avg_entry_price)
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
        except Exception as e:
            log(f"{symbol} position check error: {e}")


def test_connection():
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
    log(f"Bot starting. Stocks: {', '.join(config.STOCK_WATCHLIST)}")
    log(f"Crypto (24/7): {', '.join(config.CRYPTO_WATCHLIST)}")
    log("Running strategy every 5 minutes...")

    run_bot()  # run immediately on start

    schedule.every(5).minutes.do(run_bot)

    while True:
        schedule.run_pending()
        time.sleep(30)
