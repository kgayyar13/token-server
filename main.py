import os, re, time
from urllib.parse import urlencode, urljoin
import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# -------------------------------
# Global constants
# -------------------------------
BASE = "https://www.barrhavenvw.ca"
INV  = "/en/used-inventory"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}
cache = TTLCache(maxsize=128, ttl=1800)

# -------------------------------
# FastAPI setup
# -------------------------------
app = FastAPI()
orig = os.getenv("ALLOWED_ORIGIN")
if orig:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[orig],
        allow_methods=["*"],
        allow_headers=["*"],
    )

# -------------------------------
# Helper functions
# -------------------------------
def fetch(url):
    """Download HTML with standard headers."""
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text

def norm_km(txt):
    """Convert km text to int."""
    if not txt:
        return None
    m = re.search(r"([\d,\.]+)\s*(?:km|kilometres?|kilometers?)", txt, re.I)
    if not m:
        return None
    return int(float(m.group(1).replace(",", "")))

def text(el):
    return el.get_text(" ", strip=True) if el else ""

# -------------------------------
# Vehicle link extraction
# -------------------------------
VEH_RX = re.compile(r"/en/used-inventory/[^\"']+-id\d+", re.I)

def extract_vehicle_links(html: str):
    """Extract all vehicle page URLs from listing HTML."""
    links = set()
    soup = BeautifulSoup(html, "lxml")
    # CSS selector attempt
    for a in soup.select("a[href*='/en/used-inventory/']"):
        href = a.get("href") or ""
        if VEH_RX.search(href):
            links.add(urljoin(BASE, href))
    # Regex fallback
    for m in VEH_RX.finditer(html):
        links.add(urljoin(BASE, m.group(0)))
    return list(links)

# -------------------------------
# Enrich vehicle detail page
# -------------------------------
def enrich_vehicle(url, make=None, model=None, year=None):
    """Fetch detail page and extract key data."""
    v = {
        "url": url,
        "title": None,
        "year": year,
        "make": make,
        "model": model,
        "trim": "",
        "price": None,
        "color": None,
        "mileage_km": None,
        "stock_number": None,
        "carfax_url": None,
    }
    try:
        s = BeautifulSoup(fetch(url), "lxml")
        # title
        h = s.select_one("h1, .title, meta[property='og:title']")
        if h:
            v["title"] = h.get("content") if h.name == "meta" else h.get_text(" ", strip=True)
        else:
            v["title"] = url
        # year detection
        m = re.search(r"\b(20\d{2})\b", v["title"] or "")
        if m:
            v["year"] = int(m.group(1))
        # price
        pr = s.select_one(".price, [data-price]")
        if pr:
            v["price"] = pr.get_text(" ", strip=True)
        # stock / mileage / color
        st = s.select_one(".stock, [data-stock-number]")
        if st:
            v["stock_number"] = st.get_text(" ", strip=True)
        mil = s.select_one(".mileage, [data-mileage]")
        if mil:
            v["mileage_km"] = norm_km(mil.get_text(" ", strip=True))
        col = s.select_one(".color, [data-color]")
        if col:
            v["color"] = col.get_text(" ", strip=True)
        # carfax
        a = s.select_one("a[href*='carfax'], a[href*='vhr.carfax']")
        if a and a.has_attr("href"):
            v["carfax_url"] = urljoin(BASE, a["href"])
    except Exception as e:
        v["error"] = str(e)
    return v

# -------------------------------
# Routes
# -------------------------------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/inventory/search")
def inventory_search(
    make: str = Query(None),
    model: str = Query(None),
    year: str = Query(None),
    text: str = Query(None),
):
    key = f"links|{make}|{model}|{year}|{text}"
    if key in cache:
        return JSONResponse(cache[key])

    # Build query
    q = text or " ".join([x for x in [year, make, model] if x])
    url = f"{BASE}{INV}?text={'+'.join(q.split())}" if q else f"{BASE}{INV}"
    html = fetch(url)
    urls = extract_vehicle_links(html)

    # Optional filtering by query terms
    terms = [t.lower() for t in q.split()] if q else []
    def keep(u):
        if not terms:
            return True
        slug = u.rsplit("/", 1)[-1].replace("-", " ").lower()
        return all(t in slug for t in terms)
    urls = [u for u in urls if keep(u)]

    # Enrich top 10 vehicles
    out = [enrich_vehicle(u, make, model, year) for u in urls[:10]]
    cache[key] = out
    return JSONResponse(out)

@app.get("/inventory/carfax")
def carfax_fetch(url: str = Query(...)):
    try:
        s = BeautifulSoup(fetch(url), "lxml")
        a = s.select_one("a[href*='carfax'], a[href*='vhr.carfax']")
        carfax_url = urljoin(BASE, a["href"]) if a and a.has_attr("href") else None
        summary = text(s.select_one(".carfax, .history, .disclosure")) or None
        return {"carfax_url": carfax_url, "summary": summary}
    except Exception as e:
        return {"carfax_url": None, "summary": None, "error": str(e)}

@app.get("/inventory/links")
def inventory_links(text: str = Query("2024 volkswagen tiguan")):
    try:
        q = "+".join(text.split())
        url = f"{BASE}{INV}?text={q}"
        html = fetch(url)
        urls = extract_vehicle_links(html)
        return {"url": url, "count": len(urls), "urls": urls[:10]}
    except Exception as e:
        return {"error": str(e)}

# -------------------------------
# End of file
# -------------------------------
