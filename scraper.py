"""
scraper.py
----------
Scrapes the Meta (Facebook / Instagram) Ad Library using Playwright.

Public API
----------
    scrape_ads_library(url, pages=40, log=None) -> list[dict]

Each returned dict is one *ad* entry with:
    {
        "advertiser_name": str,
        "ad_text":         str,
        "links":           [str, ...],   # external (decoded) URLs found in the ad
        "country":         str | "",     # country param from the search url
        "query":           str | "",     # search keyword
    }

Playwright is imported lazily so the Flask app / `/health` keeps working even
when the Chromium browser binary has not been installed yet.
"""

from __future__ import annotations

import re
import time
import urllib.parse as up
from typing import Callable, List, Dict, Optional

LogFn = Optional[Callable[[str], None]]

DEFAULT_TIMEOUT_MS = 60_000
SCROLL_PAUSE_MS = 1100
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def parse_ad_library_url(url: str) -> Dict[str, str]:
    """Pull the useful params (q, country, active_status, ...) from the link."""
    out = {"query": "", "country": "US", "active_status": "active"}
    try:
        parsed = up.urlparse(url)
        qs = up.parse_qs(parsed.query)
        out["query"] = (qs.get("q", [""])[0] or "").strip()
        out["country"] = (qs.get("country", ["US"])[0] or "US").strip().upper() or "US"
        out["active_status"] = (qs.get("active_status", ["active"])[0] or "active")
    except Exception:
        pass
    return out


def build_url_with_query(original_url: str, new_query: str) -> str:
    """Return a copy of `original_url` with the `q=` param replaced."""
    try:
        parsed = up.urlparse(original_url)
        qs = up.parse_qs(parsed.query, keep_blank_values=True)
        qs["q"] = [new_query]
        # rebuild, preserving PHP-style bracket keys
        new_query_string = up.urlencode({k: (v[0] if isinstance(v, list) else v) for k, v in qs.items()})
        return up.urlunparse(parsed._replace(query=new_query_string))
    except Exception:
        return original_url


# ---------------------------------------------------------------------------
# Extraction JS (runs inside the page context)
# ---------------------------------------------------------------------------
_EXTRACT_JS = r"""
() => {
  const FB_HOSTS = ["facebook.com","instagram.com","whatsapp.com",
                    "messenger.com","meta.com","fb.com","google.com",
                    "googleusercontent.com","giphy.com","youtu.be",
                    "youtube.com","tiktok.com"];
  const SKIP = /^(see more|see less|learn more|shop now|order now|buy now|sign up|subscribe|details|library id|active|not active|started running|see ad details|sponsored|why am i seeing this ad|reported|created|create ad|create an ad|ad library|find out more|visit|open|apply now|download|get quote|contact us|watch now|play|pause)$/i;

  const decodeFb = (u) => {
    if (!u) return u;
    try {
      if (u.indexOf("l.facebook.com") !== -1 || u.indexOf("facebook.com/l.php") !== -1) {
        const m = u.match(/[?&]u=([^&]+)/);
        if (m) { try { return decodeURIComponent(decodeURIComponent(m[1])); } catch (e) {} }
      }
    } catch (e) {}
    return u;
  };
  const isExternal = (u) => {
    if (!u) return null;
    u = decodeFb(u);
    if (u.indexOf("mailto:") === 0 || u.indexOf("tel:") === 0) return null;
    try {
      const url = new URL(u, location.origin);
      const host = url.hostname.replace(/^www\./, "").toLowerCase();
      if (FB_HOSTS.some(d => host === d || host.endsWith("." + d))) return null;
      return url.href;
    } catch (e) { return null; }
  };

  const results = [];
  const seenCard = new Set();

  const getName = (card) => {
    const nodes = card.querySelectorAll("a[role='link'], a[href], strong, h1, h2, h3, h4, span");
    for (const n of nodes) {
      const t = (n.textContent || "").replace(/\s+/g, " ").trim();
      if (t.length < 2 || t.length > 90) continue;
      if (SKIP.test(t)) continue;
      if (/^\d+$/.test(t)) continue;
      return t;
    }
    return null;
  };
  const getText = (card) => (card.textContent || "").replace(/\s+/g, " ").trim().slice(0, 800);
  const getLinks = (card) => {
    const out = [];
    card.querySelectorAll("a[href]").forEach((a) => {
      const ex = isExternal(a.href);
      if (ex) out.push(ex);
    });
    // also scan plain text for URLs (some ads have raw links)
    const txt = card.textContent || "";
    const re = /https?:\/\/(?!l\.facebook)[^\s"'<>]+/gi;
    let m;
    while ((m = re.exec(txt))) {
      const ex = isExternal(m[0]);
      if (ex) out.push(ex);
    }
    return out;
  };

  // --- Strategy 1: ad cards (role=article) ---------------------------------
  let cards = Array.from(document.querySelectorAll("div[role='article']"));
  // Many Ad Library layouts wrap ads in role=article; if that is empty, try a
  // broader card selector based on the "Library ID" label.
  if (!cards.length) {
    cards = Array.from(document.querySelectorAll("div"));
  }
  cards.forEach((card) => {
    const txt = (card.textContent || "");
    // Only treat containers that actually look like an ad card.
    const looksLikeCard = card.querySelector("a[role='link']") !== null &&
                          (txt.indexOf("Library ID") !== -1 ||
                           txt.indexOf("Started running") !== -1 ||
                           txt.indexOf("This ad ran") !== -1 ||
                           txt.toLowerCase().indexOf("active") !== -1 ||
                           card.querySelectorAll("img").length > 0);
    if (!looksLikeCard) return;
    const name = getName(card);
    if (!name) return;
    const key = name.toLowerCase() + "|" + (card.querySelector("a[role='link']") ? "l" : "x");
    if (seenCard.has(key)) return;
    seenCard.add(key);
    results.push({ advertiser_name: name, ad_text: getText(card), links: getLinks(card) });
  });

  // --- Strategy 2: advertiser-page links (names) ---------------------------
  if (!results.length) {
    const names = new Set();
    document.querySelectorAll("a[role='link']").forEach((a) => {
      const t = (a.textContent || "").replace(/\s+/g, " ").trim();
      if (t.length >= 2 && t.length <= 80 && !SKIP.test(t) && !/^\d+$/.test(t)) {
        if (!names.has(t.toLowerCase())) {
          names.add(t.toLowerCase());
          results.push({ advertiser_name: t, ad_text: "", links: [] });
        }
      }
    });
  }
  return results;
}
"""


