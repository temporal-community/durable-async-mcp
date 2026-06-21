# ABOUTME: Entry point for python -m invoice_processing_mcp.client.
# Runs the UI by default; use subcommands 'worker' or 'ui' explicitly.

import click

from invoice_processing_mcp.client.ui import main as ui_cmd
from invoice_processing_mcp.client.worker import main as worker_cmd


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
