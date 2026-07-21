from datetime import datetime, timedelta, timezone
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
import config

_news_client = NewsClient(config.API_KEY, config.SECRET_KEY)

POSITIVE_KEYWORDS = [
    "beats", "beat estimates", "beats estimates", "surges", "surge", "soars",
    "rally", "upgrade", "upgraded", "record profit", "record revenue",
    "outperform", "strong demand", "raises guidance", "raises forecast",
    "buyback", "breakthrough", "approval", "approved", "bullish", "expands",
    "partnership", "strong earnings", "tops estimates", "all-time high",
    "acquisition", "beats expectations", "price target raised",
]

NEGATIVE_KEYWORDS = [
    "misses", "miss estimates", "misses estimates", "downgrade", "downgraded",
    "lawsuit", "investigation", "plunge", "plunges", "crash", "crashes",
    "recall", "fraud", "bankruptcy", "layoffs", "cuts guidance",
    "cuts forecast", "bearish", "warning", "probe", "fine", "charges",
    "delisted", "sec charges", "guidance cut", "data breach", "hack",
    "resigns", "scandal", "sell-off", "selloff", "price target cut",
]


def _score_text(text: str) -> int:
    text = text.lower()
    score = 0
    for kw in POSITIVE_KEYWORDS:
        if kw in text:
            score += 1
    for kw in NEGATIVE_KEYWORDS:
        if kw in text:
            score -= 1
    return score


def get_news_sentiment(symbol: str, lookback_hours: int = 48, limit: int = 10) -> dict:
    """Free keyword-based sentiment over recent headlines.
    Returns {'score': int, 'headline': str} — score > 0 is bullish, < 0 is
    bearish, 0 means neutral or no news found."""
    query_symbol = symbol.replace("/", "")  # e.g. "BTC/USD" -> "BTCUSD"
    try:
        request = NewsRequest(
            symbols=query_symbol,
            start=datetime.now(timezone.utc) - timedelta(hours=lookback_hours),
            limit=limit,
            exclude_contentless=True,
        )
        newsset = _news_client.get_news(request)
        articles = newsset.data.get("news", [])
    except Exception:
        return {"score": 0, "headline": "news lookup failed"}

    if not articles:
        return {"score": 0, "headline": "no recent news"}

    total = 0
    top_headline = articles[0].headline
    top_abs = -1
    for article in articles:
        text = f"{article.headline} {article.summary or ''}"
        s = _score_text(text)
        total += s
        if abs(s) > top_abs:
            top_abs = abs(s)
            top_headline = article.headline

    return {"score": total, "headline": top_headline}
