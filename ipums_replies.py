#!/usr/bin/env python3
"""
Mirror your IPUMS forum replies to a static HTML page.

Two stages, because only one of them needs your credentials:

  index   - list which posts are yours.  REQUIRES AUTH (profiles are hidden
            from anonymous visitors on forum.ipums.org).
  fetch   - download each post's body.   NO AUTH NEEDED (post bodies are public).
  render  - write out static HTML.

Both index and fetch are incremental: re-running only pulls what's new, so the
first run costs ~800 requests and every run after that costs a handful.

Usage:
    export DISCOURSE_COOKIE='_t=...'          # see AUTH below
    # or: export DISCOURSE_API_KEY=...  DISCOURSE_API_USER=Ivan_Strahof

    python ipums_replies.py index      # refresh the list of your posts
    python ipums_replies.py fetch      # download bodies for anything new
    python ipums_replies.py render     # write site/index.html
    python ipums_replies.py all        # all three

AUTH — two options, pick one:

  1. Session cookie (works right now, no one else involved, expires eventually).
     Log into forum.ipums.org in a browser, open DevTools > Application >
     Cookies, copy the value of the `_t` cookie, and set:
         export DISCOURSE_COOKIE='_t=<that value>'

  2. A real API key (durable; an IPUMS forum admin creates it for you).
     Admin > API > New API Key, scoped to read / "user_actions" for your user.
         export DISCOURSE_API_KEY=<key>
         export DISCOURSE_API_USER=Ivan_Strahof
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE = "https://forum.ipums.org"
USERNAME = os.environ.get("DISCOURSE_USERNAME", "Ivan_Strahof")

# Discourse user_action type 5 == "reply" (a post that isn't the topic starter).
# Add 4 as well if you also want topics you started.
ACTION_TYPES = "5"

OUT = Path(__file__).parent
INDEX_FILE = OUT / "post_index.json"   # [{id, topic_id, title, created_at, ...}]
POSTS_FILE = OUT / "posts.json"        # {post_id: {…full post…}}

# Where the finished index.html goes. Override for e.g. a GitHub Pages subfolder:
#     export SITE_DIR=ipums-forum
SITE_DIR = Path(os.environ.get("SITE_DIR", OUT / "site"))

DELAY = 1.0        # seconds between requests. Be polite; this is a small forum.
PAGE_SIZE = 30     # what user_actions.json returns per call


# ---------------------------------------------------------------- http

def get(path, auth=False):
    """GET a JSON endpoint. Set auth=True for endpoints that need credentials."""
    req = urllib.request.Request(
        BASE + path,
        headers={"User-Agent": f"{USERNAME}-personal-archive/1.0", "Accept": "application/json"},
    )
    if auth:
        key, user = os.environ.get("DISCOURSE_API_KEY"), os.environ.get("DISCOURSE_API_USER")
        cookie = os.environ.get("DISCOURSE_COOKIE")
        if key and user:
            req.add_header("Api-Key", key)
            req.add_header("Api-Username", user)
        elif cookie:
            req.add_header("Cookie", cookie)
        else:
            sys.exit("No credentials. Set DISCOURSE_COOKIE, or DISCOURSE_API_KEY + "
                     "DISCOURSE_API_USER. See the AUTH notes at the top of this file.")

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404) and auth:
            sys.exit(f"HTTP {e.code} on {path} — credentials are missing, expired, or "
                     f"not permitted to read this user's activity.")
        if e.code == 429:
            print("  rate limited, waiting 60s…")
            time.sleep(60)
            return get(path, auth=auth)
        raise


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- stage 1

def build_index():
    """Page through your activity to collect the IDs of every reply you've posted."""
    known = {str(p["id"]): p for p in load(INDEX_FILE, [])}
    before = len(known)
    offset, new_this_run, consecutive_known = 0, 0, 0

    while True:
        data = get(
            f"/user_actions.json?username={USERNAME}"
            f"&filter={ACTION_TYPES}&offset={offset}",
            auth=True,
        )
        actions = data.get("user_actions", [])
        if not actions:
            break

        for a in actions:
            pid = str(a.get("post_id") or a.get("id"))
            if pid in known:
                consecutive_known += 1
                continue
            consecutive_known = 0
            new_this_run += 1
            known[pid] = {
                "id": int(pid),
                "topic_id": a.get("topic_id"),
                "post_number": a.get("post_number"),
                "title": a.get("title"),
                "created_at": a.get("created_at"),
                "category_id": a.get("category_id"),
                "slug": a.get("slug"),
            }

        offset += PAGE_SIZE
        print(f"  indexed {offset} actions, {len(known)} posts known")

        # Activity comes back newest-first, so once we've seen a long run of
        # already-known posts on a refresh, everything older is known too.
        if before and consecutive_known >= PAGE_SIZE:
            print("  reached previously-indexed posts, stopping")
            break
        time.sleep(DELAY)

    index = sorted(known.values(), key=lambda p: p.get("created_at") or "")
    save(INDEX_FILE, index)
    print(f"index: {len(index)} posts total ({new_this_run} new)")


# ---------------------------------------------------------------- stage 2

