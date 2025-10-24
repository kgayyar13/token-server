import os, re, time, json
from urllib.parse import urlencode, urljoin
import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright

# ------------ constants ------------
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

LABELS_PRICE = {"purchase price","price","our price","dealer price","internet price"}
LABELS_MILE  = {"kilometres","kilometers","odometer","km"}
LABELS_COLOR = {"exterior colour","exterior color","colour","color"}
LABELS_STOCK = {"stock #","stock number","stock","stk"}

VIN_RX   = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
STOCK_RX = re.compile(r"\b[A-Z]{0,2}?\d{2}-\d{4,6}[A-Z]?\b")   # e.g., 26-0058A
TRIM_RX  = re.compile(r"Trim\s+([A-Za-z0-9\- ]+?)(?:\s+\d{2}-\d{4,6}[A-Z]?|\s+VIN|\s+Automatic|\s+Bodystyle)", re.I)
EXT_RX   = re.compile(r"Ext\.?\s*Color\s*([A-Za-z \-]+)", re.I)
ODO_RX   = re.compile(r"([\d,\.]+)\s*(?:KM|Kilometres?|Kilometers?)", re.I)
VEH_RX   = re.compile(r"/en/used-inventory/[^\"']+-id\d+", re.I)

# ------------ app ------------
app = FastAPI()
orig = os.getenv("ALLOWED_ORIGIN")
if orig:
    app.add_middleware(CORSMiddleware, allow_origins=[orig], allow_methods=["*"], allow_headers=["*"])

# ------------ utils ------------
def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text

def fetch_rendered(url: str) -> str:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=45000)
            try:
                page.wait_for_selector("a[href*='/used-inventory/']", timeout=6000)
            except Exception:
                pass
            page.wait_for_timeout(800)
            html = page.content()
            browser.close()
            return html
    except Exception:
        # Fallback to plain fetch if Playwright fails
        return fetch(url)

def norm_km(txt):
    if not txt: return None
    m = re.search(r"([\d,\.]+)\s*(?:km|kilometres?|kilometers?)", txt, re.I)
    return int(float(m.group(1).replace(",", ""))) if m else None

def text(el): return el.get_text(" ", strip=True) if el else ""

def _clean_text(t): return re.sub(r"\s+", " ", (t or "").strip())

def _spec_scope(soup):
    return (soup.select_one("section:has(h2:-soup-contains('Specification'))")
            or soup.select_one("section:has(h2:-soup-contains('Vehicle'))")
            or soup)

def _find_labeled_value_in(scope, labels:set[str]):
    for dt in scope.find_all(["dt","th"]):
        key = dt.get_text(" ", strip=True).lower()
        if any(lbl in key for lbl in labels):
            dd = dt.find_next_sibling(["dd","td"])
            if dd: return dd.get_text(" ", strip=True)
    for li in scope.find_all("li"):
        txt = li.get_text(" ", strip=True)
        low = txt.lower()
        if any(lbl in low for lbl in labels):
            return re.sub(r"(?i)(" + "|".join(labels) + r")\s*[:#-]?\s*", "", txt).strip()
    for el in scope.find_all(["div","span","p"]):
        txt = el.get_text(" ", strip=True)
        low = txt.lower()
        if any(lbl in low for lbl in labels) and ":" in txt:
            return txt.split(":",1)[1].strip()
    return None

def _jsonld_vehicle(soup):
    for tag in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for n in nodes:
            if (n.get("@type") or "").lower() in ("vehicle","product"):
                v = {}
                offers = n.get("offers") or {}
                if isinstance(offers, list): offers = offers[0]
                price = offers.get("price") or (offers.get("priceSpecification") or {}).get("price")
                if price: v["price"] = f"${int(float(str(price).replace(',',''))):,}"
                v["stock_number"] = n.get("sku") or n.get("mpn") or (offers.get("sku") if isinstance(offers, dict) else None)
                v["color"] = n.get("color")
                mileage = n.get("mileage") or n.get("vehicleMileage")
                if isinstance(mileage, dict): mileage = mileage.get("value")
                if mileage:
                    try: v["mileage_km"] = int(str(mileage).replace(",",""))
                    except: pass
                return v
    return {}

def extract_vehicle_links(html: str):
    links = set()
    s = BeautifulSoup(html, "lxml")
    for a in s.select("a[href*='/en/used-inventory/']"):
        href = a.get("href") or ""
        if VEH_RX.search(href): links.add(urljoin(BASE, href))
    for m in VEH_RX.finditer(html):
        links.add(urljoin(BASE, m.group(0)))
    return list(links)

