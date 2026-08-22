#!/usr/bin/env python3
# jshunter - real-time recursive JS/URL endpoint discovery
# author: zamur | discord.com/invite/AA92kB5GSB
#
# pip install requests beautifulsoup4 --break-system-packages

import re
import sys
import json
import time
import threading
import argparse
import requests
from datetime import datetime
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

requests.packages.urllib3.disable_warnings()

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

VERSION = "3.0"


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"

    @staticmethod
    def disable():
        for attr in ["RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW", "BLUE", "MAGENTA", "CYAN"]:
            setattr(C, attr, "")


BANNER = f"""{C.CYAN}{C.BOLD}
   JSHUNTER
{C.RESET}{C.DIM}   Real-time recursive JS/URL endpoint hunter  ·  v{VERSION}
   zamur  ·  discord.com/invite/AA92kB5GSB{C.RESET}
"""

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

STATIC_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".css",
               ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm", ".pdf")

JS_URL_REGEX = re.compile(
    r"""
    (?:"|')
    (
        ((?:[a-zA-Z]{1,10}://|//)[^"'/]{1,}\.[a-zA-Z]{2,}[^"']{0,})
        |
        ((?:/|\.\./|\./)[^"'><,;| *()(%%$^/\\\[\]][^"'><,;|()]{1,})
        |
        ([a-zA-Z0-9_\-/]{1,}/[a-zA-Z0-9_\-/]{1,}\.(?:json|php|asp|aspx|jsp|action|html|js)(?:\?[^"']*)?)
        |
        ([a-zA-Z0-9_\-]{1,}\.(?:json|php|asp|aspx|jsp|action)(?:\?[^"']*)?)
    )
    (?:"|')
    """,
    re.VERBOSE,
)

API_CALL_REGEX = re.compile(
    r"""(?:fetch|axios\.(?:get|post|put|delete|patch)|\$\.(?:get|post|ajax))\s*\(\s*(?:"|')([^"']{1,200})(?:"|')""",
    re.IGNORECASE,
)

SECRET_HINT_REGEX = re.compile(
    r"""(?:api[_-]?key|secret|token|authorization|bearer)\s*[:=]\s*(?:"|')([a-zA-Z0-9\-_\.]{8,100})(?:"|')""",
    re.IGNORECASE,
)

class LiveWriter:
    def __init__(self, path, fmt="txt"):
        self.path = path
        self.fmt = fmt
        self.lock = threading.Lock()
        self.seen = set()
        self._fh = open(path, "a", buffering=1) 

    def write(self, record):
        key = (record.get("type"), record.get("url") or record.get("value"))
        with self.lock:
            if key in self.seen:
                return
            self.seen.add(key)

            record["ts"] = datetime.now().strftime("%H:%M:%S")

            if self.fmt == "json":
                self._fh.write(json.dumps(record) + "\n")
            else:
                if record["type"] == "endpoint":
                    status = f"[{record['status']}] " if record.get("status") else ""
                    self._fh.write(f"{status}{record['url']}\n")
                elif record["type"] == "js":
                    self._fh.write(f"[JS] {record['url']}\n")
                elif record["type"] == "secret":
                    self._fh.write(f"[SECRET] {record['value']}\n")
            self._fh.flush()

    def close(self):
        with self.lock:
            self._fh.close()


