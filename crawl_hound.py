#!/usr/bin/env python3
"""
crawl_hound.py - SPA-aware web crawler and JS endpoint extractor.

Crawls a target site (including modern JS SPAs built with Umi, React,
Angular, Vue, Webpack, Vite, Rolldown), recursively discovers
dynamically-loaded JS chunks, extracts API endpoints from downloaded
bundles, and safely handles compressed (gzip/deflate/brotli/zstd) JS
regardless of what the server or urllib3 version does.
"""

import argparse
import concurrent.futures
import gzip
import hashlib
import os
import re
import sys
import threading
import time
import urllib.parse
import zlib
from collections import deque

try:
    import requests
except ImportError:
    print("This tool requires the 'requests' package. Install with: pip install requests")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except ImportError:
    HAVE_BS4 = False

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    URLLIB3_VERSION = urllib3.__version__
except Exception:
    URLLIB3_VERSION = "unknown"

# Optional decompression backends
try:
    import brotli as _brotli_mod
    HAVE_BROTLI = True
    _BROTLI_MODE = "brotli"
except ImportError:
    try:
        import brotlicffi as _brotli_mod
        HAVE_BROTLI = True
        _BROTLI_MODE = "brotlicffi"
    except ImportError:
        HAVE_BROTLI = False
        _BROTLI_MODE = ""

try:
    import zstandard as _zstd_mod
    HAVE_ZSTD = True
except ImportError:
    HAVE_ZSTD = False


# =============================================================================
# Reporter
# =============================================================================

class Reporter:
    SPINNER = "|/-\\"

    def __init__(self, verbose=False):
        self.verbose = verbose
        self._spin_i = 0
        self._last_len = 0
        self._is_tty = sys.stdout.isatty()
        self._lock = threading.Lock()
        self.counters = {"pages": 0, "js_files": 0, "chunks": 0, "endpoints": 0, "interesting": 0}

    def _spin(self):
        c = self.SPINNER[self._spin_i % len(self.SPINNER)]
        self._spin_i += 1
        return c

    def status(self, msg):
        if self.verbose or not self._is_tty:
            return
        with self._lock:
            line = "  %s %s" % (self._spin(), msg)
            pad = max(self._last_len - len(line), 0)
            sys.stdout.write("\r" + line + (" " * pad))
            sys.stdout.flush()
            self._last_len = len(line)

    def clear_status(self):
        if self.verbose or not self._is_tty:
            return
        with self._lock:
            if self._last_len:
                sys.stdout.write("\r" + (" " * self._last_len) + "\r")
                sys.stdout.flush()
                self._last_len = 0

    def log(self, msg, always=False):
        if self.verbose or always:
            self.clear_status()
            print(msg)

    def detail(self, msg):
        if self.verbose:
            self.clear_status()
            print(msg)

    def live_status(self):
        self.status(
            "pages:%d  js:%d  chunks:%d  endpoints:%d  interesting:%d" % (
                self.counters["pages"], self.counters["js_files"],
                self.counters["chunks"], self.counters["endpoints"],
                self.counters["interesting"],
            )
        )


REPORTER = Reporter()


