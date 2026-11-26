import argparse
import hashlib
import logging
import re
import sqlite3
import threading
import time
import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from lingua import Language, LanguageDetectorBuilder
from requests.adapters import HTTPAdapter

try:
    import lxml  # (much faster than html.parser)
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

USER_AGENT = "UniResearchBot/0.1 (student crawler, NCKU course; contact: 257209@edu.p.lodz.pl)"
MIN_CHARS = 300
MIN_POLISH_CONFIDENCE = 0.8
MIN_DIACRITICS = 0.01  # real Polish text is ~6% ąćęłńóśźż, so this only drops clearly foreign text
DETECT_CHARS = 1000
MAX_PAGES_PER_DOMAIN = 3000
MAX_FAILS_PER_DOMAIN = 5
NO_POLISH_LIMIT = 15
RATE_LIMIT_COOLDOWN = 60
GLOBAL_ERROR_LIMIT = 30
GLOBAL_PAUSE = 120
TIMEOUT = (5, 10)
MAX_BYTES = 5_000_000
MAX_DOWNLOAD_SECONDS = 30

SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".rar", ".7z",
                   ".mp3", ".mp4", ".avi", ".mov", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                   ".exe", ".dmg", ".iso", ".css", ".js", ".xml", ".rss", ".json")

BAD_URL = re.compile(
    r"[?&](sort|order|filter|sid|session|phpsessid|replytocom|share|print|format|lang|date"
    r"|page|paged|strona|pg|s|q|search|action|oldid|diff|veaction|printable|useskin|mobileaction)="
    r"|/(?:login|logout|register|cart|checkout|wp-login|logowanie|zaloguj|rejestracja|zarejestruj"
    r"|koszyk|zamowienie|platnosc|konto|moje-konto|profil|uzytkownik|user"
    r"|feed|wp-json|wp-admin|wp-content|wp-includes|cdn-cgi|trackback"
    r"|tag|tags|tagi|kalendarz|calendar|search|szukaj|wyszukiwarka)(?:[/?#.]|$)"
    r"|/(?:page|strona)/\d+"
    r"|/wiki/(?:Specjalna|Wikipedia|Wikipedysta|Plik|Szablon|Pomoc|Kategoria|Portal|Wikiprojekt"
    r"|MediaWiki|Special|File|Category|Template|Help|User|Talk)(?::|%3A)"
    r"|/wiki/Dyskusja[^/?#:%]*(?::|%3A)",
    re.I,
)

SKIP_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]
POLISH_LETTERS = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")

DETECTOR = LanguageDetectorBuilder.from_languages(
    Language.POLISH, Language.ENGLISH, Language.GERMAN, Language.RUSSIAN, Language.UKRAINIAN,
    Language.CZECH, Language.SLOVAK, Language.LITHUANIAN, Language.FRENCH, Language.SPANISH,
    Language.ITALIAN,
).with_preloaded_language_models().build()

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    title TEXT,
    text TEXT NOT NULL,
    n_chars INTEGER NOT NULL,
    n_words INTEGER NOT NULL,
    polish_confidence REAL NOT NULL,
    text_hash TEXT NOT NULL UNIQUE,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    depth INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_todo ON queue(done, depth, id);
CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages(domain);
"""

log = logging.getLogger("crawler")


class Crawler:

    def __init__(self, db_path, domain_delay, n_threads, any_tld=False):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.domain_delay = domain_delay
        self.n_threads = n_threads
        self.any_tld = any_tld
        self.seed_domains = set()

        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.local = threading.local()
        self.adapter = HTTPAdapter(pool_connections=300, pool_maxsize=4)

        self.robots = {}
        self.in_flight = set()
        self.next_ok = {}
        self.fails = {}
        self.banned = set()
        self.errors_in_a_row = 0
        self.pause_until = 0.0
        self.visited = 0
        self.saved = 0
        self.dom_visits = {}
        self.dom_saved = dict(self.db.execute("SELECT domain, COUNT(*) FROM pages GROUP BY domain"))
        self.queued = dict(self.db.execute("SELECT domain, COUNT(*) FROM queue GROUP BY domain"))

    # ---------- queue ----------

    @staticmethod
    def site_key(netloc):
        """histmag.org and www.histmag.org count as the same site."""
        netloc = netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc

    def domain_ok(self, url):
        """Only .pl sites and the sites from the seed file, unless --any-tld is used."""
        if self.any_tld:
            return True
        parts = urlparse(url)
        return ((parts.hostname or "").lower().endswith(".pl")
                or self.site_key(parts.netloc) in self.seed_domains)

    def add_url(self, url, depth):
        domain = urlparse(url).netloc
        if self.queued.get(domain, 0) >= MAX_PAGES_PER_DOMAIN or domain in self.banned:
            return
        if not self.domain_ok(url):
            return
        cur = self.db.execute("INSERT OR IGNORE INTO queue (url, domain, depth) VALUES (?, ?, ?)",
                              (url, domain, depth))
        if cur.rowcount:
            self.queued[domain] = self.queued.get(domain, 0) + 1

    def ban(self, domain, reason):
        self.banned.add(domain)
        self.db.execute("UPDATE queue SET done = 2 WHERE domain = ? AND done = 0", (domain,))
        log.warning("skipping %s from now on (%s)", domain, reason)

    def claim(self):
        """Return the next (id, url, domain, depth) that may be fetched now, or None."""
        with self.lock:
            now = time.time()
            busy = list(self.in_flight | self.banned)
            busy += [d for d, t in self.next_ok.items() if t > now]
            skip = f" AND domain NOT IN ({','.join('?' * len(busy))})" if busy else ""

            row = self.db.execute(
                "SELECT id, url, domain, depth FROM queue WHERE done = 0" + skip +
                " ORDER BY depth, id LIMIT 1", busy).fetchone()
            if row:
                self.db.execute("UPDATE queue SET done = 1 WHERE id = ?", (row[0],))
                self.in_flight.add(row[2])
                return row

            # nothing available right now. Is there anything left at all?
            banned = list(self.banned)
            skip = f" AND domain NOT IN ({','.join('?' * len(banned))})" if banned else ""
            todo = self.db.execute("SELECT 1 FROM queue WHERE done = 0" + skip + " LIMIT 1",
                                   banned).fetchone()
            if not todo and not self.in_flight:
                log.info("queue is empty")
                self.stop.set()
            return None

    def release(self, domain, touched):
        """Give the domain back. `touched` = we really sent a request to the site."""
        wait = self.wait_for(domain) if touched else 0
        with self.lock:
            if touched:
                self.next_ok[domain] = max(self.next_ok.get(domain, 0), time.time() + wait)
            self.in_flight.discard(domain)

    # ---------- network ----------

    def session(self):
        if not hasattr(self.local, "session"):
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            s.mount("http://", self.adapter)
            s.mount("https://", self.adapter)
            self.local.session = s
        return self.local.session

    def wait_for(self, domain):
        rp = self.robots.get(domain)
        crawl_delay = rp.crawl_delay(USER_AGENT) if rp else None
        return max(self.domain_delay, float(crawl_delay or 0))

    def allowed(self, url):
        parts = urlparse(url)
        domain = parts.netloc
        if domain not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session().get(f"{parts.scheme}://{domain}/robots.txt", timeout=TIMEOUT)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif 400 <= r.status_code < 500:
                    rp.allow_all = True
                else:
                    rp.disallow_all = True
            except requests.RequestException:
                rp.disallow_all = True
            self.robots[domain] = rp
            time.sleep(self.domain_delay)  # robots.txt was a request to this site too
        return self.robots[domain].can_fetch(USER_AGENT, url)

    def note_fail(self, domain, cooldown=0):
        with self.lock:
            self.fails[domain] = self.fails.get(domain, 0) + 1
            if cooldown:
                self.next_ok[domain] = time.time() + cooldown
            if self.fails[domain] >= MAX_FAILS_PER_DOMAIN and domain not in self.banned:
                self.banned.add(domain)
                log.warning("skipping %s from now on (too many errors)", domain)

    def fetch(self, url, domain):
        """Return (final_url, html_bytes) or None. Checks the headers before downloading the body."""
        try:
            with self.session().get(url, timeout=TIMEOUT, stream=True) as r:
                with self.lock:
                    self.errors_in_a_row = 0
                if r.status_code in (403, 429):
                    log.warning("%d from %s", r.status_code, url)
                    self.note_fail(domain, RATE_LIMIT_COOLDOWN if r.status_code == 429 else 0)
                    return None
                if r.status_code != 200:
                    return None
                with self.lock:
                    self.fails[domain] = 0
                if "text/html" not in r.headers.get("Content-Type", ""):
                    return None
                length = r.headers.get("Content-Length", "")
                if length.isdigit() and int(length) > MAX_BYTES:
                    return None

                chunks, size, deadline = [], 0, time.time() + MAX_DOWNLOAD_SECONDS
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES or time.time() > deadline:
                        return None
                    chunks.append(chunk)
                return r.url, b"".join(chunks)
        except requests.RequestException as e:
            log.info("error %s: %s", url, type(e).__name__)
            self.note_fail(domain)
            with self.lock:
                self.errors_in_a_row += 1
                if self.errors_in_a_row >= GLOBAL_ERROR_LIMIT:
                    log.warning("%d errors in a row, maybe the network is down or we are blocked. "
                                "Pausing all threads for %d s", GLOBAL_ERROR_LIMIT, GLOBAL_PAUSE)
                    self.pause_until = time.time() + GLOBAL_PAUSE
                    self.errors_in_a_row = 0
                    self.fails.clear()
            return None

    # ---------- parsing ----------

    def extract(self, soup):
        paragraphs = []
        for p in soup.find_all("p"):
            if p.find_parent(SKIP_TAGS):
                continue
            for t in p(["script", "style", "noscript"]):
                t.decompose()
            s = p.get_text(" ", strip=True)
            if len(s) >= 40:
                paragraphs.append(s)
        text = "\n".join(paragraphs)
        if len(text) < MIN_CHARS:
            return None

        sample = text[:DETECT_CHARS]
        if len(POLISH_LETTERS.findall(sample)) < len(sample) * MIN_DIACRITICS:
            return None
        confidence = DETECTOR.compute_language_confidence(sample, Language.POLISH)
        if confidence < MIN_POLISH_CONFIDENCE:
            return None

        title = soup.title.get_text(strip=True) if soup.title else None
        text_hash = hashlib.md5(" ".join(text.lower().split()).encode()).hexdigest()
        return title, text, confidence, text_hash

    def find_links(self, base_url, soup):
        links = []
        for a in soup.find_all("a", href=True):
            url = urldefrag(urljoin(base_url, a["href"]))[0]
            if not url.startswith(("http://", "https://")):
                continue
            if len(url) > 300 or BAD_URL.search(url) or urlparse(url).path.lower().endswith(SKIP_EXTENSIONS):
                continue
            links.append(url)
        return links

    # ---------- workers ----------

    def process(self, url, domain, depth):
        """Handle one URL. Returns True if a request was sent to the site (so it must rest)."""
        if domain in self.banned or not self.domain_ok(url):
            return False
        known = domain in self.robots
        if not self.allowed(url):
            return not known

        result = self.fetch(url, domain)
        with self.lock:
            self.visited += 1
        if result is None:
            return True
        final_url, body = result

        soup = BeautifulSoup(body, PARSER)
        page = self.extract(soup)
        links = self.find_links(final_url, soup) if page or depth == 0 else []

        with self.lock:
            visits = self.dom_visits[domain] = self.dom_visits.get(domain, 0) + 1
            if page:
                title, text, confidence, text_hash = page
                self.db.execute(
                    "INSERT OR IGNORE INTO pages (url, domain, title, text, n_chars, n_words,"
                    " polish_confidence, text_hash, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (final_url, urlparse(final_url).netloc, title, text, len(text), len(text.split()),
                     round(confidence, 4), text_hash,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
                self.saved += 1
                self.dom_saved[domain] = self.dom_saved.get(domain, 0) + 1
            elif visits >= NO_POLISH_LIMIT and not self.dom_saved.get(domain) and domain not in self.banned:
                self.ban(domain, f"no Polish text in the first {visits} pages")
            if page or depth == 0:
                for link in links:
                    self.add_url(link, depth + 1)
            self.db.commit()
        return True

    def worker(self):
        while not self.stop.is_set():
            if time.time() < self.pause_until:
                time.sleep(1)
                continue
            row = self.claim()
            if row is None:
                time.sleep(0.2)
                continue
            _, url, domain, depth = row
            touched = True
            try:
                touched = self.process(url, domain, depth)
            except Exception:
                log.exception("unexpected error on %s", url)
            finally:
                self.release(domain, touched)

    def run(self, seeds, hours, max_pages):
        with self.lock:
            seeds = [u if urlparse(u).path else u + "/" for u in seeds]  # https://x.pl -> https://x.pl/
            self.seed_domains = {self.site_key(urlparse(u).netloc) for u in seeds}
            for url in seeds:
                self.add_url(url, 0)
            self.db.commit()

        threads = [threading.Thread(target=self.worker, name=f"worker-{i}", daemon=True)
                   for i in range(self.n_threads)]
        start = time.time()
        end = start + hours * 3600 if hours else None
        for t in threads:
            t.start()
        log.info("started %d threads, html parser: %s, only .pl + seed sites: %s",
                 self.n_threads, PARSER, not self.any_tld)

        last_log, last_visited, last_saved = start, 0, 0
        try:
            while not self.stop.is_set():
                time.sleep(1)
                now = time.time()
                if end and now > end:
                    log.info("time limit reached")
                    break
                if max_pages and self.visited >= max_pages:
                    log.info("page limit reached")
                    break
                if now - last_log >= 30:
                    with self.lock:
                        visited, saved = self.visited, self.saved
                        left = self.db.execute("SELECT COUNT(*) FROM queue WHERE done = 0").fetchone()[0]
                        active = len(self.in_flight)
                    mins = (now - last_log) / 60
                    log.info("visited=%d saved=%d (%.0f%%) in_queue=%d active_sites=%d | last 30s: "
                             "%.0f visited/min, %.0f saved/min", visited, saved,
                             100 * saved / max(visited, 1), left, active,
                             (visited - last_visited) / mins, (saved - last_saved) / mins)
                    last_log, last_visited, last_saved = now, visited, saved
        except KeyboardInterrupt:
            log.info("stopped by user, run again to continue")
        finally:
            self.stop.set()
            for t in threads:
                t.join()
            elapsed = max(time.time() - start, 1)
            log.info("finished: visited=%d saved=%d (%.0f%%) in %.1f min (%.0f saved/hour)",
                     self.visited, self.saved, 100 * self.saved / max(self.visited, 1),
                     elapsed / 60, self.saved / elapsed * 3600)


def main():
    parser = argparse.ArgumentParser(description="Crawler for Polish text")
    parser.add_argument("--hours", type=float, default=0, help="stop after this many hours, 0 = no limit")
    parser.add_argument("--max-pages", type=int, default=0, help="stop after this many pages, 0 = no limit")
    parser.add_argument("--db", default="polish_corpus.db")
    parser.add_argument("--seeds", default="seeds.txt")
    parser.add_argument("--threads", type=int, default=32,
                        help="number of worker threads, each one works on a different website")
    parser.add_argument("--domain-delay", type=float, default=1.0,
                        help="min seconds between requests to one site")
    parser.add_argument("--any-tld", action="store_true",
                        help="also follow links to sites that don't end in .pl (seed sites are always allowed)")
    parser.add_argument("--log", help="write the log to this file")
    args = parser.parse_args()

    logging.basicConfig(filename=args.log, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    with open(args.seeds, encoding="utf-8") as f:
        seeds = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    crawler = Crawler(args.db, args.domain_delay, args.threads, args.any_tld)
    try:
        crawler.run(seeds, args.hours, args.max_pages)
    finally:
        crawler.db.commit()
        crawler.db.close()


if __name__ == "__main__":
    main()
