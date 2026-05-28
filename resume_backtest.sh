#!/usr/bin/env bash
# =============================================================================
# RESUME: Tech Stocks Backtest (Run #2)  —  AAPL/MSFT/GOOGL/NVDA/TSLA
# =============================================================================
# Paused at: 2026-05-12 (day 39 of 44).  5 trading days remain (May 13 -> 20).
#
# Progress so far (do NOT lose these — they're the _part*.log files):
#   part1  = 9 days  (qwen3:8b, 40K ctx)
#   part4  = 3 days  (qwen3:4b, 64K ctx)
#   part5  = 27 days (qwen3:4b, 4K ctx, turbo)   <- frozen as _part5_paused.log
#
# State at pause (carried forward as initial cash):
#   Cash Balance:         $13,830.29
#   Total Position Value: -$3,868.44
#   Total Value:          $9,961.85   (-2.05% vs SPY +12.04%)
#
# To resume, just run:  bash resume_backtest.sh
# =============================================================================
set -e
cd "$(dirname "$0")"

echo "Starting Ollama with turbo settings (parallel=4, flash attn, 4K ctx)..."
pkill -9 -f "ollama" 2>/dev/null || true
sleep 2
OLLAMA_NUM_PARALLEL=4 \
OLLAMA_FLASH_ATTENTION=true \
OLLAMA_KV_CACHE_TYPE=q8_0 \
OLLAMA_NEW_ENGINE=true \
OLLAMA_CONTEXT_LENGTH=4096 \
OLLAMA_KEEP_ALIVE=24h \
nohup /Applications/Ollama.app/Contents/Resources/ollama serve > /tmp/ollama_turbo.log 2>&1 &
disown
until curl -sf http://localhost:11434/api/version > /dev/null 2>&1; do sleep 1; done
echo "Ollama up."

echo "Resuming backtest for the final 5 days (2026-05-13 -> 2026-05-20)..."
rm -f logs/backtest_stocks_v2_part6.log
PYTHONUNBUFFERED=1 .venv/bin/python -m src.backtester \
  --tickers "AAPL,MSFT,GOOGL,NVDA,TSLA" \
  --start-date 2026-05-13 --end-date 2026-05-20 \
  --initial-cash 9961 \
  --ollama --model qwen3:4b \
  --analysts warren_buffett,michael_burry,peter_lynch,stanley_druckenmiller,cathie_wood,technical_analyst,fundamentals_analyst,sentiment_analyst,valuation_analyst \
  > logs/backtest_stocks_v2_part6.log 2>&1 &
BTPID=$!
disown
caffeinate -di -w $BTPID > /dev/null 2>&1 &
disown
echo "Resumed. PID $BTPID. Tail the log with:"
echo "  tail -f logs/backtest_stocks_v2_part6.log"
echo ""
echo "Dashboard (if not running): .venv/bin/python -m src.dashboard.server"
echo "  then open http://localhost:8765/backtest"
