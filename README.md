# HydraPing

Multi-protocol connection tester with live terminal UI - Because it has many "heads" (DNS, NC, ICMP) looking at many targets.

## Features

- **Multi-protocol checks**: ICMP ping, DNS resolution, TCP port connectivity, HTTP/HTTPS requests
- **Live terminal UI**: Real-time updating display with latency graphs
- **Flexible endpoint support**: Check IPs, domains, ports, and HTTP endpoints
- **Configuration file**: Store your endpoints in `~/.config/hydraping/settings.toml`

## Installation

### From Git (recommended for development)

```bash
# Clone the repository
git clone https://github.com/fprochazka/hydraping.git
cd hydraping

# Install with pipx
pipx install .

# Or install in editable mode for development
pipx install -e .
```

### From PyPI (when published)

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

### Example Output

```
1.1.1.1                      .................................................▁▁▁▁▁▁▁▁▁       8.2ms (ICMP)
8.8.8.8                      .................................................▁▁▁▁▁▁▁▁▁      12.5ms (ICMP)
google.com                   ..............................................▂▂▁▁▁▁▁▁▁▁▁▁      15.3ms (TCP)
https://filip-prochazka.com/ ................................................▃▃▄▃▃▃▃▃▃▃     145.7ms (HTTP)

Problems:
  • ICMP unavailable (no permissions) - ping checks disabled
```

The display shows:
- **Endpoint name** - What you're monitoring
- **Latency graph** - Visual history scrolling right-to-left (color-coded by latency)
- **Current latency** - Latest measurement with check type (ICMP/DNS/TCP/HTTP)
- **Problems** - Only relevant issues (smart filtering based on check hierarchy)

Graph colors: 🟢 Green (<50ms) · 🟡 Yellow (<150ms) · 🟠 Orange (<300ms) · 🔴 Red (>300ms)

## Configuration

Example `~/.config/hydraping/settings.toml`:

```toml
[endpoints]
targets = [
    "1.1.1.1",
    "8.8.8.8",
    "google.com",
    "https://filip-prochazka.com/"
]

[dns]
custom_servers = []  # Optional: custom DNS servers

[checks]
interval_seconds = 5.0  # Check every 5 seconds
timeout_seconds = 5.0   # 5 second timeout per check

[ui]
graph_width = 0  # Auto-size to terminal width
```

Supported endpoint formats:
- **IP address**: `8.8.8.8` → ICMP ping only
- **IP:port**: `1.1.1.1:53` → ICMP + TCP port check
- **Domain**: `google.com` → DNS resolution + ICMP + TCP (ports 80/443)
- **HTTP/HTTPS**: `https://example.com/` → Full stack (DNS + ICMP + TCP + HTTP request)

## Development

```bash
# Install with dev dependencies
poetry install

# Run linter with auto-fix and formatter (after code changes)
source .venv/bin/activate && ruff check --fix . && ruff format .

# Run tests
pytest
```

## License

MIT
