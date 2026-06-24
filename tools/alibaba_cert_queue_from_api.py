#!/usr/bin/env python3
"""Build a local certificate-link queue from Alibaba's product list API."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alibaba_iop import AlibabaIopClient, response_body  # noqa: E402


DB_PATH = ROOT / "logs" / "alibaba_cert_runner_506068.sqlite3"
MAX_WINDOW_ITEMS = 4950
START_AT = dt.datetime(2010, 1, 1)


def now_text() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cert_product_queue (
            item_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            api_status TEXT NOT NULL DEFAULT '',
            display TEXT NOT NULL DEFAULT '',
            gmt_modified TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'alibaba.icbu.product.list',
            queue_status TEXT NOT NULL DEFAULT 'pending',
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cert_product_queue_status
        ON cert_product_queue(queue_status, updated_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cert_queue_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            api_total INTEGER NOT NULL DEFAULT 0,
            queued_count INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_runs (
            item_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_product_runs_status
        ON product_runs(status)
        """
    )
    conn.commit()
    return conn


def product_id(product: dict) -> str:
    return str(product.get("id") or product.get("productId") or product.get("product_id") or product.get("offerId") or "").strip()


def product_title(product: dict) -> str:
    return str(product.get("title") or product.get("subject") or product.get("productName") or product.get("product_name") or "")[:500]


def product_modified(product: dict) -> str:
    return str(
        product.get("gmtModified")
        or product.get("gmt_modified")
        or product.get("modifiedTime")
        or product.get("modified_time")
        or ""
    )


def listing_products(listing: dict) -> list[dict]:
    products = listing.get("products") or []
    if isinstance(products, dict):
        products = products.get("alibaba_product_brief_response") or products.get("product") or []
    return products if isinstance(products, list) else []


def upsert_queue(conn: sqlite3.Connection, products: list[dict]) -> int:
    timestamp = now_text()
    rows = []
    for product in products:
        item_id = product_id(product)
        if not item_id:
            continue
        rows.append(
            (
                item_id,
                product_title(product),
                str(product.get("status") or product.get("productStatus") or ""),
                str(product.get("display") or product.get("isDisplay") or ""),
                product_modified(product),
                timestamp,
                timestamp,
            )
        )
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO cert_product_queue (
            item_id, title, api_status, display, gmt_modified, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            title = excluded.title,
            api_status = excluded.api_status,
            display = excluded.display,
            gmt_modified = excluded.gmt_modified,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def sync_window(
    client: AlibabaIopClient,
    conn: sqlite3.Connection,
    window_start: dt.datetime,
    window_end: dt.datetime,
    delay: float,
) -> int:
    start_text = window_start.strftime("%Y-%m-%d %H:%M:%S")
    end_text = window_end.strftime("%Y-%m-%d %H:%M:%S")
    first = response_body(client.list_products(1, modified_from=start_text, modified_to=end_text), "alibaba.icbu.product.list")
    total = int(first.get("total_item") or 0)

    if total >= MAX_WINDOW_ITEMS and (window_end - window_start).total_seconds() > 60:
        midpoint = window_start + (window_end - window_start) / 2
        print(f"split {start_text} -> {end_text}, total={total}", flush=True)
        return sync_window(client, conn, window_start, midpoint, delay) + sync_window(
            client,
            conn,
            midpoint + dt.timedelta(seconds=1),
            window_end,
            delay,
        )

    count = upsert_queue(conn, listing_products(first))
    pages = min((total + 29) // 30, 167)
    print(f"sync {start_text} -> {end_text}, total={total}, pages={pages}", flush=True)
    for page in range(2, pages + 1):
        if delay:
            time.sleep(delay)
        listing = response_body(client.list_products(page, modified_from=start_text, modified_to=end_text), "alibaba.icbu.product.list")
        count += upsert_queue(conn, listing_products(listing))
        if page % 20 == 0 or page == pages:
            print(f"  page {page}/{pages}, queued_in_window={count}", flush=True)
    return count


def sync_fixed_windows(
    client: AlibabaIopClient,
    conn: sqlite3.Connection,
    start_at: dt.datetime,
    end_at: dt.datetime,
    days: int,
    delay: float,
) -> int:
    count = 0
    window_start = start_at
    while window_start <= end_at:
        window_end = min(window_start + dt.timedelta(days=days) - dt.timedelta(seconds=1), end_at)
        count += sync_window(client, conn, window_start, window_end, delay)
        window_start = window_end + dt.timedelta(seconds=1)
    return count


def parse_datetime(value: str | None, default: dt.datetime) -> dt.datetime:
    if not value:
        return default
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(f"Invalid datetime: {value}. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")


def run(args: argparse.Namespace) -> None:
    os.environ["ALIBABA_API_TIMEOUT"] = str(args.timeout)
    os.environ["ALIBABA_API_RETRIES"] = str(args.retries)
    conn = connect_db(Path(args.db))
    started = now_text()
    cursor = conn.execute(
        "INSERT INTO cert_queue_sync_runs (started_at, status) VALUES (?, 'running')",
        (started,),
    )
    run_id = cursor.lastrowid
    conn.commit()

    try:
        client = AlibabaIopClient()
        end_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        start_at = parse_datetime(args.start_at, START_AT)
        end_at = parse_datetime(args.end_at, end_at)

        api_total = 0
        if args.read_api_total:
            first = response_body(client.list_products(1), "alibaba.icbu.product.list")
            api_total = int(first.get("total_item") or 0)
            print(f"api_total={api_total}", flush=True)

        if args.chunk_days:
            queued = sync_fixed_windows(client, conn, start_at, end_at, args.chunk_days, args.delay)
        else:
            queued = sync_window(client, conn, start_at, end_at, args.delay)
        total_in_queue = conn.execute("SELECT COUNT(*) FROM cert_product_queue").fetchone()[0]
        conn.execute(
            """
            UPDATE cert_queue_sync_runs
            SET finished_at = ?, status = 'completed', api_total = ?, queued_count = ?, message = ?
            WHERE id = ?
            """,
            (now_text(), api_total, queued, f"total_in_queue={total_in_queue}", run_id),
        )
        conn.commit()
        print(f"completed queued_this_run={queued} total_in_queue={total_in_queue}", flush=True)
    except Exception as exc:
        conn.execute(
            """
            UPDATE cert_queue_sync_runs
            SET finished_at = ?, status = 'failed', message = ?
            WHERE id = ?
            """,
            (now_text(), repr(exc), run_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download all online Alibaba product IDs into the certificate queue DB.")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite DB path.")
    parser.add_argument("--delay", type=float, default=0.15, help="Seconds to wait between API page requests.")
    parser.add_argument("--timeout", type=int, default=90, help="Per-request timeout in seconds.")
    parser.add_argument("--retries", type=int, default=7, help="Retries per API request.")
    parser.add_argument("--chunk-days", type=int, default=7, help="Sync fixed date windows of this size. Use 0 for one recursive window.")
    parser.add_argument("--start-at", default="", help="Modified time start, YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--end-at", default="", help="Modified time end, YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--read-api-total", action="store_true", help="Read full API total before syncing. Slower on large accounts.")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
