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
from fastapi import Query
from fastapi.responses import JSONResponse
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import re

@app.get("/inventory/search")
def inventory_search(
    make: str = Query(None),
    model: str = Query(None),
    year: str = Query(None),
    text: str = Query(None)
):
    # cache key
    key = f"links|{make}|{model}|{year}|{text}"
    if key in cache: return JSONResponse(cache[key])

    # 1) Build server-side search URL (?text=…)
    q = text or " ".join([x for x in [year, make, model] if x])
    url = f"{BASE}{INV}?text={'+'.join(q.split())}" if q else f"{BASE}{INV}"
    html = fetch(url)

    # 2) Extract vehicle links only (cards are inconsistent)
    soup = BeautifulSoup(html, "lxml")
    hrefs = []
    seen = set()
    for a in soup.select("a[href*='/used-inventory/']"):
        href = a.get("href") or ""
        full = urljoin(BASE, href)
        if "/used-inventory/" not in full: continue
        if full in seen: continue
        seen.add(full)
        hrefs.append(full)

    # 3) Optional title filtering from the link text if provided
    terms = [t.lower() for t in q.split()] if q else []
    def keep(url):
        if not terms: return True
        # use last path segment as pseudo-title
        slug = url.rsplit("/", 1)[-1].replace("-", " ").lower()
        return all(t in slug for t in terms)
    hrefs = [u for u in hrefs if keep(u)]

    # 4) Enrich detail pages to get real data (stock, mileage, color, carfax, title)
    out = []
    for u in hrefs[:10]:
        v = {"url": u, "title": None, "year": None, "make": make, "model": model,
             "trim": "", "price": None, "color": None, "mileage_km": None,
             "stock_number": None, "carfax_url": None}
        try:
            s = BeautifulSoup(fetch(u), "lxml")
            # title from H1/OG
            h = s.select_one("h1, .title, meta[property='og:title']")
            title = h.get("content") if h and h.name == "meta" else (h.get_text(" ", strip=True) if h else "")
            v["title"] = title or v["url"]
            m = re.search(r"\b(20\d{2})\b", title or "")
            if m: v["year"] = int(m.group(1))
            # price
            pr = s.select_one(".price, [data-price]")
            if pr: v["price"] = pr.get_text(" ", strip=True)
            # stock / mileage / color
            v["stock_number"] = (s.select_one(".stock, [data-stock-number]") or {}).get_text(" ", strip=True) if s.select_one(".stock, [data-stock-number]") else None
            mil = (s.select_one(".mileage, [data-mileage]") or {}).get_text(" ", strip=True) if s.select_one(".mileage, [data-mileage]") else None
            if mil:
                mm = re.search(r"([\d,\.]+)\s*(?:km|kilomet)", mil, re.I)
                v["mileage_km"] = int(float(mm.group(1).replace(",", ""))) if mm else None
            col = (s.select_one(".color, [data-color]") or {}).get_text(" ", strip=True) if s.select_one(".color, [data-color]") else None
            v["color"] = col or None
            # carfax link
            a = s.select_one("a[href*='carfax'], a[href*='vhr.carfax']")
            v["carfax_url"] = urljoin(BASE, a["href"]) if a and a.has_attr("href") else None
        except Exception:
            pass
        out.append(v)

    cache[key] = out
    return JSONResponse(out)


# ---- debug 
@app.get("/inventory/debug")
def inventory_debug(text: str = Query("2024 volkswagen tiguan")):
    from hashlib import md5
    from urllib.parse import urlencode
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

@app.get("/inventory/links")
def inventory_links(text: str = Query("2024 volkswagen tiguan")):
    url = f"{BASE}{INV}?text={'+'.join(text.split())}"
    html = fetch(url)
    urls = extract_vehicle_links(html)
    return {"count": len(urls), "urls": urls[:10]}
