"""
News Sentinel Sub-Bot -- Real-Time Market News & Sentiment Sourcing
===================================================================
Autonomous sub-bot that continuously scans news sources for market-moving
information related to Bitcoin and prediction market events.

Sources:
- CoinGecko API (free, no key) -- market data, trending coins
- CryptoCompare News API (free tier) -- crypto news feed
- Hacker News Algolia API (free) -- tech/crypto community sentiment
- Coinbase product stats -- volume/momentum changes
- Fear & Greed Index -- market sentiment gauge

Feeds processed intelligence into the CryptoMomentumEngine and
ResearchEngine for Bayesian probability updates.

Reusable: import NewsSentinel for any news-driven trading bot.
"""

import json
import time
import math
import logging
import threading
import urllib.request
import urllib.error
from typing import Optional
from collections import deque

logger = logging.getLogger(__name__)


class NewsItem:
    """Normalized news item from any source."""
    __slots__ = ("source", "title", "url", "timestamp", "sentiment_score", "relevance", "tags", "raw")

    def __init__(self, source: str, title: str, url: str = "", timestamp: float = 0,
                 sentiment_score: float = 0, relevance: float = 0, tags: list = None, raw: dict = None):
        self.source = source
        self.title = title
        self.url = url
        self.timestamp = timestamp or time.time()
        self.sentiment_score = sentiment_score  # -1 (bearish) to +1 (bullish)
        self.relevance = relevance              # 0-1 how relevant to our markets
        self.tags = tags or []
        self.raw = raw or {}

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "timestamp": self.timestamp,
            "sentiment": self.sentiment_score,
            "relevance": self.relevance,
            "tags": self.tags,
        }


class SentimentAnalyzer:
    """
    Lightweight keyword-based sentiment scoring.
    Designed for speed -- runs inline with no ML dependencies.

    For production: swap with a real NLP model (VADER, FinBERT, etc.)
    """

    BULLISH_KEYWORDS = {
        # Strong bullish
        "surge": 0.8, "soar": 0.8, "rally": 0.7, "breakout": 0.7,
        "all-time high": 0.9, "ath": 0.9, "moon": 0.6, "pump": 0.5,
        "bull run": 0.8, "adoption": 0.6, "institutional": 0.5,
        "approved": 0.7, "bullish": 0.8, "upgrade": 0.5,
        # Moderate bullish
        "gain": 0.4, "rise": 0.3, "up": 0.2, "growth": 0.4,
        "recovery": 0.4, "support": 0.3, "accumulate": 0.5,
        "buy": 0.3, "long": 0.3, "positive": 0.3,
    }

    BEARISH_KEYWORDS = {
        # Strong bearish
        "crash": -0.8, "plunge": -0.8, "dump": -0.7, "collapse": -0.8,
        "ban": -0.7, "hack": -0.7, "fraud": -0.8, "scam": -0.7,
        "liquidat": -0.6, "bankrupt": -0.8, "bearish": -0.8,
        # Moderate bearish
        "drop": -0.4, "fall": -0.3, "decline": -0.4, "loss": -0.3,
        "sell": -0.3, "short": -0.3, "fear": -0.5, "risk": -0.3,
        "regulation": -0.3, "crackdown": -0.6, "reject": -0.5,
    }

    @staticmethod
    def score_text(text: str) -> float:
        """
        Score text sentiment from -1 (bearish) to +1 (bullish).
        """
        text_lower = text.lower()
        score = 0
        matches = 0

        for keyword, weight in SentimentAnalyzer.BULLISH_KEYWORDS.items():
            if keyword in text_lower:
                score += weight
                matches += 1

        for keyword, weight in SentimentAnalyzer.BEARISH_KEYWORDS.items():
            if keyword in text_lower:
                score += weight  # weight is already negative
                matches += 1

        if matches == 0:
            return 0
        # Normalize by number of matches and bound to [-1, 1]
        normalized = score / max(matches, 1)
        return max(-1.0, min(1.0, normalized))

    @staticmethod
    def relevance_score(text: str, topic_keywords: list = None) -> float:
        """Score how relevant a text is to our trading targets."""
        topic_keywords = topic_keywords or [
            "bitcoin", "btc", "crypto", "ethereum", "eth",
            "prediction market", "kalshi", "polymarket",
            "fed", "interest rate", "inflation", "election",
        ]
        text_lower = text.lower()
        hits = sum(1 for kw in topic_keywords if kw in text_lower)
        return min(1.0, hits / 3)  # 3+ keyword hits = max relevance


