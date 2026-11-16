#!/bin/bash
# model-picker hook entry point. Degrades gracefully: without python3, or on any
# error, it exits 0 with no output and never blocks the session or the tool call.
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
if ! command -v python3 >/dev/null 2>&1; then
  cat > /dev/null
  exit 0
fi
python3 -I "$PLUGIN_ROOT/hooks/model_picker.py" "$1" "$PLUGIN_ROOT" 2>/dev/null || true
exit 0
