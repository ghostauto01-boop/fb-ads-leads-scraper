"""
enricher.py
-----------
Website enrichment helpers for the FB Ads Leads Scraper.

Given a brand's domain it tries (best-effort, defensive) to extract:
  * emails             -> extract_emails(domain)
  * country            -> extract_country(domain, fallback)
  * instagram handle   -> get_instagram(domain)            -> (handle, followers|None)
  * store platform     -> detect_platform(domain)          -> 'shopify' | 'woocommerce' | ...

Everything is wrapped so a single failing request never kills the pipeline.
Network access is optional: if `requests` is missing or the site is down the
functions return sensible empty defaults instead of raising.
"""

from __future__ import annotations

import re
import html as _html
from typing import Optional, Tuple, List
from urllib.parse import urljoin, urlparse, urlsplit

# ---------------------------------------------------------------------------
# Optional imports
# ---------------------------------------------------------------------------
try:
    import requests
    from bs4 import BeautifulSoup
    _HAS_NET = True
except Exception:  # pragma: no cover
    requests = None
    BeautifulSoup = None
    _HAS_NET = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_TIMEOUT = 8
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

EMAIL_RE = re.compile(r"[\w\.\-]+@[\w\.\-]+\.\w{2,}")
IG_HANDLE_RE = re.compile(r"instagram\.com/(?!p/|reel/|explore/|accounts/)([A-Za-z0-9_.]+)/?", re.I)

# Emails / domains that are obviously not a real brand contact.
_EMAIL_BLACKLIST = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "@example.", "@sentry.io",
    "wixpress.com", "@squarespace", "@example.com", "yourdomain", "@cloudflare",
    "@facebook.com", "@instagram.com", "@users.noreply", "@email.com",
    "@myshopify.com", "@godaddy", "privacy@", "domainadmin", "@domainsbyproxy",
    "@yandex", "abuse@", "sample@", "test@",
)

# Known brand store platform footprints.
_SHOPIFY_HINTS = ("cdn.shopify.com", "shopify.theme", "Shopify.theme", "/cdn/shop", "shopify",
                  "myshopify.com", "shopify.checkout", "Shopify")
_WOO_HINTS = ("woocommerce", "wp-content/plugins/woocommerce", "wc-ajax", "wp-json")
_BIGCOMMERCE_HINTS = ("bigcommerce", "storefront", "bc-static")
_MAGENTO_HINTS = ("magento", "mage/cookies", "skin/frontend")


# ---------------------------------------------------------------------------
# Small in-memory cache so we don't refetch the same homepage for every ad
# ---------------------------------------------------------------------------
_FETCH_CACHE: dict = {}


class _Net:
    """Thin wrapper around requests with caching + graceful failures."""

    @staticmethod
    def get(url: str) -> Optional[str]:
        if not _HAS_NET or not url:
            return None
        if url in _FETCH_CACHE:
            return _FETCH_CACHE[url]
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200 and "text" in (resp.headers.get("content-type", "") or ""):
                text = resp.text
                _FETCH_CACHE[url] = text
                return text
        except Exception:
            pass
        _FETCH_CACHE[url] = None
        return None


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------
def normalize_domain(domain: str) -> str:
    if not domain:
        return ""
    domain = domain.strip().lower()
    domain = domain.split(":")[0]  # strip port
    if domain.startswith("www."):
        domain = domain[4:]
    domain = domain.strip("/")
    return domain


def domain_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        netloc = urlsplit(url).netloc
    except Exception:
        return ""
    return normalize_domain(netloc)


def home_url(domain: str) -> str:
    domain = normalize_domain(domain)
    if not domain:
        return ""
    if not domain.startswith(("http://", "https://")):
        return "https://" + domain
    return domain


def _fetch_page(domain: str, path: str = "") -> Optional[str]:
    base = home_url(domain)
    if not base:
        return None
    url = base + path if path else base
    return _Net.get(url)


def _get_soup(html: str):
    if not html or not BeautifulSoup:
        return None
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        try:
            return BeautifulSoup(html, "html.parser")
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------
def extract_emails(domain: str, limit: int = 3) -> List[str]:
    """Return up to `limit` real-looking emails found on homepage + /contact."""
    domain = normalize_domain(domain)
    if not domain:
        return []

    found: List[str] = []
    seen = set()

    for path in ("", "/contact", "/contact-us", "/pages/contact", "/about", "/pages/about"):
        html = _fetch_page(domain, path)
        if not html:
            continue
        # mailto: links first (highest signal)
        for m in re.findall(r'mailto:([^"?\'\s>]+)', html, re.I):
            m = _html.unescape(m).strip().lower()
            if _good_email(m) and m not in seen:
                seen.add(m)
                found.append(m)
        # raw regex fallback
        for m in EMAIL_RE.findall(html):
            m = m.lower().rstrip(".")
            if _good_email(m) and m not in seen:
                seen.add(m)
                found.append(m)
        if len(found) >= limit:
            break

    # Priority ordering: keep "real people" addresses first, then generic.
    priority = {"info": 0, "hello": 1, "contact": 2, "sales": 3, "team": 4, "support": 5}
    found.sort(key=lambda e: priority.get(e.split("@")[0], 9))
    return found[:limit]


def _good_email(email: str) -> bool:
    if "@" not in email or email.count("@") != 1:
        return False
    local, _, host = email.partition("@")
    if len(local) < 2 or "." not in host:
        return False
    if host in ("facebook.com", "instagram.com", "example.com", "sentry.io", "wixpress.com"):
        return False
    if any(b in email.lower() for b in _EMAIL_BLACKLIST):
        return False
    return True


