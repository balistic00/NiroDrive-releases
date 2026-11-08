#!/usr/bin/env python3
"""
Türkiye şarj istasyonu listesini üretir (chargers_tr.json). NiroDrive-releases'teki haftalık
GitHub Actions bu betiği çalıştırır; uygulama listeyi oradan indirir.

Kaynak: EPDK Şarj Haritası (sarjtr.epdk.gov.tr), tüm lisanslı firmaların resmi listesi.
Her istasyonun detayından soket türü (DC/AC) ve gücü (kW) okunur.
EPDK yanıt vermezse eski yöntem (OpenStreetMap / Overpass) kullanılır.

Çıktı: {"updated": "YYYY-MM-DD", "sources": [...], "items": [[lat, lon, dc(0/1), ad, maxKw, epdkId], ...]}
Uygulama ilk 4 alanı okur; 5. ve 6. alanlar (kW, EPDK numarası) v1.13.0'dan beri kullanılır.
"""
import argparse, datetime, http.client, json, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

EPDK = "https://sarjtr.epdk.gov.tr/sarjet/api/stations"
# EPDK, tarayıcı dışı istemcilere yalnızca bu User-Agent ile yanıt veriyor (aksi halde 403).
HEADERS = {"User-Agent": "Dart/3.1 (dart:io)", "Accept": "application/json"}
OVERPASS = "https://overpass-api.de/api/interpreter"


def get_json(url, timeout=60, tries=3, pause=2.0):
    """URL'den JSON okur; hata olursa birkaç kez bekleyip tekrar dener."""
    for i in range(tries):
        try:
            # urllib "Connection: close" gönderiyor; EPDK buna bazen hiç yanıt vermiyor, http.client sorunsuz.
            u = urllib.parse.urlsplit(url)
            c = http.client.HTTPSConnection(u.netloc, timeout=timeout)
            try:
                c.request("GET", u.path + ("?" + u.query if u.query else ""), headers=HEADERS)
                r = c.getresponse()
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                return json.loads(r.read())
            finally:
                c.close()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(pause * (i + 1))


def detail_url(sid):
    """İstasyon detay adresi; EPDK bir önceki dakikanın Türkiye saatini istiyor."""
    stamp = (datetime.datetime.now(ZoneInfo("Europe/Istanbul")) - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:00")
    return f"{EPDK}/id/{sid}/{urllib.parse.quote(stamp)}"


def load_previous(path):
    """Önceki listedeki istasyonların DC/kW bilgisi (EPDK numarasına göre); detayı tekrar sorulmaz."""
    try:
        with open(path, encoding="utf-8") as f:
            return {i[5]: (i[2], i[4]) for i in json.load(f)["items"] if len(i) >= 6 and i[5] and i[4] > 0}
    except Exception:
        return {}


def epdk_items(workers, known, delay, budget_min):
    """EPDK listesini alır; detayı yalnızca yeni istasyonlar için, sunucuyu yormadan yavaşça sorar."""
    stations = get_json(EPDK, timeout=120, tries=5)
    done = [0]
    stop_at = time.time() + budget_min * 60

    def one(s):
        done[0] += 1
        if done[0] % 500 == 0:
            print(f"{done[0]}/{len(stations)} istasyon", file=sys.stderr, flush=True)
        name = " ".join(x for x in [s.get("brand"), s.get("title")] if x).strip()
        row = [round(s["lat"], 5), round(s["lng"], 5), 0, name, 0, s["id"]]
        if s["id"] in known:
            row[2], row[4] = known[s["id"]]
            return row
        if time.time() > stop_at:
            return row  # süre doldu; kalanlar bir sonraki çalıştırmada sorulur
        time.sleep(delay)
        try:
            # EPDK isteklerin yarıdan fazlasına hiç yanıt vermiyor (rastgele, yükten bağımsız); kısa bekleyip tekrar sormak en hızlısı.
            sockets = get_json(detail_url(s["id"]), timeout=3, tries=8, pause=0.1).get("sockets") or []
        except Exception:
            return row  # bu hafta alınamadı; gelecek hafta tekrar denenir
        row[2] = 1 if any(k.get("type") == "DC" for k in sockets) else 0
        row[4] = round(max((k.get("power") or 0 for k in sockets), default=0))
        return row
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, stations))


def osm_items():
    """Yedek: OpenStreetMap'teki Türkiye istasyonları (DC bilgisi eksik)."""
    q = '[out:json][timeout:180];area["ISO3166-1"="TR"]->.a;nwr["amenity"="charging_station"](area.a);out center;'
    req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(), headers={"User-Agent": "NiroDrive"})
    with urllib.request.urlopen(req, timeout=240) as r:
        data = json.load(r)
    out = []
    for e in data.get("elements", []):
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lon = e.get("lon") or e.get("center", {}).get("lon")
        t = e.get("tags", {})
        dc = any(k.startswith(("socket:type2_combo", "socket:chademo", "socket:ccs")) for k in t)
        if lat and lon:
            out.append([round(lat, 5), round(lon, 5), 1 if dc else 0, t.get("name") or t.get("operator") or "", 0, 0])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="chargers_tr.json")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--delay", type=float, default=0.3, help="detay istekleri arası bekleme (sn)")
    ap.add_argument("--budget", type=float, default=300, help="detay sorgusu için en fazla süre (dk)")
    args = ap.parse_args()
    try:
        items, sources = epdk_items(args.workers, load_previous(args.out), args.delay, args.budget), ["epdk"]
    except Exception as e:
        if load_previous(args.out):
            sys.exit(f"EPDK alınamadı ({e}); eski EPDK listesi olduğu gibi bırakıldı")
        print(f"EPDK alınamadı ({e}), OpenStreetMap kullanılıyor", file=sys.stderr)
        items, sources = osm_items(), ["osm"]
    if len(items) < 300:
        sys.exit(f"Liste şüpheli derecede kısa ({len(items)}), yazılmadı")
    updated = datetime.date.today().isoformat()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "sources": sources, "items": items}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(items)} istasyon, {sum(i[2] for i in items)} DC, kaynak: {'+'.join(sources)} -> {args.out}")


if __name__ == "__main__":
    main()
