from datetime import datetime
import alpaca_client as ac
import strategy
import excel_client
import news_client
import config


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


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

    # Full real Alpaca-tradable universe, fetched live -- not a hardcoded
    # watchlist. Crypto is always scanned; stocks only while the market is
    # open (matches the existing off-hours order-placement guard below).
    crypto_symbols = ac.get_tradable_symbols("crypto")
    stock_symbols = ac.get_tradable_symbols("stock") if market_open else []
    log(f"Universe this cycle: {len(stock_symbols)} stocks, {len(crypto_symbols)} crypto pairs.")

    for asset_class, symbols in (("stock", stock_symbols), ("crypto", crypto_symbols)):
        if not symbols:
            continue
        bars_by_symbol = ac.get_bars_batch(symbols, asset_class)
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
        log(f"{len(buy_signals)} raw buy candidate(s) across the scanned universe.")

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

    # --- Stop-loss / take-profit sweep, plus a news-driven protective exit,
    # over whatever remains open ---
    positions = ac.get_positions()
    for symbol, pos in positions.items():
        try:
            asset_class = asset_classes.get(symbol, "crypto" if "/" in symbol or symbol.endswith("USD") else "stock")
            # Regular stock market orders can't execute outside market hours —
            # attempting one just produces a canceled order. Crypto is 24/7.
            if asset_class == "stock" and not market_open:
                continue

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
    log("Bot starting. Universe: full Alpaca-tradable stock + crypto universe, fetched live each cycle.")
    log("Running strategy every 5 minutes...")

    run_bot()  # run immediately on start

    schedule.every(5).minutes.do(run_bot)

    while True:
        schedule.run_pending()
        time.sleep(30)
