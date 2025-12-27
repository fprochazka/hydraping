# HydraPing

Multi-protocol connection tester with live terminal UI - Because it has many "heads" (DNS, NC, ICMP) looking at many targets.

## Features

- **Multi-protocol checks**: ICMP ping, DNS resolution, TCP port connectivity, HTTP/HTTPS requests
- **Live terminal UI**: Real-time updating display with latency graphs
- **Flexible endpoint support**: Check IPv4/IPv6 addresses, domains, ports, and HTTP endpoints
- **IPv6 support**: Full support for IPv6 addresses with bracket notation for ports
- **Configuration file**: Store your endpoints in `~/.config/hydraping/settings.toml`
- **Async architecture**: Efficiently monitors multiple endpoints concurrently
- **Smart error handling**: Only shows relevant problems based on check hierarchy

## Requirements

- Python 3.11+
- **ICMP (ping) checks**: Require root/CAP_NET_RAW privileges on Linux. If not available, ping checks will be automatically disabled with a warning.

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
Cloudflare DNS               .................................................▁▁▁▁▁▁▁▁▁       8.2ms (ICMP)
Google DNS                   .................................................▁▁▁▁▁▁▁▁▁      12.5ms (ICMP)
google.com                   ..............................................▂▂▁▁▁▁▁▁▁▁▁▁      15.3ms (TCP)
Production API               ................................................▃▃▄▃▃▃▃▃▃▃     145.7ms (HTTP)

Problems:
  • ICMP unavailable (no permissions) - ping checks disabled
```

The display shows:
- **Endpoint name** - What you're monitoring
- **Latency graph** - Visual history scrolling right-to-left (color-coded by latency)
- **Current latency** - Latest measurement with check type (ICMP/DNS/TCP/HTTP)
- **Problems** - Only relevant issues (smart filtering based on check hierarchy)

Graph colors: 🟢 Green (<50ms) · 🟡 Yellow (50-100ms) · 🟠 Orange (100-200ms) · 🔴 Red (>200ms)

## Configuration

Example `~/.config/hydraping/settings.toml`:

```toml
[endpoints]
targets = [
    # Simple string format (uses URL as display name)
    "google.com",
    "https://example.com/",

    # Object format with custom labels
    { url = "1.1.1.1", name = "Cloudflare DNS" },
    { url = "8.8.8.8", name = "Google DNS" },
    { url = "192.168.1.1", name = "Home Router" },
    { url = "https://api.example.com/health", name = "Production API" },

    # UDP endpoints (must specify protocol = "udp")
    { url = "1.1.1.1:53", protocol = "udp", name = "Cloudflare DNS (UDP)" },
    { url = "8.8.8.8:53", protocol = "udp", name = "Google DNS (UDP)" },

    # IPv4/IPv6 preference (force specific IP version)
    { url = "google.com", ip_version = 4, name = "Google (IPv4 only)" },
    { url = "google.com", ip_version = 6, name = "Google (IPv6 only)" },

    # Custom primary check type (choose which check to graph/display)
    # Useful when ICMP is blocked but you want to monitor TCP connectivity
    { url = "example.com", primary_check_type = "tcp", name = "Example (TCP primary)" },
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
- **IPv4 address**: `8.8.8.8` → ICMP ping only
- **IPv6 address**: `2001:4860:4860::8888` → ICMP ping only
- **IPv4:port (TCP)**: `1.1.1.1:53` → ICMP + TCP port check
- **IPv6:port (TCP)**: `[2001:4860:4860::8888]:53` → ICMP + TCP port check (note the brackets)
- **UDP port**: `{ url = "1.1.1.1:53", protocol = "udp" }` → ICMP + UDP port check
- **Domain**: `google.com` → DNS resolution + ICMP + TCP (ports 80/443)
- **HTTP/HTTPS**: `https://example.com/` → Full stack (DNS + ICMP + TCP + HTTP request)
- **IPv4/IPv6 preference**: `{ url = "google.com", ip_version = 4 }` → Force IPv4 or IPv6
- **Custom primary check**: `{ url = "example.com", primary_check_type = "tcp" }` → Choose which check to display (dns/icmp/tcp/udp/http)

## Development

```bash
# Install with dev dependencies
poetry install

# Run linter with auto-fix and formatter (after code changes)
source .venv/bin/activate && ruff check --fix . && ruff format .

# Run tests
pytest
```

## How It Works

### Check Hierarchy

For each endpoint, HydraPing runs applicable checks based on the endpoint type:

- **IP addresses** (`8.8.8.8`, `2001:4860:4860::8888`): ICMP only (supports both IPv4 and IPv6)
- **IP:port** (`1.1.1.1:53`, `[2001:4860:4860::8888]:53`): ICMP + TCP
- **Domains** (`google.com`): DNS + ICMP + TCP (ports 80 and 443)
- **HTTP/HTTPS URLs**: DNS + ICMP + TCP + HTTP request

By default, the dashboard displays the highest-priority successful check result (HTTP > TCP/UDP > DNS > ICMP). If a higher-level check succeeds (e.g., HTTP), lower-level failures (e.g., ICMP) are suppressed to reduce noise.

#### Custom Primary Check Type

You can override which check type is displayed in the graph and latency column using `primary_check_type`. This is useful when:
- ICMP is blocked by firewall (common for many services)
- You want to monitor a specific layer (e.g., TCP connectivity instead of ICMP)
- You care more about application-layer health than network-layer connectivity

All applicable checks still run in the background, and failures are reported in the "Problems" section. Only the graph and latency display are affected by the primary check type setting.

### Architecture

- **Async-first design**: Uses `asyncio` to run multiple checks concurrently without blocking
- **Built with**:
  - `icmplib` - Async ICMP ping
  - `dnspython` - DNS resolution
  - `aiohttp` - Async HTTP requests
  - `Rich` - Terminal UI rendering
  - `Typer` - CLI framework
- **Time bucketing**: Results are bucketed by check interval for smooth graph scrolling
- **Smart filtering**: Only shows meaningful errors based on what checks succeeded

## License

MIT
