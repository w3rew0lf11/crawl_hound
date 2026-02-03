import argparse
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import os
import sys

# ---------------- CLI ----------------

parser = argparse.ArgumentParser(
    description="🐕 Crawling Hound – crawl pages, download JS, and log endpoints"
)

parser.add_argument("--url", required=True, help="Start URL to crawl")
parser.add_argument("--output", default="downloaded_js", help="Directory to save JS files")
parser.add_argument("--depth", type=int, default=None, help="Max crawl depth (default: infinite if not set)")
parser.add_argument("--timeout", type=int, default=10, help="Request timeout in seconds")
parser.add_argument("--no-js", action="store_true", help="Do not download JS files")
parser.add_argument("--same-domain", action="store_true", help="Restrict crawl to start domain")

args = parser.parse_args()

START_URL = args.url.rstrip("/")
OUTPUT_DIR = args.output
MAX_DEPTH = args.depth  # None = infinite
TIMEOUT = args.timeout
DOWNLOAD_JS = not args.no_js
SAME_DOMAIN_ONLY = args.same_domain

ENDPOINTS_FILE = "endpoints.txt"
CRAWLED_PAGES_FILE = "pages_crawled.txt"
JS_URLS_FILE = "js_files.txt"

VISITED_PAGES = set()
DISCOVERED_ENDPOINTS = set()
DOWNLOADED_JS = set()

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------- Helpers ----------------

def save_to_file(filename, value):
    """Append a value to a file."""
    with open(filename, "a", encoding="utf-8") as f:
        f.write(value + "\n")

def normalize_url(url):
    """Normalize a URL to scheme://netloc/path format."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

def same_domain(base, target):
    """Check if two URLs are on the same domain."""
    return urlparse(base).netloc == urlparse(target).netloc

# ---------------- Core ----------------

def crawl_page(page_url, depth=0):
    """Recursively crawl pages, download JS, and log endpoints."""
    # Only enforce depth if MAX_DEPTH is set
    if MAX_DEPTH is not None and depth > MAX_DEPTH:
        return

    page_url = normalize_url(page_url)

    if page_url in VISITED_PAGES:
        return

    depth_display = MAX_DEPTH if MAX_DEPTH is not None else "∞"
    print(f"\n🌐 Crawling [{depth}/{depth_display}]: {page_url}")
    
    VISITED_PAGES.add(page_url)
    save_to_file(CRAWLED_PAGES_FILE, page_url)

    try:
        resp = requests.get(page_url, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Failed: {e}")
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # ---- JS FILES ----
    for script in soup.find_all("script", src=True):
        js_url = normalize_url(urljoin(page_url, script["src"]))

        if js_url not in DISCOVERED_ENDPOINTS:
            DISCOVERED_ENDPOINTS.add(js_url)
            save_to_file(ENDPOINTS_FILE, js_url)
            save_to_file(JS_URLS_FILE, js_url)

        if DOWNLOAD_JS and js_url not in DOWNLOADED_JS:
            print(f"  📥 JS: {js_url}")
            download_js(js_url)
            DOWNLOADED_JS.add(js_url)

    # ---- LINKS ----
    for link in soup.find_all("a", href=True):
        next_url = normalize_url(urljoin(page_url, link["href"]))

        if next_url not in DISCOVERED_ENDPOINTS:
            DISCOVERED_ENDPOINTS.add(next_url)
            save_to_file(ENDPOINTS_FILE, next_url)

        if SAME_DOMAIN_ONLY and not same_domain(START_URL, next_url):
            continue

        crawl_page(next_url, depth + 1)

def download_js(js_url):
    """Download a JS file to the output directory."""
    try:
        r = requests.get(js_url, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"     ❌ JS download failed: {e}")
        return

    parsed = urlparse(js_url)
    filename = os.path.basename(parsed.path) or "unknown.js"
    save_path = os.path.join(OUTPUT_DIR, filename)

    with open(save_path, "wb") as f:
        f.write(r.content)

    print(f"     ✅ Saved: {save_path}")

# ---------------- START ----------------

if __name__ == "__main__":
    print("🐕 Crawling Hound starting…")
    print(f"🔗 URL: {START_URL}")
    print(f"📁 Output: {OUTPUT_DIR}")
    print(f"🧭 Depth: {MAX_DEPTH if MAX_DEPTH is not None else '∞'}")
    print(f"📥 Download JS: {DOWNLOAD_JS}")
    print(f"🌐 Same domain only: {SAME_DOMAIN_ONLY}")

    crawl_page(START_URL, depth=0)

    print("\n🐾 Done.")
    print(f"Pages crawled: {len(VISITED_PAGES)}")
    print(f"Endpoints found: {len(DISCOVERED_ENDPOINTS)}")
    print(f"JS files downloaded: {len(DOWNLOADED_JS)}")
