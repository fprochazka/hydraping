# HydraPing

Multi-protocol connection tester with live terminal UI - Because it has many "heads" (DNS, ICMP, TCP, UDP, TLS, HTTP) looking at many targets.

## Features

- **Multi-protocol checks**: ICMP ping, DNS resolution, TCP port connectivity, TLS handshake, HTTP/HTTPS requests
- **Per-layer measurement**: an HTTPS target is measured as DNS, TCP, TLS and HTTP separately, so a failure names the layer that broke
- **Live terminal UI**: Real-time updating display with latency graphs
- **Desktop window**: `hydraping gui` shows the same rows in a window, with per-layer graphs, and the latency in the top bar
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

# Open the desktop window
hydraping gui

# Start the desktop window hidden at every login
hydraping gui --install-autostart
```

### Example Output

```
Cloudflare DNS               .........................▁▁▁▁▁▁▁▁▁       8.2ms (ICMP)
Google DNS                   .........................▁▁▁▁▁▁▁▁▁      12.5ms (ICMP)
google.com:443               ......................▂▂▁▁▁▁▁▁▁▁▁▁      15.3ms (TCP:443)
Production API               ........................▃▃▄▃▃▃▃▃▃▃     145.7ms (HTTPS)

Problems:
  • ICMP unavailable (no permissions) - ping checks disabled
