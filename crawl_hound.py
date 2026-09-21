#!/usr/bin/env python3
"""
crawl_hound.py - SPA-aware web crawler and JS endpoint extractor.

Crawls a target site (including modern JS SPAs built with Umi, React,
Angular, Vue, Webpack), recursively discovers dynamically-loaded JS
chunks, and extracts API endpoints from downloaded bundles.
"""

import argparse
import concurrent.futures
import hashlib
import os
import re
import sys
import time
import urllib.parse

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
except Exception:
    pass


# ---- Reporter (managed, minimal, animated console output) ----

class Reporter:
    SPINNER = "|/-\\"

    def __init__(self, verbose=False):
        self.verbose = verbose
        self._spin_i = 0
        self._last_len = 0
        self._is_tty = sys.stdout.isatty()
        self.counters = {"pages": 0, "js_files": 0, "chunks": 0, "endpoints": 0, "interesting": 0}

    def _spin(self):
        c = self.SPINNER[self._spin_i % len(self.SPINNER)]
        self._spin_i += 1
        return c

    def status(self, msg):
        """Overwrite a single status line. No-op in verbose mode."""
        if self.verbose or not self._is_tty:
            return
        line = "  %s %s" % (self._spin(), msg)
        pad = max(self._last_len - len(line), 0)
        sys.stdout.write("\r" + line + (" " * pad))
        sys.stdout.flush()
        self._last_len = len(line)

    def clear_status(self):
        if self.verbose or not self._is_tty:
            return
        if self._last_len:
            sys.stdout.write("\r" + (" " * self._last_len) + "\r")
            sys.stdout.flush()
            self._last_len = 0

    def log(self, msg, always=False):
        """Durable output line. In compact mode only 'always' lines (page
        starts, warnings, interesting hits, summaries) print; routine
        per-file/per-chunk detail is swallowed and reflected in status()."""
        if self.verbose or always:
            self.clear_status()
            print(msg)

    def detail(self, msg):
        """Verbose-only line (per-file, per-chunk mechanics)."""
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


# ---- CLI Parsing ----

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="crawl_hound.py",
        description="Crawl web targets (including JS SPAs) and extract API endpoints from JS bundles.",
    )
    p.add_argument("--url", required=True, help="Start URL to crawl (required)")
    p.add_argument("--output", default="out", help="Output directory (default: out)")
    p.add_argument("--depth", type=int, default=None, help="Max crawl depth (default: infinite)")
    p.add_argument("--all-domains", action="store_true",
                    help="Do NOT restrict crawl to the start URL's domain+subdomains (default: restricted)")
    p.add_argument("--timeout", type=float, default=30, help="Connect timeout in seconds (default: 30)")
    p.add_argument("--read-timeout", type=float, default=120, help="Read timeout between chunks in seconds (default: 120)")
    p.add_argument("--max-js-size", type=float, default=50, help="Max JS file size in MB (default: 50)")
    p.add_argument("--max-chunks", type=int, default=500, help="Max recursive chunk downloads (default: 500)")
    p.add_argument("--retries", type=int, default=3, help="Download attempts per file (default: 3)")
    p.add_argument("--skip-sourcemaps", action="store_true", help="Skip .map / .js.map files")
    p.add_argument("--skip-framework", dest="skip_framework", action="store_true", default=True,
                    help="Don't report endpoints/API calls found inside known framework/vendor bundles (vendors*, runtime*, umi*, react*, polyfill*, moment*, antd*, etc.) -- they are still downloaded and scanned for chunk discovery, since chunk maps often live inside them. Default: on")
    p.add_argument("--include-framework", dest="skip_framework", action="store_false",
                    help="Include framework/vendor bundle contents in extracted endpoints (disables --skip-framework)")
    p.add_argument("--cookie", default=None, help="Inline cookie string, e.g. 'session=abc; csrf=xyz'")
    p.add_argument("--cookie-file", default=None, help="Cookie file (Netscape format or plain name=value; lines)")
    p.add_argument("--token", default=None, help="Bearer/raw auth token")
    p.add_argument("--auth-type", default="Bearer", choices=["Bearer", "Basic", "None"],
                    help="Auth scheme for --token (default: Bearer)")
    p.add_argument("--header", action="append", default=[], help="Custom header 'Name: Value' (repeatable)")
    p.add_argument("--user-agent", default=None, help="Override User-Agent header")
    p.add_argument("--insecure", action="store_true", help="Disable SSL certificate verification")
    p.add_argument("--verbose", action="store_true",
                    help="Print full detailed log (every file, every chunk, every endpoint). Default: compact animated status line + key events only")
    return p


