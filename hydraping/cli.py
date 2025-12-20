"""CLI interface for HydraPing."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from hydraping.config import Config, create_default_config, get_default_config_path
from hydraping.orchestrator import CheckOrchestrator
from hydraping.ui.dashboard import Dashboard

app = typer.Typer(
    name="hydraping",
    help="Multi-protocol connection tester with live terminal UI",
    add_completion=False,
)

console = Console()


@app.command()
def main(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Path to configuration file",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
):
    """Start monitoring configured endpoints."""
    # Load configuration
    if config is None:
        config = get_default_config_path()

    try:
        cfg = Config.load(config)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Error loading configuration: {e}[/red]")
        raise typer.Exit(1) from None

    # Create orchestrator and dashboard
    orchestrator = CheckOrchestrator(cfg)
    dashboard = Dashboard(orchestrator)

    # Run the dashboard
    try:
        asyncio.run(dashboard.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user[/yellow]")


@app.command()
def init(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Path for configuration file",
            dir_okay=False,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Overwrite existing configuration file",
        ),
    ] = False,
):
    """Create a default configuration file."""
    if config is None:
        config = get_default_config_path()

    # Check if file already exists
    if config.exists() and not force:
        console.print(
            f"[yellow]Configuration file already exists: {config}[/yellow]\n"
            f"Use --force to overwrite"
        )
        raise typer.Exit(1) from None

    # Create configuration
    try:
        created_path = create_default_config(config)
        console.print(f"[green]✓[/green] Created configuration file: {created_path}")
        console.print("\nEdit this file to configure your endpoints.")
    except Exception as e:
        console.print(f"[red]Error creating configuration: {e}[/red]")
        raise typer.Exit(1) from None


@app.command()
def version():
    """Show version information."""
    console.print("hydraping version 0.1.0")


if __name__ == "__main__":
    app()