def _mm_from_title(title:str):
    if not title: return None, None
    parts = title.split()
    if len(parts) >= 3 and parts[0].isdigit():
        return parts[1], parts[2]
    return None, None

# ------------ detail enrichment ------------
def enrich_vehicle(url, make=None, model=None, year=None):
    v = {
        "url": url, "title": None, "year": year,
        "make": make, "model": model, "trim": "",
        "price": None, "color": None, "mileage_km": None,
        "stock_number": None, "vin": None, "carfax_url": None
    }
    try:
        soup = BeautifulSoup(fetch(url), "lxml")

        h = soup.select_one("h1, .title, meta[property='og:title']")
        title = h.get("content") if h and h.name=="meta" else (h.get_text(" ", strip=True) if h else "")
        v["title"] = title or url
        m = re.search(r"\b(20\d{2})\b", title or "")
        if m: v["year"] = int(m.group(1))
        mk, md = _mm_from_title(title)
        v["make"]  = v["make"]  or mk
        v["model"] = v["model"] or md

        # JSON-LD hints
        j = _jsonld_vehicle(soup)
        if j:
            v["price"]        = j.get("price") or v["price"]
            v["stock_number"] = j.get("stock_number") or v["stock_number"]
            v["color"]        = j.get("color") or v["color"]
            v["mileage_km"]   = j.get("mileage_km") or v["mileage_km"]

        scope = _spec_scope(soup)
        price_txt = _find_labeled_value_in(scope, LABELS_PRICE)
        odo_txt   = _find_labeled_value_in(scope, LABELS_MILE)
        color_txt = _find_labeled_value_in(scope, LABELS_COLOR)
        stock_txt = _find_labeled_value_in(scope, LABELS_STOCK)

        if price_txt and ("rebate" not in price_txt.lower()):
            m2 = re.search(r"\$?\s*([\d,][\d,\.]*)", price_txt)
            if m2: v["price"] = f"${m2.group(1).replace(',,',',')}"
        if odo_txt and v["mileage_km"] is None:
            v["mileage_km"] = norm_km(odo_txt)
        if color_txt:
            v["color"] = _clean_text(color_txt).split()[-1].title()
        if stock_txt and v["stock_number"] is None:
            m3 = re.search(r"[A-Za-z]{0,2}[-]?\d{3,7}[A-Za-z]?", stock_txt)
            v["stock_number"] = m3.group(0) if m3 else _clean_text(stock_txt)

        full = soup.get_text(" ", strip=True)
        m = VIN_RX.search(full)
        if m: v["vin"] = m.group(0)
        if not v["stock_number"]:
            m = re.search(r"(?:Stock(?:\s+#| Number)?)\s*[:#]?\s*([A-Za-z]{0,3}\d{2}-\d{4,6}[A-Za-z]?)",
              full, re.I)
            if m:
                v["stock_number"] = m.group(1)
            else:
                # 2) Fallback: strict dashed pattern only (avoid "360" from 360 Camera)
                m = re.search(r"\b[A-Za-z]{0,3}\d{2}-\d{4,6}[A-Za-z]?\b", full)
                if m:
                    v["stock_number"] = m.group(0)
        m = TRIM_RX.search(full)
        if m: v["trim"] = m.group(1).split(" - ")[0].strip()
        if not v["color"]:
            mc = re.search(r"Ext\.?\s*Color\s*([A-Za-z][A-Za-z \-]+?)(?:\s{2,}|Int\.|Interior|Drivetrain|Frame|Bodystyle|Options|\d{1,3},?\d{0,3}\s*KM)",full, re.I)
        if mc:
            v["color"] = mc.group(1).strip().title() 
        if v["mileage_km"] is None:
            m = ODO_RX.search(full)
            if m: v["mileage_km"] = int(float(m.group(1).replace(",", "")))

        a = soup.select_one("a[href*='carfax'], a[href*='vhr.carfax']")
        if a and a.has_attr("href"):
            v["carfax_url"] = urljoin(BASE, a["href"])

    except Exception as e:
        v["error"] = str(e)
    return v

# ------------ routes ------------
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
    scope = _spec_scope(s)
    return {
        "price_raw": _find_labeled_value_in(scope, LABELS_PRICE),
        "odo_raw": _find_labeled_value_in(scope, LABELS_MILE),
        "color_raw": _find_labeled_value_in(scope, LABELS_COLOR),
        "stock_raw": _find_labeled_value_in(scope, LABELS_STOCK),
        "price_fallback": text(s.select_one("[data-price], .vehicle-price, .price")) if s.select_one("[data-price], .vehicle-price, .price") else None
    }