# ---- Session Builder ----

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

CHROME_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


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
                # Netscape cookie file format
                fields = line.split("\t")
                if len(fields) >= 7:
                    name, val = fields[5], fields[6]
                    cookies[name.strip()] = val.strip()
            elif "=" in line:
                for part in line.split(";"):
                    part = part.strip()
                    if "=" in part:
                        name, val = part.split("=", 1)
                        cookies[name.strip()] = val.strip()
    return cookies


def build_session(args, start_url):
    session = requests.Session()
    session.verify = not args.insecure

    ua = args.user_agent or DEFAULT_UA
    headers = dict(CHROME_HEADERS)
    headers["User-Agent"] = ua
    for h in args.header:
        if ":" in h:
            name, val = h.split(":", 1)
            headers[name.strip()] = val.strip()

    auth_header_set = False
    if args.token and args.auth_type != "None":
        headers["Authorization"] = "%s %s" % (args.auth_type, args.token)
        auth_header_set = True
    elif args.token and args.auth_type == "None":
        headers["Authorization"] = args.token
        auth_header_set = True

    session.headers.update(headers)

    cookies = {}
    cookies.update(parse_cookie_string(args.cookie))
    cookies.update(parse_cookie_file(args.cookie_file))

    parsed = urllib.parse.urlsplit(start_url)
    host = parsed.hostname or ""
    parent_domain = ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host

    for name, val in cookies.items():
        session.cookies.set(name, val, domain=host)
        if parent_domain and parent_domain != host:
            session.cookies.set(name, val, domain="." + parent_domain)

    return session, cookies, auth_header_set


# ---- Helper Functions ----

def ensure_dirs(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "js_files"), exist_ok=True)


def domain_of(url):
    return urllib.parse.urlsplit(url).netloc.lower()


def registrable_domain(host):
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def same_domain(url, base_domain):
    """True if url's host equals base_domain or is a subdomain of it
    (matched on registrable domain, e.g. api.example.com vs example.com)."""
    host = domain_of(url)
    return host == base_domain or host.endswith("." + base_domain) or \
        registrable_domain(host) == registrable_domain(base_domain)


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
    joined, _frag = urllib.parse.urldefrag(joined)
    return joined


STATIC_EXTS = (".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2",
               ".ttf", ".eot", ".ico", ".pdf", ".xml", ".txt", ".html", ".htm")


FRAMEWORK_NAME_RE = re.compile(
    r'^(vendors?|runtime|umi|polyfill|react(-dom)?|redux|moment|antd|lodash|'
    r'core-js|regenerator|webpack)[-.]',
    re.IGNORECASE,
)


def is_framework_bundle(url):
    name = os.path.basename(urllib.parse.urlsplit(url).path)
    return bool(FRAMEWORK_NAME_RE.match(name))


def is_static_asset(url):
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(STATIC_EXTS)


def is_js_url(url):
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(".js")


def safe_filename(url, output_dir):
    parsed = urllib.parse.urlsplit(url)
    name = os.path.basename(parsed.path) or "index.js"
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    target = os.path.join(output_dir, "js_files", name)
    if os.path.exists(target):
        base, ext = os.path.splitext(name)
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
    lower = endpoint.lower()
    hits = [kw for kw in INTERESTING_KEYWORDS if kw in lower]
    return hits


# ---- Endpoint Extraction ----

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
            if not url:
                continue
            if is_js_url(url):
                continue
            if is_static_asset(url):
                continue
            found.add(url)
    return found


# ---- Chunk Extraction ----

CHUNK_SIGNAL_PATTERNS = [
    re.compile(r'import\(\s*["\']([^"\']+\.js)["\']\s*\)'),
    re.compile(r'__webpack_require__\.p\s*\+\s*["\']([^"\']+\.js)["\']'),
    re.compile(r'["\']([a-zA-Z0-9_\-./]+\.async\.js)["\']'),
    re.compile(r'["\']([pP]__[a-zA-Z0-9_]+)["\']'),
]

