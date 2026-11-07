# HydraPing

Multi-protocol connection tester with live terminal UI - Because it has many "heads" (DNS, ICMP, TCP, UDP, TLS, HTTP) looking at many targets.

## Features

- **Multi-protocol checks**: ICMP ping, DNS resolution, TCP port connectivity, TLS handshake, HTTP/HTTPS requests
- **Per-layer measurement**: an HTTPS target is measured as DNS, TCP, TLS and HTTP separately, so a failure names the layer that broke
- **Live terminal UI**: Real-time updating display with latency graphs
- **Flexible endpoint support**: Check IPv4/IPv6 addresses, domains, ports, and HTTP endpoints
- **IPv6 support**: Full support for IPv6 addresses with bracket notation for ports
- **Configuration file**: Store your endpoints in `~/.config/hydraping/settings.toml`
- **Async architecture**: Efficiently monitors multiple endpoints concurrently
- **Smart error handling**: Only shows relevant problems based on check hierarchy

## Requirements

- Python 3.11+
- **ICMP (ping) checks** on Linux: your user's group must be in the `net.ipv4.ping_group_range` sysctl. Ubuntu allows all groups by default. If ICMP is not available, HydraPing disables ping checks and shows a warning. See [ICMP permissions on Linux](#icmp-permissions-on-linux).

## Installation

### From Git (recommended for development)

```bash
# Clone the repository
git clone https://github.com/fprochazka/hydraping.git
cd hydraping

# Install as a tool with uv
uv tool install .

# Or install in editable mode for development (auto-updates on git pull)
uv tool install -e .
```

### From PyPI (when published)

```bash
uv tool install hydraping
```

## Usage

`monitor` is the default command: bare `hydraping` runs it, the same as `hydraping monitor`, and `hydraping -- <name>` monitors a host named like a subcommand, for example `hydraping -- version`.

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
Cloudflare DNS               .........................▁▁▁▁▁▁▁▁▁       8.2ms (ICMP)
Google DNS                   .........................▁▁▁▁▁▁▁▁▁      12.5ms (ICMP)
google.com                   ......................▂▂▁▁▁▁▁▁▁▁▁▁      15.3ms (TCP)
Production API               ........................▃▃▄▃▃▃▃▃▃▃     145.7ms (HTTP)

Problems:
  • ICMP unavailable (no permissions) - ping checks disabled
```

The display shows:
- **Endpoint name** - What you're monitoring
- **Latency graph** - Visual history scrolling right-to-left (color-coded by latency)
- **Current latency** - Latest measurement with check type (ICMP/DNS/TCP/UDP/TLS/HTTP)
- **Problems** - Only relevant issues (smart filtering based on check hierarchy)

Graph colors: 🟢 Green (<50ms) · 🟡 Yellow (50-100ms) · 🟠 Orange (100-200ms) · 🔴 Red (>200ms)

A failed column shows the letter of the layer that broke: `D` DNS, `T` TCP, `S` TLS, `H` HTTP. ICMP and UDP failures show `!`. When the displayed layer did not run because an earlier one failed, the column shows the earlier layer's letter, and the latency column shows `FAIL (TCP)` with that layer's name.

## Configuration

Example `~/.config/hydraping/settings.toml`:

```toml
version = 2

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

    # Accept any certificate (self-signed devices); the TLS layer still measures the handshake
    { url = "https://router.example.internal/", insecure = true, name = "Router admin" },
]

[dns]
servers = []  # Optional: DNS servers to query instead of the system resolver

[checks]
probe_interval_seconds = 1.0  # A new probe starts every second
bucket_seconds = 5.0          # One graph column covers 5 seconds
timeout_seconds = 5.0         # A probe that takes longer counts as failed
http_success_status_max = 399 # HTTP status codes up to this count as success

