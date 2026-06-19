#!/usr/bin/env bash
set -e

COUNCIL_DIR="$HOME/.plan-council"
mkdir -p "$COUNCIL_DIR"

if [ ! -f "$COUNCIL_DIR/.env" ]; then
  cat > "$COUNCIL_DIR/.env" << 'ENV'
# plan-council API keys — set the key for your chosen provider
# DEEPSEEK_API_KEY=your_key_here
# GEMINI_API_KEY=your_key_here
# OPENAI_API_KEY=your_key_here
# ANTHROPIC_API_KEY=your_key_here
ENV
  echo "Created $COUNCIL_DIR/.env — add your API key there."
fi

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
echo ""
echo "plan-council installed at: $INSTALL_DIR"
echo ""
echo "Quick start:"
echo "  1. Edit $COUNCIL_DIR/.env and add your API key"
echo "  2. Enable a provider in providers.json (set enabled: true)"
echo "  3. python3 $INSTALL_DIR/review.py --plan PLAN.md --discover"
echo "  4. python3 $INSTALL_DIR/review.py --plan PLAN.md --council"
