"""Bounded snapshot retention for the sccache compiler cache.

GitHub Actions caches are immutable: each run saves a NEW snapshot under its own
exact key, so the set of snapshots under the compatibility prefix grows with every
distinct source/dependency identity. This module bounds that growth:

  * it lists the cache entries under the sccache compatibility prefix via the Actions
    caching API (the ``key`` parameter is a prefix match),
  * keeps the ``--keep`` most recently used snapshots,
  * deletes the older ones with DELETE /actions/caches/{id},
  * emits a diagnostic summary (count, total bytes, newest/oldest) that the metrics
    report uses to explain a miss (eviction vs branch scoping vs toolchain change).

Guarded and non-fatal by design: ANY failure (network, auth, rate limit, HTTP error)
is reported to stdout and the process still exits 0, so a cache-API problem never
fails a build. A concurrent run cannot be hurt: caches are immutable, reads are not
locked, and we only delete snapshots older than the newest ``--keep`` plus a grace
window, so a live snapshot a concurrent job just wrote or is reading is preserved.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.request

_API_BASE = "https://api.github.com"


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_ts(value):
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def api_call(method, url, token):
    """Return (status, parsed_json). status < 0 means a non-HTTP transport error.

    ``url`` may be relative (``/repos/...``); it is resolved against the GitHub
    API base so urllib gets an absolute URL.
    """
    if not url.startswith("http://") and not url.startswith("https://"):
        url = _API_BASE + url
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ninfer-cache-retention",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = {"error": body[:500]}
        return exc.code, parsed
    except Exception as exc:  # noqa: BLE001 - guarded by design
        return -1, {"error": f"{type(exc).__name__}: {exc}"}


def list_snapshots(repo, prefix, token, per_page=100, pages=10):
    """Return the list of cache entries whose key starts with ``prefix``.

    The caching API's ``key`` parameter is a prefix match, so this returns exactly our
    snapshots and never another layer's caches. Query params are URL-encoded so a prefix
    containing reserved characters is sent intact.
    """
    import urllib.parse
    entries = []
    for page in range(1, pages + 1):
        query = urllib.parse.urlencode({"key": prefix, "per_page": per_page, "page": page})
        status, data = api_call("GET", f"/repos/{repo}/actions/caches?{query}", token)
        if status < 0 or status != 200:
            raise RuntimeError(f"cache list HTTP {status}: {data}")
        items = data.get("actions_caches", [])
        entries.extend(items)
        if len(items) < per_page:
            break
    return entries


def select_deletions(entries, keep, grace_minutes, now):
    """Return (to_delete, kept) — the oldest entries beyond ``keep`` outside the grace window."""
    def sort_key(e):
        ts = (_parse_ts(e.get("last_accessed_at"))
              or _parse_ts(e.get("last_used"))
              or _parse_ts(e.get("created_at"))
              or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
        return ts
    ranked = sorted(entries, key=sort_key, reverse=True)
    to_delete, kept = [], []
    for rank, entry in enumerate(ranked):
        if rank < keep:
            kept.append(entry)
            continue
        ts = sort_key(entry)
        age_min = (now - ts).total_seconds() / 60.0 if ts > _dt.datetime.min.replace(tzinfo=_dt.timezone.utc) else float("inf")
        if age_min < grace_minutes:
            # Recently used: a concurrent run may still be reading/saving it. Preserve.
            kept.append(entry)
        else:
            to_delete.append(entry)
    return to_delete, kept


def delete_entry(repo, cache_id, token):
    return api_call("DELETE", f"/repos/{repo}/actions/caches/{cache_id}", token)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="owner/name, e.g. brx19/ninfer-5090-windows")
    parser.add_argument("--prefix", required=True, help="sccache compatibility prefix (restore key)")
    parser.add_argument("--keep", type=int, default=3, help="number of recent snapshots to retain")
    parser.add_argument("--grace-minutes", type=int, default=45,
                        help="never delete a snapshot used within this many minutes")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--dry-run", action="store_true", help="list + plan, but delete nothing")
    parser.add_argument("--out", default=None, help="write the JSON summary to this path")
    args = parser.parse_args()
    now = _utcnow()
    summary = {
        "repo": args.repo, "prefix": args.prefix, "keep": args.keep,
        "dry_run": args.dry_run, "now": now.isoformat(),
        "status": "ok", "listed": 0, "deleted": [], "kept": [], "skipped_recent": [],
    }
    try:
        if not args.token:
            raise RuntimeError("GITHUB_TOKEN is empty; cannot call the caching API")
        entries = list_snapshots(args.repo, args.prefix, args.token)
        summary["listed"] = len(entries)
        total_bytes = sum(int(e.get("size_in_bytes") or 0) for e in entries)
        summary["total_bytes"] = total_bytes
        to_delete, kept = select_deletions(entries, args.keep, args.grace_minutes, now)
        for e in kept:
            summary["kept"].append({"id": e.get("id"), "key": e.get("key"),
                                    "size": e.get("size_in_bytes"),
                                    "last_used": e.get("last_accessed_at") or e.get("last_used")})
        for e in to_delete:
            summary["skipped_recent"].append({"id": e.get("id"), "key": e.get("key")})
        if args.dry_run:
            summary["status"] = "dry-run"
        else:
            for e in to_delete:
                status, data = delete_entry(args.repo, e.get("id"), args.token)
                ok = status in (200, 204)
                summary["deleted"].append({"id": e.get("id"), "key": e.get("key"),
                                           "http": status, "ok": ok})
                if not ok:
                    summary["status"] = "degraded"
                    summary.setdefault("errors", []).append(f"delete {e.get('id')} -> {status}: {data}")
    except Exception as exc:  # noqa: BLE001 - guarded by design
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2)

    # Human-readable line for the workflow log / step summary.
    keep_n = len(summary.get("kept", []))
    del_n = len(summary.get("deleted", []))
    total = summary.get("total_bytes", 0)
    print(f"retention> status={summary['status']} listed={summary.get('listed',0)} "
          f"kept={keep_n} deleted={del_n} total={total/1048576:.2f} MiB")
    for line in summary.get("errors", []):
        print(f"retention> ERROR {line}")
    if summary.get("error"):
        print(f"retention> ERROR {summary['error']}")
    # Always exit 0: a cache-API problem must never fail the build.
    sys.exit(0)


if __name__ == "__main__":
    main()
