# ---- imports and setup ----
import os, re, time
from urllib.parse import urlencode, urljoin
import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---- constants ----
BASE = "https://www.barrhavenvw.ca"
INV  = "/en/used-inventory"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}
cache = TTLCache(maxsize=128, ttl=1800)

# ---- FastAPI app ----
app = FastAPI()
orig = os.getenv("ALLOWED_ORIGIN")
if orig:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[orig],
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ---- helper functions (already in your file) ----
def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text

def norm_km(txt):
    if not txt: return None
    m = re.search(r"([\d,\.]+)\s*(?:km|kilometres?|kilometers?)", txt, re.I)
    return int(float(m.group(1).replace(",",""))) if m else None

def text(el): return el.get_text(" ", strip=True) if el else ""

def enrich(v):
    try:
        s = BeautifulSoup(fetch(v["url"]), "lxml")
        if not v.get("stock_number"):
            v["stock_number"] = text(s.select_one(".stock, [data-stock-number]")) or None
        if not v.get("mileage_km"):
            v["mileage_km"] = norm_km(text(s.select_one(".mileage, [data-mileage]")))
        if not v.get("color"):
            v["color"] = text(s.select_one(".color, [data-color]")) or None
        a = s.select_one("a[href*='carfax'], a[href*='vhr.carfax']")
        v["carfax_url"] = urljoin(BASE, a["href"]) if a and a.has_attr("href") else None
    except Exception:
        pass
    return v

# ---- health endpoint ----
@app.get("/health")
def health(): return {"ok": True}

# replace your /inventory/search handler with this

from fastapi import Query
from fastapi.responses import JSONResponse
import re
from urllib.parse import urlencode, urljoin
from bs4 import BeautifulSoup

TERM_SPLIT = re.compile(r"\s+")

def build_search_url(make, model, year, text):
    if text:
        q = text.strip()
    else:
        parts = [str(year or "").strip(), str(make or "").strip(), str(model or "").strip()]
        q = " ".join([p for p in parts if p])
    return f"{BASE}{INV}?{urlencode({'text': q})}"

def title_matches(title, make, model, year, text):
    t = (title or "").lower()
    if text:
        # all words in text must appear
        for w in TERM_SPLIT.split(text.lower()):
            if w and w not in t:
                return False
        return True
    hit = True
    if make:  hit &= make.lower()  in t
    if model: hit &= model.lower() in t
    if year:  hit &= str(year)     in t
    return hit

# ---- inventory_fetch 
@app.get("/inventory/search")
def inventory_search(
    make: str = Query(None),
    model: str = Query(None),
    year: str = Query(None),
    text: str = Query(None)
):
    key = f"{make}|{model}|{year}|{text}"
    if key in cache:
        return JSONResponse(cache[key])

    url = build_search_url(make, model, year, text)
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")

    # broad selection of possible cards/links
    nodes = soup.select("[data-vehicle-card], .vehicle-card, .result-item, li.vehicle-list-item, article") \
            or soup.select("a[href*='/used-inventory/']")
    results = []
    seen = set()

    for el in nodes:
        a = el if el.name == "a" else el.select_one("a[href*='/used-inventory/']")
        href = a.get("href") if a else None
        if not href: continue
        url_v = urljoin(BASE, href)
        if url_v in seen: continue
        seen.add(url_v)

        title = (el.select_one(".title, h2, h3") or a)
        title = title.get_text(" ", strip=True) if title else ""

        # keep if title contains requested terms
        if not title_matches(title, make, model, year, text):
            continue

        price  = (el.select_one(".price, [data-price]") or {}).get_text(" ", strip=True) if hasattr(el.select_one(".price, [data-price]"), 'get_text') else ""
        stock  = (el.select_one(".stock, [data-stock-number]") or {}).get_text(" ", strip=True) if hasattr(el.select_one(".stock, [data-stock-number]"), 'get_text') else ""
        mileage= (el.select_one(".mileage, [data-mileage]") or {}).get_text(" ", strip=True) if hasattr(el.select_one(".mileage, [data-mileage]"), 'get_text') else ""
        color  = (el.select_one(".color, [data-color]") or {}).get_text(" ", strip=True) if hasattr(el.select_one(".color, [data-color]"), 'get_text') else ""

        yr = None
        m = re.search(r"\b(20\d{2})\b", title)
        if m: yr = int(m.group(1))

        results.append({
            "title": title, "year": yr or year, "make": make, "model": model,
            "trim": "", "price": price or None, "color": color or None,
            "mileage_km": norm_km(mileage) if mileage else None,
            "stock_number": stock or None, "url": url_v, "carfax_url": None
        })

        if len(results) >= 10:
            break

    # enrich first 6
    enriched = [enrich(v) for v in results[:6]]
    cache[key] = enriched
    return JSONResponse(enriched)


# ---- carfax_fetch 
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

# ---- debug 
@app.get("/inventory/debug")
def inventory_debug(text: str = Query("2024 volkswagen tiguan")):
    from hashlib import md5
    q = "+".join(text.split())
    url = f"{BASE}{INV}?text={q}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        html = r.text
        return {
            "url": url,
            "status": r.status_code,
            "len": len(html),
            "md5": md5(html.encode("utf-8")).hexdigest(),
            "has_used_inventory_links": "/used-inventory/" in html,
            "first_800": html[:800]
        }
    except Exception as e:
        return {"url": url, "error": str(e)}
