#!/usr/bin/env bash

# Boots the full purchase-order demo in a tmux session, one pane each:
#   0. Temporal dev server
#   1. bizservice worker        (InvoiceWorkflow + PayLineItem)
#   2. durable MCP client worker (adopts the tasks plugin; spawns the MCP server over stdio)
#   3. the purchase-order UI     (interactive — submit a PO here)
# The short staggered sleeps let Temporal come up before the workers connect.
# Optional NiceGUI board (not started here): python -m invoice_processing_mcp.client.gui

set -e

SESSION="invoice-demo"
VENV="source .venv/bin/activate"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session '$SESSION' already exists — attaching."
    echo "(stop it with:  tmux kill-session -t $SESSION)"
    exec tmux attach -t "$SESSION"
fi

# Pane 0 — Temporal dev server
tmux new-session -d -s "$SESSION"
tmux send-keys -t "$SESSION" 'temporal server start-dev' C-m

# Pane 1 — bizservice worker
tmux split-window -t "$SESSION" -v
tmux send-keys -t "$SESSION" "$VENV && sleep 6 && python -m bizservice.worker" C-m

# Pane 2 — durable MCP client worker
tmux split-window -t "$SESSION" -v
tmux send-keys -t "$SESSION" "$VENV && sleep 8 && python -m invoice_processing_mcp.client.worker" C-m

# Pane 3 — purchase-order UI (interactive)
tmux split-window -t "$SESSION" -v
tmux send-keys -t "$SESSION" "$VENV && sleep 10 && python -m invoice_processing_mcp.client.ui" C-m

tmux select-layout -t "$SESSION" tiled
# Land on the UI pane so you can: submit samples/invoice_large.json
tmux select-pane -t "$SESSION":0.3
exec tmux attach -t "$SESSION"
