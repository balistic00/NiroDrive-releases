#!/usr/bin/env python3
"""Türkiye şarj istasyonu listesini (chargers_tr.json) üretir.

Kaynaklar: OpenStreetMap (Overpass) ve OCM_API_KEY ortam değişkeni varsa Open Charge Map.
Uygulama bu listeyi gömülü olarak taşır; NiroDrive-releases reposundaki haftalık GitHub Actions
işi de aynı betikle listeyi yeniler, uygulama yeni listeyi haftada bir indirir.

Kullanım: python3 scripts/update_chargers.py [--osm overpass.json] [--out yol]
"""
import argparse
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request

QUERY = ('[out:json][timeout:170];area["ISO3166-1"="TR"][admin_level=2]->.a;'
         'nwr["amenity"="charging_station"](area.a);out tags center;')
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OCM_URL = "https://api.openchargemap.io/v3/poi?output=json&countrycode=TR&maxresults=20000&compact=true&verbose=false"
DEFAULT_OUT = os.path.join(os.path.dirname(__file__), "..", "app", "src", "main", "assets", "chargers_tr.json")
UA = {"User-Agent": "NiroDrive charger list (github.com/balistic00)"}


def fetch_osm():
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    for url in OVERPASS:
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, body, UA), timeout=200))
        except Exception as e:  # noqa: BLE001
            print(url, e, file=sys.stderr)
    sys.exit("Overpass yanıt vermedi")


def osm_dc(t):
    if any(k.startswith(("socket:type2_combo", "socket:chademo", "socket:ccs")) for k in t):
        return True
    m = re.search(r"(\d+)\s*kW", t.get("charging_station:output") or t.get("socket:type2_combo:output") or "")
    return bool(m and int(m.group(1)) >= 40)


def osm_items(data):
    for e in data["elements"]:
        t = e.get("tags", {})
        lat = e.get("lat", e.get("center", {}).get("lat"))
        lon = e.get("lon", e.get("center", {}).get("lon"))
        if lat is None or lon is None:
            continue
        name = next((t[k] for k in ("name", "operator", "brand", "network") if t.get(k)), "")
        yield [round(lat, 5), round(lon, 5), 1 if osm_dc(t) else 0, name]


def ocm_items(key):
    req = urllib.request.Request(OCM_URL, headers={**UA, "X-API-Key": key})
    for p in json.load(urllib.request.urlopen(req, timeout=200)):
        a = p.get("AddressInfo") or {}
        lat, lon = a.get("Latitude"), a.get("Longitude")
        if lat is None or lon is None:
            continue
        # CurrentTypeID 30 = DC, LevelID 3 = hızlı şarj.
        dc = any(c.get("CurrentTypeID") == 30 or c.get("LevelID") == 3 or (c.get("PowerKW") or 0) >= 40
                 for c in p.get("Connections") or [])
        yield [round(lat, 5), round(lon, 5), 1 if dc else 0, a.get("Title") or ""]


def dist_m(a, b):
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(x, math.radians(b[0] - a[0])) * 6371000


def merge(items):
    """~100 m içindeki kayıtlar aynı istasyon sayılır; DC bilgisi ve ilk dolu ad korunur."""
    grid = {}
    out = []
    for it in items:
        cell = (round(it[0] * 100), round(it[1] * 100))
        near = [o for dx in (-1, 0, 1) for dy in (-1, 0, 1) for o in grid.get((cell[0] + dx, cell[1] + dy), [])]
        same = next((o for o in near if dist_m(o, it) < 100), None)
        if same:
            same[2] = same[2] or it[2]
            same[3] = same[3] or it[3]
        else:
            out.append(it)
            grid.setdefault(cell, []).append(it)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", help="hazır Overpass yanıtı (json)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    osm = json.load(open(args.osm)) if args.osm else fetch_osm()
    items = list(osm_items(osm))
    sources = ["osm"]
    key = os.environ.get("OCM_API_KEY", "").strip()
    if key:
        try:
            items += list(ocm_items(key))
            sources.append("ocm")
        except Exception as e:  # noqa: BLE001
            print("Open Charge Map:", e, file=sys.stderr)
    items = merge(items)
    if len(items) < 300:
        sys.exit(f"Liste şüpheli derecede kısa ({len(items)}), yazılmadı")
    updated = osm.get("osm3s", {}).get("timestamp_osm_base", "")[:10]
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "sources": sources, "items": items}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(items)} istasyon, {sum(i[2] for i in items)} DC, kaynak: {'+'.join(sources)} -> {args.out}")


if __name__ == "__main__":
    main()
