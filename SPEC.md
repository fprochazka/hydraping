# HydraPing - Because it has many "heads" (DNS, NC, ICMP) looking at many targets.

* There is venv already inited with poetry installed, start a new project whose purpose will be to provide a binary for connection checking. 
* Make sure to install ruff, and the whole project will be distributed using pipx. 
* I really like /home/fprochazka/devel/libs/denilsonsa/prettyping, but it's not enough. 
* If you need any inspiration around project structure look at /home/fprochazka/devel/tools/glab-search-code
* I want to be able to configure endpoints to test (ip, ip:port, domain, http(s) endpoint)
* For each check type I want to test all the connection mechanisms that make sense (i.e. for http endpoint check: dns, ping, full http request; but for ip check just ping)
* I want there to be a `~/.config/hydraping/settings.toml` config file, that will store the user's configuration (endpoints, etc.)
* Dns check will be always done against system DNS but optionally also against custom DNS servers (if configured)
* Once configured, I want the hydraping to be "interactive" terminal UI
    * each endpoint will be a line in a table, the endpoints "column" will be aligned to be the same width
    * then a graph column, that will stretch to accomodate terminal width, the graph will be bars with height and color symbolizing latency like prettyping does, the graph will move from right to left as timee passes 
    * then a latency column
* Under the table, there will be textual description of problems (dns timeout, http >=300, ip unreachable, ...) per endpoint, if there are no problems then no description will be shown.



----

# Research by gemini: 

To implement a high-performance, multi-endpoint connection tester like `prettyping`, you'll want to prioritize **asynchronous execution**. 
Since network tests are "I/O bound" (your code spends most of its time waiting for a response), using an `async` approach allows you to check 100 endpoints almost as fast as checking one.

Here are the best Python libraries and tools for each part of your project:

### 1. The Core Testing Engines

* **For ICMP (Ping): `icmplib**`
  * **Why:** It’s modern, pure Python, and supports `async`. Unlike the system `ping` command, it doesn't require you to parse text output, giving you raw latency numbers and packet loss data directly.
  * *Note:* Ping often requires root/admin privileges to create "raw sockets."
* **For DNS Checks: `dnspython**`
  * **Why:** The gold standard for DNS in Python. You can check not just if a domain resolves, but verify specific records (A, MX, TXT) and measure how long the DNS server takes to respond.
* **For Port Checks (Netcat-style): `asyncio` (Built-in)**
  * **Why:** You don't actually need an external library like `nc`. Python's built-in `asyncio.open_connection()` can attempt a TCP handshake. If it succeeds, the port is open; if it times out or is refused, the service is down.

### 2. Making it "Pretty" (The UI)

Since you mentioned `prettyping`, you likely want a beautiful terminal interface with live updates.

* **`Rich` (Highly Recommended):** * This is the best library for terminal formatting. It has built-in support for **Live Displays**, **Progress Bars**, **Tables**, and **Layouts**. You can use `rich.live` to create a dashboard that updates every few milliseconds as pings return.
* **`Textual`:** * If you want to go a step further and create a TUI (Terminal User Interface) with mouse support and clickable tabs (e.g., one tab for Ping, one for DNS), use Textual. It’s built by the same creators as Rich.
* **`Plotext`:** * If you want to recreate the "graphing" look of `prettyping` (showing latency trends over time) inside the terminal, `plotext` allows you to draw line charts and scatter plots using ASCII characters.

### 3. Orchestration & Concurrency

* **`asyncio`:** This should be the backbone of your app. It will allow you to fire off a DNS query, a TCP check, and a Ping simultaneously without one waiting for the other.
* **`Typer`:** For the command-line interface logic (handling arguments like `--timeout` or `--targets`). It’s much more modern and "developer-friendly" than the standard `argparse`.

### A "Pro" Tip for your DNS Check

Instead of just checking if `google.com` resolves, your tool could perform a **"DNS Health Check"** by querying multiple DNS providers (like 8.8.8.8 and 1.1.1.1) to see if there is a discrepancy in the results, which is a common cause of "it works on my machine" network issues.