# =============================================================================
# CLI
# =============================================================================

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="crawl_hound.py",
        description="Crawl web targets (including JS SPAs) and extract API endpoints from JS bundles.",
    )
    p.add_argument("--url", required=True, help="Start URL to crawl (required)")
    p.add_argument("--output", default="out", help="Output directory (default: out)")
    p.add_argument("--depth", type=int, default=None, help="Max crawl depth (default: infinite)")

    # Domain scoping
    p.add_argument("--all-domains", action="store_true",
                    help="Do NOT restrict crawl to the start URL's domain")
    p.add_argument("--same-subdomain", action="store_true",
                    help="Restrict crawl to the EXACT host of --url")
    p.add_argument("--strip-query", action="store_true",
                    help="Strip query strings from URLs before deduplication")

    # Network
    p.add_argument("--timeout", type=float, default=30,
                    help="Connect timeout in seconds (default: 30)")
    p.add_argument("--read-timeout", type=float, default=120,
                    help="Read timeout between chunks in seconds (default: 120)")
    p.add_argument("--retries", type=int, default=3,
                    help="Download attempts per file (default: 3)")

    # JS handling
    p.add_argument("--max-js-size", type=float, default=50,
                    help="Max JS file size in MB (default: 50)")
    p.add_argument("--max-chunks", type=int, default=0,
                    help="Max recursive chunk downloads (0 = unlimited)")
    p.add_argument("--skip-sourcemaps", action="store_true",
                    help="Skip .map / .js.map files")
    p.add_argument("--skip-framework", dest="skip_framework", action="store_true", default=True,
                    help="Skip endpoint extraction from framework bundles. Default: on")
    p.add_argument("--include-framework", dest="skip_framework", action="store_false",
                    help="Include framework bundle endpoint extraction")

    # Auth
    p.add_argument("--cookie", default=None, help="Inline cookie string")
    p.add_argument("--cookie-file", default=None, help="Cookie file (Netscape or plain)")
    p.add_argument("--token", default=None, help="Bearer/raw auth token")
    p.add_argument("--auth-type", default="Bearer", choices=["Bearer", "Basic", "None"],
                    help="Auth scheme for --token (default: Bearer)")
    p.add_argument("--header", action="append", default=[], help="Custom header 'Name: Value'")
    p.add_argument("--user-agent", default=None, help="Override User-Agent header")
    p.add_argument("--insecure", action="store_true", help="Disable SSL verification")

    # Compression
    p.add_argument("--no-compression", action="store_true",
                    help="Send 'Accept-Encoding: identity' - bypasses all decompression issues. "
                         "Slower on large bundles but 100%% reliable.")
    p.add_argument("--compression", default="auto",
                    choices=["auto", "brotli-zstd", "brotli", "zstd", "gzip", "none"],
                    help="Force a specific Accept-Encoding. Default: auto (best available)")

    # Parallelism / optimization
    p.add_argument("--concurrency", type=int, default=6,
                    help="Parallel download workers (default: 6). Set to 1 for sequential.")
    p.add_argument("--preflight", action="store_true",
                    help="HEAD-check chunk URLs before downloading (slower startup, fewer wasted GETs)")
    p.add_argument("--no-preflight", dest="preflight", action="store_false",
                    help="Skip HEAD preflight (default)")

    # Output
    p.add_argument("--verbose", action="store_true", help="Full detailed log")
    p.add_argument("--resume", action="store_true", help="Resume from existing output dir")
    p.add_argument("--json", action="store_true", help="Write results.json summary")

    p.set_defaults(preflight=False)
    return p


# =============================================================================
# Session Builder
# =============================================================================

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

CHROME_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _accept_encoding(args):
    if args.no_compression or args.compression == "none":
        return "identity"
    if args.compression == "brotli-zstd":
        return "gzip, deflate, br, zstd"
    if args.compression == "brotli":
        return "gzip, deflate, br"
    if args.compression == "zstd":
        return "gzip, deflate, zstd"
    if args.compression == "gzip":
        return "gzip, deflate"
    # auto
    parts = ["gzip", "deflate"]
    if HAVE_BROTLI:
        parts.append("br")
    if HAVE_ZSTD:
        parts.append("zstd")
    return ", ".join(parts)


def parse_cookie_string(s):
    cookies = {}
    if not s:
        return cookies
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, val = part.split("=", 1)
        cookies[name.strip()] = val.strip()
    return cookies


def parse_cookie_file(path):
    cookies = {}
    if not path or not os.path.isfile(path):
        return cookies
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                fields = line.split("\t")
                if len(fields) >= 7:
                    cookies[fields[5].strip()] = fields[6].strip()
            elif "=" in line:
                for part in line.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k.strip()] = v.strip()
    return cookies


def make_session(args, start_url, base_cookies=None):
    """Create a fresh requests.Session configured with the CLI's auth and headers.
    Each parallel worker calls this - Session objects are not thread-safe."""
    session = requests.Session()
    session.verify = not args.insecure

    headers = dict(CHROME_HEADERS)
    headers["User-Agent"] = args.user_agent or DEFAULT_UA
    headers["Accept-Encoding"] = _accept_encoding(args)

    for h in args.header:
        if ":" in h:
            name, val = h.split(":", 1)
            headers[name.strip()] = val.strip()

    if args.token:
        if args.auth_type and args.auth_type.lower() != "none":
            headers["Authorization"] = "%s %s" % (args.auth_type, args.token)
        else:
            headers["Authorization"] = args.token

    session.headers.update(headers)

    parsed = urllib.parse.urlsplit(start_url)
    host = parsed.hostname or ""
    parent = ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host

    cli_cookies = {}
    cli_cookies.update(parse_cookie_string(args.cookie))
    cli_cookies.update(parse_cookie_file(args.cookie_file))

    for name, val in cli_cookies.items():
        session.cookies.set(name, val, domain=host, path="/")
        if parent and parent != host:
            session.cookies.set(name, val, domain="." + parent, path="/")

    if base_cookies:
        for c in base_cookies:
            try:
                session.cookies.set_cookie(c)
            except Exception:
                pass

    return session


def build_session(args, start_url):
    """Return (session, cookie_count, auth_header_set)."""
    session = make_session(args, start_url)
    n = len(session.cookies)
    auth_set = "Authorization" in session.headers
    return session, n, auth_set


# =============================================================================
# Small helpers
# =============================================================================

_STRIP_QUERY = False


def ensure_dirs(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "js_files"), exist_ok=True)


def domain_of(url):
    return urllib.parse.urlsplit(url).netloc.lower()


