#!/usr/bin/env bash

# Quick boot script for the Temporal invoice demo.
# Starts Temporal server and the worker in a tmux session.

set -e

SESSION="invoice-demo"

# Start new tmux session detached running the Temporal server
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux new-session -d -s "$SESSION"
fi

# Send command to first pane
tmux send-keys -t "$SESSION":0.0 'temporal server start-dev' C-m

# Create second pane if it does not exist
if [ "$(tmux list-panes -t "$SESSION" | wc -l)" -lt 2 ]; then
    tmux split-window -t "$SESSION" -v
fi

# Run worker in second pane
tmux send-keys -t "$SESSION":0.1 'source .venv/bin/activate && python -m bizservice.worker' C-m

# Attach to the session
tmux select-pane -t "$SESSION":0.0
exec tmux attach -t "$SESSION"
