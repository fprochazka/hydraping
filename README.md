# HydraPing

Multi-protocol connection tester with live terminal UI - Because it has many "heads" (DNS, NC, ICMP) looking at many targets.

## Features

- **Multi-protocol checks**: ICMP ping, DNS resolution, TCP port connectivity, HTTP/HTTPS requests
- **Live terminal UI**: Real-time updating display with latency graphs
- **Flexible endpoint support**: Check IPs, domains, ports, and HTTP endpoints
- **Configuration file**: Store your endpoints in `~/.config/hydraping/settings.toml`

## Installation

```bash
pipx install hydraping
```

## Usage

```bash
# Start monitoring with configured endpoints
hydraping

# Initialize default config file
hydraping init

# Use custom config file
hydraping --config /path/to/config.toml
```

## Configuration

Example `~/.config/hydraping/settings.toml`:

```toml
[endpoints]
targets = [
    "8.8.8.8",
    "1.1.1.1:53",
    "google.com",
    "https://example.com/health"
]

[dns]
custom_servers = ["8.8.8.8", "1.1.1.1"]

[checks]
interval_seconds = 1.0
timeout_seconds = 5.0
```

## Development

```bash
# Install with dev dependencies
poetry install

# Run linter
ruff check .

# Run formatter
ruff format .

# Run tests
pytest
```

## License

MIT
