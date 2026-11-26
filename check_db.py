import os
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "polish_corpus.db"
if not os.path.exists(db):
    sys.exit(f"{db} not found, run main.py first")

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
pages, sites, words, chars, first, last, span_hours = conn.execute(
    "SELECT COUNT(*), COUNT(DISTINCT domain), COALESCE(SUM(n_words), 0), COALESCE(SUM(n_chars), 0),"
    " MIN(fetched_at), MAX(fetched_at),"
    " (julianday(MAX(fetched_at)) - julianday(MIN(fetched_at))) * 24 FROM pages"
).fetchone()
if pages == 0:
    sys.exit("database is empty, check the log")


def pages_since(minutes):
    return conn.execute(
        "SELECT COUNT(*) FROM pages WHERE datetime(fetched_at) >= datetime('now', ?)",
        (f"-{minutes} minutes",),
    ).fetchone()[0]


last_hour = pages_since(60)
last_10 = pages_since(10)

print(f"database:   {db} ({os.path.getsize(db) / 1e6:.1f} MB)")
print(f"pages:      {pages:,}")
print(f"sites:      {sites:,}")
print(f"words:      {words:,}")
print(f"characters: {chars:,}")
print(f"first page: {first} UTC")
print(f"last page:  {last} UTC")
todo, done = conn.execute("SELECT COALESCE(SUM(done = 0), 0), COALESCE(SUM(done = 1), 0) FROM queue").fetchone()
print(f"queue:      {done:,} visited, {todo:,} waiting")

print("\nspeed (saved pages):")
if span_hours >= 1 / 60:
    print(f"  overall:      {pages / span_hours:9,.0f} pages/hour  ({pages:,} pages in {span_hours:.2f} h)")
else:
    print("  overall:      not enough data yet")
print(f"  last 60 min:  {last_hour:9,} pages")
print(f"  last 10 min:  {last_10:9,} pages  (= {last_10 * 6:,} pages/hour)")

hourly = conn.execute(
    "SELECT strftime('%Y-%m-%d %H:00', fetched_at) AS h, COUNT(*), SUM(n_words) FROM pages"
    " GROUP BY h ORDER BY h DESC LIMIT 12"
).fetchall()
print("\npages per hour (UTC, newest first, the current hour is not finished yet):")
peak = max(n for _, n, _ in hourly)
for hour, n, w in hourly:
    print(f"  {hour}  {n:7,} pages {w:11,} words  {'#' * round(30 * n / peak)}")

print("\ntop sites:")
for domain, n, w in conn.execute(
    "SELECT domain, COUNT(*), SUM(n_words) FROM pages GROUP BY domain ORDER BY COUNT(*) DESC LIMIT 15"
):
    print(f"  {n:7,} pages {w:11,} words  {domain}")

print("\nnewest pages:")
for url, conf, text in conn.execute(
    "SELECT url, polish_confidence, substr(text, 1, 160) FROM pages ORDER BY id DESC LIMIT 3"
):
    print(f"  {url}  ({conf:.2f})")
    print(f"    {' '.join(text.split())}")
conn.close()
