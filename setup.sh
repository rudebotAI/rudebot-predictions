#!/bin/bash
# PredictionBot v4.0 -- Setup Script
# Run this once before starting the bot.

echo "=================================="
echo "  PredictionBot v4.0 Setup"
echo "=================================="

# 1. Core dependencies (already in requirements.txt)
echo "[1/4] Installing core dependencies..."
pip install pyyaml requests 2>/dev/null || pip3 install pyyaml requests 2>/dev/null
echo "  ✓ Core deps installed"

# 2. WebSocket (required for real-time feeds)
echo "[2/4] Installing websocket-client for real-time feeds..."
pip install websocket-client 2>/dev/null || pip3 install websocket-client 2>/dev/null
echo "  ✓ websocket-client installed"

# 3. Optional: py-clob-client (only needed for Polymarket trading, not scanning)
echo "[3/4] Installing py-clob-client (optional, for Polymarket trading)..."
pip install py-clob-client 2>/dev/null || pip3 install py-clob-client 2>/dev/null || echo "  ⚠ py-clob-client failed -- Polymarket trading disabled (scanning still works)"

# 4. Create required directories
echo "[4/4] Creating directories..."
mkdir -p logs/backtest
mkdir -p 