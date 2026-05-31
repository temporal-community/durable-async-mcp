# ABOUTME: Entry point for python -m async_mcp.client_worker.
# Runs the UI by default; use subcommands 'worker' or 'ui' explicitly.

import sys
import click

from async_mcp.client_worker.worker import main as worker_cmd
from async_mcp.client_worker.ui import main as ui_cmd


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Invoice processing client. Runs the UI when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(ui_cmd)


cli.add_command(worker_cmd, name="worker")
cli.add_command(ui_cmd, name="ui")


if __name__ == "__main__":
    cli()