def registrable_domain(host):
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def same_domain(url, base_domain):
    host = domain_of(url)
    return (host == base_domain
            or host.endswith("." + base_domain)
            or registrable_domain(host) == registrable_domain(base_domain))


def same_host(url, base_host):
    return domain_of(url) == base_host.lower()


def host_allowed(url, args, base_domain):
    if args.all_domains:
        return True
    if args.same_subdomain:
        return same_host(url, base_domain)
    return same_domain(url, base_domain)


def normalize_url(candidate, base_url):
    if not candidate:
        return None
    candidate = candidate.strip().strip("'\"")
    if candidate.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
        return None
    try:
        joined = urllib.parse.urljoin(base_url, candidate)
    except Exception:
        return None
    joined, _ = urllib.parse.urldefrag(joined)
    if _STRIP_QUERY:
        joined = urllib.parse.urlsplit(joined)._replace(query="").geturl()
    return joined


STATIC_EXTS = (".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2",
               ".ttf", ".eot", ".ico", ".pdf", ".xml", ".txt", ".html", ".htm")

FRAMEWORK_NAME_RE = re.compile(
    r'^(vendors?|runtime|umi|polyfill|react(-dom)?|redux|moment|antd|lodash|'
    r'core-js|regenerator|webpack|rolldown)[-.]',
    re.IGNORECASE,
)


def is_framework_bundle(url):
    name = os.path.basename(urllib.parse.urlsplit(url).path)
    return bool(FRAMEWORK_NAME_RE.match(name))


def is_static_asset(url):
    return urllib.parse.urlsplit(url).path.lower().endswith(STATIC_EXTS)


def is_js_url(url):
    return urllib.parse.urlsplit(url).path.lower().endswith((".js", ".mjs", ".cjs"))


def safe_filename(url, output_dir):
    parsed = urllib.parse.urlsplit(url)
    name = os.path.basename(parsed.path) or "index.js"
    target = os.path.join(output_dir, "js_files", name)
    if os.path.exists(target):
        base, ext = os.path.splitext(name)
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
        name = "%s.%s%s" % (base, h, ext)
        target = os.path.join(output_dir, "js_files", name)
    return target


def append_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_sorted_set(path, items):
    with open(path, "w", encoding="utf-8") as f:
        for item in sorted(items):
            f.write(item + "\n")


INTERESTING_KEYWORDS = ("auth", "login", "api", "token", "admin", "user", "password",
                         "secret", "key", "config", "v1", "v2", "v3", "graphql", "rest")


def tag_interesting(endpoint):
    low = endpoint.lower()
    return [kw for kw in INTERESTING_KEYWORDS if kw in low]


# =============================================================================
# Compression / decode
# =============================================================================

def _looks_like_text(raw):
    if not raw:
        return False
    sample = raw[:512]
    text_chars = sum(1 for b in sample if 0x20 <= b < 0x7f or b in (0x09, 0x0a, 0x0d))
    return (text_chars / len(sample)) > 0.90


def _decompress_bytes(raw):
    """Return (data, was_compressed). Never corrupts plain text."""
    if not raw:
        return raw, False
    if _looks_like_text(raw):
        return raw, False

    # gzip
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw), True
        except Exception:
            pass

    # zlib
    if raw[:1] == b"\x78":
        try:
            return zlib.decompress(raw), True
        except Exception:
            pass

    # zstd (with magic)
    if HAVE_ZSTD and raw[:4] == b"\x28\xb5\x2f\xfd":
        try:
            return _zstd_mod.ZstdDecompressor().decompress(raw), True
        except Exception:
            pass

    # zstd (magicless, try after other checks)
    if HAVE_ZSTD:
        try:
            out = _zstd_mod.ZstdDecompressor().decompress(raw, max_output_size=200 * 1024 * 1024)
            if out and _looks_like_text(out):
                return out, True
        except Exception:
            pass

    # brotli
    if HAVE_BROTLI:
        try:
            out = _brotli_mod.decompress(raw)
            if out and _looks_like_text(out):
                return out, True
        except Exception:
            pass

    # raw deflate
    try:
        out = zlib.decompress(raw, -15)
        if out and _looks_like_text(out):
            return out, True
    except Exception:
        pass

    return raw, False