# Webpack chunk id -> hash maps, e.g. {3:"39731c9d",8:"c6b64dcc"} or
# {"3":"39731c9d","8":"c6b64dcc"}. Chunk filenames are built at runtime as
# "<id>.<hash>.async.js" (or "<id>.<hash>.chunk.js"), so no literal string
# exists in the bundle for these -- the map itself must be parsed.
WEBPACK_ID_HASH_MAP_RE = re.compile(r'\{(\s*"?[0-9]+"?\s*:\s*"[0-9a-fA-F]{6,}"\s*,?\s*)+\}')
WEBPACK_ID_HASH_ENTRY_RE = re.compile(r'"?([0-9]+)"?\s*:\s*"([0-9a-fA-F]{6,})"')


def extract_id_hash_chunks(text, base_url):
    """Find webpack chunk-id -> hash map objects and reconstruct plausible
    chunk filenames from them, since these are built at runtime via string
    concatenation rather than appearing as literal filenames."""
    found = set()
    for map_match in WEBPACK_ID_HASH_MAP_RE.finditer(text):
        block = map_match.group(0)
        entries = WEBPACK_ID_HASH_ENTRY_RE.findall(block)
        if len(entries) < 2:
            continue
        for chunk_id, chunk_hash in entries:
            for suffix in (".async.js", ".chunk.js"):
                candidate = "%s.%s%s" % (chunk_id, chunk_hash, suffix)
                url = normalize_url(candidate, base_url)
                if url:
                    found.add(url)
    return found

CHUNK_NAME_VALID = re.compile(
    r'('
    r'\.async\.js$'
    r'|^vendors.*\.js$'
    r'|chunk[-.][0-9a-fA-F]+\.js$'
    r'|runtime\.[0-9a-fA-F]+\.js$'
    r'|^webpack-.*'
    r'|\.[0-9a-fA-F]{6,}\.js$'
    r')'
)


def looks_like_valid_chunk_name(name):
    return bool(CHUNK_NAME_VALID.search(name))


def extract_chunks_from_text(text, base_url):
    found = set()
    for pattern in CHUNK_SIGNAL_PATTERNS:
        for m in pattern.finditer(text):
            candidate = m.group(1)
            if not candidate:
                continue
            if candidate.lower().startswith(("p__", "P__".lower())) is False and candidate.startswith(("p__", "P__")):
                pass
            if re.match(r'^[pP]__[a-zA-Z0-9_]+$', candidate):
                # Umi chunkName expansion -> filename
                candidate = candidate + ".async.js"
            name = os.path.basename(candidate)
            if not looks_like_valid_chunk_name(name):
                continue
            url = normalize_url(candidate, base_url)
            if not url:
                continue
            found.add(url)
    found |= extract_id_hash_chunks(text, base_url)
    return found


# ---- SPA Route Extraction ----

SPA_ROUTE_RE = re.compile(r'[pP]__([a-zA-Z0-9_]+)__([a-zA-Z0-9_]+)\.async\.js$')


def derive_spa_route(chunk_url, start_url):
    name = os.path.basename(urllib.parse.urlsplit(chunk_url).path)
    m = SPA_ROUTE_RE.search(name)
    if not m:
        return None
    section, page = m.group(1), m.group(2)
    return start_url.rstrip("/") + "/#/" + section + "/" + page


# ---- JS Download (streaming + retry) ----

def human_size(num_bytes):
    return "%.2f MB" % (num_bytes / (1024.0 * 1024.0))


