# HydraPing Implementation Plan

## Project Overview
A multi-protocol connection tester with live terminal UI showing DNS, ping, and port connectivity for multiple endpoints simultaneously.

## Phase 1: Project Setup

### 1.1 Initialize Poetry Project
- [x] venv already exists with poetry installed
- [ ] Initialize new poetry project with appropriate metadata
- [ ] Configure for binary distribution via pipx
- [ ] Add core dependencies: `icmplib`, `dnspython`, `rich`, `typer`, `toml`
- [ ] Add dev dependencies: `ruff`, `pytest`
- [ ] Configure ruff linting/formatting

### 1.2 Project Structure
```
hydraping/
├── pyproject.toml
├── README.md
├── SPEC.md
├── PLAN.md (this file)
├── hydraping/
│   ├── __init__.py
│   ├── __main__.py           # Entry point for `python -m hydraping`
│   ├── cli.py                # Typer CLI interface
│   ├── config.py             # Configuration loading/parsing
│   ├── models.py             # Data models (Endpoint, CheckResult, etc.)
│   ├── checkers/
│   │   ├── __init__.py
│   │   ├── base.py           # Base checker class
│   │   ├── dns.py            # DNS resolver checker
│   │   ├── icmp.py           # ICMP ping checker
│   │   ├── tcp.py            # TCP port checker
│   │   └── http.py           # HTTP/HTTPS checker
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── dashboard.py      # Main terminal UI
│   │   ├── graph.py          # Latency graph rendering
│   │   └── table.py          # Endpoint table rendering
│   └── orchestrator.py       # Async orchestration of all checks
└── tests/
    ├── __init__.py
    ├── test_checkers.py
    ├── test_config.py
    └── test_models.py
```

## Phase 2: Core Data Models

### 2.1 Endpoint Types
- [ ] Create base `Endpoint` class/dataclass
- [ ] Subclasses: `IPEndpoint`, `IPPortEndpoint`, `DomainEndpoint`, `HTTPEndpoint`
- [ ] Parse endpoint strings into appropriate types
- [ ] Determine which checks apply to each endpoint type:
  - IP: ICMP only
  - IP:port: ICMP + TCP
  - Domain: DNS + ICMP (on resolved IP)
  - HTTP/HTTPS: DNS + ICMP + TCP + HTTP request

### 2.2 Check Results
- [ ] `CheckResult` dataclass with fields:
  - timestamp
  - success: bool
  - latency: float | None
  - error_message: str | None
  - check_type: str (dns/icmp/tcp/http)

## Phase 3: Configuration System

### 3.1 TOML Configuration
- [ ] Define config schema:
  ```toml
  [endpoints]
  targets = [
      "8.8.8.8",
      "1.1.1.1:53",
      "google.com",
      "https://example.com/health"
  ]

  [dns]
  custom_servers = ["8.8.8.8", "1.1.1.1"]  # Optional

  [checks]
  interval_seconds = 1.0
  timeout_seconds = 5.0

  [ui]
  graph_width = 60  # Number of bars in graph
  ```
- [ ] Implement `load_config()` from `~/.config/hydraping/settings.toml`
- [ ] Create default config if file doesn't exist
- [ ] Validate configuration

## Phase 4: Async Checkers

### 4.1 Base Checker
- [ ] Abstract base class with `async def check()` method
- [ ] Common timeout handling
- [ ] Error catching and result formatting

### 4.2 DNS Checker
- [ ] Use `dnspython` async resolver
- [ ] Query against system DNS
- [ ] Optionally query against custom DNS servers (if configured)
- [ ] Measure resolution time
- [ ] Return resolved IPs + latency

### 4.3 ICMP Checker
- [ ] Use `icmplib.async_ping()`
- [ ] Handle permission issues (needs root/CAP_NET_RAW)
- [ ] Return latency in milliseconds
- [ ] Detect packet loss

### 4.4 TCP Checker
- [ ] Use `asyncio.open_connection()` for TCP handshake
- [ ] Measure connection time
- [ ] Close connection immediately after success

### 4.5 HTTP Checker
- [ ] Use `aiohttp` for async HTTP requests
- [ ] Support both HTTP and HTTPS
- [ ] Check status codes (>=300 is problem)
- [ ] Measure full request/response time
- [ ] Follow redirects (configurable)

## Phase 5: Orchestration

### 5.1 Check Orchestrator
- [ ] Main async loop that runs checks for all endpoints
- [ ] Run checks at configured interval (default 1s)
- [ ] For each endpoint, run applicable checks concurrently
- [ ] Store results in a rolling buffer (for graph history)
- [ ] Emit results to UI

### 5.2 Result Aggregation
- [ ] Maintain history of last N results per endpoint
- [ ] Calculate statistics (avg latency, packet loss %)
- [ ] Detect and categorize problems:
  - DNS timeout
  - DNS resolution failed
  - ICMP unreachable
  - TCP connection refused
  - HTTP status >= 300
  - HTTP timeout

