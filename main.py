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

LABELS_PRICE = {"price","our price","dealer price","internet price"}
LABELS_MILE  = {"kilometres","kilometers","km","odometer"}
LABELS_COLOR = {"exterior colour","exterior color","colour","color"}
LABELS_STOCK = {"stock #","stock","stock number","stk"}

def _clean_text(t):
    return re.sub(r"\s+", " ", (t or "").strip())

def _find_labeled_value(soup, labels:set[str]):
    # 1) definition lists or spec tables (dt/dd, th/td)
    for dt in soup.find_all(["dt","th"]):
        key = _clean_text(dt.get_text()).lower()
        if any(lbl in key for lbl in labels):
            dd = dt.find_next_sibling(["dd","td"])
            if dd:
                return _clean_text(dd.get_text())
    # 2) list items like "<li><strong>Stock</strong> L0135</li>"
    for li in soup.find_all("li"):
        txt = _clean_text(li.get_text(" "))
        low = txt.lower()
        if any(lbl in low for lbl in labels):
            # remove the key part, keep value
            for lbl in labels:
                low = low.replace(lbl, "")
                txt  = re.sub(lbl, "", txt, flags=re.I)
            return _clean_text(txt)
    # 3) generic “label: value” spans/divs
    for el in soup.find_all(["div","span","p"]):
        txt = _clean_text(el.get_text(" "))
        low = txt.lower()
        if any(lbl in low for lbl in labels) and ":" in txt:
            return _clean_text(txt.split(":",1)[1])
    return None

def _parse_money(val):
    if not val: return None
    m = re.search(r"\$?\s*([\d,][\d,\.]*)", val)
    return f"${m.group(1).replace(',,',',')}" if m else None

import json

def _jsonld_vehicle(soup):
    # find Vehicle/Product JSON-LD
    for tag in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        # handle list or single object
        nodes = data if isinstance(data, list) else [data]
        for n in nodes:
            t = (n.get("@type") or "").lower()
            if t in ("vehicle","product"):
                v = {}
                # price
                offers = n.get("offers") or {}
                if isinstance(offers, list): offers = offers[0]
                v["price"] = offers.get("price") or offers.get("priceSpecification", {}).get("price")
                if v["price"]:
                    v["price"] = f"${int(float(str(v['price']).replace(',',''))):,}"
                # stock / sku
                v["stock_number"] = n.get("sku") or n.get("mpn") or (offers.get("sku") if isinstance(offers, dict) else None)
                # color
                v["color"] = n.get("color") or (n.get("additionalProperty", {}).get("color") if isinstance(n.get("additionalProperty"), dict) else None)
                # mileage (some sites use mileage/vehicleMileage/kilometers)
                mileage = n.get("mileage") or n.get("vehicleMileage")
                if isinstance(mileage, dict):
                    mileage = mileage.get("value")
                if mileage:
                    try:
                        v["mileage_km"] = int(str(mileage).replace(",",""))
                    except Exception:
                        pass
                return v
    return {}

def _spec_scope(soup):
    # narrow to the vehicle spec area to avoid "Similar vehicles"
    scope = soup.select_one("section:has(h2:-soup-contains('Specification'))") \
         or soup.select_one("section:has(h2:-soup-contains('Vehicle'))") \
         or soup  # fallback whole page
    return scope

LABELS_PRICE = {"purchase price","price"}
LABELS_MILE  = {"kilometres","kilometers","odometer","km"}
LABELS_COLOR = {"exterior colour","exterior color","colour","color"}
LABELS_STOCK = {"stock #","stock number","stock","stk"}

def _find_labeled_value_in(scope, labels:set[str]):
    # table-like (dt/dd or th/td)
    for dt in scope.find_all(["dt","th"]):
        key = dt.get_text(" ", strip=True).lower()
        if any(lbl in key for lbl in labels):
            dd = dt.find_next_sibling(["dd","td"])
            if dd: return dd.get_text(" ", strip=True)
    # list items: "<li><strong>Stock</strong> L0135</li>"
    for li in scope.find_all("li"):
        txt = li.get_text(" ", strip=True)
        low = txt.lower()
        if any(lbl in low for lbl in labels):
            return re.sub(r"(?i)(" + "|".join(labels) + r")\s*[:#-]?\s*", "", txt).strip()
    # generic "Label: Value"
    for el in scope.find_all(["div","span","p"]):
        txt = el.get_text(" ", strip=True)
        low = txt.lower()
        if any(lbl in low for lbl in labels) and ":" in txt:
            return txt.split(":",1)[1].strip()
    return None

def enrich_vehicle(url, make=None, model=None, year=None):
    v = {
        "url": url, "title": None, "year": year, "make": make, "model": model,
        "trim": "", "price": None, "color": None, "mileage_km": None,
        "stock_number": None, "carfax_url": None
    }
    try:
        soup = BeautifulSoup(fetch(url), "lxml")

        # title/year
        h = soup.select_one("h1, .title, meta[property='og:title']")
        v["title"] = h.get("content") if h and h.name=="meta" else (h.get_text(" ", strip=True) if h else url)
        m = re.search(r"\b(20\d{2})\b", v["title"] or "")
        if m: v["year"] = int(m.group(1))

        # 1) JSON-LD first
        j = _jsonld_vehicle(soup)
        if j:
            v["price"]        = j.get("price") or v["price"]
            v["stock_number"] = j.get("stock_number") or v["stock_number"]
            v["color"]        = j.get("color") or v["color"]
            v["mileage_km"]   = j.get("mileage_km") or v["mileage_km"]

        # 2) Scoped spec scraping (avoid "Similar vehicles")
        scope = _spec_scope(soup)
        price_txt = _find_labeled_value_in(scope, LABELS_PRICE)
        odo_txt   = _find_labeled_value_in(scope, LABELS_MILE)
        color_txt = _find_labeled_value_in(scope, LABELS_COLOR)
        stock_txt = _find_labeled_value_in(scope, LABELS_STOCK)

        # normalize
        if price_txt and ("rebate" not in price_txt.lower()):
            m2 = re.search(r"\$?\s*([\d,][\d,\.]*)", price_txt)
            if m2: v["price"] = f"${m2.group(1).replace(',,',',')}"
        if odo_txt:
            v["mileage_km"] = norm_km(odo_txt)
        if color_txt:
            v["color"] = color_txt.split()[-1].capitalize()
        if stock_txt:
            m3 = re.search(r"[A-Za-z]{0,2}[-]?\d{3,7}", stock_txt)
            v["stock_number"] = m3.group(0) if m3 else stock_txt

        # Carfax
        a = soup.select_one("a[href*='carfax'], a[href*='vhr.carfax']")
        v["carfax_url"] = urljoin(BASE, a["href"]) if a and a.has_attr("href") else None

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

@app.get("/inventory/debug-detail")
def debug_detail(url: str):
    s = BeautifulSoup(fetch(url), "lxml")
    return {
        "price_raw": _find_labeled_value(s, LABELS_PRICE),
        "odo_raw": _find_labeled_value(s, LABELS_MILE),
        "color_raw": _find_labeled_value(s, LABELS_COLOR),
        "stock_raw": _find_labeled_value(s, LABELS_STOCK),
        "price_fallback": text(s.select_one("[data-price], .vehicle-price, .price")) if s.select_one("[data-price], .vehicle-price, .price") else None
    }
