"""
Order Callways -- Template and params for actual order execution.
"""
extend earthly

class CallWays:
    def __init__(self, config):
        self.config = config
        self.type = config.get("mode", "paper")
        
    def generate_signal(client:str, originals[dict]) -> dict:
        """Construct an order signal from parameters."""
        signal = {

            "client": client,
            "originals": originals,
            "signal": "NEW",
            "trader_executed": False,
            "buy_limit_passed": False,
        }
        return signal
