import argparse
import hashlib
import logging
import re
import sqlite3
import time
import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from lingua import Language, LanguageDetectorBuilder

USER_AGENT = "UniResearchBot/0.1 (student crawler, NCKU course; contact: 257209@edu.p.lodz.pl)"
MIN_CHARS = 300
MIN_POLISH_CONFIDENCE = 0.8
MAX_PAGES_PER_DOMAIN = 3000
MAX_FAILS_PER_DOMAIN = 5

SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".rar", ".7z",
                   ".mp3", ".mp4", ".avi", ".mov", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                   ".exe", ".dmg", ".iso", ".css", ".js", ".xml", ".rss", ".json")

BAD_URL = re.compile(
    r"[?&](sort|order|filter|sid|session|phpsessid|replytocom|share|print|format|lang|date)="
    r"|/(?:login|logout|register|cart|checkout|wp-login|logowanie|zaloguj|rejestracja|zarejestruj"
    r"|koszyk|zamowienie|platnosc|konto|moje-konto|profil|uzytkownik|user)(?:[/?#.]|$)",
    re.I,
)

DETECTOR = LanguageDetectorBuilder.from_languages(
    Language.POLISH, Language.ENGLISH, Language.GERMAN, Language.RUSSIAN, Language.UKRAINIAN,
    Language.CZECH, Language.SLOVAK, Language.LITHUANIAN, Language.FRENCH, Language.SPANISH,
    Language.ITALIAN,
).build()

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
    def __init__(self, db_path, delay, domain_delay):
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.delay = delay
        self.domain_delay = domain_delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.robots = {}
        self.last_visit = {}
        self.fails = {}
        self.banned = set()
        self.errors_in_a_row = 0
        self.queued = dict(self.db.execute("SELECT domain, COUNT(*) FROM queue GROUP BY domain"))

    def add_url(self, url, depth):
        domain = urlparse(url).netloc
        if self.queued.get(domain, 0) >= MAX_PAGES_PER_DOMAIN or domain in self.banned:
            return
        cur = self.db.execute("INSERT OR IGNORE INTO queue (url, domain, depth) VALUES (?, ?, ?)",
                              (url, domain, depth))
        if cur.rowcount:
            self.queued[domain] = self.queued.get(domain, 0) + 1

    def next_url(self):
        now = time.time()
        busy = [d for d, t in self.last_visit.items() if now - t < self.wait_for(d)]
        busy += list(self.banned)
        marks = ",".join("?" * len(busy))
        sql = "SELECT id, url, domain, depth FROM queue WHERE done = 0"
        if busy:
            sql += f" AND domain NOT IN ({marks})"
        return self.db.execute(sql + " ORDER BY depth, id LIMIT 1", busy).fetchone()

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
                r = self.session.get(f"{parts.scheme}://{domain}/robots.txt", timeout=15)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif 400 <= r.status_code < 500:
                    rp.allow_all = True
                else:
                    rp.disallow_all = True
            except requests.RequestException:
                rp.disallow_all = True
            self.robots[domain] = rp
            time.sleep(self.delay)
        return self.robots[domain].can_fetch(USER_AGENT, url)

    def fetch(self, url):
        domain = urlparse(url).netloc
        self.last_visit[domain] = time.time()
        try:
            r = self.session.get(url, timeout=20)
        except requests.RequestException as e:
            log.info("error %s: %s", url, type(e).__name__)
            self.errors_in_a_row += 1
            if self.errors_in_a_row >= 20:
                log.warning("20 errors in a row, maybe we are blocked. Waiting 5 minutes")
                time.sleep(300)
                self.errors_in_a_row = 0
            return None
        self.errors_in_a_row = 0

        if r.status_code in (403, 429):
            self.fails[domain] = self.fails.get(domain, 0) + 1
            log.warning("%d from %s", r.status_code, url)
            if r.status_code == 429:
                time.sleep(60)
            if self.fails[domain] >= MAX_FAILS_PER_DOMAIN:
                log.warning("skipping %s from now on", domain)
                self.banned.add(domain)
            return None
        if r.status_code != 200:
            return None
        self.fails[domain] = 0
        if "text/html" not in r.headers.get("Content-Type", ""):
            return None
        if len(r.content) > 5_000_000:
            return None
        return r

    def save(self, url, soup):
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n".join(p for p in paragraphs if len(p) >= 40)
        if len(text) < MIN_CHARS:
            return False

        confidence = DETECTOR.compute_language_confidence(text[:3000], Language.POLISH)
        if confidence < MIN_POLISH_CONFIDENCE:
            return False

        title = soup.title.get_text(strip=True) if soup.title else None
        text_hash = hashlib.md5(" ".join(text.lower().split()).encode()).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO pages (url, domain, title, text, n_chars, n_words, polish_confidence,"
            " text_hash, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, urlparse(url).netloc, title, text, len(text), len(text.split()), round(confidence, 4),
             text_hash, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        return True

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

    def run(self, seeds, hours, max_pages):
        for url in seeds:
            self.add_url(url, 0)
        self.db.commit()

        end = time.time() + hours * 3600 if hours else None
        visited = saved = 0
        while True:
            if end and time.time() > end:
                log.info("time limit reached")
                break
            if max_pages and visited >= max_pages:
                log.info("page limit reached")
                break

            row = self.next_url()
            if row is None:
                if not self.db.execute("SELECT 1 FROM queue WHERE done = 0 LIMIT 1").fetchone():
                    log.info("queue is empty")
                    break
                time.sleep(0.5)
                continue

            qid, url, domain, depth = row
            self.db.execute("UPDATE queue SET done = 1 WHERE id = ?", (qid,))
            if domain in self.banned or not self.allowed(url):
                continue

            r = self.fetch(url)
            visited += 1
            if r is not None:
                soup = BeautifulSoup(r.content, "html.parser")
                links = self.find_links(r.url, soup)
                is_polish = self.save(r.url, soup)
                saved += is_polish
                if is_polish or depth == 0:
                    for link in links:
                        self.add_url(link, depth + 1)
            self.db.commit()

            if visited % 50 == 0:
                left = self.db.execute("SELECT COUNT(*) FROM queue WHERE done = 0").fetchone()[0]
                log.info("visited=%d saved=%d in_queue=%d", visited, saved, left)
            time.sleep(self.delay)

        log.info("finished: visited=%d saved=%d", visited, saved)


def main():
    parser = argparse.ArgumentParser(description="Crawler for Polish text")
    parser.add_argument("--hours", type=float, default=0, help="stop after this many hours, 0 = no limit")
    parser.add_argument("--max-pages", type=int, default=0, help="stop after this many pages, 0 = no limit")
    parser.add_argument("--db", default="polish_corpus.db")
    parser.add_argument("--seeds", default="seeds.txt")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds to sleep after every request")
    parser.add_argument("--domain-delay", type=float, default=3.0, help="min seconds between requests to one site")
    parser.add_argument("--log", help="write the log to this file")
    args = parser.parse_args()

    logging.basicConfig(filename=args.log, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    with open(args.seeds, encoding="utf-8") as f:
        seeds = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    crawler = Crawler(args.db, args.delay, args.domain_delay)
    try:
        crawler.run(seeds, args.hours, args.max_pages)
    except KeyboardInterrupt:
        log.info("stopped by user, run again to continue")
    finally:
        crawler.db.commit()
        crawler.db.close()


if __name__ == "__main__":
    main()
