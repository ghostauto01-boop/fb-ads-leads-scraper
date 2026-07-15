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

Robustness notes
----------------
* The advertiser name is read primarily from <a> links whose href contains
  "view_all_page_id=" or "advertiser_ids=" — the most stable signal in the
  Ad Library DOM. Several fallback strategies are layered on top.
* We wait for real content markers (a non-trivial body) before extracting, and
  re-extract after every scroll, keeping the union of results.
* If 0 ads are found, a diagnostic snapshot (title / url / wall keywords / body
  snippet) is emitted to the log so the cause (login wall, block, empty
  results, anti-bot) is visible.
"""

from __future__ import annotations

import os

# --- Playwright browser path (Render / ephemeral-home fix) ------------------
# On Render the default Playwright browser cache (~/.cache/ms-playwright) does
# NOT persist from build -> runtime, so Chromium goes "missing" at launch with
# "Executable doesn't exist at .../ms-playwright/...". To fix it we install the
# browsers into the PROJECT SOURCE directory (which DOES persist on Render) and
# point Playwright at that location. This MUST be set before Playwright is
# imported / launched. The matching build step in render.yaml installs to
# $RENDER_SOURCE_DIR/ms-playwright (which == this path on Render).
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", os.path.join(_REPO_DIR, "ms-playwright")
)

import re
import urllib.parse as up
from typing import Callable, List, Dict, Optional

LogFn = Optional[Callable[[str], None]]

DEFAULT_TIMEOUT_MS = 60_000
SCROLL_PAUSE_MS = 1300
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


# ---------------------------------------------------------------------------
# Extraction JS (runs inside the page context)
# ---------------------------------------------------------------------------
_EXTRACT_JS = r"""
() => {
  const FB_HOSTS = ["facebook.com","instagram.com","whatsapp.com",
                    "messenger.com","meta.com","fb.com","google.com",
                    "googleusercontent.com","giphy.com","youtu.be",
                    "youtube.com","tiktok.com","cdninstagram.com","fbcdn.net"];
  // Words that are navigation / status labels, not brand names.
  const SKIP = /^(see more|see less|learn more|shop now|order now|buy now|sign up|log in|login|subscribe|details|library id|active|not active|inactive|started running|see ad details|sponsored|why am i seeing this ad|reported|create ad|create an ad|ad library|find out more|visit|open|apply now|download|get quote|contact us|watch now|play|pause|next|previous|close|share|save|like|comment|more|all|filters|reset|search|search results|\d+)$/i;

  const decodeFb = (u) => {
    if (!u) return u;
    try {
      if (u.indexOf("l.facebook.com") !== -1 || u.indexOf("facebook.com/l.php") !== -1 || u.indexOf("lm.facebook.com") !== -1) {
        let m = u.match(/[?&]u=([^&]+)/);
        if (!m) return u;
        let val = m[1];
        for (let i = 0; i < 3; i++) {
          try { const d = decodeURIComponent(val); if (d === val) break; val = d; } catch (e) { break; }
        }
        return val;
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

  const cleanName = (t) => (t || "").replace(/\s+/g, " ").trim();
  const isGoodName = (t) => {
    if (!t) return false;
    if (t.length < 2 || t.length > 80) return false;
    if (SKIP.test(t)) return false;
    if (/^\d/.test(t) && t.replace(/\D/g, "").length > 6) return false; // id-ish
    return true;
  };
  const getText = (el) => cleanName(el && el.textContent || "");
  const getLinks = (el) => {
    const out = [];
    if (!el) return out;
    el.querySelectorAll && el.querySelectorAll("a[href]").forEach((a) => {
      const ex = isExternal(a.href);
      if (ex) out.push(ex);
    });
    const txt = (el && el.textContent) || "";
    const re = /https?:\/\/(?!l\.facebook|m\.facebook|facebook\.com\/l\.php)[^\s"'<>]+/gi;
    let m;
    while ((m = re.exec(txt))) {
      const ex = isExternal(m[0]);
      if (ex) out.push(ex);
    }
    // dedupe within card, keep order
    const seen = new Set();
    return out.filter((u) => { if (seen.has(u)) return false; seen.add(u); return true; });
  };

  const results = [];
  const seenKey = new Set();
  const add = (name, text, links, ctxEl) => {
    name = cleanName(name);
    if (!isGoodName(name)) return;
    const key = name.toLowerCase();
    if (seenKey.has(key)) {
      // merge extra links/text into the existing entry
      const ex = results.find((r) => r.advertiser_name.toLowerCase() === key);
      if (ex) {
        (links || []).forEach((l) => { if (!ex.links.includes(l)) ex.links.push(l); });
        if (!ex.ad_text && text) ex.ad_text = text.slice(0, 800);
      }
      return;
    }
    seenKey.add(key);
    results.push({
      advertiser_name: name,
      ad_text: (text || getText(ctxEl) || "").slice(0, 800),
      links: links || getLinks(ctxEl),
    });
  };

  // ---- Strategy 1 (PRIMARY): advertiser links -----------------------------
  // The Ad Library renders each advertiser name as an <a> whose href carries
  // view_all_page_id= or advertiser_ids= (or points to /ads/library with that).
  const ADV_HREF = /(view_all_page_id|advertiser_ids|page_id)=/i;
  document.querySelectorAll("a[href]").forEach((a) => {
    if (ADV_HREF.test(a.href)) {
      const name = cleanName(a.textContent);
      if (isGoodName(name)) {
        add(name, getText(a.closest && (a.closest("div") || a.parentElement)),
            getLinks(a.closest && (a.closest("div") || a.parentElement)), a);
      }
    }
  });

  // ---- Strategy 2: "Library ID" anchored cards ----------------------------
  // Each ad card contains the literal "Library ID" text. Find those and grab
  // the advertiser name (usually the topmost non-status link/text in the card).
  const idNodes = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
  while (walker.nextNode()) {
    if (/library id/i.test(walker.currentNode.nodeValue || "")) {
      idNodes.push(walker.currentNode.parentElement);
    }
    if (idNodes.length > 400) break;
  }
  const cardEls = new Set();
  idNodes.forEach((n) => {
    let el = n, hops = 0;
    while (el && hops < 6) { // climb to a card-sized container
      if (el.querySelectorAll && el.querySelectorAll("img").length > 0 && getText(el).length > 40) {
        cardEls.add(el); break;
      }
      el = el.parentElement; hops++;
    }
  });
  cardEls.forEach((card) => {
    // advertiser name: first good link text in the header area
    let name = null;
    const links = card.querySelectorAll("a[role='link'], a[href]");
    for (const a of links) {
      const t = cleanName(a.textContent);
      if (isGoodName(t)) { name = t; break; }
    }
    if (!name) {
      // fall back to first non-status span/strong
      for (const s of card.querySelectorAll("span, strong, h1, h2, h3, h4")) {
        const t = cleanName(s.textContent);
        if (isGoodName(t)) { name = t; break; }
      }
    }
    if (name) add(name, getText(card), getLinks(card), card);
  });

  // ---- Strategy 3: profile-image alt text ---------------------------------
  document.querySelectorAll("img[alt]").forEach((img) => {
    const alt = cleanName(img.getAttribute("alt") || "");
    const src = img.getAttribute("src") || "";
    if (isGoodName(alt) && /profile|page|advertiser/i.test(src) === false
        && /profile|page/i.test(alt) === false) {
      add(alt, null, null, img.closest && (img.closest("div") || img.parentElement));
    }
  });

  // ---- Strategy 4 (fallback): generic role=link names ---------------------
  if (!results.length) {
    const names = new Set();
    document.querySelectorAll("a[role='link']").forEach((a) => {
      const t = cleanName(a.textContent);
      if (isGoodName(t) && !names.has(t.toLowerCase())) {
        names.add(t.toLowerCase());
        add(t, getText(a), getLinks(a.parentElement), a);
      }
    });
  }

  return results;
}
"""

# Diagnostic snapshot emitted when extraction finds nothing — reveals whether
# the page is a login wall, an anti-bot block, genuinely empty, etc.
_DIAGNOSTIC_JS = r"""
() => {
  const body = (document.body && document.body.textContent) || "";
  const low = body.toLowerCase();
  const walls = [];
  if (/log (in|out)|sign (in|up)|continue to log|create new account/.test(low)) walls.push("login/signup");
  if (/temporarily|unusual activity|security check|verify you are|captcha|blocked|not available|something went wrong/.test(low)) walls.push("anti-bot/error");
  if (/no ads to|no results|no ad matches|didn't match/.test(low)) walls.push("no-results");
  if (/accept cookies|allow cookies|cookie/.test(low)) walls.push("cookie-consent");
  return {
    url: location.href,
    title: document.title || "",
    bodyChars: body.length,
    linkCount: document.querySelectorAll("a").length,
    articleCount: document.querySelectorAll("[role='article'], [role='link']").length,
    walls: walls,
    snippet: body.replace(/\s+/g, " ").trim().slice(0, 300),
  };
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
        "Reject", "Reject all", "OK", "Got it", "Continue", "Close",
        "Only allow necessary cookies", "Agree", "Consent",
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


def _has_content(page) -> bool:
    """True if the page has a non-trivial amount of rendered content."""
    try:
        info = page.evaluate(
            "() => ({len: document.body ? document.body.innerText.length : 0,"
            " links: document.querySelectorAll('a').length})"
        )
        return info.get("len", 0) > 500 or info.get("links", 0) > 10
    except Exception:
        return False


def _dump_diagnostics(page, emit) -> None:
    try:
        diag = page.evaluate(_DIAGNOSTIC_JS)
        emit("[scraper] DIAGNOSTIC (0 ads found):")
        emit(f"[scraper]   url        : {diag.get('url')}")
        emit(f"[scraper]   title      : {diag.get('title')}")
        emit(f"[scraper]   body chars : {diag.get('bodyChars')} | "
             f"links: {diag.get('linkCount')} | roles: {diag.get('articleCount')}")
        emit(f"[scraper]   walls      : {', '.join(diag.get('walls') or []) or 'none detected'}")
        emit(f"[scraper]   snippet    : {(diag.get('snippet') or '')[:300]}")
        walls = diag.get("walls") or []
        if "login/signup" in walls:
            emit("[scraper] NOTE: page looks like a login wall. The Ad Library is "
                 "public, but Facebook sometimes forces login for headless browsers. "
                 "Retrying / lowering scroll may help; if persistent, the region/IP may be flagged.")
        if "anti-bot/error" in walls:
            emit("[scraper] NOTE: page looks like an anti-bot / error wall.")
        if "cookie-consent" in walls:
            emit("[scraper] NOTE: cookie consent may still be blocking content.")
    except Exception as e:
        emit(f"[scraper] (diagnostic unavailable: {e})")


# ---------------------------------------------------------------------------
# GraphQL response interception (PRIMARY data source)
# ---------------------------------------------------------------------------
# The Ad Library page loads its ads from /api/graphql/ as structured JSON. That
# JSON is far more reliable to parse than the DOM, so we intercept those
# responses and pull advertiser entries straight out of them. DOM extraction
# remains as a fallback.
_FB_HOST_RE = re.compile(
    r"(?:^|\.)(facebook\.com|instagram\.com|whatsapp\.com|messenger\.com|"
    r"meta\.com|fb\.com|google\.com|googleusercontent\.com|giphy\.com|"
    r"youtu\.be|youtube\.com|tiktok\.com|cdninstagram\.com|fbcdn\.net)$",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def _extract_urls_from_node(node) -> List[str]:
    """Recursively collect all http(s) URLs found in any string field of a node."""
    urls: List[str] = []

    def walk(n):
        if isinstance(n, dict):
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
        elif isinstance(n, str):
            for m in _URL_RE.findall(n):
                mm = m
                low = mm.lower()
                if ("l.facebook.com" in low or "facebook.com/l.php" in low
                        or "lm.facebook.com" in low):
                    q = re.search(r"[?&]u=([^&]+)", mm)
                    if q:
                        try:
                            from urllib.parse import unquote
                            mm = unquote(q.group(1))
                        except Exception:
                            pass
                urls.append(mm)
    walk(node)
    return urls


def _filter_external_urls(urls: List[str]) -> List[str]:
    """Keep only real brand (non-Facebook/IG/social) URLs, deduped."""
    out = []
    seen = set()
    for u in urls:
        try:
            host = up.urlparse(u).netloc.lower().replace("www.", "")
            if not host:
                continue
            if _FB_HOST_RE.search(host):
                continue
        except Exception:
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _parse_graphql_ads(payload, query: str = "", country: str = "") -> List[Dict]:
    """Recursively walk a GraphQL JSON payload and extract advertiser entries.

    Identifies ad nodes by the presence of a `page_name` (the advertiser) field,
    accumulates `ad_creative_bodies`, and harvests external brand URLs from all
    string fields. Robust to schema variation since it walks everything.
    """
    results: List[Dict] = []
    seen: set = set()

    def is_ad_node(obj: dict) -> bool:
        if not isinstance(obj, dict):
            return False
        return (
            (isinstance(obj.get("page_name"), str) and obj.get("page_name").strip())
            or (obj.get("ad_archive_id") and obj.get("collation_id"))
        )

    def node_name(obj: dict) -> str:
        for key in ("page_name", "advertiser_name", "collation_name", "brand_name"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def walk(n):
        if isinstance(n, dict):
            if is_ad_node(n):
                name = node_name(n)
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    body = ""
                    b = n.get("ad_creative_bodies")
                    if isinstance(b, list):
                        body = " ".join(str(x) for x in b[:5])
                    elif isinstance(b, str):
                        body = b
                    if not body:
                        b2 = n.get("ad_creative_link_descriptions") or n.get("ad_creative_link_titles")
                        if isinstance(b2, list):
                            body = " ".join(str(x) for x in b2[:5])
                    low = name.lower()
                    if low in ("facebook", "instagram", "meta", "ad library"):
                        return
                    urls = _filter_external_urls(_extract_urls_from_node(n))
                    results.append({
                        "advertiser_name": name,
                        "ad_text": (body or "").strip()[:800],
                        "links": urls,
                        "country": country,
                        "query": query,
                    })
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    try:
        walk(payload)
    except Exception:
        pass
    return results


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

    # Surface the resolved browser path in the log — confirms the Render fix.
    emit(f"[scraper] Playwright browsers path: "
         f"{os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '<default ~/.cache/ms-playwright>')}")

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
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--window-size=1366,2000",
                ],
            )
            context = browser.new_context(
                user_agent=UA,
                viewport={"width": 1366, "height": 1000},
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            # Stealth: hide the webdriver flag and make us look like a real browser.
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
                "window.chrome = window.chrome || {runtime:{}};"
            )
            page = context.new_page()

            # --- Intercept Facebook's GraphQL ad-data responses (PRIMARY) ----
            # The Ad Library loads ads from /api/graphql/ as structured JSON.
            # Capturing it is far more reliable than DOM scraping, so we do it
            # first and keep the DOM extraction as a fallback.
            gql_ads: List[Dict] = []
            gql_seen: set = set()

            def _on_response(response):
                try:
                    rurl = response.url or ""
                    if "graphql" not in rurl:
                        return
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    parsed = _parse_graphql_ads(
                        payload, query=url_meta.get("query", ""),
                        country=url_meta.get("country", "US"),
                    )
                    for ad in parsed:
                        key = ad["advertiser_name"].lower()
                        if key in gql_seen:
                            continue
                        gql_seen.add(key)
                        gql_ads.append(ad)
                except Exception:
                    pass

            page.on("response", _on_response)

            # --- Navigate: try networkidle first, fall back to domcontentloaded.
            loaded = False
            for wait_strat in ("networkidle", "domcontentloaded"):
                try:
                    page.goto(url, wait_until=wait_strat, timeout=DEFAULT_TIMEOUT_MS)
                    loaded = True
                    break
                except PWTimeout:
                    emit(f"[scraper] '{wait_strat}' timed out — trying next strategy.")
                except Exception as e:
                    emit(f"[scraper] navigation error ({wait_strat}): {e}")
            if not loaded:
                emit("[scraper] Could not fully load the page; attempting extraction anyway.")

            _dismiss_cookie_dialog(page)
            # Give the SPA time to render ad cards.
            page.wait_for_timeout(3000)

            # Wait (briefly) for real content to appear before scrolling.
            try:
                page.wait_for_function(
                    "() => (document.body && document.body.innerText.length > 500) "
                    "|| document.querySelectorAll('a').length > 15",
                    timeout=20_000,
                )
                emit("[scraper] Content detected, beginning extraction.")
            except Exception:
                emit("[scraper] Content marker not reached within 20s — extracting what's present.")

            # First extraction pass.
            new_entries = _extract_once(page, url_meta, seen_ads)
            raw.extend(new_entries)
            emit(f"[scraper] Pass 0: +{len(new_entries)} ads (total unique {len(raw)})")

            stable_rounds = 0
            for i in range(max(1, int(pages))):
                try:
                    page.mouse.wheel(0, 9000)
                except Exception:
                    pass
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(SCROLL_PAUSE_MS)
                new_entries = _extract_once(page, url_meta, seen_ads)
                if new_entries:
                    raw.extend(new_entries)
                    stable_rounds = 0
                else:
                    stable_rounds += 1
                emit(f"[scraper] Scroll {i + 1}/{pages}: "
                     f"+{len(new_entries)} ads (total unique {len(raw)})")
                # Stop early when the feed has clearly ended (no growth for a while).
                if stable_rounds >= 6:
                    emit("[scraper] Feed stopped growing — finishing early.")
                    break
                if i > 8 and len(raw) == 0 and stable_rounds >= 3:
                    emit("[scraper] No ads after several scrolls — stopping early.")
                    break

            # Diagnostics if we got nothing.
            if len(raw) == 0 and len(gql_ads) == 0:
                emit("[scraper] WARNING: 0 ads extracted (DOM + GraphQL). Dumping diagnostics…")
                _dump_diagnostics(page, emit)

            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        emit(f"[scraper] Fatal error: {e}")
        return raw

    # --- Merge GraphQL-captured ads with the DOM-extracted ones. -----------
    # GraphQL is the primary source; DOM extraction is the fallback. Dedupe by
    # advertiser name and union their links.
    by_name: Dict[str, Dict] = {}
    for ad in raw + gql_ads:
        name = (ad.get("advertiser_name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in by_name:
            by_name[key] = {
                "advertiser_name": name,
                "ad_text": ad.get("ad_text", ""),
                "links": [],
                "country": ad.get("country", url_meta.get("country", "US")),
                "query": ad.get("query", url_meta.get("query", "")),
            }
        elif not by_name[key]["ad_text"] and ad.get("ad_text"):
            by_name[key]["ad_text"] = ad["ad_text"]
        for l in ad.get("links", []) or []:
            if isinstance(l, str) and l not in by_name[key]["links"]:
                by_name[key]["links"].append(l)
    raw = list(by_name.values())

    emit(f"[scraper] GraphQL captured {len(gql_ads)} advertisers; "
         f"final unique total {len(raw)}.")
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
