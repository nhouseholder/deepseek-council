#!/usr/bin/env bash
set -e

COUNCIL_DIR="$HOME/.plan-council"
mkdir -p "$COUNCIL_DIR"

if [ ! -f "$COUNCIL_DIR/.env" ]; then
  cat > "$COUNCIL_DIR/.env" << 'ENV'
# deepseek-council API keys
# DeepSeek V4 Pro is the default provider — get a key at platform.deepseek.com
DEEPSEEK_API_KEY=your_key_here

# Uncomment to enable other providers (also set "enabled": true in providers.json):
# GEMINI_API_KEY=your_key_here
# OPENAI_API_KEY=your_key_here
# ANTHROPIC_API_KEY=your_key_here
ENV
  echo "Created $COUNCIL_DIR/.env — replace 'your_key_here' with your DeepSeek API key."
fi

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
echo ""
echo "deepseek-council installed at: $INSTALL_DIR"
echo ""
echo "Quick start:"
echo "  1. Edit $COUNCIL_DIR/.env — add your DEEPSEEK_API_KEY"
echo "  2. python3 $INSTALL_DIR/review.py --plan PLAN.md --discover"
echo "  3. python3 $INSTALL_DIR/review.py --plan PLAN.md --council"
