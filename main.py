import os, re, time
from urllib.parse import urlencode, urljoin
import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright

BASE = "https://www.barrhavenvw.ca"
INV  = "/en/used-inventory"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}
cache = TTLCache(maxsize=128, ttl=1800)

app = FastAPI()
orig = os.getenv("ALLOWED_ORIGIN")
if orig:
    app.add_middleware(CORSMiddleware, allow_origins=[orig], allow_methods=["*"], allow_headers=["*"])

def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text

def fetch_rendered(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(url, wait_until="networkidle", timeout=45000)
        # give SPA a moment to paint cards if needed
        page.wait_for_timeout(1200)
        html = page.content()
        browser.close()
        return html

def norm_km(txt):
    if not txt: return None
    m = re.search(r"([\d,\.]+)\s*(?:km|kilometres?|kilometers?)", txt, re.I)
    return int(float(m.group(1).replace(",", ""))) if m else None

def text(el): return el.get_text(" ", strip=True) if el else ""

VEH_RX = re.compile(r"/en/used-inventory/[^\"']+-id\d+", re.I)

def extract_vehicle_links(html: str):
    links = set()
    s = BeautifulSoup(html, "lxml")
    for a in s.select("a[href*='/en/used-inventory/']"):
        href = a.get("href") or ""
        if VEH_RX.search(href):
            links.add(urljoin(BASE, href))
    for m in VEH_RX.finditer(html):
        links.add(urljoin(BASE, m.group(0)))
    return list(links)

def enrich_vehicle(url, make=None, model=None, year=None):
    """Fetch detail page and extract all key data."""
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

        # Title
        h = s.select_one("h1, .title, meta[property='og:title']")
        v["title"] = h.get("content") if h and h.name == "meta" else (
            h.get_text(" ", strip=True) if h else url
        )
        m = re.search(r"\b(20\d{2})\b", v["title"] or "")
        if m:
            v["year"] = int(m.group(1))

        # Price — several possible containers
        p = s.select_one(
            "[data-price], .vehicle-price, .vehicle-info__price, .price span, .price"
        )
        if p:
            raw = p.get_text(" ", strip=True)
            if "$" in raw:
                raw = raw.split("$")[-1]
            v["price"] = "$" + "".join(ch for ch in raw if ch.isdigit() or ch == ",")

        # Stock number
        st = s.find(
            lambda tag: tag.name in ["li", "span", "div"]
            and "stock" in tag.get_text(" ", strip=True).lower()
        )
        if st:
            txt = st.get_text(" ", strip=True)
            m = re.search(r"([A-Z]?\d{3,6})", txt)
            if m:
                v["stock_number"] = m.group(1)

        # Mileage
        mil = s.find(
            lambda tag: tag.name in ["li", "span", "div"]
            and "km" in tag.get_text(" ", strip=True).lower()
        )
        if mil:
            txt = mil.get_text(" ", strip=True)
            v["mileage_km"] = norm_km(txt)

        # Color
        col = s.find(
            lambda tag: tag.name in ["li", "span", "div"]
            and any(k in tag.get_text(" ", strip=True).lower() for k in ["color", "colour"])
        )
        if col:
            raw = col.get_text(" ", strip=True)
            # take the last word as color
            v["color"] = raw.split()[-1].capitalize()

        # Carfax link
        a = s.select_one("a[href*='carfax'], a[href*='vhr.carfax']")
        if a and a.has_attr("href"):
            v["carfax_url"] = urljoin(BASE, a["href"])

    except Exception as e:
        v["error"] = str(e)
    return v


@app.get("/health")
def health(): return {"ok": True}

@app.get("/inventory/links")
def inventory_links(text: str = Query("2024 volkswagen tiguan")):
    q = "+".join(text.split())
    url = f"{BASE}{INV}?text={q}"
    html = fetch_rendered(url)
    urls = extract_vehicle_links(html)
    return {"url": url, "count": len(urls), "urls": urls[:10]}

@app.get("/inventory/search")
def inventory_search(
    make: str = Query(None),
    model: str = Query(None),
    year: str = Query(None),
    text: str = Query(None),
):
    key = f"render|{make}|{model}|{year}|{text}"
    if key in cache: return JSONResponse(cache[key])

    q = text or " ".join([x for x in [year, make, model] if x])
    url = f"{BASE}{INV}?text={'+'.join((q or '').split())}" if q else f"{BASE}{INV}"
    html = fetch_rendered(url)
    urls = extract_vehicle_links(html)

    terms = [t.lower() for t in (q or "").split()]
    def keep(u):
        if not terms: return True
        slug = u.rsplit("/", 1)[-1].replace("-", " ").lower()
        return all(t in slug for t in terms)

    urls = [u for u in urls if keep(u)]
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
