import os, re, time
from urllib.parse import urlencode, urljoin
import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

BASE = "https://www.barrhavenvw.ca"
INV  = "/en/used-inventory"
HEADERS = {
  "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
  "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language":"en-CA,en;q=0.9",
}
cache = TTLCache(maxsize=128, ttl=1800)

app = FastAPI()
orig = os.getenv("ALLOWED_ORIGIN")
if orig:
  app.add_middleware(CORSMiddleware, allow_origins=[orig], allow_methods=["*"], allow_headers=["*"])

def fetch(url):
  r = requests.get(url, headers=HEADERS, timeout=20)
  r.raise_for_status()
  return r.text

def norm_km(txt):
  if not txt: return None
  m = re.search(r"([\d,\.]+)\s*(?:km|kilometres?|kilometers?)", txt, re.I)
  return int(float(m.group(1).replace(",",""))) if m else None

def text(el): return el.get_text(" ", strip=True) if el else ""

def parse_list(html):
  s = BeautifulSoup(html, "lxml")
  items = s.select("[data-vehicle-card], .vehicle-card, .result-item, li.vehicle-list-item, article")
  out = []
  for el in items:
    a = el if el.name=="a" else el.select_one("a[href*='/used-inventory/']")
    href = a.get("href") if a else None
    if not href: continue
    url = urljoin(BASE, href)
    title = text(el.select_one(".title, h2, h3")) or text(a)
    price = text(el.select_one(".price, [data-price]"))
    stock = text(el.select_one(".stock, [data-stock-number]"))
    mileage = text(el.select_one(".mileage, [data-mileage]"))
    color = text(el.select_one(".color, [data-color]"))
    # parse year/make/model/trim from title
    yr, make, model, trim = None, "", "", ""
    m = re.search(r"(20\d{2})\s+([A-Za-z]+)\s+([A-Za-z]+)(.*)", title or "")
    if m:
      yr = int(m.group(1)); make = m.group(2); model = m.group(3); trim = m.group(4).strip(" -•")
    out.append({
      "title": title, "year": yr, "make": make, "model": model, "trim": trim,
      "price": price or None, "color": color or None,
      "mileage_km": norm_km(mileage) if mileage else None,
      "stock_number": stock or None, "url": url, "carfax_url": None
    })
  return out

def enrich(v):
  try:
    s = BeautifulSoup(fetch(v["url"]), "lxml")
    if not v.get("stock_number"):
      v["stock_number"] = text(s.select_one(".stock, [data-stock-number], li:has(strong:contains('Stock'))")) or None
    if not v.get("mileage_km"):
      v["mileage_km"] = norm_km(text(s.select_one(".mileage, [data-mileage], li:has(strong:contains('Kilometres'))")))
    if not v.get("color"):
      v["color"] = text(s.select_one(".color, [data-color], li:has(strong:contains('Colour'))")) or None
    a = s.select_one("a[href*='carfax'], a[href*='vhr.carfax']")
    v["carfax_url"] = urljoin(BASE, a["href"]) if a and a.has_attr("href") else None
  except Exception:
    pass
  return v

@app.get("/health")
def health(): return {"ok": True}

@app.get("/inventory/search")
def inventory_search(make: str = Query(None), model: str = Query(None), year: str = Query(None)):
  key = f"{make}|{model}|{year}"
  if key in cache: return JSONResponse(cache[key])
  # server-side search via ?text=year+make+model
  q = " ".join([x for x in [year, make, model] if x]).strip()
  url = f"{BASE}{INV}?{urlencode({'text': q})}" if q else f"{BASE}{INV}"
  html = fetch(url)
  items = parse_list(html)
  # client-side filter safeguard
  def ok(v):
    if make and v.get("make") and make.lower() not in v["make"].lower(): return False
    if model and v.get("model") and model.lower() not in v["model"].lower(): return False
    if year and v.get("year") and str(v["year"]) != str(year): return False
    return True
  results = [enrich(v) for v in items if ok(v)][:10]
  cache[key] = results
  time.sleep(0.3)
  return JSONResponse(results)

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
