#!/bin/bash
# ── Prediction Market Bot -- One-Click Launcher ──
# Usage: bash start.sh

set -e
cd "$(dirname "$0")"

echo "================================="
echo "  Prediction Market Quant Bot"
echo "================================="

# 1. Check Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 not found. Install from python.org"
    exit 1
fi
echo "✅ Python 3 found: $(python3 --version)"

# 2. Install dependencies
echo "📦 Installing dependencies..."
pip3 install -r requirements.txt --quiet 2>/dev/null || pip3 install --user -r requirements.txt --quiet
echo "✅ Dependencies installed"

# 3. Check config
if ! grep -q "YOUR_BOT_TOKEN" config.yaml 2>/dev/null; then
    echo "✅ Config looks customized"
else
    echo "⚠️  config.yaml still has placeholder values"
    echo "   Edit config.yaml and add your Telegram bot_token + chat_id"
    echo "   (Bot works without Telegram, but you won't get alerts)"
fi

# 4. Test scan
echo ""
echo "🔍 Running test scan..."
python3 main.py --once
echo ""
echo "✅ Test scan complete!"

# 5. Ask to start full loop
echo ""
read -p "Start 24/7 mode? (y/n): " choice
if [[ "$choice" == "y" || "$choice" == "Y" ]]; then
    echo "🚀 Starting bot... (Ctrl+C to stop)"
    python3 main.py
else
    echo "Run 'python3 main.py' when you're ready to go 24/7"
fi
