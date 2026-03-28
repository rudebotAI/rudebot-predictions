"""
MCP Plugin -- Central Tool Dispatcher.
Matches the diagram: MCP Plugin sits at the hub, dispatching tool calls
between Coinbase API, Polymarket MCP, Strategy Engine, and Order Router.

This module exposes the bot's capabilities as callable tools that can be
invoked by AI agents (Claude, Grok, etc.) via the MCP protocol.

For now, this is a local dispatcher. To expose over MCP protocol,
wrap with FastMCP (pip install fastmcp) when ready.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MCPPlugin:
    """
    Central tool dispatcher. Routes calls to the appropriate subsystem.

    Tools exposed:
    - get_markets()       -> Polymarket/Kalshi market data
    - get_price()         -> Coinbase spot/candle prices
    - get_momentum()      -> BTC/ETH price momentum
    - scan_opportunities() -> Run EV scanner
    - get_positions()     -> Current open positions
    - get_pnl()          -> P&L summary
    - place_order()      -> Route order through execution layer
    - get_status()       -> Full bot status
    - research_topic()   -> Run research engine on a topic
    """

    def __init__(self, bot):
        """
        Args:
            bot: PredictionBot instance (gives access to all subsystems)
        """
        self.bot = bot
        self.call_count = 0
        self.tools = {
            "get_markets": self.get_markets,
            "get_price": self.get_price,
            "get_momentum": self.get_momentum,
            "scan_opportunities": self.scan_opportunities,
            "get_positions": self.get_positions,
            "get_pnl": self.get_pnl,
            "place_order": self.place_order,
            "get_status": self.get_status,
            "research_topic": self.research_topic,
            "notify_trader": self.notify_trader,
        }

    def dispatch(self, tool_name: str, params: dict = None) -> dict:
        """
        Dispatch a tool call.
        Returns: {"result": ..., "error": None} or {"result": None, "error": "..."}
        """
        self.call_count += 1
        params = params or {}

        if tool_name not in self.tools:
            return {"result": None, "error": f"Unknown tool: {tool_name}"}

        try:
            result = self.tools[tool_name](**params)
            logger.debug(f"MCP dispatch: {tool_name} -> OK")
            return {"result": result, "error": None}
        except Exception as e:
            logger.error(f"MCP dispatch error: {tool_name} -- {e}")
            return {"result": None, "error": str(e)}

    def list_tools(self) -> list:
        """List available tools with descriptions."""
        return [
            {"name": "get_markets", "description": "Fetch active markets from Polymarket and Kalshi", "params": ["platform", "limit"]},
            {"name": "get_price", "description": "Get spot price from Coinbase", "params": ["pair"]},
            {"name": "get_momentum", "description": "Get BTC/ETH price momentum (5min candles)", "params": ["product_id", "periods"]},
            {"name": "scan_opportunities", "description": "Run full EV scan across all markets", "params": []},
            {"name": "get_positions", "description": "Get current open positions", "params": []},
            {"name": "get_pnl", "description": "Get P&L summary", "params": []},
            {"name": "place_order", "description": "Place a trade through the order router", "params": ["opportunity", "size_usd"]},
            {"name": "get_status", "description": "Get full bot status", "params": []},
            {"name": "research_topic", "description": "Research a topic across Reddit, HN, web, X", "params": ["question"]},
            {"name": "notify_trader", "description": "Send a notification to the trader", "params": ["message"]},
        ]

    # ── Tool Implementations ──

    def get_markets(self, platform: str = "all", limit: int = 20) -> dict:
        """Fetch markets from specified platform."""
        result = {"polymarket": [], "kalshi": []}
        if platform in ("all", "polymarket"):
            result["polymarket"] = self.bot.poly.scan_markets_with_prices(limit=limit)
        if platform in ("all", "kalshi"):
            result["kalshi"] = self.bot.kalshi.scan_markets_with_prices(limit=limit)
        return result

    def get_price(self, pair: str = "BTC-USD") -> Optional[dict]:
        """Get spot price from Coinbase."""
        if not hasattr(self.bot, "coinbase"):
            return {"error": "Coinbase connector not initialized"}
        price = self.bot.coinbase.get_spot_price(pair)
        ticker = self.bot.coinbase.get_ticker(pair)
        return {"pair": pair, "spot": price, "ticker": ticker}

    def get_momentum(self, product_id: str = "BTC-USD", periods: int = 12) -> Optional[dict]:
        """Get price momentum from Coinbase candles."""
        if not hasattr(self.bot, "coinbase"):
            return {"error": "Coinbase connector not initialized"}
        return self.bot.coinbase.get_price_momentum(product_id, periods)

    def scan_opportunities(self) -> dict:
        """Run a full EV scan."""
        poly = self.bot.poly.scan_markets_with_prices(limit=50)
        kalshi = self.bot.kalshi.scan_markets_with_prices(limit=50)
        all_markets = self.bot.scanner.cross_reference_markets(poly, kalshi)
        opportunities = self.bot.scanner.scan(all_markets)
        return {
            "total_markets": len(all_markets),
            "opportunities": len(opportunities),
            "top_5": opportunities[:5],
        }

    def get_positions(self) -> list:
        """Get open positions from state store."""
        if hasattr(self.bot, "state_store"):
            return self.bot.state_store.get_positions()
        return self.bot.paper.get_open_positions()

    def get_pnl(self) -> dict:
        """Get P&L summary."""
        if hasattr(self.bot, "state_store"):
            return self.bot.state_store.get_pnl_summary()
        return self.bot.paper.get_performance()

    def place_order(self, opportunity: dict, size_usd: float) -> dict:
        """Route an order through the execution layer."""
        if hasattr(self.bot, "order_router"):
            return self.bot.order_router.route_order(opportunity, size_usd)
        return {"error": "Order router not initialized"}

    def get_status(self) -> dict:
        """Full bot status."""
        status = {
            "mode": self.bot.mode,
            "scan_number": self.bot.scan_number,
            "risk": self.bot.risk.status(),
            "mcp_calls": self.call_count,
        }
        if hasattr(self.bot, "state_store"):
            status["state"] = self.bot.state_store.get_full_state()
        if hasattr(self.bot, "coinbase"):
            btc = self.bot.coinbase.get_spot_price("BTC-USD")
            if btc:
                status["btc_price"] = btc
        return status

    def research_topic(self, question: str) -> dict:
        """Research a topic using the research engine."""
        return self.bot.research.research_market(question)

    def notify_trader(self, message: str) -> dict:
        """Send notification via Telegram."""
        if self.bot.telegram.is_configured():
            self.bot.telegram.send(message)
            return {"sent": True}
        return {"sent": False, "reason": "Telegram not configured"}