## Phase 6: Terminal UI

### 6.1 Live Dashboard (using Rich)
- [ ] Use `rich.live.Live` for auto-updating display
- [ ] Main layout (simple, no borders):
  ```
  Endpoint            Graph                                  Latency
  8.8.8.8             ░░░░░░░░░░░░░░░░░░░░▂▄▆█▆▄▂            2.3ms
  google.com          ░░░░░░░░░░░░░░░▁▂▃▅▇█▅▃▂▁░          15.2ms
  https://ex.com      ░░░░░░░░░░░░░░░░░░▄▆█▆▄▂▁░          45.8ms

  Problems:
  • google.com: DNS resolution slow (>100ms)
  • https://ex.com: HTTP 503 Service Unavailable
  ```
- [ ] Column width calculation:
  - Endpoint column: fixed width based on longest endpoint name (or max width)
  - Graph column: **fixed width** (calculated as: terminal_width - endpoint_width - latency_width - spacing)
  - Latency column: fixed width (~10 chars for "999.99ms")
  - All graphs must be exactly the same width so latency column aligns
- [ ] Graph rendering:
  - Starts from the right side, new data appears on right
  - Scrolls right-to-left as time passes
  - Always pad with dots (░) on the left to maintain fixed graph width
  - Each graph is exactly the same character width regardless of data points

### 6.2 Graph Rendering
- [ ] Inspired by prettyping: bars with height and color based on latency
- [ ] Color scheme:
  - Green: <50ms
  - Yellow: 50-150ms
  - Orange: 150-300ms
  - Red: >300ms
  - Gray/dim: timeout/error
- [ ] Use Unicode block characters (░▁▂▃▄▅▆▇█)
- [ ] Adjust graph width to terminal size

### 6.3 Problem Display
- [ ] Below the table, show human-readable problems
- [ ] Group by endpoint
- [ ] Only show if there are active problems
- [ ] Clear when problems resolve

### 6.4 Keyboard Controls (Optional Enhancement)
- [ ] `q` or `Ctrl+C`: quit
- [ ] `r`: reset statistics
- [ ] `↑/↓`: scroll if many endpoints

## Phase 7: CLI Interface

### 7.1 Typer Commands
- [ ] `hydraping` - Start interactive monitoring (default)
- [ ] `hydraping --config <path>` - Use custom config file
- [ ] `hydraping init` - Create default config file
- [ ] `hydraping add <endpoint>` - Add endpoint to config
- [ ] `hydraping version` - Show version

### 7.2 Arguments/Flags
- [ ] `--interval` - Override check interval
- [ ] `--timeout` - Override timeout
- [ ] `--no-dns` - Skip DNS checks
- [ ] `--no-icmp` - Skip ICMP checks

## Phase 8: Binary Distribution

### 8.1 Poetry Configuration
- [ ] Set up `[tool.poetry.scripts]` for CLI entry point
- [ ] Configure package metadata
- [ ] Ensure compatible with pipx installation

### 8.2 Documentation
- [ ] README with installation instructions
- [ ] Usage examples
- [ ] Configuration file documentation

## Phase 9: Testing & Polish

### 9.1 Testing
- [ ] Unit tests for checkers
- [ ] Config parsing tests
- [ ] Endpoint parsing tests
- [ ] Mock network responses for reliability

### 9.2 Error Handling
- [ ] Graceful handling of missing permissions (ICMP)
- [ ] Network unreachable
- [ ] Invalid configuration
- [ ] Terminal too small

### 9.3 Performance
- [ ] Ensure async operations don't block
- [ ] Optimize UI refresh rate
- [ ] Handle 100+ endpoints efficiently

## Implementation Order

1. **Start Here**: Project setup + basic structure
2. Core models (Endpoint, CheckResult)
3. Configuration system
4. One checker at a time (ICMP → DNS → TCP → HTTP)
5. Basic orchestrator (single endpoint, single check)
6. Expand orchestrator (multiple endpoints, multiple checks)
7. Basic UI (table only, no graph)
8. Add graph rendering
9. Add problem display
10. CLI interface
11. Testing
12. Polish & distribution

## Dependencies Summary

### Core
- `icmplib` - ICMP ping (async)
- `dnspython` - DNS resolution
- `aiohttp` - HTTP requests
- `rich` - Terminal UI
- `typer` - CLI framework
- `tomli` (or use stdlib `tomllib` in Python 3.11+) - TOML parsing

### Dev
- `ruff` - Linting & formatting
- `pytest` - Testing
- `pytest-asyncio` - Async test support

## Notes

- ICMP requires root/CAP_NET_RAW on Linux - handle gracefully if not available
- Consider using `asyncio.gather()` for concurrent checks
- Rich's Live display updates at ~4Hz by default
- Terminal width detection: `rich.console.Console().width`
- Graph should scroll smoothly (right to left) with each new data point