def download_js(session, url, referer, args, dest_path, state):
    max_bytes = int(args.max_js_size * 1024 * 1024)
    headers = {}
    if referer:
        headers["Referer"] = referer

    for attempt in range(1, args.retries + 1):
        try:
            resp = session.get(
                url, headers=headers, stream=True,
                timeout=(args.timeout, args.read_timeout),
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < args.retries:
                wait = 2 ** attempt
                REPORTER.detail("     Network error (%s), retrying in %ds (%d/%d)..." %
                                 (type(e).__name__, wait, attempt, args.retries))
                time.sleep(wait)
                continue
            REPORTER.log("     Failed after %d attempts: %s" % (args.retries, e), always=True)
            return None

        if resp.status_code >= 500:
            resp.close()
            if attempt < args.retries:
                wait = 2 ** attempt
                REPORTER.detail("     Server error %d, retrying in %ds (%d/%d)..." %
                                 (resp.status_code, wait, attempt, args.retries))
                time.sleep(wait)
                continue
            REPORTER.log("     Failed after %d attempts: HTTP %d" % (args.retries, resp.status_code), always=True)
            return None

        if resp.status_code >= 400:
            resp.close()
            REPORTER.detail("     HTTP %d, not retrying (permanent)" % resp.status_code)
            return None

        declared = resp.headers.get("Content-Length")
        if declared:
            try:
                declared_mb = int(declared) / (1024.0 * 1024.0)
                REPORTER.detail("     %.2f MB declared, streaming..." % declared_mb)
            except ValueError:
                REPORTER.detail("     streaming...")
        else:
            REPORTER.detail("     streaming (size unknown)...")

        total = 0
        last_reported = 0
        start_time = time.time()
        try:
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        REPORTER.log("     Exceeded max JS size (%.0f MB), aborting download" % args.max_js_size, always=True)
                        resp.close()
                        try:
                            os.remove(dest_path)
                        except OSError:
                            pass
                        return None
                    f.write(chunk)
                    if total - last_reported >= 512 * 1024:
                        REPORTER.status("downloading %s -- %.2f MB" % (os.path.basename(dest_path), total / (1024.0 * 1024.0)))
                        last_reported = total
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            resp.close()
            if attempt < args.retries:
                wait = 2 ** attempt
                REPORTER.detail("     Stream interrupted (%s), retrying in %ds (%d/%d)..." %
                                 (type(e).__name__, wait, attempt, args.retries))
                time.sleep(wait)
                continue
            REPORTER.log("     Failed after %d attempts: %s" % (args.retries, e), always=True)
            return None
        finally:
            resp.close()

        elapsed = max(time.time() - start_time, 0.001)
        rate_kb = (total / 1024.0) / elapsed
        REPORTER.detail("     Saved: %s (%s in %.1fs, %d KB/s)" %
                         (dest_path, human_size(total), elapsed, int(rate_kb)))
        return dest_path

    return None


# ---- Chunk Pre-flight (parallel HEAD) ----

def head_check(session, url, args):
    try:
        resp = session.head(url, timeout=(args.timeout, args.timeout), allow_redirects=True)
        return url, resp.status_code
    except requests.exceptions.RequestException:
        return url, None


def preflight_chunks(session, urls, args, state):
    urls = list(urls)
    if not urls:
        return [], []

    REPORTER.status("pre-checking %d chunk URLs (HEAD)..." % len(urls))
    valid = []
    dead = []
    head_unsupported = False

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(head_check, session, u, args): u for u in urls}
            for fut in concurrent.futures.as_completed(futures):
                url, status = fut.result()
                if status is None:
                    head_unsupported = True
                    continue
                if status == 405:
                    head_unsupported = True
                    continue
                if 200 <= status < 400:
                    valid.append(url)
                else:
                    dead.append(url)
                    state["dead_chunks"].add(url)
    except Exception:
        head_unsupported = True

    if head_unsupported and not valid and not dead:
        REPORTER.detail("  HEAD not supported, falling back to sequential GET with tightened filter.")
        return urls, []

    REPORTER.detail("  Valid: %d  Dead (404/skipped): %d" % (len(valid), len(dead)))
    return valid, dead


# ---- Output State ----

def new_state(output_dir):
    return {
        "endpoints": set(),
        "pages_crawled": set(),
        "js_files": set(),
        "js_endpoints": set(),
        "js_chunks": set(),
        "spa_routes": set(),
        "dead_chunks": set(),
        "output_dir": output_dir,
        "endpoints_path": os.path.join(output_dir, "endpoints.txt"),
        "pages_path": os.path.join(output_dir, "pages_crawled.txt"),
        "jsfiles_path": os.path.join(output_dir, "js_files.txt"),
        "jsendpoints_path": os.path.join(output_dir, "js_endpoints.txt"),
        "jschunks_path": os.path.join(output_dir, "js_chunks.txt"),
        "routes_path": os.path.join(output_dir, "spa_routes.txt"),
    }


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


# ---- Page Crawl (recursive) ----

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
        for m in re.finditer(r'<a[^>]*\bhref=["\']([^"\']+)["\']', html, re.IGNORECASE):
            url = normalize_url(m.group(1), base_url)
            if url:
                links.add(url)

    return scripts, links, inline_js