def fetch_posts():
    """Download the body of each indexed post. No auth needed — these are public."""
    index = load(INDEX_FILE, [])
    if not index:
        sys.exit("No index yet. Run `index` first.")

    posts = load(POSTS_FILE, {})
    todo = [p for p in index if str(p["id"]) not in posts]
    print(f"fetching {len(todo)} new posts ({len(posts)} already cached)")

    for i, entry in enumerate(todo, 1):
        pid = entry["id"]
        try:
            d = get(f"/posts/{pid}.json")
        except urllib.error.HTTPError as e:
            print(f"  [{i}/{len(todo)}] post {pid}: HTTP {e.code}, skipping")
            continue

        posts[str(pid)] = {
            "id": pid,
            "cooked": d.get("cooked", ""),
            "created_at": d.get("created_at"),
            "topic_id": d.get("topic_id"),
            "topic_slug": d.get("topic_slug"),
            "topic_title": d.get("topic_title") or entry.get("title"),
            "post_number": d.get("post_number"),
            "category_id": d.get("category_id") or entry.get("category_id"),
            "reply_count": d.get("reply_count", 0),
            "accepted_answer": d.get("accepted_answer", False),
        }

        if i % 25 == 0:
            save(POSTS_FILE, posts)   # checkpoint, so a crash doesn't cost the run
            print(f"  [{i}/{len(todo)}] cached")
        time.sleep(DELAY)

    save(POSTS_FILE, posts)
    print(f"fetch: {len(posts)} posts cached")


# ---------------------------------------------------------------- stage 3

CSS = """
:root { --fg:#1a1a1a; --muted:#666; --rule:#e2e2e2; --accent:#00263a; --bg:#fff; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e8e8; --muted:#999; --rule:#333; --accent:#7fb2cc; --bg:#161616; }
}
* { box-sizing: border-box; }
body { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1rem 5rem;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Georgia, serif;
  color: var(--fg); background: var(--bg); }
h1 { font-size: 1.7rem; margin-bottom: .25rem; }
.sub { color: var(--muted); margin-bottom: 2.5rem; }
h2 { font-size: 1.1rem; margin: 2.5rem 0 1rem; padding-bottom: .3rem;
  border-bottom: 2px solid var(--rule); letter-spacing: .04em; text-transform: uppercase; }
article { padding: 1.25rem 0; border-bottom: 1px solid var(--rule); }
article h3 { font-size: 1.05rem; margin: 0 0 .3rem; }
article h3 a { color: var(--accent); text-decoration: none; }
article h3 a:hover { text-decoration: underline; }
.meta { font-size: .82rem; color: var(--muted); margin-bottom: .75rem; }
.solved { color: #2e7d32; font-weight: 600; }
@media (prefers-color-scheme: dark) { .solved { color: #7fd18a; } }
.body { font-size: .95rem; }
.body p:first-child { margin-top: 0; }
.body pre { background: rgba(127,127,127,.12); padding: .75rem; overflow-x: auto;
  font-size: .85rem; border-radius: 4px; }
.body code { font-size: .88em; }
.body img, .body table { max-width: 100%; }
.body aside.quote, .body blockquote { border-left: 3px solid var(--rule);
  margin-left: 0; padding-left: 1rem; color: var(--muted); }
"""

CATEGORIES = {
    24: "USA", 22: "CPS", 23: "International", 20: "Global Health", 26: "NHGIS",
    63: "IHGIS", 19: "Time Use", 21: "Health Surveys", 13: "Higher Ed", 27: "Terra",
    18: "ipumsr", 17: "Abacus", 1: "General", 3: "Feedback", 15: "Announcements",
    16: "IPUMS API", 62: "Teaching with IPUMS",
}


def render():
    posts = list(load(POSTS_FILE, {}).values())
    if not posts:
        sys.exit("Nothing cached. Run `fetch` first.")

    posts.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    by_year = defaultdict(list)
    for p in posts:
        by_year[(p.get("created_at") or "????")[:4]].append(p)

    out = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>IPUMS Forum Replies</title>",
        f"<style>{CSS}</style></head><body>",
        "<h1>IPUMS Forum Replies</h1>",
        f"<p class='sub'>{len(posts)} replies on "
        f"<a href='{BASE}'>forum.ipums.org</a>, helping researchers with IPUMS "
        f"microdata. Each links back to its original thread.</p>",
    ]

    for year in sorted(by_year, reverse=True):
        group = by_year[year]
        out.append(f"<h2>{year} &middot; {len(group)} replies</h2>")
        for p in group:
            url = f"{BASE}/t/{p.get('topic_slug') or 'topic'}/{p['topic_id']}/{p.get('post_number', 1)}"
            title = html.escape(p.get("topic_title") or "Untitled topic")
            date = (p.get("created_at") or "")[:10]
            cat = CATEGORIES.get(p.get("category_id"), "")
            bits = [b for b in (date, cat) if b]
            if p.get("accepted_answer"):
                bits.append("<span class='solved'>marked as solution</span>")
            out.append(
                f"<article><h3><a href='{url}'>{title}</a></h3>"
                f"<div class='meta'>{' &middot; '.join(bits)}</div>"
                f"<div class='body'>{p.get('cooked', '')}</div></article>"
            )

    out.append("</body></html>")

    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text("\n".join(out), encoding="utf-8")
    print(f"render: wrote {SITE_DIR / 'index.html'} ({len(posts)} replies)")


# ---------------------------------------------------------------- cli

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("index", "all"):
        build_index()
    if cmd in ("fetch", "all"):
        fetch_posts()
    if cmd in ("render", "all"):
        render()
    if cmd not in ("index", "fetch", "render", "all"):
        sys.exit(__doc__)
