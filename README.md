# crawl_hound

A single-file, SPA-aware web crawler and JavaScript endpoint extractor.

Crawls sites — including JS-heavy SPAs (Umi, React, Angular, Vue, Webpack, Vite, Rolldown) — recursively discovers dynamically-loaded JS chunks, and extracts API endpoints from the downloaded bundles. No headless browser required.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [Output Files](#output-files)
- [How It Works](#how-it-works)
- [Authentication](#authentication)
- [Compression Handling](#compression-handling)
- [Parallelism and Performance](#parallelism-and-performance)
- [Resume and Recovery](#resume-and-recovery)
- [Domain Scoping](#domain-scoping)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

---

## Features

- **SPA-aware crawling** — follows `<script src>`, `<link rel="modulepreload">`, and dynamically-imported chunks
- **Chunk discovery** — recognizes Webpack, Umi, Vite, Rolldown, and webpack id-to-hash chunk maps
- **Endpoint extraction** — matches `fetch`, `axios`, `$.ajax`, `XMLHttpRequest`, absolute URLs, relative paths, template literals, and WebSocket URLs
- **SPA route derivation** — reconstructs `#/section/page` routes from Umi chunk names
- **Authentication** — cookies (inline, file, Netscape format), Bearer/Basic/raw tokens, and arbitrary custom headers
- **Compression-safe** — handles gzip, deflate, brotli, and zstd regardless of what urllib3 does; optional `--no-compression` bypass
- **Parallel downloads** — configurable concurrency via thread pool (default 6 workers)
- **Resume support** — interrupted runs pick up where they left off, auto-repairing any unreadable files
- **Streaming and retry** — large bundles stream to disk; network errors and 5xx retry with backoff, 4xx fails fast
- **Byte-sniff decompression** — files on disk are always plain JS text after first read

---

## Requirements

- Python 3.8+
- `requests`
- `beautifulsoup4` (optional — falls back to regex HTML parsing if missing)
- `brotli` (optional — needed to decode `Content-Encoding: br`)
- `zstandard` (optional — needed to decode `Content-Encoding: zstd`)

Install everything:

```bash
pip install -r requirements.txt
```

Or manually:

```bash
pip install requests beautifulsoup4 brotli zstandard
```

---

## Installation

The script is a single file. Two ways to make it a global command.

### Option A — Symlink (fastest)

```bash
chmod +x crawl_hound.py
ln -sf "$(pwd)/crawl_hound.py" ~/.local/bin/crawl_hound
```

Ensure `~/.local/bin` is on your `$PATH`. Then:

```bash
crawl_hound --help
```

### Option B — pipx (isolated venv)

Add a `pyproject.toml` (see sample in the repo), then:

```bash
pipx install -e .
crawl_hound --help
```

The `-e` (editable) flag keeps the command in sync with your working copy.

---

## Quick Start

```bash
# Crawl a plain site
crawl_hound --url https://example.com --output out

# Crawl only the exact subdomain, JSON output
crawl_hound --url https://beta.example.com --output out --same-subdomain --json

# Authenticated SPA crawl with slow-server-safe timeouts
crawl_hound --url https://app.example.com/dashboard --output out \
  --same-subdomain \
  --cookie "session=abc123; csrf=xyz" \
  --timeout 30 --read-timeout 120 \
  --concurrency 8

# With a bearer token
crawl_hound --url https://api.example.com --output out \
  --header "Authorization: Bearer eyJhbGci..."
```

Output lands in `--output` (default: `out/`).

---

## CLI Reference

### Core

| Flag | Default | Description |
|---|---|---|
| `--url` | required | Start URL to crawl |
| `--output` | `out` | Output directory for all files |
| `--depth` | infinite | Maximum BFS depth |
| `--verbose` | off | Print every file, chunk, and endpoint |
| `--resume` | off | Load state from existing output dir and continue |
| `--json` | off | Also write `results.json` |

### Domain scoping

| Flag | Description |
|---|---|
| (default) | Crawl parent domain and all subdomains |
| `--same-subdomain` | Crawl only the exact host of `--url` |
| `--all-domains` | Follow links to any domain |
| `--strip-query` | Strip query strings before deduplication |

### Networking

| Flag | Default | Description |
|---|---|---|
| `--timeout` | `30` | Connect timeout (seconds) |
| `--read-timeout` | `120` | Read timeout between chunks (seconds) |
| `--retries` | `3` | Download attempts per file (4xx fails immediately) |
| `--insecure` | off | Disable SSL verification |
| `--user-agent` | Chrome 124 | Override `User-Agent` |

### Authentication

| Flag | Description |
|---|---|
| `--cookie "name=val; name2=val2"` | Inline cookie string |
| `--cookie-file cookies.txt` | Netscape-format or plain cookie file |
| `--token <value>` | Authorization token |
| `--auth-type Bearer\|Basic\|None` | Auth scheme (default: `Bearer`) |
| `--header "Name: Value"` | Custom header (repeatable) |

### JS handling

| Flag | Default | Description |
|---|---|---|
| `--max-js-size` | `50` | Per-file size cap (MB) |
| `--max-chunks` | `0` | Max recursive chunk downloads (0 = unlimited) |
| `--skip-sourcemaps` | off | Skip `.map` and `.js.map` files |
| `--skip-framework` | on | Skip endpoint extraction from framework bundles |
| `--include-framework` | — | Include framework bundle endpoints |

### Compression

| Flag | Default | Description |
|---|---|---|
| `--compression` | `auto` | `auto`, `brotli-zstd`, `brotli`, `zstd`, `gzip`, `none` |
| `--no-compression` | off | Force `Accept-Encoding: identity` — bypasses all decompression issues |

### Parallelism

| Flag | Default | Description |
|---|---|---|
| `--concurrency` | `6` | Parallel download workers |
| `--preflight` | off | HEAD-check chunk URLs before downloading (slower startup, fewer wasted GETs) |

---

## Output Files

All output is written under `--output`:

```
out/
├── endpoints.txt       all URLs (pages + JS files + links + extracted)
├── pages_crawled.txt   successfully crawled pages
├── js_files.txt        JS file URLs found in HTML
├── js_endpoints.txt    API endpoints extracted from JS bodies
├── js_chunks.txt       recursively discovered chunk URLs
├── spa_routes.txt      derived SPA hash routes
├── results.json        summary (only with --json)
└── js_files/           downloaded JavaScript bundles
    ├── index-zKvqqEtx.js
    ├── react-DbFXTX6E.js
    └── ...
```

Sets are deduplicated and sorted at the end of each run. `--resume` reads these back to skip re-discovery.

---

## How It Works

### Crawl model

Breadth-first from the start URL using an explicit queue (no recursion — safe on very deep link structures):

1. **Fetch** the page with the authenticated session
2. **Extract** `<script src>`, `<link rel="modulepreload">`, `<a href>`, and inline `<script>` bodies
3. **Register** discovered URLs into state files
4. **Download and analyze** all JS files in a parallel batch
5. **Recurse** into chunks discovered inside JS
6. **Enqueue** new pages from `<a href>` (subject to domain scoping)

### Chunk discovery patterns

| Build system | Pattern | Example |
|---|---|---|
| Umi | `.async.js` names | `p__selfcare__MyInfo.async.js` |
| Umi (chunkName) | `"p__section__page"` strings | expanded to `.async.js` |
| Webpack | `import("./chunk.js")` | `import("./chunk-abc123.js")` |
| Webpack | `__webpack_require__.p + "file.js"` | `"chunk-abc123.js"` |
| Webpack | id-to-hash maps | `{3:"39731c9d"}` → `3.39731c9d.async.js` |
| Vite/Rolldown | `name-<8charHash>.js` | `react-DbFXTX6E.js` |

False positives (locale files like `ar-ma.js`, `pt.js`) are rejected by filename validation.

### Endpoint extraction patterns

The extractor matches:

- `https://...` absolute URLs
- `"/api/..."`, `"./data/..."`, `"../x/..."` relative paths
- `fetch("...")`
- `axios.get|post|put|delete|patch|request("...")`
- `$.ajax|get|post|getJSON({ url: "..." })`
- `.open("GET", "...")`
- `url|baseURL|apiUrl|endpoint: "..."`
- Template literals with `${...}` and paths
- `ws://` and `wss://` URLs

Static asset extensions are filtered out. JS URLs are filtered out (they're chunks, not endpoints).

---

## Authentication

Cookies and tokens are attached per-session. On `--resume`, previously-stored cookies are reused. A `[probe]` request at startup shows the response status and any `Set-Cookie` headers so you can verify the server accepted your credentials.

### Common sources

- **Cookies** — DevTools → Network → any request → copy the `Cookie:` header
- **Bearer tokens** — DevTools → Network → copy the `Authorization:` header value
- **Netscape cookie file** — export from Cookie-Editor extension
- **SPA session tokens** — often stored in `localStorage`, not cookies. Pass as `--header "Authorization: Bearer <token>"`

### Login-redirect detection

If the crawler requests a page and lands on `/login` or `/signin`, it prints:

```
WARNING: redirected to login/signin page -- auth may NOT be active.
```

That's a reliable signal your credentials are wrong, expired, or you started from a page that requires a different token.

---

## Compression Handling

Servers send JS with `Content-Encoding: gzip`, `deflate`, `br`, or `zstd`. Some setups break silently — urllib3 v1.x won't decode brotli unless you install the older `brotlipy` package, and some CDNs send `br` even when the client didn't advertise it.

This tool is bulletproof:

1. **Advertises only what's decodable** — `Accept-Encoding` is built from installed modules
2. **Byte-sniffs on read** — tries gzip, zlib, zstd, brotli, then raw deflate, keyed on magic bytes
3. **Text-gates output** — decompression results are rejected if they don't look like text (prevents corruption)
4. **Persists decompressed form** — files on disk become plain JS after first read
5. **`--no-compression`** — nuclear option: sends `Accept-Encoding: identity`, server returns plain bytes

If you see a file where `file X.js` reports `data` instead of `ASCII text`:

```bash
# Fix in place and re-download broken files
crawl_hound --url ... --output ... --resume

# Or bypass entirely
crawl_hound --url ... --output ... --no-compression
```

---

## Parallelism and Performance

Default settings are tuned for real-world targets:

- **6 parallel workers** — most wall-clock time is server wait, not CPU. 8–12 is safe for sites that don't rate-limit.
- **BFS** — shallow pages crawl first, so you get useful results quickly
- **Streaming** — 64 KB chunks, 50 MB per-file cap by default
- **Retries only on 5xx and network errors** — 4xx fails immediately (no wasted retries)

For a site with 50 JS bundles averaging 30 s each on a slow server, sequential download takes ~25 minutes; `--concurrency 8` finishes in ~4 minutes.

### Tuning

```bash
# Fastest, minimal safety net
crawl_hound --url ... --concurrency 12 --retries 1

# Slow server, be patient
crawl_hound --url ... --timeout 30 --read-timeout 180 --concurrency 4

# Bandwidth-limited
crawl_hound --url ... --no-compression --concurrency 2
```

---

## Resume and Recovery

`--resume` reads the existing output directory and:

- Loads all discovered URLs from `.txt` files
- Verifies every downloaded JS file is readable text (decompressing if needed)
- Marks unrecoverable files for re-download
- Continues the BFS queue from where it stopped

Safe to use after `Ctrl+C`, network failures, or the machine going to sleep. Pressing `Ctrl+C` at any time flushes state before exit.

```bash
crawl_hound --url ... --output out --resume --concurrency 8
```

---

## Domain Scoping

Three modes controlled by flags:

| Mode | Flag | Behavior |
|---|---|---|
| Parent domain (default) | none | Crawl `example.com` and any `*.example.com` |
| Exact subdomain | `--same-subdomain` | Crawl only `beta.example.com` |
| Unrestricted | `--all-domains` | Follow links anywhere |

The default is a good middle ground: covers APIs on `api.example.com` and blogs on `blog.example.com` without leaving the site's ecosystem.

---

## Troubleshooting

### All JS files are binary

Either install the decoders or bypass compression:

```bash
pip install brotli zstandard
# or
crawl_hound --url ... --no-compression
```

Then run with `--resume` — existing files are repaired automatically.

### Only 1 JS file found on an SPA

The page probably uses `<link rel="modulepreload">` (Vite/Rolldown). The current version extracts these tags. If only one file was found, verify the page isn't blocking the crawler (check `[probe]` output) or serving different HTML to non-browser clients.

### Redirected to login

Your credentials aren't being sent correctly, or they're expired:

```bash
# Verify by probing directly
curl -v -H "Cookie: session=..." https://target.com/dashboard
```

If curl works but the crawler doesn't:
- Check the cookie domain matches the target (use `--cookie`, let the script set the domain, or use `--header "Cookie: ..."` to send verbatim)
- Verify the token scheme (`--auth-type Bearer` vs `None`)

### Crawl blows up past 10,000 pages

The site has locale prefixes or tracking query strings creating near-duplicates:

```bash
# Collapse query variants
crawl_hound --url ... --strip-query

# Or bound depth
crawl_hound --url ... --depth 5
```

### urllib3 v1.x says brotli isn't supported

urllib3 v1.x uses the `brotlipy` package, not `brotli`. Either:

```bash
pip install brotlipy
# or
pip install --upgrade urllib3
# or just
crawl_hound --url ... --no-compression
```

---

## Limitations

By design, the tool does not execute JavaScript. It cannot:

- Click buttons, fill forms, or trigger UI actions
- Wait for XHR requests that only fire after user interaction
- Follow client-side router navigation (e.g. `/login` → `/dashboard` after auth)
- Discover chunks imported conditionally based on runtime state

For a fully-rendered view of an authenticated SPA, combine this tool with a headless browser (Playwright or Selenium) that logs in and dumps the DOM; feed the resulting HTML to this crawler as a starting point.

---

## License

MIT