# ---------------------------------------------------------------------------
# Country
# ---------------------------------------------------------------------------
_COUNTRY_KEYWORDS = {
    "United States": ("united states", "usa", "u.s.a", " united states "),
    "United Kingdom": ("united kingdom", "uk", "u.k.", "england", "scotland", "wales"),
    "Canada": ("canada",),
    "Australia": ("australia",),
    "Germany": ("germany", "deutschland"),
    "France": ("france"),
    "Spain": ("spain", "españa"),
    "Italy": ("italy", "italia"),
    "Netherlands": ("netherlands", "nederland"),
    "Ireland": ("ireland",),
    "Sweden": ("sweden", "sverige"),
    "India": ("india",),
    "UAE": ("united arab emirates", "dubai", "u.a.e"),
}
_TLD_COUNTRY = {
    ".us": "US", ".com": None, ".co": None, ".co.uk": "United Kingdom",
    ".uk": "United Kingdom", ".ca": "Canada", ".com.au": "Australia",
    ".au": "Australia", ".de": "Germany", ".fr": "France", ".es": "Spain",
    ".it": "Italy", ".nl": "Netherlands", ".ie": "Ireland", ".se": "Sweden",
    ".in": "India", ".ae": "UAE", ".nz": "New Zealand", ".ch": "Switzerland",
}


def extract_country(domain: str, fallback: str = "US") -> str:
    domain = normalize_domain(domain)
    html = _fetch_page(domain) if domain else None

    # 1) Try footer / contact text mentions.
    if html:
        low = html.lower()
        for country, keys in _COUNTRY_KEYWORDS.items():
            if any(k in low for k in keys):
                return country
        # "Ships to" / address-style hints.
        m = re.search(r'\b([A-Z][a-zA-Z]{2,},\s*(?:USA|United States|UK|Canada|Australia))\b', html)
        if m:
            return "United States"

    # 2) TLD guess.
    for suffix, country in sorted(_TLD_COUNTRY.items(), key=lambda kv: -len(kv[0])):
        if suffix != ".com" and domain.endswith(suffix) and country:
            return country

    return fallback or "US"


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------
def get_instagram(domain: str) -> Tuple[Optional[str], Optional[int]]:
    """Return (handle, follower_count_or_None). Follower count is best-effort."""
    domain = normalize_domain(domain)
    if not domain:
        return None, None
    html = _fetch_page(domain)
    handle = _find_ig_handle(html)
    if not handle:
        return None, None
    followers = get_instagram_followers(handle)
    return handle, followers


def _find_ig_handle(html: Optional[str]) -> Optional[str]:
    if not html:
        return None
    for m in IG_HANDLE_RE.findall(html):
        m = m.strip(" ./")
        if m and m.lower() not in ("p", "reel", "explore", "accounts", "tv", "about"):
            return m
    # og meta fallback
    m = re.search(r'<meta[^>]+(?:property|name)=["\']og:url["\'][^>]+content=["\']'
                  r'https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)', html, re.I)
    if m:
        return m.group(1)
    return None


def get_instagram_followers(handle: str) -> Optional[int]:
    """Best-effort public follower count. Instagram blocks most of this now,
    so failures silently return None (brand still kept)."""
    if not _HAS_NET or not handle:
        return None
    urls = [
        f"https://www.instagram.com/{handle}/",
        f"https://www.instagram.com/{handle}/?__a=1&__d=dis",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
            if resp.status_code != 200:
                continue
            text = resp.text
            # Graph API JSON shape
            m = re.search(r'"edge_followed_by"\s*:\s*\{\s*"count"\s*:\s*(\d+)', text)
            if m:
                return int(m.group(1))
            # og:description "1,234 Followers, ..."
            m = re.search(r'([\d.,]+[KkMm]?)\s+Followers', text)
            if m:
                return _parse_count(m.group(1))
        except Exception:
            continue
    return None


def _parse_count(token: str) -> int:
    token = token.strip().replace(",", "")
    mult = 1
    if token[-1] in "kK":
        mult, token = 1_000, token[:-1]
    elif token[-1] in "mM":
        mult, token = 1_000_000, token[:-1]
    try:
        return int(float(token) * mult)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Store platform / DTC signals
# ---------------------------------------------------------------------------
def detect_platform(domain: str) -> str:
    domain = normalize_domain(domain)
    html = _fetch_page(domain) if domain else None
    if not html:
        return "unknown"
    low = html.lower()
    if any(h.lower() in low for h in _SHOPIFY_HINTS):
        return "shopify"
    if any(h.lower() in low for h in _WOO_HINTS):
        return "woocommerce"
    if any(h.lower() in low for h in _BIGCOMMERCE_HINTS):
        return "bigcommerce"
    if any(h.lower() in low for h in _MAGENTO_HINTS):
        return "magento"
    # custom cart / any checkout
    if "add to cart" in low or "/cart" in low or "checkout" in low:
        return "custom-cart"
    return "unknown"


def has_brand_story(domain: str) -> bool:
    """Heuristic: an About page / 'our story' section usually means a real DTC brand."""
    domain = normalize_domain(domain)
    if not domain:
        return False
    for path in ("", "/about", "/pages/about", "/our-story", "/pages/our-story"):
        html = _fetch_page(domain, path)
        if not html:
            continue
        low = html.lower()
        if any(k in low for k in ("our story", "our mission", "founded in", "we believe",
                                  "about us", "started in", "based in", "made in")):
            return True
    return False


def product_count_heuristic(domain: str) -> int:
    """Rough product count from /collections or sitemap-like links. 0 == unknown."""
    domain = normalize_domain(domain)
    html = _fetch_page(domain) if domain else None
    if not html:
        return 0
    low = html.lower()
    m = re.search(r'(\d+)\s+(?:products|items|styles)', low)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    # count /products/ links on homepage
    return len(set(re.findall(r'/products/[A-Za-z0-9_\-]+', low)))