def process_js_content(text, source_url, state, args):
    skip_endpoints = args.skip_framework and is_framework_bundle(source_url)

    endpoints = set() if skip_endpoints else extract_endpoints_from_text(text, source_url)
    new_endpoints = 0
    t0 = time.time()
    for ep in endpoints:
        if add_new(state, "js_endpoints", state["jsendpoints_path"], ep):
            new_endpoints += 1
        add_new(state, "endpoints", state["endpoints_path"], ep)
        hits = tag_interesting(ep)
        if hits:
            REPORTER.counters["interesting"] += 1
            REPORTER.log("     [INTERESTING] %s (contains: %s)" % (ep, ", ".join(hits)), always=True)
    elapsed = time.time() - t0
    if skip_endpoints:
        REPORTER.detail("     Skipping endpoint extraction (framework/vendor bundle)")
    else:
        REPORTER.detail("     Found %d endpoints (%d new) in %.1fs" % (len(endpoints), new_endpoints, elapsed))
    REPORTER.counters["endpoints"] = len(state["endpoints"])

    chunks = extract_chunks_from_text(text, source_url)
    new_chunks = 0
    for c in chunks:
        if c in state["dead_chunks"]:
            continue
        if add_new(state, "js_chunks", state["jschunks_path"], c):
            new_chunks += 1
    REPORTER.counters["chunks"] = len(state["js_chunks"])
    REPORTER.detail("     Found %d JS chunks (%d new)" % (len(chunks), new_chunks))
    REPORTER.live_status()
    return chunks