class JSHunter:
    def __init__(self, base_url, depth=1, threads=10, delay=0.0, timeout=10,
                 same_domain_only=True, include_subdomains=False,
                 validate=False, find_secrets=False,
                 skip_static=True, user_agent=None, proxy=None, retries=2,
                 bug_mode=False, render_mode=False, verbose=True, writer=None, resume_seen=None):
        self.base_url = base_url.rstrip("/")
        self.base_domain = urlparse(self.base_url).netloc
        self.apex_domain = ".".join(self.base_domain.split(".")[-2:])
        self.depth = depth
        self.threads = threads
        self.delay = delay
        self.timeout = timeout
        self.same_domain_only = same_domain_only
        self.include_subdomains = include_subdomains
        self.validate = validate
        self.find_secrets = find_secrets
        self.skip_static = skip_static
        self.verbose = verbose
        self.writer = writer
        self.headers = {"User-Agent": user_agent or DEFAULT_UA}
        self.retries = retries
        self.proxy = proxy
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.bug_mode = bug_mode
        self.render_mode = render_mode

        self.session = requests.Session()
        self.visited_pages = set()
        self.visited_js = set()
        self.checked_maps = set()
        self.seed_urls = set()
        self.found_endpoints = set(resume_seen or set())
        self.found_secrets = set()
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._render_lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._backoff_until = 0

    def log(self, msg):
        if self.verbose:
            print(msg)

    def live_stats(self):
        if not self.verbose:
            return
        with self._stats_lock:
            line = (f"\r  {C.DIM}pages:{C.RESET}{C.BOLD}{len(self.visited_pages):<5}{C.RESET}"
                    f"{C.DIM}js:{C.RESET}{C.BOLD}{len(self.visited_js):<5}{C.RESET}"
                    f"{C.DIM}endpoints:{C.RESET}{C.BOLD}{len(self.found_endpoints):<5}{C.RESET}"
                    f"{C.DIM}secrets:{C.RESET}{C.BOLD}{len(self.found_secrets):<5}{C.RESET}")
            sys.stdout.write(line)
            sys.stdout.flush()

    def emit(self, record):
        if self.writer:
            self.writer.write(record)

    def fetch(self, url):
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                r = self.session.get(url, headers=self.headers, timeout=self.timeout,
                                      verify=False, proxies=self.proxies)
                if r.status_code in (429, 403):
                    self._backoff_until = time.time() + 5
                if attempt > 0:
                    self.log(f"    {C.YELLOW}[retry]{C.RESET} {url} (attempt {attempt + 1})")
                return r
            except requests.RequestException as e:
                last_exc = e
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
        self.log(f"{C.RED}[!]{C.RESET} Failed to reach {url}: {last_exc}")
        return None

    def in_scope(self, url):
        if not self.same_domain_only:
            return True
        try:
            host = urlparse(url).netloc
        except Exception:
            return False
        if host == self.base_domain:
            return True
        if self.include_subdomains:
            return host == self.apex_domain or host.endswith("." + self.apex_domain)
        return False

    def is_static_asset(self, url):
        return self.skip_static and urlparse(url).path.lower().endswith(STATIC_EXTS)

    def seed_from_robots_and_sitemap(self):
        p = urlparse(self.base_url)
        root = f"{p.scheme}://{p.netloc}"

        r = self.fetch(f"{root}/robots.txt")
        if r and r.status_code == 200:
            for line in r.text.splitlines():
                line = line.strip()
                if line.lower().startswith(("disallow:", "allow:")):
                    path = line.split(":", 1)[1].strip()
                    if path and path != "/":
                        self.seed_urls.add(urljoin(root, path))
                elif line.lower().startswith("sitemap:"):
                    self.seed_urls.add(line.split(":", 1)[1].strip())

        for sm_url in [u for u in self.seed_urls if u.endswith(".xml")] + [f"{root}/sitemap.xml"]:
            r = self.fetch(sm_url)
            if not r or r.status_code != 200:
                continue
            try:
                soup = BeautifulSoup(r.text, "xml")
                for loc in soup.find_all("loc"):
                    if loc.text:
                        self.seed_urls.add(loc.text.strip())
            except Exception:
                pass

        self.seed_urls = {u for u in self.seed_urls if self.in_scope(u) and not u.endswith(".xml")}
        if self.seed_urls:
            self.log(f"    {C.CYAN}[seed]{C.RESET} +{len(self.seed_urls)} URL(s) from robots.txt/sitemap.xml")

    def check_source_map(self, js_url):
        if js_url in self.checked_maps:
            return
        self.checked_maps.add(js_url)
        map_url = js_url if js_url.endswith(".map") else js_url + ".map"
        r = self.fetch(map_url)
        if r and r.status_code == 200 and len(r.text) > 20:
            self.log(f"    {C.MAGENTA}[sourcemap]{C.RESET} {map_url}")
            for m in JS_URL_REGEX.finditer(r.text):
                cand = next(g for g in m.groups()[1:] if g)
                cand = cand.strip()
                if len(cand) > 3:
                    self.add_endpoint(cand)

    def start_browser(self):
        with self._render_lock:
            if self._browser is not None:
                return
            self._playwright = sync_playwright().start()
            launch_args = {"headless": True}
            if self.proxy:
                launch_args["proxy"] = {"server": self.proxy}
            self._browser = self._playwright.chromium.launch(**launch_args)

    def close_browser(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def render_page(self, url):
        self.start_browser()
        captured = set()
        html = ""
        with self._render_lock:
            context = self._browser.new_context(user_agent=self.headers["User-Agent"], ignore_https_errors=True)
            page = context.new_page()
            page.on("request", lambda req: captured.add(req.url))
            try:
                page.goto(url, timeout=int(self.timeout * 1000), wait_until="networkidle")
                html = page.content()
            except Exception as e:
                self.log(f"{C.RED}[!]{C.RESET} Render failed for {url}: {e}")
            context.close()

        for req_url in captured:
            path = urlparse(req_url).path
            if not self.is_static_asset(req_url) and (self.in_scope(req_url) or "/api/" in path or "/graphql" in path):
                self.add_endpoint(req_url)
        return html

    def add_endpoint(self, cand):
        with self._lock:
            if cand in self.found_endpoints:
                return
            self.found_endpoints.add(cand)
        url = self.normalize(cand)
        if not self.validate:
            self.emit({"type": "endpoint", "url": url})
            self.log(f"    {C.GREEN}[endpoint]{C.RESET} {url}")
        self.live_stats()

    def add_js(self, js_url):
        with self._lock:
            if js_url in self.visited_js:
                return False
            self.visited_js.add(js_url)
        self.emit({"type": "js", "url": js_url})
        self.log(f"    {C.CYAN}[js]{C.RESET} {js_url}")
        self.live_stats()
        return True

    def add_secret(self, value):
        with self._lock:
            if value in self.found_secrets:
                return
            self.found_secrets.add(value)
        self.emit({"type": "secret", "value": value})
        self.log(f"    {C.RED}{C.BOLD}[secret]{C.RESET} {value[:100]}")
        self.live_stats()

    def extract_from_html(self, url, html):
        soup = BeautifulSoup(html, "html.parser")
        js_files, page_links = set(), set()

        for tag in soup.find_all("script"):
            src = tag.get("src")
            if src:
                full = urljoin(url, src)
                if self.in_scope(full):
                    js_files.add(full)
            elif tag.string:
                for m in JS_URL_REGEX.finditer(tag.string):
                    cand = next(g for g in m.groups()[1:] if g)
                    self.add_endpoint(cand.strip())

        for tag in soup.find_all("a", href=True):
            link = urljoin(url, tag["href"])
            if self.in_scope(link) and not self.is_static_asset(link):
                page_links.add(link.split("#")[0])

        return js_files, page_links

    def extract_from_js(self, js_url):
        r = self.fetch(js_url)
        if not r or r.status_code != 200:
            return
        content = r.text

        for m in JS_URL_REGEX.finditer(content):
            cand = next(g for g in m.groups()[1:] if g)
            cand = cand.strip()
            if len(cand) > 3:
                self.add_endpoint(cand)

        for m in API_CALL_REGEX.finditer(content):
            cand = m.group(1).strip()
            if len(cand) > 1:
                self.add_endpoint(cand)

        if self.find_secrets:
            for m in SECRET_HINT_REGEX.finditer(content):
                self.add_secret(f"{js_url} -> {m.group(0)[:80]}")

    def get_page_html(self, url):
        if self.render_mode:
            return self.render_page(url)
        r = self.fetch(url)
        if r and "text/html" in r.headers.get("Content-Type", ""):
            return r.text
        return None

    def crawl(self):
        current_level = {self.base_url}

        if self.bug_mode:
            self.log(f"{C.CYAN}[*]{C.RESET} Bug mode: seeding from robots.txt / sitemap.xml...")
            self.seed_from_robots_and_sitemap()
            current_level |= self.seed_urls

        if self.render_mode:
            self.start_browser()

        for level in range(1, self.depth + 1):
            self.log(f"\n{C.BLUE}[*]{C.RESET} {C.BOLD}Depth {level}/{self.depth}{C.RESET} "
                      f"— {len(current_level)} page(s) queued")
            next_level = set()

            pages_to_visit = [p for p in current_level if p not in self.visited_pages and self.in_scope(p)]
            self.visited_pages.update(pages_to_visit)

            if self.render_mode:
                results = []
                for page in pages_to_visit:
                    if self._backoff_until > time.time():
                        time.sleep(self._backoff_until - time.time())
                    results.append((page, self.get_page_html(page)))
                    if self.delay:
                        time.sleep(self.delay)
            else:
                with ThreadPoolExecutor(max_workers=self.threads) as ex:
                    futures = {}
                    for page in pages_to_visit:
                        if self._backoff_until > time.time():
                            time.sleep(self._backoff_until - time.time())
                        futures[ex.submit(self.get_page_html, page)] = page
                    results = []
                    for fut in as_completed(futures):
                        page = futures[fut]
                        results.append((page, fut.result()))
                        if self.delay:
                            time.sleep(self.delay)

            for page, html in results:
                if not html:
                    continue
                self.log(f"    {C.GREEN}[page]{C.RESET} crawled: {page}")
                js_files, page_links = self.extract_from_html(page, html)
                for js in js_files:
                    self.add_js(js)
                next_level.update(page_links - self.visited_pages)
                self.live_stats()

            current_level = next_level
            if not current_level:
                self.log(f"\n{C.YELLOW}[*]{C.RESET} No new pages found, stopping early.")
                break

        self.log(f"\n{C.BLUE}[*]{C.RESET} Scanning {C.BOLD}{len(self.visited_js)}{C.RESET} JS file(s)...")
        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            futures = [ex.submit(self.extract_from_js, js) for js in list(self.visited_js)]
            for fut in as_completed(futures):
                fut.result()
                if self.delay:
                    time.sleep(self.delay)

        if self.bug_mode and self.visited_js:
            self.log(f"{C.BLUE}[*]{C.RESET} Bug mode: probing {C.BOLD}{len(self.visited_js)}{C.RESET} JS file(s) for source maps...")
            with ThreadPoolExecutor(max_workers=self.threads) as ex:
                futures = [ex.submit(self.check_source_map, js) for js in list(self.visited_js)]
                for fut in as_completed(futures):
                    fut.result()
                    if self.delay:
                        time.sleep(self.delay)

        self.log("")

    def normalize(self, path):
        if path.startswith(("http://", "https://", "//")):
            return urljoin(self.base_url, path)
        if path.startswith("/"):
            p = urlparse(self.base_url)
            return f"{p.scheme}://{p.netloc}{path}"
        return urljoin(self.base_url, path)

    def validate_url(self, url):
        try:
            r = self.session.get(url, headers=self.headers, timeout=self.timeout,
                                  verify=False, allow_redirects=True, proxies=self.proxies)
            return url, r.status_code, len(r.content)
        except requests.RequestException:
            return url, None, None

    def run(self):
        start = time.time()
        self.crawl()
        normalized = sorted({self.normalize(p) for p in self.found_endpoints if len(p) > 1})

        elapsed = time.time() - start
        self.log(f"\n{C.MAGENTA}{'='*60}{C.RESET}")
        self.log(f"{C.BOLD}Pages crawled  : {C.RESET}{len(self.visited_pages)}")
        self.log(f"{C.BOLD}JS files found : {C.RESET}{len(self.visited_js)}")
        self.log(f"{C.BOLD}Endpoints found: {C.RESET}{len(normalized)}")
        self.log(f"{C.BOLD}Time elapsed   : {C.RESET}{elapsed:.1f}s")
        self.log(f"{C.MAGENTA}{'='*60}{C.RESET}\n")

        if self.validate:
            self.log(f"{C.BLUE}[*]{C.RESET} Validating endpoints live (saving as each result arrives)...")
            with ThreadPoolExecutor(max_workers=self.threads) as ex:
                futures = [ex.submit(self.validate_url, u) for u in normalized]
                for fut in as_completed(futures):
                    url, code, size = fut.result()
                    if self.delay:
                        time.sleep(self.delay)
                    if code:
                        color = C.GREEN if code < 300 else C.YELLOW if code < 400 else C.RED
                        print(f"{color}[{code}]{C.RESET} {url}  {C.DIM}({size} bytes){C.RESET}")
                        self.emit({"type": "endpoint", "url": url, "status": code, "size": size})
                    else:
                        print(f"{C.DIM}[unreachable]{C.RESET} {url}")

        if self.find_secrets and self.found_secrets:
            self.log(f"\n{C.RED}{C.BOLD}[!] Potential secrets/tokens found:{C.RESET}")
            for s in self.found_secrets:
                print(f"    {C.RED}{s}{C.RESET}")

        if self.render_mode:
            self.close_browser()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jshunter",
        description=f"{C.CYAN}JSHunter{C.RESET} — Real-time recursive JS/URL endpoint discovery tool.\n"
                     "Crawls a live target N levels deep, harvests every JS file, and\n"
                     "regex-mines it for hidden endpoints, API routes and paths — no\n"
                     "reliance on stale Wayback Machine snapshots. Every finding is\n"
                     "streamed to disk the moment it's discovered, not just at the end.",
        epilog=f"""{C.BOLD}QUICK START{C.RESET}
  jshunter https://target.com

{C.BOLD}RECOMMENDED COMBOS{C.RESET}

  {C.DIM}Everyday bug bounty recon — scope covers *.target.com, save + validate{C.RESET}
  jshunter https://target.com -d 2 --include-subdomains --validate -o results.txt

  {C.DIM}Maximum coverage before a nuclei run — robots/sitemap/source-maps + clean list{C.RESET}
  jshunter https://target.com -d 3 --bug --include-subdomains --nuclei-out urls.txt
  nuclei -l urls.txt -t ~/nuclei-templates/

  {C.DIM}Target is a React/Vue/Angular SPA — plain HTTP crawl finds almost nothing{C.RESET}
  jshunter https://target.com -d 3 --render --include-subdomains -o results.txt

  {C.DIM}SPA + everything else combined, going as deep as possible{C.RESET}
  jshunter https://target.com -d 5 --render --bug --include-subdomains --validate --nuclei-out urls.txt

  {C.DIM}Single host only, quick look, nothing fancy{C.RESET}
  jshunter https://target.com -d 1 -o results.txt

  {C.DIM}Target has a WAF and keeps blocking you — go slow and quiet{C.RESET}
  jshunter https://target.com -d 2 --threads 2 --delay 1.5 -o results.txt

  {C.DIM}Routing through Burp to inspect/replay what jshunter finds{C.RESET}
  jshunter https://target.com -d 2 --proxy http://127.0.0.1:8080

  {C.DIM}Long scan you might need to stop and pick back up later{C.RESET}
  jshunter https://target.com -d 4 -o results.txt
  {C.DIM}# ...Ctrl+C, come back later...{C.RESET}
  jshunter https://target.com -d 4 -o results.txt --resume

  {C.DIM}Hunting for hardcoded API keys/tokens across the whole app{C.RESET}
  jshunter https://target.com -d 3 --include-subdomains --secrets -o secrets.txt

  {C.DIM}Piping straight into other recon tools as JSON Lines{C.RESET}
  jshunter https://target.com -d 2 --format json -o results.jsonl | jq .

{C.BOLD}SCOPE CHEAT SHEET{C.RESET}
  (default)              only the exact host you gave it — app.target.com stays on app.target.com
  --include-subdomains   also follows api.target.com, static.target.com, etc.
  --external             no domain restriction at all — follows anything, anywhere

{C.BOLD}Author:{C.RESET} zamur
{C.BOLD}Discord:{C.RESET} https://discord.com/invite/AA92kB5GSB

{C.YELLOW}Use only against targets you own or are authorized to test.{C.RESET}
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("url", help="Target base URL (e.g. https://target.com)")

    scan = parser.add_argument_group("scan options")
    scan.add_argument("-d", "--depth", type=int, default=1, metavar="N",
                       help="Recursive crawl depth (default: 1). Higher = follows more links "
                            "deeper into the site, discovering more JS/endpoints but taking longer.")
    scan.add_argument("--external", action="store_true",
                       help="No domain restriction at all — follow links to any domain the crawl "
                            "runs into (third-party sites, CDNs, everything). Rarely what you want; "
                            "usually --include-subdomains is the right call instead.")
    scan.add_argument("--include-subdomains", action="store_true",
                       help="Scope the crawl to the target's apex domain, not just the exact host — "
                            "e.g. starting on app.target.com will also follow links to "
                            "api.target.com, static.target.com, target.com, etc. Off by default "
                            "(single-host only), which is what you want for a focused scan; turn "
                            "this on when the bounty/pentest scope covers *.target.com.")
    scan.add_argument("--secrets", action="store_true",
                       help="Also flag likely hardcoded API keys/tokens found inside JS files")
    scan.add_argument("--validate", action="store_true",
                       help="Send a live request to each discovered endpoint and report its status code")
    scan.add_argument("--bug", action="store_true",
                       help="Deep/aggressive discovery mode: seeds the crawl from robots.txt and "
                            "sitemap.xml on top of normal link-following, and probes every JS file "
                            "for an exposed .js.map source map to mine for extra routes. Finds "
                            "significantly more than a plain crawl — combine with --nuclei-out to "
                            "get a clean URL list ready for `nuclei -l`.")
    scan.add_argument("--render", action="store_true",
                       help="Use a real headless browser (Playwright/Chromium) instead of raw "
                            "HTTP requests. Actually executes the page's JavaScript, so it sees "
                            "routes and links that only exist after a React/Vue/Angular app "
                            "renders, and records every fetch()/XHR call the app makes while "
                            "loading — the only reliable way to crawl a modern SPA. Slower and "
                            "heavier than the default mode; requires "
                            "`pip install playwright && playwright install chromium`.")

    perf = parser.add_argument_group("performance / evasion")
    perf.add_argument("--threads", type=int, default=10, metavar="N",
                       help="Number of concurrent worker threads (default: 10)")
    perf.add_argument("--delay", type=float, default=0.0, metavar="SEC",
                       help="Delay in seconds between requests — increase this to reduce the "
                            "chance of tripping rate-limits or WAF rules (default: 0)")
    perf.add_argument("--timeout", type=float, default=10, metavar="SEC",
                       help="Per-request timeout in seconds (default: 10)")
    perf.add_argument("--user-agent", metavar="STRING",
                       help="Custom User-Agent header (default: a recent Chrome UA string)")
    perf.add_argument("--no-skip-static", action="store_true",
                       help="Also follow static asset links (images/css/fonts/media) while "
                            "crawling pages — normally skipped since they never contain endpoints "
                            "and just waste requests")
    perf.add_argument("--proxy", metavar="URL",
                       help="Route all requests through a proxy, e.g. http://127.0.0.1:8080 "
                            "(handy for routing through Burp, or a rotating proxy to spread load)")
    perf.add_argument("--retries", type=int, default=2, metavar="N",
                       help="Retry a failed request this many times before giving up (default: 2)")

    out = parser.add_argument_group("output")
    out.add_argument("-o", "--out", metavar="FILE",
                      help="Stream results to this file IN REAL TIME as they're discovered "
                           "(not just when the scan finishes). Safe to `tail -f` while running, "
                           "and results are preserved even if the scan is interrupted.")
    out.add_argument("--format", choices=["txt", "json"], default="txt",
                      help="Output format for --out. 'txt' = one result per line. "
                           "'json' = JSON Lines (one JSON object per line), ideal for streaming "
                           "and piping into other tools (default: txt)")
    out.add_argument("-q", "--quiet", action="store_true",
                      help="Suppress progress logs, only print final results")
    out.add_argument("--no-color", action="store_true", help="Disable colored output")
    out.add_argument("-v", "--version", action="version", version=f"JSHunter v{VERSION} by zamur")
    out.add_argument("--resume", action="store_true",
                      help="Skip endpoints already present in the --out file from a previous run, "
                           "instead of rediscovering and rewriting them")
    out.add_argument("--nuclei-out", metavar="FILE",
                      help="Also write a plain, deduped, one-URL-per-line file with no status "
                           "codes or JSON wrapping — feed it straight into nuclei with "
                           "`nuclei -l FILE -t <templates>`")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.no_color:
        C.disable()

    if not args.quiet:
        print(BANNER)

    if args.render and not PLAYWRIGHT_AVAILABLE:
        print(f"{C.RED}[!] --render needs playwright, which isn't installed.{C.RESET}\n"
              f"    Install it with:\n"
              f"      pip install playwright --break-system-packages\n"
              f"      playwright install chromium\n")
        sys.exit(1)

    if not args.url.startswith(("http://", "https://")):
        args.url = "https://" + args.url

    resume_seen = set()
    if args.resume and args.out:
        try:
            with open(args.out) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if args.format == "json":
                        try:
                            resume_seen.add(json.loads(line).get("url", ""))
                        except json.JSONDecodeError:
                            continue
                    else:
                        resume_seen.add(line.split("] ", 1)[-1])
            print(f"{C.YELLOW}[+]{C.RESET} Resuming — {len(resume_seen)} endpoint(s) already known from {args.out}\n")
        except FileNotFoundError:
            pass

    writer = LiveWriter(args.out, fmt=args.format) if args.out else None
    if writer and resume_seen:
        writer.seen.update(("endpoint", u) for u in resume_seen)
    if writer:
        print(f"{C.GREEN}[+]{C.RESET} Live-saving results to {C.BOLD}{args.out}{C.RESET} "
              f"({args.format}) — try `tail -f {args.out}` in another terminal.\n")

    hunter = JSHunter(
        base_url=args.url,
        depth=args.depth,
        threads=args.threads,
        delay=args.delay,
        timeout=args.timeout,
        same_domain_only=not args.external,
        include_subdomains=args.include_subdomains,
        validate=args.validate,
        find_secrets=args.secrets,
        skip_static=not args.no_skip_static,
        user_agent=args.user_agent,
        proxy=args.proxy,
        retries=args.retries,
        bug_mode=args.bug,
        render_mode=args.render,
        verbose=not args.quiet,
        writer=writer,
        resume_seen=resume_seen,
    )

    try:
        hunter.run()
    except KeyboardInterrupt:
        print(f"\n{C.RED}[!] Interrupted by user — partial results are already saved.{C.RESET}")
    finally:
        if writer:
            writer.close()
            print(f"\n{C.GREEN}[+]{C.RESET} All results saved to {C.BOLD}{args.out}{C.RESET}")

    if args.nuclei_out:
        urls = sorted({hunter.normalize(p) for p in hunter.found_endpoints if len(p) > 1})
        with open(args.nuclei_out, "w") as f:
            f.write("\n".join(urls) + "\n")
        print(f"{C.GREEN}[+]{C.RESET} {len(urls)} plain URL(s) written to {C.BOLD}{args.nuclei_out}{C.RESET} "
              f"— run: {C.CYAN}nuclei -l {args.nuclei_out} -t <templates>{C.RESET}")


if __name__ == "__main__":
    main()
