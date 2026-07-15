"""
discovery.py
------------
Brand-discovery agent logic.

Takes the *raw* ad entries produced by `scraper.scrape_ads_library` and turns
them into a clean, de-duplicated, scored, sorted table of real DTC brands:

    discover_brands(raw_ads, target, original_url, log=None, scraper_fn=None)

It performs:
  1. Domain extraction (own domain only, never facebook/instagram/marketplaces)
  2. Hard exclusion filters  (agencies, wholesalers, dropshippers, amazon-only,
     affiliate, large public corps, marketplaces/resellers)
  3. Enrichment              (emails, country, category, IG handle & followers,
     store platform, brand-story check)
  4. Scoring & activity level
  5. De-dup by domain
  6. Related-keyword expansion (if count < target and a scraper_fn is supplied)
  7. Final sort: High > Medium > Low, then active_ads_count desc, then score desc
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import enricher

LogFn = Optional[Callable[[str], None]]

# ---------------------------------------------------------------------------
# Keyword lists for hard filters
# ---------------------------------------------------------------------------
AGENCY_KEYWORDS = [
    "agency", "marketing agency", "growth agency", "we help brands",
    "we scale", "media buyer", "performance marketing", "ads management",
    "consulting", "consultancy", "creative agency", "ad agency",
    "digital agency", "advertising agency", "full-service", "we help you",
    "client results", "managed ads", "shopify agency",
]
AGENCY_URL_KEYWORDS = ["/services", "/marketing", "/agency", "/what-we-do"]

WHOLESALE_KEYWORDS = [
    "wholesale", "bulk orders", "bulk order", "distributor", "b2b",
    "become a retailer", "trade account", "minimum order", "case quantity",
    "reseller program", "for retailers", "stockists",
]
MARKETPLACE_KEYWORDS = [
    "marketplace", "multi-brand", "curates brands", "curated brands",
    "we curate", "collection of brands", "shop brands", "thousands of brands",
]
DROPSHIP_KEYWORDS = [
    "aliexpress", "ali express", "dropship", "drop ship", "dropshipping",
    "ships in 2-4 weeks", "shipping 15-30 days", "worldwide free shipping",
    "processing time 3-7", "from our overseas warehouse",
]
AFFILIATE_KEYWORDS = [
    "affiliate", "coupon", "cashback", "deals", "promo codes", "discount codes",
    "best deals", "compare prices", "review site", "we earn a commission",
]
LARGE_CORP_KEYWORDS = [
    "fortune 500", "inc. 500", "nyse:", "nasdaq:", "publicly traded",
    "investor relations", "press release", "annual report", "subsidiary of",
    "a division of", "part of the group", "shareholder",
]
# Known big public / marketplace domains to exclude outright.
HARD_BLOCK_DOMAINS = {
    "amazon.com", "amazon.co.uk", "amazon.de", "amazon.ca", "etsy.com",
    "ebay.com", "walmart.com", "aliexpress.com", "alibaba.com",
    "wish.com", "temu.com", "shein.com", "dhgate.com", "banggood.com",
    "target.com", "bestbuy.com", "wayfair.com", "overstock.com",
    "rakuten.com", "honey.com", "retailmenot.com", "groupon.com",
    "trustpilot.com", "wikipedia.org", "pinterest.com", "tiktok.com",
    "youtube.com", "google.com", "linkedin.com", "twitter.com", "x.com",
    "reddit.com", "apple.com", "microsoft.com", "meta.com", "facebook.com",
    "instagram.com", "youtu.be",
}
PLATFORM_DOMAINS = {"shopify.com", "myshopify.com", "bigcartel.com", "wix.com",
                    "squarespace.com", "godaddy.com"}

# ---------------------------------------------------------------------------
# Category keyword mapping (no LLM)
# ---------------------------------------------------------------------------
CATEGORY_MAP = [
    ("Women Fashion", ["women", "ladies", "dress", "blouse", "skirt", "women's",
                       "womens", "female", "boutique", "lingerie", "swimwear",
                       "swimsuit", "bikini", "leggings", "yoga pants", "heels"]),
    ("Men Fashion", ["men", "mens", "menswear", "suit", "tie", "polo shirt",
                     "boxer", "razor", "beard", "shave"]),
    ("Beauty", ["beauty", "skincare", "skin care", "makeup", "cosmetic",
                "serum", "moisturizer", "cream", "lotion", "mascara", "lipstick",
                "foundation", "cleanser", "shampoo", "conditioner", "hair care",
                "nail", "fragrance", "perfume"]),
    ("Fitness", ["fitness", "gym", "workout", "protein", "supplement", "pre-workout",
                 "creatine", "yoga", "athletic", "sportswear", "activewear",
                 "resistance", "dumbbell", "training"]),
    ("Home", ["home", "furniture", "decor", "kitchen", "bedding", "lamp", "rug",
              "cushion", "storage", "cookware", "appliance", "cleaning"]),
    ("Jewelry", ["jewelry", "jewellery", "ring", "necklace", "earring", "bracelet",
                 "pendant", "diamond", "gold", "silver"]),
    ("Pets", ["pet", "dog", "cat", "puppy", "animal", "kitten", "leash", "kibble"]),
    ("Food & Drink", ["food", "snack", "coffee", "tea", "protein bar", "nutrition",
                      "keto", "vegan", "sauce", "spice", "chocolate", "drink"]),
    ("Health", ["health", "wellness", "vitamin", "probiotic", "magnesium",
                "cbd", "sleep", "immune", "supplement", "personal care"]),
    ("Tech & Gadgets", ["gadget", "electronic", "phone", "charger", "cable",
                        "headphone", "earbud", "speaker", "smart", "app-enabled",
                        "accessory", "device"]),
    ("Baby & Kids", ["baby", "kid", "child", "toddler", "infant", "newborn",
                     "toy", "nursery", "stroller", "diaper"]),
    ("Accessories", ["accessories", "bag", "handbag", "wallet", "watch", "sunglass",
                     "hat", "scarf", "belt"]),
]


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
def _noop(msg: str):
    pass


# ---------------------------------------------------------------------------
# Domain / DTC checks
# ---------------------------------------------------------------------------
def _decode_fb_redirect(url: str) -> str:
    """Resolve `https://l.facebook.com/l.php?u=<real url>` / `fbclid` style links
    to the destination URL so we can extract the real brand domain."""
    try:
        from urllib.parse import unquote, urlparse, parse_qs
        low = (url or "").lower()
        if "l.facebook.com" in low or "facebook.com/l.php" in low:
            qs = parse_qs(urlparse(url).query)
            u = qs.get("u", [None])[0]
            if u:
                # the value can be multi-encoded — decode until stable
                for _ in range(3):
                    nu = unquote(u)
                    if nu == u:
                        break
                    u = nu
                return u
    except Exception:
        pass
    return url


def extract_domain(ad: dict) -> str:
    """Best-effort: take the most 'brand-like' external link from the ad."""
    candidates = ad.get("links") or []
    for raw_url in candidates:
        url = _decode_fb_redirect(raw_url)
        d = enricher.domain_from_url(url)
        if d and not _is_blocked_domain(d):
            return d
    # If no link, derive from advertiser name as a guess: brandname.com
    name = (ad.get("advertiser_name") or "").strip()
    if name:
        slug = re.sub(r"[^A-Za-z0-9]", "", name.lower())
        slug = slug.replace("official", "")
        if slug:
            return f"{slug}.com"  # guessed; will be verified at enrichment
    return ""


def _is_blocked_domain(domain: str) -> bool:
    domain = enricher.normalize_domain(domain)
    if not domain:
        return True
    for bad in HARD_BLOCK_DOMAINS:
        if domain == bad or domain.endswith("." + bad):
            return True
    # strip known storefront hosts
    for plat in PLATFORM_DOMAINS:
        if domain.endswith(plat):
            return True
    # social-only / link-in-bio
    if domain in ("linktr.ee", "beacons.ai", "lnk.bio", "campsite.bio"):
        return True
    return False


def _text_of(*parts) -> str:
    return " ".join(p for p in parts if isinstance(p, str)).lower()


# ---------------------------------------------------------------------------
# Exclusion filters
# ---------------------------------------------------------------------------
def is_agency(name: str, text: str = "", domain: str = "") -> bool:
    blob = _text_of(name, text)
    if any(k in blob for k in AGENCY_KEYWORDS):
        return True
    # url footprint
    for kw in AGENCY_URL_KEYWORDS:
        if domain and kw in f" {domain} ":
            return True
    return False


def is_wholesaler(name: str, text: str = "", domain: str = "") -> bool:
    blob = _text_of(name, text)
    return any(k in blob for k in WHOLESALE_KEYWORDS)


def is_marketplace(name: str, text: str = "", domain: str = "") -> bool:
    blob = _text_of(name, text)
    if any(k in blob for k in MARKETPLACE_KEYWORDS):
        return True
    # domain-level marketplace check
    d = enricher.normalize_domain(domain)
    for bad in ("etsy.com", "ebay.com", "amazon.com", "walmart.com", "rakuten.com"):
        if d == bad or d.endswith("." + bad):
            return True
    return False


def is_amazon_only(domain: str) -> bool:
    d = enricher.normalize_domain(domain)
    return d.startswith("amazon.") or "amazon.com" in d


def is_affiliate(name: str, text: str = "") -> bool:
    blob = _text_of(name, text)
    return any(k in blob for k in AFFILIATE_KEYWORDS)


def is_dropshipper(domain: str, name: str, text: str = "") -> bool:
    """Heuristic. Needs the website to look like a low-effort reseller."""
    blob = _text_of(name, text)
    if any(k in blob for k in DROPSHIP_KEYWORDS):
        return True
    if not domain:
        return False
    n_products = enricher.product_count_heuristic(domain)
    has_story = enricher.has_brand_story(domain)
    if n_products and n_products > 200 and not has_story:
        return True
    return False


def is_large_corp(name: str, text: str = "", domain: str = "") -> bool:
    blob = _text_of(name, text)
    if any(k in blob for k in LARGE_CORP_KEYWORDS):
        return True
    # Fortune-500-ish name hints
    if re.search(r"\b(nike|adidas|apple|samsung|sony|l\\'or|loreal|unilever|procter|coca|pepsi|amazon|walmart|target|costco)\b", blob):
        return True
    return False


def passes_filters(name: str, text: str, domain: str, log: LogFn) -> bool:
    """Return True if the brand survives all hard exclusion filters."""
    if is_agency(name, text, domain):
        if log:
            log(f"[filter] EXCLUDE agency: {name}")
        return False
    if is_amazon_only(domain):
        if log:
            log(f"[filter] EXCLUDE amazon-only: {name} ({domain})")
        return False
    if is_marketplace(name, text, domain):
        if log:
            log(f"[filter] EXCLUDE marketplace/reseller: {name} ({domain})")
        return False
    if is_wholesaler(name, text, domain):
        if log:
            log(f"[filter] EXCLUDE wholesaler/distributor: {name} ({domain})")
        return False
    if is_affiliate(name, text):
        if log:
            log(f"[filter] EXCLUDE affiliate/deal site: {name}")
        return False
    if is_large_corp(name, text, domain):
        if log:
            log(f"[filter] EXCLUDE large public corp: {name}")
        return False
    try:
        if is_dropshipper(domain, name, text):
            if log:
                log(f"[filter] EXCLUDE likely dropshipper: {name} ({domain})")
            return False
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Category + activity level
# ---------------------------------------------------------------------------
def classify_category(ad_text: str, domain: str = "") -> str:
    blob = _text_of(ad_text, domain)
    for category, kws in CATEGORY_MAP:
        if any(kw in blob for kw in kws):
            return category
    return "Other"


def get_activity_level(active_ads_count: int) -> str:
    if active_ads_count >= 16:
        return "High"
    if active_ads_count >= 6:
        return "Medium"
    return "Low"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_brand(brand: dict) -> Tuple[int, int, int]:
    """Return (total, dtc_score, smb_score)."""
    dtc = 0
    smb = 0

    platform = brand.get("platform", "")
    own_domain = brand.get("website", "")
    has_story = bool(brand.get("has_brand_story"))
    ig_followers = brand.get("instagram_followers")

    # DTC signals
    if own_domain and not own_domain.startswith(("facebook.", "instagram.", "amazon.")):
        dtc += 5  # owns a real .com
    if platform in ("shopify", "woocommerce", "bigcommerce", "magento", "custom-cart"):
        dtc += 3  # ecommerce site (+3 per rubric "ecommerce websites")
    if has_story:
        dtc += 2

    # Independent brand (not part of conglomerate) -> already filtered large
    # corps, so everything here is independent. Small boost.
    dtc += 0

    # SMB signals
    if ig_followers is not None:
        if ig_followers < 200:
            smb += 3
        elif ig_followers < 50_000:
            smb += 3
        elif ig_followers < 200_000:
            smb += 1
        else:
            smb += 0
    else:
        smb += 1  # Unknown but kept -> modest SMB credit
    if has_story:
        smb += 0

    # Advertising activity boost
    level = brand.get("activity_level", "Low")
    activity_boost = {"High": 4, "Medium": 2, "Low": 1}.get(level, 1)

    total = dtc + smb + activity_boost
    return total, dtc, smb


# ---------------------------------------------------------------------------
# Related-keyword expansion
# ---------------------------------------------------------------------------
_SYNONYMS = {
    "fashion": ["clothing", "apparel", "wear", "boutique", "style", "outfits"],
    "clothing": ["fashion", "apparel", "wear", "garments"],
    "women": ["womens", "ladies", "female"],
    "women's": ["womens", "ladies", "female"],
    "mens": ["men", "menswear", "mens"],
    "men": ["mens", "menswear"],
    "beauty": ["skincare", "cosmetics", "makeup"],
    "skincare": ["beauty", "skin care"],
    "fitness": ["gym", "workout", "athletic"],
    "jewelry": ["jewellery", "accessories"],
    "home": ["decor", "furniture", "living"],
    "pet": ["dog", "cat", "animal"],
    "shoes": ["sneakers", "footwear"],
    "watch": ["watches", "timepiece"],
}


def generate_related_keywords(query: str, limit: int = 5) -> List[str]:
    q = (query or "").strip().lower()
    if not q:
        return []
    tokens = [t for t in re.split(r"[\s,]+", q) if len(t) > 2]
    out: List[str] = []
    seen = {q}
    # 1) swap each token for a synonym
    for tok in tokens:
        for syn in _SYNONYMS.get(tok, []):
            variant = " ".join(syn if t == tok else t for t in tokens)
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
    # 2) append a synonym
    for tok in tokens:
        for syn in _SYNONYMS.get(tok, [])[:2]:
            variant = f"{q} {syn}"
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
    # 3) plural / singular nudges
    if query.lower().endswith("s"):
        out.append(query[:-1])
    else:
        out.append(query + "s")
    # dedupe preserving order
    deduped = []
    for k in out:
        if k and k not in deduped:
            deduped.append(k)
    return deduped[:limit]


# ---------------------------------------------------------------------------
# Build / merge brands
# ---------------------------------------------------------------------------
def _enrich_one(ad: dict, domain: str, country_fallback: str, log: LogFn) -> dict:
    """Run all enrichment for a brand (cached by domain inside enricher)."""
    name = ad.get("advertiser_name", "").strip()
    website = domain
    log(f"[enrich] {name} -> {website} (emails/country/IG...)")

    emails = []
    country = country_fallback
    category = classify_category(ad.get("ad_text", ""), website)
    ig_handle, ig_followers = None, None
    platform = "unknown"
    has_story = False

    try:
        emails = enricher.extract_emails(website, limit=3)
    except Exception:
        pass
    try:
        country = enricher.extract_country(website, fallback=country_fallback) or country_fallback
    except Exception:
        pass
    try:
        ig_handle, ig_followers = enricher.get_instagram(website)
    except Exception:
        ig_handle, ig_followers = None, None
    try:
        platform = enricher.detect_platform(website)
    except Exception:
        pass
    try:
        has_story = enricher.has_brand_story(website)
    except Exception:
        pass
    # Re-classify with website context if category was Other
    if category == "Other":
        try:
            category = classify_category(ad.get("ad_text", ""), website)
        except Exception:
            pass

    if emails:
        log(f"[enrich]   emails: {', '.join(emails)}")
    if ig_followers is not None:
        log(f"[enrich]   IG @{ig_handle}: {ig_followers:,} followers")

    return {
        "brand_name": name,
        "website": website,
        "website_url": ("https://" + website) if website else "",
        "emails": emails,
        "country": country,
        "category": category,
        "instagram_handle": ig_handle or "",
        "instagram_followers": ig_followers,
        "platform": platform,
        "has_brand_story": has_story,
        "first_ad_text": (ad.get("ad_text", "") or "")[:200],
    }


def process_ads(raw_ads: List[dict], brands: Dict[str, dict],
                original_url: str, log: LogFn) -> Dict[str, dict]:
    """Merge a batch of raw ads into the running `brands` dict (keyed by domain)."""
    country_fallback = "US"
    try:
        import scraper as _scraper
        country_fallback = _scraper.parse_ad_library_url(original_url).get("country", "US")
    except Exception:
        pass

    # Count active ads per (domain, advertiser) first.
    counts: Dict[Tuple[str, str], int] = {}
    sample_ad: Dict[Tuple[str, str], dict] = {}
    for ad in raw_ads:
        name = (ad.get("advertiser_name") or "").strip()
        if not name:
            continue
        domain = extract_domain(ad)
        if not domain:
            continue
        key = (domain.lower(), name.lower())
        counts[key] = counts.get(key, 0) + 1
        if key not in sample_ad:
            sample_ad[key] = ad

    for (domain, name), active_count in counts.items():
        ad = sample_ad[(domain, name)]
        nice_name = ad.get("advertiser_name", name)

        if _is_blocked_domain(domain):
            if log:
                log(f"[filter] EXCLUDE blocked domain: {nice_name} ({domain})")
            continue
        if not passes_filters(nice_name, ad.get("ad_text", ""), domain, log):
            continue

        if domain in brands:
            brands[domain]["active_ads_count"] += active_count
            # keep the nicer name
            if len(nice_name) > len(brands[domain]["brand_name"]):
                brands[domain]["brand_name"] = nice_name
            continue

        brand = _enrich_one(ad, domain, country_fallback, log)
        brand["active_ads_count"] = active_count
        # If enrichment found nothing and the domain looks unresolvable,
        # still keep the brand (best-effort), but mark platform unknown.

        # IG follower cap handling: preferred 0-50k.
        # If we DID fetch followers and > 50k, drop unless we have very few brands.
        brands[domain] = brand
        log(f"[discovery] KEEP: {nice_name} ({domain}) x{active_count} ads")

    return brands


def finalize(brands: Dict[str, dict], target: int, log: LogFn) -> List[dict]:
    """Score, cap by IG followers, sort, and trim to `target`."""
    rows = list(brands.values())

    # Activity level + scores
    for b in rows:
        b["active_ads_count"] = max(1, int(b.get("active_ads_count") or 1))
        b["activity_level"] = get_activity_level(b["active_ads_count"])
        total, dtc, smb = score_brand(b)
        b["dtc_score"] = dtc
        b["smb_score"] = smb
        b["total_score"] = total

    # Preferred social size 0-50k. If we have more than the target *with* sub-50k
    # followers, drop the >50k ones. If we are short, keep them but ranked lower.
    sub_50k = [b for b in rows
               if b.get("instagram_followers") is None
               or (b.get("instagram_followers") or 0) <= 50_000]
    over_50k = [b for b in rows
                if (b.get("instagram_followers") or 0) > 50_000]
    if log and over_50k:
        log(f"[discovery] {len(over_50k)} brands have >50k IG followers (deprioritized).")
    if len(sub_50k) >= target:
        rows = sub_50k
    else:
        rows = sub_50k + over_50k

    level_rank = {"High": 0, "Medium": 1, "Low": 2}
    rows.sort(key=lambda b: (
        level_rank.get(b.get("activity_level", "Low"), 3),
        -(b.get("active_ads_count") or 0),
        -(b.get("total_score") or 0),
        b.get("brand_name", "").lower(),
    ))

    if log:
        log(f"[discovery] Final list: {len(rows)} brands "
            f"(trimmed to {min(len(rows), target)})")
    return rows[:target]


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def discover_brands(raw_ads: List[dict], target: int = 50,
                    original_url: str = "", log: LogFn = None,
                    pages: int = 40,
                    scraper_fn: Optional[Callable] = None) -> List[dict]:
    """Discover, enrich, filter, score and sort brands.

    `scraper_fn(url, pages, log)` is optional. When provided and the first
    pass yields fewer than `target` brands, related keywords are generated
    from the search `q` param and scraped until the target is met (max 3 extra
    searches).
    """
    log = log or _noop
    brands: Dict[str, dict] = {}

    log("[discovery] === Pass 1: original query ===")
    process_ads(raw_ads, brands, original_url, log)

    # Related-keyword expansion
    if scraper_fn is not None:
        try:
            import scraper as _scraper
            base_query = _scraper.parse_ad_library_url(original_url).get("query", "")
        except Exception:
            base_query = ""

        tried = 0
        while len(brands) < target and tried < 3:
            related = generate_related_keywords(base_query, limit=3 + tried)
            if tried >= len(related):
                break
            kw = related[tried]
            tried += 1
            log(f"[discovery] === Related keyword {tried}: {kw!r} "
                f"({len(brands)}/{target} so far) ===")
            try:
                rel_url = _scraper.build_url_with_query(original_url, kw)
                rel_ads = scraper_fn(rel_url, pages=pages, log=log)
            except Exception as e:
                log(f"[discovery] related-keyword scrape failed: {e}")
                rel_ads = []
            if not rel_ads:
                continue
            process_ads(rel_ads, brands, original_url, log)

    return finalize(brands, target, log)