# ---------------------------------------------------------------------------
# Cookie / consent dismissal
# ---------------------------------------------------------------------------
def _dismiss_cookie_dialog(page) -> None:
    """Click common EU cookie-consent buttons so scrolling works."""
    candidates = [
        "Allow all cookies", "Allow essential and optional cookies",
        "Accept All", "Accept all", "Accept", "Allow cookies",
        "Decline optional cookies", "Only allow essential cookies",
        "Reject", "Reject all", "OK", "Got it", "Continue",
        "Only allow necessary cookies",
    ]
    for label in candidates:
        try:
            page.get_by_role("button", name=label, exact=False).first.click(timeout=1500)
            page.wait_for_timeout(400)
            return
        except Exception:
            continue
    # data-testid based fallbacks
    for tid in ("u_0_0", "cookie-policy-banner-accept"):
        try:
            page.locator(f'[data-testid="{tid}"]').first.click(timeout=1000)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------
def scrape_ads_library(url: str, pages: int = 40, log: LogFn = None) -> List[Dict]:
    """Scrape `pages` scroll-loads of the Ad Library search at `url`.

    Returns a de-duplicated list of ad dicts (see module docstring).
    """
    def emit(msg: str):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    if not url:
        emit("[scraper] No URL provided.")
        return []

    url_meta = parse_ad_library_url(url)
    emit(f"[scraper] Launching headless Chromium for: {url_meta['query']!r} ({url_meta['country']})")

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except Exception as e:  # pragma: no cover
        emit(f"[scraper] Playwright not available: {e}")
        return []

    raw: List[Dict] = []
    seen_ads = set()  # dedupe across scroll passes

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage", "--disable-gpu",
                    "--window-size=1366,2000",
                ],
            )
            context = browser.new_context(
                user_agent=UA,
                viewport={"width": 1366, "height": 1000},
                locale="en-US",
                timezone_id="America/New_York",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
            except PWTimeout:
                emit("[scraper] Initial load timed out, continuing with whatever loaded.")
            except Exception as e:
                emit(f"[scraper] Initial navigation error: {e}")

            _dismiss_cookie_dialog(page)
            page.wait_for_timeout(2500)

            # First extraction pass.
            new_entries = _extract_once(page, url_meta, seen_ads)
            raw.extend(new_entries)
            emit(f"[scraper] Pass 0: +{len(new_entries)} ads (total unique {len(raw)})")

            for i in range(max(1, int(pages))):
                try:
                    page.mouse.wheel(0, 9000)
                except Exception:
                    pass
                try:
                    page.evaluate(
                        "() => window.scrollTo(0, document.body.scrollHeight)"
                    )
                except Exception:
                    pass
                page.wait_for_timeout(SCROLL_PAUSE_MS)
                new_entries = _extract_once(page, url_meta, seen_ads)
                if new_entries:
                    raw.extend(new_entries)
                emit(f"[scraper] Scroll {i + 1}/{pages}: "
                     f"+{len(new_entries)} ads (total unique {len(raw)})")
                # Stop early if the feed clearly stopped growing for a while.
                if i > 8 and len(new_entries) == 0 and len(raw) == 0:
                    emit("[scraper] No ads detected after several scrolls; stopping early.")
                    break

            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        emit(f"[scraper] Fatal error: {e}")
        return raw

    emit(f"[scraper] Done. {len(raw)} unique ad entries collected.")
    return raw


def _extract_once(page, url_meta: Dict[str, str], seen_ads: set) -> List[Dict]:
    """Run the extraction JS once and return only *new* ad entries."""
    try:
        data = page.evaluate(_EXTRACT_JS)
    except Exception:
        return []

    out: List[Dict] = []
    if not isinstance(data, list):
        return out
    for item in data:
        try:
            name = (item.get("advertiser_name") or "").strip()
            if not name:
                continue
            text = (item.get("ad_text") or "").strip()
            links = [l for l in (item.get("links") or []) if isinstance(l, str)]
            key = (name.lower(), text[:120])
            if key in seen_ads:
                continue
            seen_ads.add(key)
            out.append({
                "advertiser_name": name,
                "ad_text": text,
                "links": links,
                "country": url_meta.get("country", "US"),
                "query": url_meta.get("query", ""),
            })
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import sys, json
    test_url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://web.facebook.com/ads/library/?active_status=active&ad_type=all"
        "&country=US&is_targeted_country=false&media_type=all&q=women%20fashion"
        "&search_type=keyword_unordered&sort_data[mode]=total_impressions"
        "&sort_data[direction]=desc"
    )
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    def printer(m):
        print(m, flush=True)

    rows = scrape_ads_library(test_url, pages=pages, log=printer)
    print(json.dumps(rows, indent=2)[:4000])