```

The display shows:
- **Endpoint name** - What you're monitoring
- **Latency graph** - Visual history scrolling right-to-left (color-coded by latency)
- **Current latency** - Latest measurement with check type: `ICMP`, `DNS`, `TCP:<port>`, `UDP`, `TLS`, `HTTP` or `HTTPS` for the HTTP layer of an `http` or `https` URL, or `TS direct` and `TS relay` for a Tailscale peer
- **Interface** - The network interface the kernel routes the target through, for example `via tailscale0`, looked up with `ip -j route get` at most once every 5 seconds per address. A name shows it once it has been resolved. An address the kernel has no route to, such as a VPN subnet while the VPN is down, shows `via no route`. For a Tailscale peer whose reply came through a relay, the column shows the relay instead, for example `via DERP xyz`. The column is left out when the `ip` command (iproute2) is missing or can't look up the route to 127.0.0.1, and in a terminal too narrow for it.
- **Problems** - Only relevant issues (smart filtering based on check hierarchy)

Graph colors: 🟢 Green (<50ms) · 🟡 Yellow (50-100ms) · 🟠 Orange (100-200ms) · 🔴 Red (>200ms)

A failed column shows the letter of the layer that broke: `D` DNS, `T` TCP, `S` TLS, `H` HTTP. ICMP, UDP and Tailscale failures show `!`. When the displayed layer did not run because an earlier one failed, the column shows the earlier layer's letter, and the latency column shows `FAIL (TCP)` with that layer's name.

### Desktop window

`hydraping gui` shows the same rows as the terminal UI in a window: name, latency graph, latency, layer and interface. Click a row to show a small graph for each of its layers, for example DNS, TCP, TLS and HTTP of an HTTPS target. Hover over a graph column to see the time of its bucket, its last result and how many probes in it succeeded or failed. The Problems list appears below the rows when something fails, with the same lines as the terminal UI. The window fits its height to the rows and problems, and you can drag its width.

The window needs the `gui` extra, which installs PySide6 (Qt) and dbus-fast:

```bash
uv tool install 'hydraping[gui]'
```

Without it, `hydraping gui` prints that command and exits. The extra pins PySide6 to 6.11, because PySide6 6.12.0 crashes Python 3.11 after a few minutes of use.

#### Top bar

`hydraping gui` also puts an item in the top bar, next to the clock and the system menu. The icon has one bar per chain and per target, up to five, in the graph colors: the taller and redder the bar, the slower the target. A target or chain that is down gets a red bar of full height, and grey bars mean no result yet. Next to the icon, a label shows the most important fact:

- `Jumpbox down` when a chain is broken, with the name of the broken hop;
- `2 down` when targets outside chains fail;
- `38 ms`, the slowest latency, when everything works;
- `…` before the first result.

The top-bar item updates every `tray_refresh_seconds`, 5 by default, so it doesn't flicker. The window keeps updating twice a second.

Each chain counts as one item, with the latency of its last hop and the color of its slowest hop. Targets count too, unless they have `tray = false`:

```toml
[endpoints]
targets = [
    { url = "192.0.2.1", name = "Printer", tray = false },
]
```

In Ubuntu's top bar, a click on the icon opens a menu with Open HydraPing and Quit, and a double click opens the window. While the item is in the top bar, closing the window only hides it; Quit in the menu stops HydraPing. Ctrl+C in the terminal stops it too.

Known limits:

- The item needs a desktop that shows StatusNotifierItems. Ubuntu does, through its AppIndicator extension, which is on by default. Plain GNOME without that extension shows nothing. Then HydraPing prints a line saying so, and closing the window quits HydraPing.
- The label is an Ayatana extension of the protocol. Ubuntu's top bar shows it; other desktops may show only the icon.
- The tooltip lists one line per item, but Ubuntu's top bar doesn't show tooltips.
- On Wayland, the window can't open under the icon: the desktop decides where it goes.
- On Wayland, Open HydraPing in the menu may not bring a window that is already open to the front, because the desktop sends no activation token with a menu click. A double click on the icon sends one.
- Running `hydraping` and `hydraping gui` at the same time probes every target twice.

#### One window per session

Only one `hydraping gui` runs in a desktop session. A second `hydraping gui` asks the running one to show its window, prints that it did, and exits. The running one keeps its own configuration, so a `--config` given to the second one has no effect. Quit the running one first to load another file.

The running app owns the D-Bus name `io.github.fprochazka.HydraPing` on the session bus and serves the `org.freedesktop.Application` interface there. Without a session bus, there is no such check, and each `hydraping gui` runs on its own.

#### Start at login

`hydraping gui --hidden` starts with only the top bar item, and the window opens from the item. When no top bar host is there at the start, `--hidden` waits 8 seconds for one, because at login the host may start after HydraPing, and then shows the window if none came. The same applies when the host goes away while the window is hidden: the window shows if the host isn't back within 8 seconds. So HydraPing never runs invisible. A second `hydraping gui --hidden` while HydraPing runs does nothing.

To start HydraPing at every login, run:

```bash
hydraping gui --install-autostart
```

This writes `~/.config/autostart/hydraping.desktop`, or the same file under `$XDG_CONFIG_HOME` when that is set. The entry runs `hydraping gui --hidden` with the absolute path of the `hydraping` command you ran, so install HydraPing with `uv tool install` first. A `--config` given with `--install-autostart` goes into the entry as an absolute path. The command checks that the configuration loads before it writes the entry. Run it again after you move HydraPing or the configuration file.

To stop starting HydraPing at login, run:

```bash
hydraping gui --remove-autostart
```

Both commands print the path of the file they wrote or removed.

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

    # Leave a target out of the desktop top bar; it still shows in the window and the terminal UI
    { url = "192.0.2.1", tray = false, name = "Printer" },
]

[dns]
servers = []  # Optional: DNS servers to query instead of the system resolver

[checks]
probe_interval_seconds = 1.0  # A new probe starts every second
bucket_seconds = 5.0          # One graph column covers 5 seconds
timeout_seconds = 5.0         # A probe that takes longer counts as failed
http_success_status_max = 399 # HTTP status codes up to this count as success

[ui]
graph_width = 0             # Auto-size to terminal width
tray_refresh_seconds = 5.0  # The hydraping gui top-bar item updates at most this often
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
- **Not in the top bar**: `{ url = "192.0.2.1", tray = false }` → Leave the target out of the `hydraping gui` top-bar item; not allowed on chain hops, which count as their chain
- **DNS resolver**: `{ url = "192.0.2.53", check = "dns", query = "example.com" }` → ICMP + a query for a random name under the zone; see [DNS resolver hops](#dns-resolver-hops)
- **Tailscale peer**: `{ url = "jumpbox", check = "tailscale" }` → `tailscale ping` latency and whether the path is direct or relayed; see [Tailscale hops](#tailscale-hops)

### Chains

Some targets are only reachable through others: the company intranet through a VPN jumpbox, and the jumpbox through the internet. A chain lists such hops in order, so the problems section can say where the path breaks instead of listing every target behind the break as failed:

```toml
[[chains]]
name = "Company"
hops = [
    { url = "router", name = "Router" },
    { url = "1.1.1.1", name = "Internet" },
    { url = "jumpbox.example.ts.net:22", name = "Jumpbox" },
    { url = "https://intranet.example.internal/health", name = "Intranet" },
]
```

Hops take the same formats and keys as `targets`, except `tray`, and each hop is measured like a target. The dashboard shows the chain as a header row with its hops indented below it.

The chain is broken at the first hop whose displayed check failed in the current graph column, or in the column just before it while the current one has no result yet. The problems section then shows one line for the chain, for example `Company: broken at Jumpbox (TCP: Connection refused on port 22); Router, Internet OK`. The hops after the broken one keep being probed, so a recovery shows up right away, but their own problem lines are left out, because they fail with the broken hop. The diagnosis stops at a hop that has no result yet, for example while its first probe is still running: the hops after it are not judged until it answers, so the chain's status stays empty unless a hop before it is broken.

A hop that measures the same thing as a target, or as a hop of another chain, shares its probes and its history with it. Endpoints given on the command line replace both the targets and the chains.

### DNS resolver hops

`check = "dns"` measures a DNS server instead of a host. `url` is the server's IP address, optionally with a port, and `query` is a zone the server can resolve:

```toml
[endpoints]
targets = [
    { url = "192.0.2.53", check = "dns", query = "example.internal", name = "Company DNS" },
    { url = "198.51.100.53:5353", check = "dns", query = "example.com", transport = "tcp" },
]
```

Each probe asks the server for the A record of a new random name, `hp-<8 random letters>.<zone>`. No cache on the way can hold that name, so the latency covers the whole resolver path: the server, its forwarders, and the authoritative servers of the zone. Any answer counts as success, including NXDOMAIN, because the server did resolve the question. A timeout, SERVFAIL, REFUSED or any other error code is a failure. Queries go over UDP, or over TCP with `transport = "tcp"`.

The server's address is also pinged, so the row has ICMP and DNS layers, and DNS is the one graphed. The keys `protocol`, `ip_version`, `insecure`, `probe_hex` and `probe_ascii` don't apply to a DNS hop and are rejected. It works in `targets` and as a chain hop.

### Tailscale hops

`check = "tailscale"` measures a peer of your tailnet with `tailscale ping`. `url` is the peer's name or Tailscale IP, as `tailscale ping` takes it:

```toml
[[chains]]
name = "Company"
hops = [
    { url = "1.1.1.1", name = "Internet" },
    { url = "jumpbox.example.ts.net", check = "tailscale", name = "Jumpbox VPN" },
    { url = "https://intranet.example.internal/health", name = "Intranet" },
]
```

Each probe runs `tailscale ping -c 1 --timeout <timeout_seconds>s <peer>`. The latency is the one `tailscale ping` reports, measured at the Tailscale layer, so a firewall on either host doesn't affect it. The latency column shows `(TS direct)` when the reply came over a direct path between the two hosts, and `(TS relay)` when it came through a relay. For a relayed reply, the interface column names the relay: `via DERP xyz` for Tailscale's relay servers, with the region code, or `via peer relay` for a relay node in your tailnet. A path that stays relayed usually means NAT traversal failed, which costs latency.

A probe fails when the `tailscale` command is missing, when tailscaled isn't running, when the peer doesn't exist, or when no reply comes within the timeout. The problem line says which one. The graph shows `!` for a failed probe. The row has only the Tailscale layer: there is no ICMP, because tailscale resolves the peer name itself. The keys `protocol`, `ip_version`, `insecure`, `probe_hex`, `probe_ascii`, `query` and `transport` don't apply to a Tailscale hop and are rejected.

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

The Tailscale integration test is skipped unless `HYDRAPING_TEST_TAILSCALE_PEER` names a peer of your tailnet to ping, for example `HYDRAPING_TEST_TAILSCALE_PEER=jumpbox uv run pytest`.

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
  - `PySide6` (Qt) - Desktop window, optional
  - `dbus-fast` - Top-bar item over D-Bus, optional
  - `Typer` - CLI framework
- **Probe scheduler**: every check runs on its own schedule; one slow target never delays the others. The engine runs on its own thread, and both the terminal UI and the desktop window only read snapshots of the results
- **Time bucketing**: results land in epoch-aligned buckets by the time they finished, so all rows share column boundaries
- **Smart filtering**: Only shows meaningful errors based on what checks succeeded

## License

MIT