class NewsSentinel:
    """
    Autonomous news monitoring and sentiment analysis sub-bot.
    Runs in a background thread, aggregating news from free APIs.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.poll_interval = config.get("news_poll_interval_sec", 120)
        self.max_items = config.get("max_news_items", 200)
        self.analyzer = SentimentAnalyzer()

        self._news_feed = deque(maxlen=self.max_items)
        self._sentiment_history = deque(maxlen=500)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._api_reachable = None  # None=untested, True/False=cached
        self._last_fetch = {}  # source -> timestamp

        # Aggregate sentiment state
        self._aggregate_sentiment = 0.0
        self._fear_greed_index = 50  # 0=extreme fear, 100=extreme greed

    def start(self):
        """Start background news monitoring."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="NewsSentinel")
        self._thread.start()
        logger.info("[NEWS] Sentinel started -- monitoring crypto news feeds")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self):
        while self._running:
            try:
                self._fetch_all()
            except Exception as e:
                logger.debug(f"[NEWS] Fetch error: {e}")
            time.sleep(self.poll_interval)

    def _http_get(self, url: str, headers: dict = None, timeout: int = 3) -> Optional[dict]:
        if self._api_reachable is False:
            return None
        try:
            req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._api_reachable = True
                return json.loads(resp.read().decode())
        except Exception as e:
            if self._api_reachable is None:
                self._api_reachable = False
                logger.debug(f"[NEWS] APIs unreachable -- skipping: {e}")
            return None

    def _fetch_all(self):
        """Fetch from all sources."""
        items = []
        items.extend(self._fetch_coingecko())
        items.extend(self._fetch_cryptocompare())
        items.extend(self._fetch_hackernews())
        items.extend(self._fetch_fear_greed())

        for item in items:
            self._news_feed.append(item)

        # Update aggregate sentiment
        if items:
            sentiments = [i.sentiment_score for i in items if i.sentiment_score != 0]
            if sentiments:
                self._aggregate_sentiment = sum(sentiments) / len(sentiments)
                self._sentiment_history.append({
                    "timestamp": time.time(),
                    "sentiment": self._aggregate_sentiment,
                    "num_items": len(items),
                })

        logger.info(
            f"[NEWS] Fetched {len(items)} items | "
            f"Aggregate sentiment: {self._aggregate_sentiment:+.3f} | "
            f"Fear/Greed: {self._fear_greed_index}"
        )

    def _fetch_coingecko(self) -> list:
        """Fetch trending coins and market overview from CoinGecko."""
        items = []

        # Trending coins
        data = self._http_get("https://api.coingecko.com/api/v3/search/trending")
        if data and "coins" in data:
            for coin in data["coins"][:5]:
                item_data = coin.get("item", {})
                name = item_data.get("name", "Unknown")
                symbol = item_data.get("symbol", "")
                score = item_data.get("score", 0)

                news = NewsItem(
                    source="coingecko_trending",
                    title=f"Trending: {name} ({symbol}) -- rank #{score + 1}",
                    relevance=0.5 if symbol.upper() in ("BTC", "ETH") else 0.2,
                    sentiment_score=0.3,  # Trending = mild bullish
                    tags=["trending", symbol.lower()],
                    raw=item_data,
                )
                items.append(news)

        # BTC market data
        data = self._http_get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true"
        )
        if data and "bitcoin" in data:
            btc = data["bitcoin"]
            change_24h = btc.get("usd_24h_change", 0)
            sentiment = max(-1, min(1, change_24h / 10))  # ±10% -> ±1.0

            news = NewsItem(
                source="coingecko_market",
                title=f"BTC 24h change: {change_24h:+.2f}%",
                relevance=1.0,
                sentiment_score=round(sentiment, 3),
                tags=["btc", "market_data"],
                raw=btc,
            )
            items.append(news)

        return items

    def _fetch_cryptocompare(self) -> list:
        """Fetch latest crypto news from CryptoCompare (free, no key)."""
        items = []
        data = self._http_get(
            "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=latest"
        )
        if not data or "Data" not in data:
            return items

        for article in data["Data"][:10]:
            title = article.get("title", "")
            sentiment = self.analyzer.score_text(title)
            relevance = self.analyzer.relevance_score(title)

            news = NewsItem(
                source="cryptocompare",
                title=title,
                url=article.get("url", ""),
                timestamp=article.get("published_on", time.time()),
                sentiment_score=round(sentiment, 3),
                relevance=round(relevance, 3),
                tags=article.get("categories", "").lower().split("|"),
                raw=article,
            )
            items.append(news)

        return items

    def _fetch_hackernews(self) -> list:
        """Fetch crypto-related stories from Hacker News."""
        items = []
        data = self._http_get(
            "https://hn.algolia.com/api/v1/search_by_date?query=bitcoin%20OR%20crypto&tags=story&numericFilters=points%3E10&hitsPerPage=10"
        )
        if not data or "hits" not in data:
            return items

        for story in data["hits"]:
            title = story.get("title", "")
            if not title:
                continue
            sentiment = self.analyzer.score_text(title)
            relevance = self.analyzer.relevance_score(title)
            points = story.get("points", 0)

            news = NewsItem(
                source="hackernews",
                title=title,
                url=story.get("url", ""),
                timestamp=story.get("created_at_i", time.time()),
                sentiment_score=round(sentiment, 3),
                relevance=round(relevance, 3),
                tags=["hackernews", "tech"],
                raw={"points": points, "num_comments": story.get("num_comments", 0)},
            )
            items.append(news)

        return items

    def _fetch_fear_greed(self) -> list:
        """Fetch the Crypto Fear & Greed Index."""
        items = []
        data = self._http_get("https://api.alternative.me/fng/?limit=1")
        if not data or "data" not in data:
            return items

        fng = data["data"][0] if data["data"] else {}
        value = int(fng.get("value", 50))
        classification = fng.get("value_classification", "Neutral")
        self._fear_greed_index = value

        # Convert to sentiment: 0-25=extreme fear(-1), 75-100=extreme greed(+1)
        sentiment = (value - 50) / 50  # Linear map: 0->-1, 50->0, 100->+1

        news = NewsItem(
            source="fear_greed",
            title=f"Fear & Greed Index: {value} ({classification})",
            relevance=0.8,
            sentiment_score=round(sentiment, 3),
            tags=["sentiment", "market_mood"],
            raw=fng,
        )
        items.append(news)
        return items

    # ── Public Interface ──

    def get_sentiment_summary(self) -> dict:
        """Get current aggregated sentiment state."""
        recent = list(self._news_feed)[-20:]
        sentiments = [n.sentiment_score for n in recent if n.sentiment_score != 0]

        return {
            "aggregate_sentiment": round(self._aggregate_sentiment, 3),
            "fear_greed_index": self._fear_greed_index,
            "fear_greed_label": self._fng_label(self._fear_greed_index),
            "recent_sentiment_avg": round(sum(sentiments) / len(sentiments), 3) if sentiments else 0,
            "total_items": len(self._news_feed),
            "bullish_items": sum(1 for n in recent if n.sentiment_score > 0.1),
            "bearish_items": sum(1 for n in recent if n.sentiment_score < -0.1),
            "neutral_items": sum(1 for n in recent if abs(n.sentiment_score) <= 0.1),
        }

    def get_sentiment_lr(self) -> float:
        """
        Get a Bayesian likelihood ratio from current news sentiment.
        Compatible with the ResearchEngine's LR system.

        LR > 1 = bullish evidence, LR < 1 = bearish evidence, LR = 1 = neutral.
        """
        summary = self.get_sentiment_summary()
        sentiment = summary["aggregate_sentiment"]
        fng = summary["fear_greed_index"]

        # Combine aggregate sentiment and fear/greed
        # Weight: 60% news sentiment, 40% fear/greed
        combined = sentiment * 0.6 + ((fng - 50) / 50) * 0.4

        # Convert to likelihood ratio (bounded 0.3 - 3.0)
        lr = math.exp(combined * 1.5)
        return round(max(0.3, min(3.0, lr)), 3)

    def get_recent_news(self, limit: int = 20, min_relevance: float = 0.3) -> list:
        """Get recent high-relevance news items."""
        recent = [n for n in self._news_feed if n.relevance >= min_relevance]
        return [n.to_dict() for n in list(recent)[-limit:]]

    def _fng_label(self, value: int) -> str:
        if value <= 20:
            return "Extreme Fear"
        elif value <= 40:
            return "Fear"
        elif value <= 60:
            return "Neutral"
        elif value <= 80:
            return "Greed"
        return "Extreme Greed"

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "total_items": len(self._news_feed),
            "sentiment": self.get_sentiment_summary(),
            "sources": {
                "coingecko": True,
                "cryptocompare": True,
                "hackernews": True,
                "fear_greed": True,
            },
        }
