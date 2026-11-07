"""CLI interface for HydraPing."""

from importlib.metadata import version as get_version
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from typer.core import TyperGroup

from hydraping.core.config import Config, create_default_config, get_default_config_path
from hydraping.core.engine import Engine
from hydraping.core.models import Endpoint
from hydraping.tui.dashboard import Dashboard

DEFAULT_COMMAND = "monitor"


class DefaultCommandGroup(TyperGroup):
    """Run the command named by the first positional argument, or ``monitor`` if it names none.

    The monitor command takes a variadic list of endpoints. As a group callback argument, that
    list would swallow subcommand names, so ``hydraping version`` would monitor a host called
    "version". Routing happens here, before Click parses any parameter.
    """

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        # Everything after "--" is an endpoint, so "hydraping -- version" monitors "version".
        has_separator = "--" in args
        before_separator = args[: args.index("--")] if has_separator else args
        position = self._first_positional(ctx, before_separator)
        if position is not None and args[position] in self.commands:
            # Options typed before the command name belong to that command.
            args = [args[position], *args[:position], *args[position + 1 :]]
        elif (
            position is not None
            or has_separator
            or not set(before_separator) & set(ctx.help_option_names)
        ):
            args = [DEFAULT_COMMAND, *args]
        return super().parse_args(ctx, args)

    def _first_positional(self, ctx: typer.Context, args: list[str]) -> int | None:
        """Return the index of the first argument that is not an option or an option value."""
        monitor = self.commands[DEFAULT_COMMAND]
        value_options = {
            name
            for param in monitor.get_params(ctx)
            if param.param_type_name == "option" and not getattr(param, "is_flag", False)
            for name in param.opts
        }
        index = 0
        while index < len(args):
            arg = args[index]
            if not arg.startswith("-") or arg == "-":
                return index
            index += 2 if arg in value_options else 1
        return None


app = typer.Typer(
    name="hydraping",
    help="Multi-protocol connection tester with live terminal UI",
    cls=DefaultCommandGroup,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)

console = Console()


def start_monitoring(cfg: Config) -> None:
    """Run the live dashboard for ``cfg`` until Ctrl+C. Tests replace this to skip monitoring."""
    dashboard = Dashboard(Engine(cfg), console=console)
    try:
        dashboard.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user[/yellow]")


@app.command(name=DEFAULT_COMMAND)
def monitor(
    endpoints: Annotated[
        list[str] | None,
        typer.Argument(
            help="Endpoints to monitor (overrides config file). "
            "Examples: 8.8.8.8, 1.1.1.1:53, google.com, https://example.com"
        ),
    ] = None,
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
    """Start monitoring endpoints (default command)."""
    # Load base configuration
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

    # Override endpoints if provided as arguments
    if endpoints:
        try:
            cfg.endpoints = [Endpoint.parse(ep) for ep in endpoints]
        except Exception as e:
            console.print(f"[red]Error parsing endpoint: {e}[/red]")
            raise typer.Exit(1) from None

    start_monitoring(cfg)


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
    try:
        pkg_version = get_version("hydraping")
    except Exception:
        pkg_version = "unknown"
    console.print(f"hydraping version {pkg_version}")


if __name__ == "__main__":
    app()
