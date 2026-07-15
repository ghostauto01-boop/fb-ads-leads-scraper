# 🎯 FB Ads Leads Scraper — DTC Brand Discovery

> Facebook & Instagram advertiser discovery agent. Paste any Meta Ad Library link, and it finds **DTC brands actively running Meta ads**, filters out agencies / resellers / dropshippers / large public corps, enriches each brand with **website, emails, country, category, and Instagram followers (0–50k preferred)**, and returns a clean, sorted, de-duplicated table.

**Live job:** paste a link → scrape → filter → enrich → sort → export CSV/XLSX. **No analysis text, only the final table.**

---

## ✨ What it does

1. **Scrape** the Meta Ad Library URL you paste (Playwright headless Chromium, 20–80 scroll-loads).
2. **Extract** every advertiser + their ad copy + external links.
3. **Filter (hard excludes):** agencies, wholesalers/distributors, marketplaces & resellers, Amazon-only sellers, affiliate/coupon sites, likely dropshippers, and large public corporations.
4. **Prioritize (scoring boost):** real DTC brands (+5), independent brands, SMBs (employee count / IG followers < 200k), active advertisers (+4), ecommerce sites (Shopify/WooCommerce/custom cart, +3).
5. **Enrich** each surviving brand: own-domain website, up to 3 emails (homepage + `/contact`, `mailto:` + regex), country (footer/TLD), product category (keyword mapping, no LLM), IG handle & follower count (best-effort public scrape).
6. **Expand** with related keywords (e.g. `women fashion → women clothing, ladies apparel, womens wear…`) if fewer than your target are found.
7. **De-dup by domain**, then **sort** by Activity **High > Medium > Low**, then by active-ad count desc.

---

## 📁 Project structure (flat, all at repo root)

```
fb-ads-leads-scraper/
├── app.py                 # Flask app: /, /api/discover, /api/job/<id>, /api/download/<id>/<file>, /health
├── scraper.py             # Playwright Ad Library scraper: scrape_ads_library(url, pages)
├── discovery.py           # Brand agent: filter, enrich, category, IG, scoring, dedup, sort
├── enricher.py            # Website scraper: emails, country, IG handle/followers, platform
├── requirements.txt
├── render.yaml            # Python env (NOT Docker) — Render free tier
├── templates/index.html   # Premium SaaS UI (Tailwind CDN, vanilla JS)
├── static/outputs/.gitkeep
├── .github/workflows/
│   ├── discover.yml       # workflow_dispatch: run discovery + upload CSV artifact
│   └── fix-nested.yml     # auto-flatten if the project is pushed inside a sub-folder
├── .gitignore
└── README.md
```

---

## 🚀 Deploy on Render (FREE tier)

Render uses the **Python environment** (no Dockerfile), which keeps the build under the free-tier limits.

1. **Fork / push** this repo to your GitHub.
2. Go to **[dashboard.render.com](https://dashboard.render.com)** → **New +** → **Web Service** → connect the `fb-ads-leads-scraper` repo.
3. Render reads **`render.yaml`** automatically:
   - **Runtime / Environment:** `Python 3` (`PYTHON_VERSION=3.11.0`)
   - **Plan:** Free
   - **Build:** `pip install … -r requirements.txt && python -m playwright install chromium`
   - **Start:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 300`
   - **Health check:** `/health`
4. Click **Apply / Create Web Service**. The first build installs Playwright Chromium (~1–2 min).
5. When deploy finishes, open your service URL and test:
   - **`/health`** → `{"status":"ok"}`
   - **`/`** → the UI.

> The build installs `greenlet==3.0.3` with `--only-binary` first (falls back to source), avoiding the classic "Failed building wheel for greenlet" error on the free tier.

---

## 🧪 Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python app.py            # http://localhost:5000
```

Run the scraper standalone:

```bash
python scraper.py "https://web.facebook.com/ads/library/?active_status=active&...&q=women%20fashion&..." 10
```

---

## 🔌 API

### `POST /api/discover`
```json
{ "url": "https://web.facebook.com/ads/library/?...&q=women%20fashion&...",
  "target_brands": 50, "pages": 40 }
```
→ `{"job_id":"<id>","status":"queued"}`

### `GET /api/job/<id>`
Returns `{status, logs[], brands[], brand_count, error}`. Poll every ~1.5s.

### `GET /api/download/<id>/brands.csv` & `/brands.xlsx`
Download the final table.

### `GET /health`
Liveness probe: `{"status":"ok"}`.

---

## 📊 Output columns

| Brand | Website | Emails | Country | Category | Active Ads | Activity | IG Followers |
|-------|---------|--------|---------|----------|-----------|----------|--------------|
| … | own domain | up to 3 | from footer/TLD | keyword-mapped | count | High/Med/Low badge | 0–50k preferred |

Internal scores (`dtc_score`, `smb_score`, `total_score`) are included in CSV/XLSX exports.

---

## ⚙️ Tech stack
Python 3.11 · Flask · Flask-CORS · Playwright (Chromium) · BeautifulSoup/lxml · gunicorn · single-file Tailwind+vanilla-JS UI.

## ⚠️ Notes & limitations
- The Meta Ad Library DOM changes frequently; the scraper uses several defensive extraction strategies and de-duplicates across scroll passes. If results look thin, raise **Pages** and **Target**.
- Instagram blocks most unauthenticated follower lookups today; when a count can't be fetched, the brand is kept and shown as **Unknown**.
- Everything runs **best-effort**: a single failing enrichment never kills the pipeline.
