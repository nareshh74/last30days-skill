"""Opt-in, domain-scoped Firefox session for YouTube (yt-dlp) and Reddit.

Enabled ONLY when LAST30DAYS_FIREFOX_PROFILE points at one Firefox profile dir.
Reads cookie rows for exactly one domain family per use (youtube.com or
reddit.com), through a read-only SQLite connection, in memory. Values are never
logged, exported, copied or written to disk. Not a general FROM_BROWSER reader.

As a script: ``python scoped_firefox.py <yt-dlp args>`` runs yt-dlp with the
YouTube-scoped cookie jar (used by youtube_yt._run_ytdlp).
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

ENV_KEY = "LAST30DAYS_FIREFOX_PROFILE"


def profile_dir() -> Optional[Path]:
    raw = os.environ.get(ENV_KEY, "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if (p / "cookies.sqlite").is_file() else None


def _pred(domain: str) -> str:
    # domain comes from a fixed internal allowlist, never user input
    return (f"(lower(ltrim(host,'.'))='{domain}' "
            f"OR lower(ltrim(host,'.')) LIKE '%.{domain}')")


_ALLOWED = ("reddit.com", "youtube.com")


def _rows(domain: str, columns: str = "name,value") -> list:
    if domain not in _ALLOWED:
        raise PermissionError("domain not allowed")
    prof = profile_dir()
    if prof is None:
        raise RuntimeError("scoped Firefox profile not configured")
    uri = (prof / "cookies.sqlite").as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0)) as c:
        return c.execute(
            f"SELECT {columns} FROM moz_cookies WHERE {_pred(domain)}"
        ).fetchall()


# ---------------------------------------------------------------- Reddit
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_opener = urllib.request.build_opener(_NoRedirect())


def reddit_available() -> bool:
    return profile_dir() is not None


def reddit_get_json(url: str, timeout: float = 15) -> Any:
    """GET a www.reddit.com JSON URL with the in-memory Reddit session.

    Raises lib.http.HTTPError carrying the status so callers/doctor classify
    401/403 (auth), 429 (rate limit) and timeouts distinctly.
    """
    from . import http
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname != "www.reddit.com":
        raise PermissionError("only https://www.reddit.com is allowed")
    rows = _rows("reddit.com")
    if not any(n == "reddit_session" for n, _ in rows):
        raise http.HTTPError("Reddit session cookie missing: not logged in", status_code=401)
    header = "; ".join(f"{n}={v}" for n, v in rows)
    req = urllib.request.Request(url, headers={
        "User-Agent": "last30days/3.26 (scoped-firefox)",
        "Accept": "application/json", "Cookie": header})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise http.HTTPError(f"Native Reddit HTTP {e.code}", status_code=e.code) from None
    except (TimeoutError, OSError) as e:
        if "timed out" in str(e).lower():
            raise http.HTTPError("Native Reddit request timed out") from None
        raise http.HTTPError(f"Native Reddit connection error: {type(e).__name__}") from None
    except json.JSONDecodeError:
        raise http.HTTPError("Native Reddit invalid JSON (non-json interstitial)") from None


def reddit_search(topic: str, from_date: str, to_date: str, depth: str = "default",
                  comment_threads: int = 3) -> list:
    from . import reddit_public as rp
    limit = rp.DEPTH_LIMITS.get(depth, 25)
    url = "https://www.reddit.com/search.json?" + urllib.parse.urlencode(
        {"q": topic, "sort": "relevance", "t": "month", "limit": limit, "raw_json": 1})
    posts = rp._parse_posts(reddit_get_json(url))
    posts = [p for p in posts if not p.get("date") or from_date <= p["date"] <= to_date]
    for i, p in enumerate(posts):
        p["id"] = f"R{i + 1}"
        p["why_relevant"] = "Reddit search (authenticated session)"
        if i < comment_threads and p.get("num_comments"):
            try:
                data = reddit_get_json(p["url"].rstrip("/") + ".json?limit=5&sort=top&raw_json=1")
                tops = [c["data"] for c in data[1]["data"]["children"]
                        if c.get("kind") == "t1" and c.get("data", {}).get("body")]
                p["top_comments"] = [{
                    "author": c.get("author"), "score": c.get("score", 0),
                    "excerpt": c["body"][:400],
                    "url": "https://www.reddit.com" + c.get("permalink", ""),
                } for c in tops[:3]]
                p["comment_insights"] = [c["excerpt"] for c in p["top_comments"]]
            except Exception as exc:  # comment enrichment is best-effort
                p.setdefault("metadata", {})["comments_error"] = str(exc)[:120]
    return posts


# ---------------------------------------------------------------- yt-dlp
def wrapper_cmd(cmd: list) -> list:
    """Rewrite ['yt-dlp', ...] into a scoped-cookie wrapper invocation."""
    if not cmd or cmd[0] != "yt-dlp" or profile_dir() is None:
        return cmd
    return [_ytdlp_python(), "-I", "-B", str(Path(__file__).resolve())] + cmd[1:]


def _ytdlp_python() -> str:
    """sys.executable if it has yt_dlp, else the interpreter beside the yt-dlp launcher."""
    import importlib.util
    import shutil
    if importlib.util.find_spec("yt_dlp") is not None:
        return sys.executable
    exe = shutil.which("yt-dlp")
    if exe:
        for name in ("python.exe", "python"):
            cand = Path(exe).resolve().parent / name
            if cand.exists():
                return str(cand)
    return sys.executable


def _run_ytdlp_main(argv: list) -> int:
    import yt_dlp
    from yt_dlp import cookies
    prof = profile_dir()
    if prof is None:
        sys.stderr.write("ERROR: scoped Firefox profile not configured\n")
        return 2
    db = (prof / "cookies.sqlite").resolve()
    yt_pred = _pred("youtube.com")

    class _Cur:
        def __init__(self, cur):
            self.cur = cur
            self.connection = cur.connection

        def execute(self, sql, *a):
            if sql.startswith("SELECT host, name, value, path, expiry, isSecure FROM moz_cookies"):
                sql = sql.split(" WHERE ")[0] + " WHERE " + yt_pred  # ignore other filters
            elif sql != "PRAGMA user_version;":
                raise RuntimeError("unexpected cookie query")
            self.cur.execute(sql, *a)
            return self

        def fetchone(self):
            return self.cur.fetchone()

        def fetchall(self):
            return self.cur.fetchall()

    def open_scoped(path, tmpdir):
        if Path(path).resolve() != db:
            raise PermissionError("unexpected cookie database")
        return _Cur(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0).cursor())

    cookies._open_database_copy = open_scoped
    args = [a for a in argv if a != "--no-cookies-from-browser"]
    forbidden = ("--cookies", "--cookies-from-browser", "--print-traffic", "--verbose", "-v")
    if any(a in forbidden for a in args):
        sys.stderr.write("ERROR: option not allowed in scoped wrapper\n")
        return 2
    pre = ["--cookies-from-browser", f"firefox:{prof}"]
    import shutil
    node = shutil.which("node")
    if node and "--js-runtimes" not in args:  # yt-dlp needs a JS runtime for YouTube
        pre += ["--js-runtimes", f"node:{node}"]
    if "--ignore-no-formats-error" not in args:  # metadata/captions need no media format
        pre += ["--ignore-no-formats-error"]
    args = pre + args
    try:
        yt_dlp.main(args)
    except SystemExit as e:
        return int(e.code or 0)
    return 0


if __name__ == "__main__":
    sys.exit(_run_ytdlp_main(sys.argv[1:]))
