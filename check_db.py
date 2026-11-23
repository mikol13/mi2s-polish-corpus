import os
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "polish_corpus.db"
if not os.path.exists(db):
    sys.exit(f"{db} not found, run main.py first")

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
pages, sites, words, chars, first, last = conn.execute(
    "SELECT COUNT(*), COUNT(DISTINCT domain), COALESCE(SUM(n_words), 0), COALESCE(SUM(n_chars), 0),"
    " MIN(fetched_at), MAX(fetched_at) FROM pages"
).fetchone()
if pages == 0:
    sys.exit("database is empty, check the log")

recent = conn.execute(
    "SELECT COUNT(*) FROM pages WHERE datetime(fetched_at) >= datetime('now', '-10 minutes')"
).fetchone()[0]

print(f"database:   {db} ({os.path.getsize(db) / 1e6:.1f} MB)")
print(f"pages:      {pages:,}")
print(f"sites:      {sites:,}")
print(f"words:      {words:,}")
print(f"characters: {chars:,}")
print(f"first page: {first} UTC")
print(f"last page:  {last} UTC")
print(f"last 10 min: {recent} pages")
todo, done = conn.execute("SELECT SUM(done = 0), SUM(done = 1) FROM queue").fetchone()
print(f"queue:      {done:,} visited, {todo:,} waiting")

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