def handle_js_file(session, url, referer, args, state, downloaded_urls):
    if url in downloaded_urls:
        return set()
    if url in state["dead_chunks"]:
        return set()
    if args.skip_sourcemaps and (url.endswith(".map") or url.endswith(".js.map")):
        return set()
    if len(downloaded_urls) >= args.max_chunks:
        return set()

    downloaded_urls.add(url)
    add_new(state, "js_files", state["jsfiles_path"], url)
    REPORTER.counters["js_files"] = len(state["js_files"])

    dest_path = safe_filename(url, state["output_dir"])
    REPORTER.detail("  JS: %s" % url)
    REPORTER.status("downloading %s" % os.path.basename(dest_path))
    result = download_js(session, url, referer, args, dest_path, state)
    if not result:
        state["dead_chunks"].add(url)
        return set()

    try:
        size = os.path.getsize(result)
        REPORTER.detail("     Analyzing %s of JS..." % human_size(size))
        with open(result, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError as e:
        REPORTER.log("     Could not read downloaded file: %s" % e, always=True)
        return set()

    chunks = process_js_content(text, url, state, args)

    for chunk_url in chunks:
        route = derive_spa_route(chunk_url, args.url)
        if route and add_new(state, "spa_routes", state["routes_path"], route):
            pass

    return chunks


def crawl_page(session, url, depth, args, state, base_domain, visited, downloaded_urls):
    if url in visited:
        return
    if args.depth is not None and depth > args.depth:
        return
    if not args.all_domains and not same_domain(url, base_domain):
        return

    visited.add(url)
    label = "[%d/%s]" % (depth, str(args.depth) if args.depth is not None else "inf")
    REPORTER.log("\nCrawling %s: %s" % (label, url), always=True)
    REPORTER.counters["pages"] = len(state["pages_crawled"]) + 1

    try:
        resp = session.get(url, timeout=(args.timeout, args.read_timeout), allow_redirects=True)
    except requests.exceptions.RequestException as e:
        REPORTER.log("  Failed to fetch: %s" % e, always=True)
        return

    final_url = resp.url
    if "login" in final_url.lower() or "signin" in final_url.lower():
        if "login" not in url.lower() and "signin" not in url.lower():
            REPORTER.log("  WARNING: redirected to login/signin page -- auth may NOT be active.", always=True)

    add_new(state, "pages_crawled", state["pages_path"], url)
    add_new(state, "endpoints", state["endpoints_path"], url)

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" not in content_type and not resp.text.strip().startswith("<"):
        return

    html = resp.text
    scripts, links, inline_js = extract_links_and_scripts(html, final_url)

    for block in inline_js:
        process_js_content(block, final_url, state, args)

    for link in links:
        add_new(state, "endpoints", state["endpoints_path"], link)

    all_chunks = set()
    for script_url in scripts:
        add_new(state, "js_files", state["jsfiles_path"], script_url)
        add_new(state, "endpoints", state["endpoints_path"], script_url)

    for script_url in sorted(scripts):
        found = handle_js_file(session, script_url, final_url, args, state, downloaded_urls)
        all_chunks |= found

    process_chunk_queue(session, all_chunks, final_url, args, state, downloaded_urls)

    if args.depth is None or depth < args.depth:
        for link in sorted(links):
            if not args.all_domains and not same_domain(link, base_domain):
                continue
            crawl_page(session, link, depth + 1, args, state, base_domain, visited, downloaded_urls)


# ---- Chunk Queue Processor ----

def process_chunk_queue(session, initial_chunks, referer, args, state, downloaded_urls):
    queue = set(initial_chunks)
    seen_in_queue = set(queue)

    while queue and len(downloaded_urls) < args.max_chunks:
        batch = list(queue)[:64]
        for u in batch:
            queue.discard(u)

        candidates = [u for u in batch if u not in downloaded_urls and u not in state["dead_chunks"]]
        if not candidates:
            continue

        valid, _dead = preflight_chunks(session, candidates, args, state)

        for chunk_url in valid:
            if len(downloaded_urls) >= args.max_chunks:
                REPORTER.log("  Max chunk limit (%d) reached, stopping chunk discovery." % args.max_chunks, always=True)
                break
            REPORTER.detail("  CHUNK: %s" % chunk_url)
            REPORTER.status("chunk %s" % os.path.basename(urllib.parse.urlsplit(chunk_url).path))
            new_chunks = handle_js_file(session, chunk_url, referer, args, state, downloaded_urls)
            for nc in new_chunks:
                if nc not in seen_in_queue and nc not in downloaded_urls and nc not in state["dead_chunks"]:
                    queue.add(nc)
                    seen_in_queue.add(nc)


# ---- Startup Probe ----

def probe_start_url(session, args):
    try:
        resp = session.get(args.url, timeout=(args.timeout, args.read_timeout), allow_redirects=True)
        sent_cookies = list(session.cookies.get_dict().keys())
        set_cookie_headers = resp.raw.headers.get_all("Set-Cookie") if hasattr(resp.raw, "headers") and hasattr(resp.raw.headers, "get_all") else []
        if not set_cookie_headers:
            set_cookie_headers = [v for k, v in resp.headers.items() if k.lower() == "set-cookie"]
        REPORTER.detail("[probe] status=%d final_url=%s" % (resp.status_code, resp.url))
        REPORTER.detail("[probe] sent cookies: %s" % sent_cookies)
        REPORTER.detail("[probe] response Set-Cookie: %s" % set_cookie_headers)
        if "login" in resp.url.lower() or "signin" in resp.url.lower():
            REPORTER.log("[probe] WARNING: start URL redirected to a login page -- session may be unauthenticated.", always=True)
    except requests.exceptions.RequestException as e:
        REPORTER.log("[probe] Failed to reach start URL: %s" % e, always=True)


# ---- Main Entry Point ----

def print_summary(state):
    print("\nDone.")
    print("Pages crawled: %d" % len(state["pages_crawled"]))
    print("Endpoints found: %d" % len(state["endpoints"]))
    print("JS files downloaded: %d" % len(state["js_files"]))
    print("JS chunks discovered: %d" % len(state["js_chunks"]))
    print("JS endpoints extracted: %d" % len(state["js_endpoints"]))
    print("\nAll output saved to: %s/" % state["output_dir"])


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    REPORTER.verbose = args.verbose

    ensure_dirs(args.output)
    state = new_state(args.output)
    for path in (state["endpoints_path"], state["pages_path"], state["jsfiles_path"],
                 state["jsendpoints_path"], state["jschunks_path"], state["routes_path"]):
        open(path, "a", encoding="utf-8").close()

    session, cookies, auth_header_set = build_session(args, args.url)

    print("Crawling Hound  |  %s  |  domain: %s  |  framework JS: %s  |  ssl: %s" % (
        args.url,
        "restricted" if not args.all_domains else "unrestricted",
        "skipped" if args.skip_framework else "included",
        "off" if args.insecure else "on",
    ))
    print("Output: %s/   Cookies: %d %s   Auth header: %s" % (
        args.output, len(cookies), list(cookies.keys()) if cookies else "",
        "yes" if auth_header_set else "no",
    ))
    if not HAVE_BS4:
        print("Note: beautifulsoup4 not installed, falling back to regex HTML parsing.")

    probe_start_url(session, args)

    base_domain = domain_of(args.url)
    visited = set()
    downloaded_urls = set()

    try:
        crawl_page(session, args.url, 0, args, state, base_domain, visited, downloaded_urls)
    except KeyboardInterrupt:
        REPORTER.clear_status()
        print("\nInterrupted by user, saving current progress...")
    finally:
        REPORTER.clear_status()
        flush_final(state)
        print_summary(state)


if __name__ == "__main__":
    main()