[ui]
graph_width = 0  # Auto-size to terminal width
```

Unknown keys stop the start with an error that names the key, so a typo cannot silently fall back to a default.

### Probe timing

A new probe starts for every check every `probe_interval_seconds`, whether or not the previous one has answered. Each probe gives up after `timeout_seconds`. The graph groups results into columns of `bucket_seconds`; the rightmost column is the one still in progress, and it shows the newest result until its time is over. When a probe finishes, the older probes of the same check that are still running are cancelled, so a slow answer never overwrites a newer one. HydraPing keeps 3600 columns of history per check, five hours at the default width, whatever the terminal size.

### Migration from the version 1 format

A `settings.toml` without a `version` key is in the version 1 format. HydraPing migrates it on the first start:

1. It copies the file to `settings.toml.v1-backup-<date>-<time>` in the same directory.
2. It rewrites the file in place: `version = 2` is added, `checks.interval_seconds` becomes `checks.bucket_seconds`, `checks.probe_interval_seconds = 1.0` is added, and `dns.custom_servers` becomes `dns.servers`. Comments and layout stay as they were.
3. It prints one line to stderr with the path of the backup.

If the directory is not writable, the start stops with an error that says what to change by hand. This also applies to files given with `--config`.

Supported endpoint formats:
- **IPv4 address**: `8.8.8.8` → ICMP ping only
- **IPv6 address**: `2001:4860:4860::8888` → ICMP ping only
- **IPv4:port (TCP)**: `1.1.1.1:53` → ICMP + TCP port check
- **IPv6:port (TCP)**: `[2001:4860:4860::8888]:53` → ICMP + TCP port check (note the brackets)
- **Domain:port (TCP)**: `example.com:8080` → DNS resolution + ICMP + TCP port check
- **UDP port**: `{ url = "1.1.1.1:53", protocol = "udp" }` → ICMP + UDP port check
- **Domain**: `google.com` → DNS resolution + ICMP + TCP to ports 80 and 443, each port with its own history
- **HTTP**: `http://example.com/` → DNS + ICMP + TCP + HTTP request
- **HTTPS**: `https://example.com/` → DNS + ICMP + TCP + TLS handshake + HTTP request
- **IPv4/IPv6 preference**: `{ url = "google.com", ip_version = 4 }` → Force IPv4 or IPv6
- **Custom primary check**: `{ url = "example.com", primary_check_type = "tcp" }` → Choose which check to display (dns/icmp/tcp/udp/tls/http)
- **Any certificate**: `{ url = "https://router.example.internal/", insecure = true }` → Skip certificate verification in the TLS layer (HTTPS only)

## Troubleshooting

### ICMP permissions on Linux

HydraPing sends pings through unprivileged ICMP sockets, so it needs neither root nor `CAP_NET_RAW`.
The kernel allows these sockets only for groups inside the `net.ipv4.ping_group_range` sysctl.
Ubuntu allows all groups by default through the systemd sysctl defaults.

Check the current setting:

```bash
sysctl net.ipv4.ping_group_range
```

The output `net.ipv4.ping_group_range = 0 2147483647` allows all groups. The output `1 0` is an empty range, which allows no group.

If the range doesn't include your group, allow all groups:

1. Create `/etc/sysctl.d/99-ping.conf`:

   ```bash
   echo "net.ipv4.ping_group_range = 0 2147483647" | sudo tee /etc/sysctl.d/99-ping.conf
   ```

2. Apply the setting:

   ```bash
   sudo sysctl -p /etc/sysctl.d/99-ping.conf
   ```

The setting persists across reboots.

`setcap cap_net_raw+ep` on the `hydraping` command has no effect: `hydraping` is a Python script, and the kernel ignores file capabilities on scripts.

If ICMP is not available, HydraPing disables ping checks and keeps monitoring with the other checks (DNS, TCP, UDP, HTTP).

## Development

```bash
# Install with dev dependencies
uv sync

# Run linter with auto-fix and formatter (after code changes)
uv run ruff check --fix . && uv run ruff format .

# Run tests
uv run pytest
```

