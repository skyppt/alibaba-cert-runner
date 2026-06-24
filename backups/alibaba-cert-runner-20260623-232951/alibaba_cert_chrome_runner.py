#!/usr/bin/env python3
"""Use the user's current Chrome tab to link certificates for queued products."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "logs" / "alibaba_cert_runner_506068.sqlite3"
EDIT_URL_TEMPLATE = "https://post.alibaba.com/product/publish.htm?spm=a2747.product_manager.0.0.70ae71d272fQuG&itemId={item_id}"


def now_text() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def edit_url_for_item_id(template: str, item_id: str) -> str:
    if "{item_id}" in template:
        return template.format(item_id=item_id)
    parsed = urlparse(template)
    query = parse_qs(parsed.query)
    query["itemId"] = [item_id]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def item_id_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query).get("itemId", [""])[0]


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
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
    conn.commit()
    return conn


def mark(conn: sqlite3.Connection, item_id: str, status: str, message: str, url: str) -> None:
    timestamp = now_text()
    conn.execute(
        """
        INSERT INTO product_runs (item_id, status, message, url, attempts, first_seen_at, updated_at)
        VALUES (?, ?, ?, ?, CASE WHEN ? = 'processing' THEN 1 ELSE 0 END, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            status = excluded.status,
            message = excluded.message,
            url = excluded.url,
            attempts = product_runs.attempts + CASE WHEN excluded.status = 'processing' THEN 1 ELSE 0 END,
            updated_at = excluded.updated_at
        """,
        (item_id, status, message, url, status, timestamp, timestamp),
    )
    queue_status = "done" if status == "success" else "skipped" if status == "skipped" else "failed" if status == "failed" else "pending"
    if status in {"success", "failed", "skipped"}:
        conn.execute(
            "UPDATE cert_product_queue SET queue_status = ?, updated_at = ? WHERE item_id = ?",
            (queue_status, timestamp, item_id),
        )
    conn.commit()


def pending_ids(conn: sqlite3.Connection, limit: int, retry_failed: bool) -> list[str]:
    statuses = ("pending", "failed") if retry_failed else ("pending",)
    placeholders = ",".join("?" for _ in statuses)
    sql = f"""
        SELECT q.item_id
        FROM cert_product_queue q
        LEFT JOIN product_runs r ON r.item_id = q.item_id
        WHERE q.queue_status IN ({placeholders})
          AND COALESCE(r.status, '') != 'success'
        ORDER BY q.updated_at, q.item_id
    """
    if limit:
        sql += " LIMIT ?"
        params: tuple[object, ...] = (*statuses, limit)
    else:
        params = statuses
    return [row[0] for row in conn.execute(sql, params).fetchall()]


def chrome_process_product(url: str, publish: bool, timeout: int) -> dict:
    expected_item_id = item_id_from_url(url)
    script = f"""
    const chrome = Application('Google Chrome');
    function sleep(ms) {{ delay(ms / 1000); }}
    const tabs = chrome.windows[0].tabs();
    let tab = null;
    for (let i = 0; i < tabs.length; i++) {{
      const candidate = tabs[i];
      const candidateUrl = String(candidate.url());
      if (candidateUrl.includes('post.alibaba.com/product/publish.htm') || candidateUrl.includes('post.alibaba.com/product/success.htm')) {{
        tab = candidate;
        break;
      }}
    }}
    if (!tab) {{
      tab = chrome.windows[0].activeTab();
    }}
    function tabUrl() {{ return String(tab.url()); }}
    function assertAlibabaTab() {{
      const current = tabUrl();
      if (
        current
        && !current.includes('post.alibaba.com/')
        && !current.includes('hz-productposting.alibaba.com/')
        && !current.includes('login')
        && current !== 'about:blank'
      ) {{
        throw new Error('browser tab left Alibaba edit flow: ' + current);
      }}
    }}
    function run(js) {{
      assertAlibabaTab();
      return String(chrome.execute(tab, {{ javascript: js }}));
    }}
    function waitFor(predicate, timeoutMs) {{
      const start = Date.now();
      let last = null;
      while (Date.now() - start < timeoutMs) {{
        try {{
          last = JSON.parse(run(`JSON.stringify((${{predicate}})())`));
          if (last && last.ok) return last;
        }} catch (e) {{
          last = {{ ok: false, error: String(e) }};
        }}
        sleep(1000);
      }}
      throw new Error('timeout waiting: ' + JSON.stringify(last));
    }}
    function pageText() {{ return run('document.body ? document.body.innerText : ""'); }}
    function pageHref() {{ return run('location.href'); }}
    function waitDocumentComplete(timeoutMs) {{
      return waitFor(`() => {{
        return {{ ok: document.readyState === 'complete', href: location.href, ready: document.readyState }};
      }}`, timeoutMs);
    }}

    let finalResult = null;
    try {{
      tab.url = String({json.dumps(url)});
      waitDocumentComplete(120000);
      sleep(10000);
      waitFor(`() => {{
        const text = document.body ? document.body.innerText : '';
        const expected = {json.dumps(expected_item_id)};
        return {{
          ok: document.readyState === 'complete'
            && (location.href.includes('itemId=' + expected) || location.href.includes('login'))
            && ((text.includes('关联商品证书') && text.includes('提交')) || location.href.includes('login')),
          href: location.href,
          text: text.slice(0, 200)
        }};
      }}`, 120000);
      sleep(3000);

      const state = JSON.parse(run(`JSON.stringify((() => {{
        const text = document.body ? document.body.innerText : '';
        const blockStart = text.indexOf('关联商品证书');
        const block = blockStart >= 0 ? text.slice(blockStart, blockStart + 900) : '';
        return {{
          href: location.href,
          login: location.href.includes('login'),
          hasCertBlock: blockStart >= 0,
          hasCerts: block.includes('CE') && block.includes('UL 508')
        }};
      }})())`));

      if (state.login) {{
        finalResult = {{ status: 'failed', message: 'login required', url: state.href }};
      }} else if (!state.hasCertBlock) {{
        finalResult = {{ status: 'skipped', message: 'certificate block not found; likely non-structured product', url: state.href }};
      }} else {{
        const paymentUpdated = JSON.parse(run(`JSON.stringify((() => {{
          const slash = String.fromCharCode(92);
          const desired = 'Paypal' + slash + 'TT' + slash + 'Western Union' + slash + 'Trade Assurance';
          const inputs = [...document.querySelectorAll('input, textarea')];
          const idx = inputs.findIndex(el => (el.value || '').trim() === 'Payment');
          if (idx < 0) return {{ ok: false, message: 'Payment title input not found' }};
          const valueInput = inputs[idx + 1];
          if (!valueInput) return {{ ok: false, message: 'Payment value input not found' }};
          valueInput.scrollIntoView({{ block: 'center' }});
          valueInput.focus();
          const proto = valueInput.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
          setter.call(valueInput, desired);
          valueInput.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: desired }}));
          valueInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
          valueInput.blur();
          return {{ ok: true, value: valueInput.value }};
        }})())`));
        if (!paymentUpdated.ok) {{
          finalResult = {{ status: 'skipped', message: paymentUpdated.message, url: pageHref() }};
        }}
      }}

      if (!finalResult && !state.hasCerts) {{
        const opened = JSON.parse(run(`JSON.stringify((() => {{
          const buttons = [...document.querySelectorAll('button')].filter(el => (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
          const target = buttons.find(el => (el.innerText || el.textContent || '').includes('添加关联证书'));
          if (!target) return {{ ok: false, message: 'add certificate button not found; likely non-structured product' }};
          target.scrollIntoView({{ block: 'center' }});
          window.scrollBy(0, -80);
          target.click();
          return {{ ok: true }};
        }})())`));

        if (!opened.ok) {{
          finalResult = {{ status: 'skipped', message: opened.message, url: pageHref() }};
        }} else {{
          sleep(2000);
          waitFor(`() => {{
            const text = document.body ? document.body.innerText : '';
            return {{ ok: text.includes('选择已验真过的证书') || text.includes('还没有符合当前商品所在类目的产品证书'), text: text.slice(-700) }};
          }}`, 90000);
          sleep(2000);

          const dialogText = pageText();
          if (dialogText.includes('还没有符合当前商品所在类目的产品证书')) {{
            finalResult = {{ status: 'skipped', message: 'no available certificate for this product/category', url: pageHref() }};
          }} else {{
            const selected = JSON.parse(run(`JSON.stringify((() => {{
              const dialog = [...document.querySelectorAll('[role="dialog"], .next-dialog, .next-overlay-wrapper')].find(el => (el.innerText || '').includes('选择已验真过的证书')) || document.body;
              const inputs = [...dialog.querySelectorAll('input.next-checkbox-input[role="checkbox"]')].filter(el => {{
                const rowText = (el.closest('.next-table-row') || el.closest('tr') || el.parentElement || dialog).innerText || '';
                return rowText.includes('CE') || rowText.includes('UL 508');
              }});
              if (inputs.length < 2) return {{ ok: false, message: 'CE/UL checkbox inputs not found' }};
              inputs.slice(0, 2).forEach(el => {{
                if (el.checked) return;
                el.scrollIntoView({{ block: 'center' }});
                el.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true, cancelable: true, view: window }}));
                el.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true, cancelable: true, view: window }}));
                el.click();
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
              }});
              return {{ ok: true, count: inputs.length, checked: inputs.slice(0, 2).filter(el => el.checked).length }};
            }})())`));

            if (!selected.ok) {{
              finalResult = {{ status: 'skipped', message: selected.message, url: pageHref() }};
            }} else {{
              sleep(2000);
              waitFor(`() => {{
                const text = document.body ? document.body.innerText : '';
                return {{ ok: text.includes('选择已验真过的证书') && text.includes('确认(2)'), text: text.slice(-1000) }};
              }}`, 30000);
              const confirmed = JSON.parse(run(`JSON.stringify((() => {{
                const buttons = [...document.querySelectorAll('button')].filter(el => (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                const btn = buttons.find(el => (el.innerText || el.textContent || '').includes('确认(2)'));
                if (!btn) return {{ ok: false, message: 'confirm(2) button not found; two certificates were not selected' }};
                btn.click();
                return {{ ok: true, text: btn.innerText || btn.textContent || '' }};
              }})())`));

              if (!confirmed.ok) {{
                finalResult = {{ status: 'skipped', message: confirmed.message, url: pageHref() }};
              }} else {{
                sleep(5000);
                try {{
                  waitFor(`() => {{
                    const text = document.body ? document.body.innerText : '';
                    const blockStart = text.indexOf('关联商品证书');
                    const block = blockStart >= 0 ? text.slice(blockStart, blockStart + 900) : '';
                    return {{ ok: block.includes('CE') && block.includes('UL 508'), text: block }};
                  }}`, 90000);
                }} catch (e) {{
                  finalResult = {{ status: 'skipped', message: 'certificates not visible after confirm; likely non-structured product', url: pageHref() }};
                }}
              }}
            }}
          }}
        }}
      }}

      if (!finalResult && {str(publish).lower()}) {{
        sleep(5000);
        run(`(() => {{
          const text = document.body ? document.body.innerText : '';
          const section = [...document.querySelectorAll('.sell-card, div')]
            .find(el => (el.innerText || '').includes('物流信息') && (el.innerText || '').includes('物流提供方式'));
          if (section) {{
            section.scrollIntoView({{ block: 'center' }});
          }} else {{
            window.scrollTo(0, Math.floor(document.body.scrollHeight * 0.55));
          }}
        }})()`);
        sleep(2000);
        const logisticsSelected = JSON.parse(run(`JSON.stringify((() => {{
          const pageText = document.body ? document.body.innerText : '';
          const needsLogisticsProvider = pageText.includes('物流提供方式') || pageText.includes('错误项物流提供方式');
          if (!needsLogisticsProvider) {{
            return {{ ok: true, message: 'logistics provider section not present; skipping smart freight selection' }};
          }}
          const logisticsSection = [...document.querySelectorAll('.sell-card, div')]
            .find(el => (el.innerText || '').includes('物流提供方式') && (el.innerText || '').includes('智能运费模板'));
          if (logisticsSection) logisticsSection.scrollIntoView({{ block: 'center' }});
          const card = document.querySelector('.LogisticsDeliveryMethod-container-content-list-office');
          if (!card) return {{ ok: false, message: 'smart freight template card not found' }};
          card.scrollIntoView({{ block: 'center', inline: 'center' }});
          const clickable = card.querySelector('.LogisticsDeliveryMethod-container-content-list-office-title, .LogisticsDeliveryMethod-select') || card;
          const r = clickable.getBoundingClientRect();
          const target = document.elementFromPoint(r.left + Math.min(r.width / 2, 120), r.top + r.height / 2) || clickable;
          for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
            target.dispatchEvent(new MouseEvent(type, {{
              bubbles: true,
              cancelable: true,
              view: window,
              clientX: r.left + Math.min(r.width / 2, 120),
              clientY: r.top + r.height / 2
            }}));
          }}
          return {{
            ok: true,
            message: card.className
          }};
        }})())`));
        if (!logisticsSelected.ok) {{
          throw new Error('logistics provider not selected: ' + logisticsSelected.message);
        }}
        sleep(1000);
        const submitted = JSON.parse(run(`JSON.stringify((() => {{
          const agreements = [...document.querySelectorAll('input[type="checkbox"]')];
          const agreement = agreements.find(box => (box.closest('label') || box.parentElement || document.body).innerText.includes('合法和有效'));
          if (agreement && !agreement.checked) {{
            const label = agreement.closest('label') || agreement.parentElement;
            if (label) label.click(); else agreement.click();
          }}
          const buttons = [...document.querySelectorAll('button')].filter(el => (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
          const btn = buttons.reverse().find(el => (el.innerText || el.textContent || '').trim() === '提交');
          if (!btn) return {{ ok: false, message: 'submit button not found' }};
          btn.scrollIntoView({{ block: 'center' }});
          window.scrollBy(0, -80);
          btn.click();
          return {{ ok: true }};
        }})())`));
        if (!submitted.ok) throw new Error(submitted.message);
        sleep(10000);
        const warningHandled = JSON.parse(run(`JSON.stringify((() => {{
          const buttons = [...document.querySelectorAll('button')].filter(el => (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
          const btn = buttons.reverse().find(el => (el.innerText || el.textContent || '').trim() === '继续提交');
          if (btn) {{
            btn.click();
            return {{ ok: true, clicked: true }};
          }}
          return {{ ok: true, clicked: false }};
        }})())`));
        if (warningHandled.clicked) sleep(10000);
        const done = waitFor(`() => {{
          const text = document.body ? document.body.innerText : '';
          const expected = {json.dumps(expected_item_id)};
          const href = location.href;
          return {{
            ok: href.includes('/product/success.htm') && href.includes('primaryId=' + expected) && (href.includes('isSuccess=true') || text.includes('成功')),
            href,
            text: text.slice(0, 500)
          }};
        }}`, 180000);
        finalResult = {{ status: 'success', message: 'submit success page confirmed', url: done.href }};
      }} else if (!finalResult) {{
        finalResult = {{ status: 'dry_run', message: 'certificates linked; not submitted', url: pageHref() }};
      }}
    }} catch (e) {{
      finalResult = {{ status: 'failed', message: String(e), url: {json.dumps(url)} }};
    }}
    console.log('RESULT_JSON:' + JSON.stringify(finalResult));
    "ok"
    """
    with tempfile.NamedTemporaryFile("w", suffix=".jxa", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", script_path],
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    combined_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    result_match = re.search(r"RESULT_JSON:(\{.*\})", combined_output)
    text = result_match.group(1) if result_match else ""
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    if not text and result.stderr:
        return {
            "status": "skipped",
            "message": f"chrome automation did not return a usable page result: {result.stderr.strip()[:300]}",
            "url": url,
        }
    if not text:
        text = "{}"
    try:
        parsed = json.loads(text)
        if parsed:
            return parsed
        message = result.stderr.strip() or "empty osascript result"
        return {"status": "skipped", "message": message, "url": url}
    except json.JSONDecodeError:
        if result.returncode != 0:
            return {"status": "skipped", "message": result.stderr.strip() or result.stdout.strip(), "url": url}
        return {"status": "skipped", "message": text[:500], "url": url}


def run(args: argparse.Namespace) -> None:
    conn = connect_db(Path(args.db))
    ids = pending_ids(conn, args.limit, args.retry_failed)
    print(f"Loaded {len(ids)} queued product IDs.", flush=True)
    consecutive_failures = 0
    for index, item_id in enumerate(ids, start=1):
        url = edit_url_for_item_id(args.edit_url_template, item_id)
        mark(conn, item_id, "processing", "started", url)
        print(f"{index}/{len(ids)} [start] {item_id}", flush=True)
        result = chrome_process_product(url, args.publish, args.product_timeout)
        status = result.get("status", "failed")
        message = result.get("message", "")
        final_url = result.get("url", url)
        mark(conn, item_id, status, message, final_url)
        print(f"{index}/{len(ids)} [{status}] {item_id}: {message}", flush=True)
        if status == "failed":
            consecutive_failures += 1
        elif status == "success":
            consecutive_failures = 0
        if args.single_current:
            break
        if args.stop_after_success and status == "success":
            break
        if args.max_consecutive_failures and consecutive_failures >= args.max_consecutive_failures:
            print(
                f"Stopped after {consecutive_failures} consecutive failures. Please inspect the latest failed product.",
                flush=True,
            )
            break
        if args.sleep_between:
            time.sleep(args.sleep_between)
    conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process Alibaba certificate queue in the user's current Chrome tab.")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite DB path.")
    parser.add_argument("--edit-url-template", default=EDIT_URL_TEMPLATE, help="Edit URL template with {item_id}.")
    parser.add_argument("--limit", type=int, default=1, help="Maximum products to process. Use 0 for all pending IDs.")
    parser.add_argument("--publish", action="store_true", help="Click Submit after linking certificates.")
    parser.add_argument("--retry-failed", action="store_true", help="Retry queue rows previously marked failed.")
    parser.add_argument("--stop-after-success", action="store_true", help="Stop the batch immediately after the first confirmed success.")
    parser.add_argument("--single-current", action="store_true", help="Process exactly one queued product and stop, even if it is skipped or failed.")
    parser.add_argument("--product-timeout", type=int, default=360, help="Seconds before giving up on one product.")
    parser.add_argument("--max-consecutive-failures", type=int, default=3, help="Stop after this many consecutive failed products. Use 0 to disable.")
    parser.add_argument("--sleep-between", type=float, default=2.0, help="Seconds to pause between products.")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