def load_js_text(path):
    """Read a JS file from disk, decompress if needed, and persist decompressed form.
    Returns (text, was_decompressed) or (None, False) if still unreadable."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, False
    if not raw:
        return None, False

    decoded, was = _decompress_bytes(raw)
    if was:
        try:
            with open(path, "wb") as f:
                f.write(decoded)
        except OSError:
            pass

    try:
        text = decoded.decode("utf-8", errors="ignore")
    except Exception:
        return None, was

    if not text:
        return None, was

    sample = text[:2000]
    printable = sum(1 for c in sample if c.isprintable() or c in "\n\r\t")
    if (printable / len(sample)) < 0.85:
        return None, was
    return text, was


# =============================================================================
# Endpoint extraction
# =============================================================================

ENDPOINT_PATTERNS = [
    re.compile(r'https?://[^\s"\'<>\\)\]]+'),
    re.compile(r'["\'](\.{0,2}/[a-zA-Z0-9_\-./%]+)["\']'),
    re.compile(r'fetch\(\s*["\']([^"\']+)["\']'),
    re.compile(r'axios\.(?:get|post|put|delete|patch|request)\(\s*["\']([^"\']+)["\']'),
    re.compile(r'\$\.(?:ajax|get|post|getJSON)\(\s*\{?\s*(?:url\s*:\s*)?["\']([^"\']+)["\']'),
    re.compile(r'\.open\(\s*["\'][A-Z]+["\']\s*,\s*["\']([^"\']+)["\']'),
    re.compile(r'(?:url|baseURL|apiUrl|endpoint)\s*:\s*["\']([^"\']+)["\']'),
    re.compile(r'`([^`]*\$\{[^}]*\}[^`]*/[^`]*)`'),
    re.compile(r'wss?://[^\s"\'<>\\)\]]+'),
]


def extract_endpoints_from_text(text, base_url):
    found = set()
    for pattern in ENDPOINT_PATTERNS:
        for m in pattern.finditer(text):
            candidate = m.group(1) if m.groups() else m.group(0)
            if not candidate:
                continue
            url = normalize_url(candidate, base_url)
            if not url or is_js_url(url) or is_static_asset(url):
                continue
            found.add(url)
    return found


# =============================================================================
# Chunk extraction
# =============================================================================

CHUNK_SIGNAL_PATTERNS = [
    re.compile(r'import\(\s*["\']([^"\']+\.m?js)["\']\s*\)'),
    re.compile(r'__webpack_require__\.p\s*\+\s*["\']([^"\']+\.js)["\']'),
    re.compile(r'["\']([a-zA-Z0-9_\-./]+\.async\.js)["\']'),
    re.compile(r'["\']([pP]__[a-zA-Z0-9_]+)["\']'),
]

WEBPACK_ID_HASH_MAP_RE = re.compile(r'\{(\s*"?[0-9]+"?\s*:\s*"[0-9a-fA-F]{6,}"\s*,?\s*)+\}')
WEBPACK_ID_HASH_ENTRY_RE = re.compile(r'"?([0-9]+)"?\s*:\s*"([0-9a-fA-F]{6,})"')

CHUNK_NAME_VALID = re.compile(
    r'('
    r'\.async\.js$'
    r'|^vendors.*\.js$'
    r'|^chunk[-.][0-9a-fA-F]+\.m?js$'
    r'|^runtime\.[0-9a-fA-F]+\.m?js$'
    r'|^webpack-.*'
    r'|\.[0-9a-fA-F]{6,}\.m?js$'
    r'|[-.][a-zA-Z0-9_-]{6,}\.m?js$'
    r')'
)


def looks_like_valid_chunk_name(name):
    return bool(CHUNK_NAME_VALID.search(name))


def extract_id_hash_chunks(text, base_url, session=None, args=None, state=None):
    found = set()
    known_suffix = state.get("chunk_suffix") if state else None

    for map_match in WEBPACK_ID_HASH_MAP_RE.finditer(text):
        entries = WEBPACK_ID_HASH_ENTRY_RE.findall(map_match.group(0))
        if len(entries) < 2:
            continue

        if known_suffix is None and session and args and state:
            probe_id, probe_hash = entries[0]
            for suffix in (".async.js", ".chunk.js"):
                probe_url = normalize_url("%s.%s%s" % (probe_id, probe_hash, suffix), base_url)
                if not probe_url:
                    continue
                try:
                    resp = session.head(probe_url, timeout=(args.timeout, args.timeout),
                                        allow_redirects=True)
                    if resp.status_code != 405 and 200 <= resp.status_code < 400:
                        known_suffix = suffix
                        state["chunk_suffix"] = suffix
                        break
                except requests.exceptions.RequestException:
                    continue

        suffixes = (known_suffix,) if known_suffix else (".async.js", ".chunk.js")
        for chunk_id, chunk_hash in entries:
            for suffix in suffixes:
                url = normalize_url("%s.%s%s" % (chunk_id, chunk_hash, suffix), base_url)
                if url:
                    found.add(url)
    return found


def extract_chunks_from_text(text, base_url, session=None, args=None, state=None):
    found = set()
    for pattern in CHUNK_SIGNAL_PATTERNS:
        for m in pattern.finditer(text):
            candidate = m.group(1)
            if not candidate:
                continue
            if re.match(r'^[pP]__[a-zA-Z0-9_]+$', candidate):
                candidate = candidate + ".async.js"
            name = os.path.basename(candidate)
            if not looks_like_valid_chunk_name(name):
                continue
            url = normalize_url(candidate, base_url)
            if url:
                found.add(url)
    found |= extract_id_hash_chunks(text, base_url, session=session, args=args, state=state)
    return found


SPA_ROUTE_RE = re.compile(r'[pP]__([a-zA-Z0-9_]+)__([a-zA-Z0-9_]+)\.async\.js$')


def derive_spa_route(chunk_url, start_url):
    name = os.path.basename(urllib.parse.urlsplit(chunk_url).path)
    m = SPA_ROUTE_RE.search(name)
    if not m:
        return None
    return start_url.rstrip("/") + "/#/" + m.group(1) + "/" + m.group(2)


# =============================================================================
# JS download
# =============================================================================

def human_size(n):
    return "%.2f MB" % (n / (1024.0 * 1024.0))


def download_js(session, url, referer, args, dest_path):
    """Download one JS file. Returns dest_path on success, None on failure."""
    max_bytes = int(args.max_js_size * 1024 * 1024)
    headers = {"Referer": referer} if referer else {}

    for attempt in range(1, args.retries + 1):
        try:
            resp = session.get(url, headers=headers, stream=True,
                               timeout=(args.timeout, args.read_timeout))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < args.retries:
                time.sleep(2 ** attempt)
                continue
            REPORTER.detail("     failed %s: %s" % (url, e))
            return None

        if resp.status_code >= 500:
            resp.close()
            if attempt < args.retries:
                time.sleep(2 ** attempt)
                continue
            return None

        if resp.status_code >= 400:
            resp.close()
            return None

        total = 0
        try:
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        resp.close()
                        try:
                            os.remove(dest_path)
                        except OSError:
                            pass
                        return None
                    f.write(chunk)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            resp.close()
            if attempt < args.retries:
                time.sleep(2 ** attempt)
                continue
            REPORTER.detail("     stream error %s: %s" % (url, e))
            return None
        finally:
            resp.close()

        return dest_path

    return None


# =============================================================================
# Parallel JS download + analysis
# =============================================================================

def _download_worker(url, referer, args, dest_dir, master_cookies):
    """Runs in a worker thread. Returns (url, path or None, text or None)."""
    worker = make_session(args, args.url, base_cookies=master_cookies)
    dest = safe_filename(url, dest_dir)
    result = download_js(worker, url, referer, args, dest)
    if not result:
        return (url, None, None)
    text, _was = load_js_text(result)
    return (url, result, text)


def parallel_download_batch(urls, referer, args, state, master_cookies):
    """
    Download a batch of URLs in parallel. Analyzes each JS body for endpoints
    and chunks, updating shared state from the main thread only.

    Returns set of newly discovered chunk URLs.
    """
    if not urls:
        return set()

    # Filter candidates on the main thread
    todo = [u for u in urls
            if u not in state["downloaded_urls"]
            and u not in state["dead_chunks"]]
    if not todo:
        return set()

    # Respect --max-chunks
    if args.max_chunks:
        remaining = args.max_chunks - len(state["downloaded_urls"])
        if remaining <= 0:
            return set()
        todo = todo[:remaining]

    # Optional preflight
    if args.preflight:
        valid = _preflight(todo, args, state)
        todo = valid
        if not todo:
            return set()

    # Register URLs as downloaded (files will be added once complete)
    for u in todo:
        state["downloaded_urls"].add(u)
        add_new(state, "js_files", state["jsfiles_path"], u)

    REPORTER.counters["js_files"] = len(state["js_files"])

    workers = max(1, min(args.concurrency, len(todo)))
    discovered_chunks = set()

    if workers == 1:
        # Sequential fallback
        for u in todo:
            REPORTER.status("downloading %s" % os.path.basename(urllib.parse.urlsplit(u).path))
            url, path, text = _download_worker(u, referer, args, state["output_dir"], master_cookies)
            if path is None:
                state["dead_chunks"].add(url)
                continue
            if text is None:
                REPORTER.log("  binary JS unreadable: %s (try --no-compression)" % url, always=True)
                continue
            discovered_chunks |= _analyze_js(url, text, state, args)
        return discovered_chunks

    # Parallel
    done = 0
    total = len(todo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_download_worker, u, referer, args, state["output_dir"], master_cookies): u
            for u in todo
        }
        for fut in concurrent.futures.as_completed(futures):
            url = futures[fut]
            done += 1
            REPORTER.status("downloading %d/%d (%.0f%%)" % (done, total, 100.0 * done / total))
            try:
                r_url, r_path, r_text = fut.result()
            except Exception as e:
                REPORTER.detail("     worker crash %s: %s" % (url, e))
                state["dead_chunks"].add(url)
                continue

            if r_path is None:
                state["dead_chunks"].add(url)
                continue
            if r_text is None:
                REPORTER.log("  binary JS unreadable: %s (try --no-compression)" % r_url, always=True)
                continue
            discovered_chunks |= _analyze_js(r_url, r_text, state, args)

    return discovered_chunks


def _analyze_js(url, text, state, args):
    """Extract endpoints and chunks from JS text. Returns new chunk URLs."""
    skip_endpoints = args.skip_framework and is_framework_bundle(url)

    if not skip_endpoints:
        endpoints = extract_endpoints_from_text(text, url)
        for ep in endpoints:
            add_new(state, "js_endpoints", state["jsendpoints_path"], ep)
            add_new(state, "endpoints", state["endpoints_path"], ep)
            hits = tag_interesting(ep)
            if hits:
                REPORTER.counters["interesting"] += 1
                REPORTER.log("     [INTERESTING] %s (%s)" % (ep, ", ".join(hits)), always=True)

    REPORTER.counters["endpoints"] = len(state["endpoints"])

    chunks = extract_chunks_from_text(text, url, session=SESSION, args=args, state=state)
    new_chunks = set()
    for c in chunks:
        if c in state["dead_chunks"]:
            continue
        if add_new(state, "js_chunks", state["jschunks_path"], c):
            new_chunks.add(c)

    REPORTER.counters["chunks"] = len(state["js_chunks"])
    return new_chunks


def _preflight(urls, args, state):
    """Quick HEAD check to skip dead URLs. Optional via --preflight."""
    def check(u):
        try:
            r = SESSION.head(u, timeout=(args.timeout, args.timeout), allow_redirects=True)
            return u, r.status_code
        except Exception:
            return u, None

    valid = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for url, status in ex.map(check, urls):
            if status is None or status == 405:
                valid.append(url)  # HEAD unsupported, try GET
            elif 200 <= status < 400:
                valid.append(url)
            else:
                state["dead_chunks"].add(url)
    return valid


# =============================================================================
# State
# =============================================================================

def new_state(output_dir):
    return {
        "endpoints": set(),
        "pages_crawled": set(),
        "js_files": set(),
        "js_endpoints": set(),
        "js_chunks": set(),
        "spa_routes": set(),
        "dead_chunks": set(),
        "downloaded_urls": set(),
        "chunk_suffix": None,
        "output_dir": output_dir,
        "endpoints_path": os.path.join(output_dir, "endpoints.txt"),
        "pages_path": os.path.join(output_dir, "pages_crawled.txt"),
        "jsfiles_path": os.path.join(output_dir, "js_files.txt"),
        "jsendpoints_path": os.path.join(output_dir, "js_endpoints.txt"),
        "jschunks_path": os.path.join(output_dir, "js_chunks.txt"),
        "routes_path": os.path.join(output_dir, "spa_routes.txt"),
    }


RESUME_KEYS = (
    ("pages_crawled", "pages_path"),
    ("js_files", "jsfiles_path"),
    ("js_endpoints", "jsendpoints_path"),
    ("js_chunks", "jschunks_path"),
    ("spa_routes", "routes_path"),
    ("endpoints", "endpoints_path"),
)


def load_resume_state(state):
    loaded = 0
    for set_key, path_key in RESUME_KEYS:
        path = state[path_key]
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    state[set_key].add(line)
                    loaded += 1

    js_dir = os.path.join(state["output_dir"], "js_files")
    redo = 0
    if os.path.isdir(js_dir):
        existing = set(os.listdir(js_dir))
        for url in state["js_files"]:
            name = os.path.basename(urllib.parse.urlsplit(url).path)
            if name not in existing:
                continue
            full = os.path.join(js_dir, name)
            text, _was = load_js_text(full)
            if text is not None:
                state["downloaded_urls"].add(url)
            else:
                redo += 1
    if redo:
        REPORTER.log("Resume: %d unreadable file(s) will be re-downloaded." % redo, always=True)
    return loaded


def add_new(state, key, path, item):
    if item in state[key]:
        return False
    state[key].add(item)
    append_line(path, item)
    return True


def flush_final(state):
    write_sorted_set(state["endpoints_path"], state["endpoints"])
    write_sorted_set(state["jsendpoints_path"], state["js_endpoints"])
    write_sorted_set(state["jschunks_path"], state["js_chunks"])


def write_json_results(state, args):
    import json
    payload = {
        "url": args.url,
        "pages_crawled": sorted(state["pages_crawled"]),
        "endpoints": sorted(state["endpoints"]),
        "js_files": sorted(state["js_files"]),
        "js_endpoints": sorted(state["js_endpoints"]),
        "js_chunks": sorted(state["js_chunks"]),
        "spa_routes": sorted(state["spa_routes"]),
        "counts": {k: len(state[k]) for k in
                   ("pages_crawled", "endpoints", "js_files", "js_chunks", "js_endpoints")},
    }
    path = os.path.join(state["output_dir"], "results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


# =============================================================================
# HTML parsing
# =============================================================================

def _rel_list(tag):
    rel = tag.get("rel", [])
    if isinstance(rel, list):
        return " ".join(rel).lower()
    return str(rel).lower()


def extract_links_and_scripts(html, base_url):
    scripts = set()
    links = set()
    inline_js = []

    if HAVE_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script"):
            src = tag.get("src")
            if src:
                url = normalize_url(src, base_url)
                if url:
                    scripts.add(url)
            elif tag.string:
                inline_js.append(tag.string)
        for tag in soup.find_all("link"):
            href = tag.get("href")
            if not href:
                continue
            if not any(k in _rel_list(tag) for k in ("modulepreload", "preload", "prefetch")):
                continue
            url = normalize_url(href, base_url)
            if url and is_js_url(url):
                scripts.add(url)
        for tag in soup.find_all("a"):
            href = tag.get("href")
            if href:
                url = normalize_url(href, base_url)
                if url:
                    links.add(url)
    else:
        for m in re.finditer(r'<script[^>]*\bsrc=["\']([^"\']+)["\']', html, re.IGNORECASE):
            url = normalize_url(m.group(1), base_url)
            if url:
                scripts.add(url)
        for m in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.IGNORECASE | re.DOTALL):
            if m.group(1).strip():
                inline_js.append(m.group(1))
        for m in re.finditer(
            r'<link\b[^>]*\brel=["\']([^"\']*?(?:modulepreload|preload|prefetch)[^"\']*)["\'][^>]*\bhref=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        ):
            url = normalize_url(m.group(2), base_url)
            if url and is_js_url(url):
                scripts.add(url)
        for m in re.finditer(r'<a[^>]*\bhref=["\']([^"\']+)["\']', html, re.IGNORECASE):
            url = normalize_url(m.group(1), base_url)
            if url:
                links.add(url)

    return scripts, links, inline_js


def process_inline_js(text, source_url, state, args):
    skip = args.skip_framework and is_framework_bundle(source_url)
    if not skip:
        for ep in extract_endpoints_from_text(text, source_url):
            add_new(state, "js_endpoints", state["jsendpoints_path"], ep)
            add_new(state, "endpoints", state["endpoints_path"], ep)
    for c in extract_chunks_from_text(text, source_url, session=SESSION, args=args, state=state):
        add_new(state, "js_chunks", state["jschunks_path"], c)


# =============================================================================
# BFS crawl
# =============================================================================

SESSION = None  # master session, set in main()
MASTER_COOKIES = []  # snapshot of cookies for worker sessions


def crawl(args, state, base_domain):
    visited = set(state["pages_crawled"])
    queue = deque([(args.url, 0)])

    while queue:
        url, depth = queue.popleft()
        if url in visited:
            continue
        if args.depth is not None and depth > args.depth:
            continue
        if not host_allowed(url, args, base_domain):
            continue

        visited.add(url)
        label = "[%d/%s]" % (depth, args.depth if args.depth is not None else "inf")
        REPORTER.log("\nCrawling %s: %s" % (label, url), always=True)
        REPORTER.counters["pages"] = len(state["pages_crawled"]) + 1

        try:
            resp = SESSION.get(url, timeout=(args.timeout, args.read_timeout), allow_redirects=True)
        except requests.exceptions.RequestException as e:
            REPORTER.log("  Failed to fetch: %s" % e, always=True)
            continue

        final_url = resp.url
        if ("login" in final_url.lower() or "signin" in final_url.lower()) \
                and "login" not in url.lower() and "signin" not in url.lower():
            REPORTER.log("  WARNING: redirected to login -- auth may NOT be active.", always=True)

        add_new(state, "pages_crawled", state["pages_path"], url)
        add_new(state, "endpoints", state["endpoints_path"], url)

        ctype = resp.headers.get("Content-Type", "")
        if "text/html" not in ctype and not resp.text.strip().startswith("<"):
            continue

        html = resp.text
        scripts, links, inline_js = extract_links_and_scripts(html, final_url)

        # Inline <script> bodies
        for block in inline_js:
            process_inline_js(block, final_url, state, args)

        # Collect link endpoints
        for link in links:
            add_new(state, "endpoints", state["endpoints_path"], link)

        # Register <script> URLs (even before download, so they show in output)
        for script_url in scripts:
            add_new(state, "js_files", state["jsfiles_path"], script_url)
            add_new(state, "endpoints", state["endpoints_path"], script_url)

        # Download + analyze scripts in parallel
        chunk_queue = set(parallel_download_batch(scripts, final_url, args, state, MASTER_COOKIES))

        # Process chunk queue recursively
        while chunk_queue:
            if args.max_chunks and len(state["downloaded_urls"]) >= args.max_chunks:
                REPORTER.log("  Max chunk limit (%d) reached." % args.max_chunks, always=True)
                break
            batch = list(chunk_queue)[:max(1, args.concurrency * 2)]
            for u in batch:
                chunk_queue.discard(u)
            new_chunks = parallel_download_batch(batch, final_url, args, state, MASTER_COOKIES)
            chunk_queue |= new_chunks

        # Derive SPA routes from discovered chunk names
        for chunk_url in state["js_chunks"]:
            route = derive_spa_route(chunk_url, args.url)
            if route:
                add_new(state, "spa_routes", state["routes_path"], route)

        # Enqueue child pages (BFS)
        if args.depth is None or depth < args.depth:
            for link in sorted(links):
                if not host_allowed(link, args, base_domain):
                    continue
                if link not in visited:
                    queue.append((link, depth + 1))


# =============================================================================
# Probe
# =============================================================================

def probe(args):
    try:
        r = SESSION.get(args.url, timeout=(args.timeout, args.read_timeout), allow_redirects=True)
        REPORTER.detail("[probe] status=%d final_url=%s" % (r.status_code, r.url))
        REPORTER.detail("[probe] sent cookies: %s" % list(SESSION.cookies.get_dict().keys()))
        if "login" in r.url.lower() or "signin" in r.url.lower():
            REPORTER.log("[probe] WARNING: redirected to a login page -- auth may be inactive.", always=True)
    except requests.exceptions.RequestException as e:
        REPORTER.log("[probe] Failed to reach start URL: %s" % e, always=True)


# =============================================================================
# Main
# =============================================================================

def print_summary(state):
    print("\nDone.")
    print("Pages crawled: %d" % len(state["pages_crawled"]))
    print("Endpoints found: %d" % len(state["endpoints"]))
    print("JS files downloaded: %d" % len(state["js_files"]))
    print("JS chunks discovered: %d" % len(state["js_chunks"]))
    print("JS endpoints extracted: %d" % len(state["js_endpoints"]))
    print("\nAll output saved to: %s/" % state["output_dir"])


def main():
    global _STRIP_QUERY, SESSION, MASTER_COOKIES

    parser = build_arg_parser()
    args = parser.parse_args()

    _STRIP_QUERY = bool(args.strip_query)
    REPORTER.verbose = args.verbose

    ensure_dirs(args.output)
    state = new_state(args.output)
    for path in (state["endpoints_path"], state["pages_path"], state["jsfiles_path"],
                 state["jsendpoints_path"], state["jschunks_path"], state["routes_path"]):
        open(path, "a", encoding="utf-8").close()

    SESSION, n_cookies, auth_set = build_session(args, args.url)
    MASTER_COOKIES = list(SESSION.cookies)

    domain_scope = "unrestricted" if args.all_domains else \
                   ("same-subdomain" if args.same_subdomain else "same-domain")

    print("Crawling Hound  |  %s" % args.url)
    print("  domain: %s | framework JS: %s | ssl: %s | strip-query: %s" % (
        domain_scope,
        "skipped" if args.skip_framework else "included",
        "off" if args.insecure else "on",
        "on" if args.strip_query else "off",
    ))
    print("  output: %s/ | cookies: %d | auth header: %s | max chunks: %s" % (
        args.output, n_cookies,
        "yes" if auth_set else "no",
        str(args.max_chunks) if args.max_chunks else "unlimited",
    ))
    print("  concurrency: %d | preflight: %s | Accept-Encoding: %s" % (
        args.concurrency, "on" if args.preflight else "off", _accept_encoding(args),
    ))
    print("  requests: %s | urllib3: %s | brotli: %s | zstd: %s" % (
        requests.__version__, URLLIB3_VERSION,
        (_BROTLI_MODE if HAVE_BROTLI else "missing"),
        ("yes" if HAVE_ZSTD else "missing"),
    ))

    if not HAVE_BS4:
        print("  NOTE: beautifulsoup4 not installed - using regex HTML parser")
    if not HAVE_BROTLI and not args.no_compression and args.compression == "auto":
        print("  NOTE: brotli not installed - br encoding will not be advertised")
        print("        install with: pip install brotli zstandard")
    if not HAVE_ZSTD and args.compression == "auto":
        pass  # quiet, brotli note covers it
    if URLLIB3_VERSION.startswith("1.") and HAVE_BROTLI and not args.no_compression:
        print("  NOTE: urllib3 v1.x uses 'brotlipy', not 'brotli'. If downloads")
        print("        land as binary, use --no-compression or upgrade urllib3.")

    probe(args)

    base_domain = domain_of(args.url)

    if args.resume:
        loaded = load_resume_state(state)
        print("  resumed: %d prior entries loaded, %d JS files verified on disk" % (
            loaded, len(state["downloaded_urls"])))

    try:
        crawl(args, state, base_domain)
    except KeyboardInterrupt:
        REPORTER.clear_status()
        print("\nInterrupted - saving progress...")
    finally:
        REPORTER.clear_status()
        flush_final(state)
        print_summary(state)
        if args.json:
            print("JSON results: %s" % write_json_results(state, args))


if __name__ == "__main__":
    main()