## How It Works

### Check Hierarchy

For each endpoint, HydraPing runs applicable checks based on the endpoint type:

- **IP addresses** (`8.8.8.8`, `2001:4860:4860::8888`): ICMP only (supports both IPv4 and IPv6)
- **IP:port** (`1.1.1.1:53`, `[2001:4860:4860::8888]:53`): ICMP + TCP
- **Domains** (`google.com`): DNS + ICMP + TCP to ports 80 and 443. The two ports are probed as separate chains that share the DNS layer and keep separate TCP histories, so a closed port 80 shows up as a steady problem line next to a working port 443 instead of a flickering TCP layer. With `primary_check_type = "tcp"` the graph and latency column show port 443.
- **HTTP URLs**: DNS + ICMP + TCP + HTTP request
- **HTTPS URLs**: DNS + ICMP + TCP + TLS handshake + HTTP request

If a higher-level check succeeds (e.g., HTTP), lower-level failures (e.g., ICMP) are suppressed in the Problems list to reduce noise. The order is HTTP > TLS > TCP/UDP > DNS > ICMP.

### Connection layers

ICMP runs on its own schedule. The other layers of one target run as one probe, in order: DNS, TCP connect, TLS handshake (HTTPS only), HTTP request (URLs only). Each layer is timed on its own, so the DNS time is the lookup, the TCP time is the connect, the TLS time is the handshake, and the HTTP time runs from the request to the status line. A layer runs only when the one before it succeeded. The layers after a failed one are recorded as skipped, not as failures, and the Problems list names the layer that failed.

Details worth knowing:

- The DNS layer uses the system resolver (`getaddrinfo`), so it sees `/etc/hosts` and the local stub resolver, the same as any application on the machine. With `[dns] servers` set, it queries those servers directly instead. The TCP layer connects to the first address returned, and the ICMP check pings that same address.
- The TLS layer verifies the certificate chain and the hostname with the system CA store. `insecure = true` turns verification off for one endpoint; the handshake is still measured.
- The HTTP layer sends one HTTP/1.1 `GET` with `Host`, `User-Agent: hydraping/<version>` and `Connection: close`, reads the status line and closes. Redirects are not followed: a `301` is a result, not a hop, and it counts as success while `http_success_status_max` is 399 or more. HTTP/2 and HTTP/3 are not used.
- All layers of one probe share one deadline of `timeout_seconds`, counted from the probe's start. The layer that is still running when the deadline passes fails with a message that says where the time went, for example `Timeout: probe deadline 5.0s reached (DNS took 4.6s)`, and the layers after it are skipped. The layers that finished keep their own latency.

#### Custom Primary Check Type

You can override which check type is displayed in the graph and latency column using `primary_check_type` (`dns`, `icmp`, `tcp`, `udp`, `tls` or `http`). This is useful when:
- ICMP is blocked by firewall (common for many services)
- You want to monitor a specific layer (e.g., TCP connectivity instead of ICMP, or the TLS handshake alone)
- You care more about application-layer health than network-layer connectivity

All applicable checks still run in the background, and failures are reported in the "Problems" section. Only the graph and latency display are affected by the primary check type setting.

### Architecture

- **Async-first design**: Uses `asyncio` to run multiple checks concurrently without blocking
- **Built with**:
  - `icmplib` - Async ICMP ping
  - `dnspython` - DNS queries against configured servers
  - `asyncio` streams and `ssl` from the standard library - TCP connect, TLS handshake and the raw HTTP/1.1 request
  - `Rich` - Terminal UI rendering
  - `Typer` - CLI framework
- **Probe scheduler**: every check runs on its own schedule; one slow target never delays the others. The engine runs on its own thread, and the terminal UI only reads snapshots of the results
- **Time bucketing**: results land in epoch-aligned buckets by the time they finished, so all rows share column boundaries
- **Smart filtering**: Only shows meaningful errors based on what checks succeeded

## License

MIT
