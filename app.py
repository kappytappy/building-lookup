"""Building Lookup - Cook County / Chicago building details from public data.

Type any Cook County address, get the building's story:
- Property ID (PIN), municipality, property class
- Building characteristics (year built, sq footage, beds/baths, construction)
- Assessed value history, recent sales
- Chicago building permits + violations (Chicago addresses)

All data comes from free public government APIs - no keys, no cost:
- U.S. Census Geocoder (address -> coordinates)
- Cook County GIS parcel service (coordinates -> PIN)
- Cook County Open Data (datacatalog.cookcountyil.gov)
- City of Chicago Open Data (data.cityofchicago.org)
"""
import re
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, request, render_template_string

app = Flask(__name__)
HTTP_TIMEOUT = 20


def _windows_proxies():
    """Read the system proxy from the Windows registry (browsers use this, but
    Python's requests ignores it, so a PC behind a proxy/VPN gets TLS errors)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
            if not enabled:
                return {}
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        proxies = {}
        if "=" in server:
            for part in server.split(";"):
                if "=" in part:
                    scheme, host = part.split("=", 1)
                    proxies[scheme.strip().lower()] = "http://" + host.strip()
        else:
            proxies = {"http": "http://" + server.strip(),
                       "https": "http://" + server.strip()}
        return proxies
    except Exception:
        return {}


SESSION = requests.Session()
SESSION.proxies.update(_windows_proxies())

COOK_VIEWER = ("https://gis.cookcountyil.gov/traditional/rest/services/"
               "CookViewer3Parcels/MapServer/0/query")
COOK_ORTHO = ("https://gis.cookcountyil.gov/traditional/rest/services/"
              "Ortho_Reference_Tiles/MapServer/export")
COOK_PARCEL_MAP = ("https://gis.cookcountyil.gov/traditional/rest/services/"
                   "CookViewer3Parcels/MapServer/export")
COOK_SOCRATA = "https://datacatalog.cookcountyil.gov/resource"
CHI_SOCRATA = "https://data.cityofchicago.org/resource"

CLASS_DESC = {
    "1": "Vacant land", "2": "Residential", "3": "Multi-family / mixed-use",
    "5": "Commercial / industrial", "7": "Commercial", "9": "Other",
}


def class_description(code):
    code = str(code or "")
    if not code:
        return ""
    prefix = {"1": "Vacant land", "2": "Residential", "3": "Multi-family",
              "5": "Commercial/industrial", "7": "Commercial", "9": "Other"}.get(code[0], "")
    return f"{code} - {prefix}" if prefix else code


def clean_num(v):
    """'1881.0' -> '1881', None/'' -> ''."""
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return "" if s.lower() in ("none", "null") else s


# ---------------- step 1: address -> coordinates ----------------

def geocode(address):
    try:
        r = SESSION.get(
            "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
            params={"address": address, "benchmark": "2020", "format": "json"},
            timeout=HTTP_TIMEOUT)
        matches = r.json().get("result", {}).get("addressMatches", [])
        if not matches:
            return None, "Address not found. Try adding city and ZIP, e.g. '233 S Wacker Dr, Chicago, IL 60606'."
        m = matches[0]
        return {"lat": m["coordinates"]["y"], "lng": m["coordinates"]["x"],
                "matched": m["matchedAddress"]}, None
    except Exception as e:
        return None, f"Geocoding failed: {e}"


# ---------------- step 2: coordinates -> parcel / PIN ----------------

def _street_key(addr):
    """'631 W SCHUBERT AVE, CHICAGO, IL 60614' -> ('631', 'W SCHUBERT AVE')."""
    street = addr.split(",")[0].upper().strip()
    parts = street.split(None, 1)
    if len(parts) == 2 and parts[0].replace("-", "").isdigit():
        return parts[0], parts[1]
    return "", street


def find_parcels(lat, lng, matched_address):
    """Envelope query on the Assessor's parcel service, then keep every parcel
    whose house number AND street match the geocoded address. The house number
    must match — a street-name-only match is never accepted, so a nearby parcel
    can never be silently returned for the wrong address."""
    try:
        d = 0.00045  # ~50 m: tolerates Census address-interpolation error
        r = SESSION.get(COOK_VIEWER, params={
            "geometry": f"{lng-d},{lat-d},{lng+d},{lat+d}",
            "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false",
            "outFields": "PIN14,street_address,class_info_display,latitude,longitude",
            "f": "json"}, timeout=HTTP_TIMEOUT)
        feats = r.json().get("features", [])
        if not feats:
            return None, "No Cook County parcel found at that location. Is the address in Cook County?"
        num, street = _street_key(matched_address)
        matches = []
        for f in feats:
            a = f["attributes"]
            ps = (a.get("street_address") or "").upper().strip()
            pnum, pstreet = _street_key(ps)
            if (num and pnum == num and street and pstreet
                    and (street == pstreet or street in pstreet or pstreet in street)):
                matches.append(f)
        if not matches:
            return None, ("Found nearby parcels but none match that house number — "
                          "the map point may be slightly off. Try the full address with ZIP code.")
        parts = [p.strip() for p in matched_address.split(",")]
        city = parts[1].title() if len(parts) >= 2 else ""
        out = []
        for f in sorted(matches, key=lambda f: f["attributes"].get("PIN14") or ""):
            a = f["attributes"]
            class_info = a.get("class_info_display") or ""
            code = class_info.split()[0] if class_info else ""
            out.append({"pin": a.get("PIN14") or "", "municipality": city,
                        "bldg_class": code, "class_info": class_info})
        return out, None
    except Exception as e:
        return None, f"Parcel lookup failed: {e}"


# ---------------- step 3: PIN -> characteristics / values / sales ----------------

def socrata(base, dataset, params):
    r = SESSION.get(f"{base}/{dataset}.json", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def latest(rows):
    """Pick the row with the highest year."""
    def yr(r):
        try:
            return float(r.get("year") or 0)
        except (TypeError, ValueError):
            return 0
    return max(rows, key=yr) if rows else None


def dash_pin(pin14):
    """'17162160090000' -> '17-16-216-009-0000' (format used by some datasets)."""
    p = re.sub(r"\D", "", pin14 or "")
    if len(p) != 14:
        return pin14
    return f"{p[0:2]}-{p[2:4]}-{p[4:7]}-{p[7:10]}-{p[10:14]}"


def model_link(code):
    """Pick the Assessor's valuation-model repo matching a property class code."""
    try:
        c = int(str(code).strip()[:3])
    except (TypeError, ValueError):
        c = 0
    if c in (299, 399):
        return ("condominium valuation model", "https://github.com/ccao-data/model-condo-avm")
    if 200 <= c < 300:
        return ("residential valuation model", "https://github.com/ccao-data/model-res-avm")
    return ("Assessor valuation models", "https://github.com/ccao-data")


def get_characteristics(pin):
    """Returns (kind_label, [(label, value), ...])."""
    try:
        rows = socrata(COOK_SOCRATA, "x54s-btds",
                       {"pin": pin, "$order": "year DESC", "$limit": "5"})
        row = latest(rows)
        if row:
            fields = [("Year built", "char_yrblt"), ("Building sq ft", "char_bldg_sf"),
                      ("Land sq ft", "char_land_sf"), ("Residence type", "char_type_resd"),
                      ("Use", "char_use"), ("Units", "char_ncu"), ("Bedrooms", "char_beds"),
                      ("Rooms", "char_rooms"), ("Full baths", "char_fbath"),
                      ("Half baths", "char_hbath"), ("Fireplaces", "char_frpl"),
                      ("Basement", "char_bsmt"), ("Basement finish", "char_bsmt_fin"),
                      ("Attic finish", "char_attic_fnsh"), ("Heating", "char_heat"),
                      ("A/C", "char_air"), ("Exterior walls", "char_ext_wall"),
                      ("Roof", "char_roof_cnst"), ("Construction quality", "char_cnst_qlty"),
                      ("Condition", "char_repair_cnd"), ("Garage", "char_gar1_size")]
            def show(label, key):
                v = clean_num(row.get(key))
                if not v:
                    return None
                if label == "Units" and v == "0":
                    return None
                return (label, v)
            return "Residential building", [x for x in
                                            (show(l, k) for l, k in fields) if x]
    except Exception:
        pass
    try:
        rows = socrata(COOK_SOCRATA, "3r7i-mrz4",
                       {"pin": pin, "$order": "year DESC", "$limit": "5"})
        row = latest(rows)
        if row:
            fields = [("Year built", "char_yrblt"), ("Unit sq ft", "char_unit_sf"),
                      ("Bedrooms", "char_bedrooms"), ("Building sq ft", "char_building_sf"),
                      ("Land sq ft", "char_land_sf"),
                      ("Parking/common area", "is_parking_space"),
                      ("Mixed-use building", "bldg_is_mixed_use")]
            return "Condominium unit", [(l, clean_num(row.get(k))) for l, k in fields
                                        if clean_num(row.get(k))]
    except Exception:
        pass
    try:
        dashed = dash_pin(pin)
        rows = socrata(COOK_SOCRATA, "csik-bsws",
                       {"pins": dashed, "$order": "year DESC", "$limit": "5"})
        if not rows and dashed != pin:
            rows = socrata(COOK_SOCRATA, "csik-bsws",
                           {"keypin": dashed, "$order": "year DESC", "$limit": "5"})
        row = latest(rows)
        if row:
            fields = [("Year built", "yearbuilt"), ("Building sq ft", "bldgsf"),
                      ("Land sq ft", "landsf"), ("Property type/use", "property_type_use"),
                      ("Market value", "finalmarketvalue"),
                      ("Investment rating", "investmentrating")]
            return "Commercial property", [(l, clean_num(row.get(k))) for l, k in fields
                                           if clean_num(row.get(k))]
    except Exception:
        pass
    return "", []


def get_values(pin):
    try:
        rows = socrata(COOK_SOCRATA, "uzyt-m557",
                       {"pin": pin, "$order": "year DESC", "$limit": "6"})
        out = []
        for r in rows:
            total = r.get("certified_tot") or r.get("mailed_tot") or r.get("board_tot")
            if clean_num(r.get("year")) and clean_num(total):
                out.append({"year": clean_num(r.get("year")),
                            "class": clean_num(r.get("class")),
                            "total": clean_num(total)})
        return out
    except Exception:
        return []


def get_sales(pin):
    try:
        rows = socrata(COOK_SOCRATA, "wvhk-k5uv",
                       {"pin": pin, "$order": "sale_date DESC", "$limit": "5"})
        out = []
        for r in rows:
            if r.get("sale_date"):
                out.append({"date": str(r.get("sale_date", ""))[:10],
                            "price": clean_num(r.get("sale_price")),
                            "deed": clean_num(r.get("deed_type")),
                            "seller": clean_num(r.get("seller_name")),
                            "buyer": clean_num(r.get("buyer_name"))})
        return out
    except Exception:
        return []


# ---------------- step 4: Chicago permits + violations ----------------

def get_permits(lat, lng):
    try:
        rows = socrata(CHI_SOCRATA, "ydr8-5enu", {
            "$where": f"within_circle(location, {lat}, {lng}, 150)",
            "$order": "issue_date DESC", "$limit": "15"})
        out = []
        for r in rows:
            addr = " ".join(clean_num(r.get(k)) for k in
                            ("street_number", "street_direction", "street_name")).strip()
            out.append({"date": str(r.get("issue_date", ""))[:10],
                        "type": clean_num(r.get("permit_type")),
                        "desc": clean_num(r.get("work_description"))[:160],
                        "cost": clean_num(r.get("reported_cost")),
                        "address": addr})
        return out
    except Exception:
        return []


def get_violations(lat, lng):
    try:
        rows = socrata(CHI_SOCRATA, "22u3-xenr", {
            "$where": f"within_circle(location, {lat}, {lng}, 150)",
            "$order": "violation_date DESC", "$limit": "15"})
        out = []
        for r in rows:
            addr = " ".join(p for p in [
                clean_num(r.get("street_number")),
                clean_num(r.get("street_direction")),
                clean_num(r.get("street_name")),
                clean_num(r.get("street_type"))] if p)
            out.append({"date": str(r.get("violation_date", ""))[:10],
                        "addr": addr.title(),
                        "desc": clean_num(r.get("violation_description"))[:160],
                        "status": clean_num(r.get("violation_status")),
                        "code": clean_num(r.get("violation_ordinance")),
                        "comments": clean_num(r.get("violation_inspector_comments")),
                        "bureau": clean_num(r.get("department_bureau"))})
        return out
    except Exception:
        return []


# ---------------- step 5: aerial property photo ----------------

def get_parcel_photo(pin):
    """Returns (aerial_img_url, outline_img_url) — a county aerial photo of the
    parcel with the parcel boundaries drawn over it. No API key needed."""
    try:
        feats = []
        for attempt in range(2):
            try:
                r = SESSION.get(COOK_VIEWER, params={
                    "where": f"PIN14='{pin}'", "returnGeometry": "true",
                    "outSR": "4326", "outFields": "PIN14", "f": "json"},
                    timeout=40)
                feats = r.json().get("features", [])
                if feats:
                    break
            except Exception:
                continue
        if not feats:
            return None, None
        rings = feats[0]["geometry"]["rings"]
        xs = [p[0] for ring in rings for p in ring]
        ys = [p[1] for ring in rings for p in ring]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        span = max(max(xs) - min(xs), max(ys) - min(ys), 0.00055) * 1.8
        span = min(span, 0.004)
        w, h = span / 2, span * 0.75 / 2  # 4:3 aspect
        bbox = f"{cx-w},{cy-h},{cx+w},{cy+h}"
        common = (f"bbox={bbox}&bboxSR=4326&imageSR=4326&size=800,600&f=image")
        aerial = (f"{COOK_ORTHO}?{common}&format=jpg")
        outline = (f"{COOK_PARCEL_MAP}?{common}&format=png32&transparent=true"
                   f"&layers=show:0")
        return aerial, outline
    except Exception:
        return None, None


# ---------------- UI ----------------

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Building Lookup - Cook County</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;max-width:1200px;margin:0 auto;padding:20px;color:#222}
h1{font-size:24px;margin-bottom:4px}.sub{color:#666;margin-bottom:18px;font-size:14px}
form{display:flex;gap:8px;margin-bottom:20px}
input[type=text]{flex:1;padding:10px;font-size:16px;border:1px solid #ccc;border-radius:6px}
button{padding:10px 22px;font-size:16px;background:#0b5ed7;color:#fff;border:0;border-radius:6px;cursor:pointer}
.err{background:#fdecea;color:#a33;padding:12px;border-radius:6px;margin-bottom:16px}
.layout{display:grid;grid-template-columns:minmax(300px,380px) 1fr;gap:14px;align-items:start}
.leftcol{position:sticky;top:12px}
@media(max-width:900px){.layout{grid-template-columns:1fr}.leftcol{position:static}}
.card{border:1px solid #ddd;border-radius:8px;padding:14px;margin-bottom:14px}
.card h2{font-size:17px;margin:0 0 10px}
.kv{display:grid;grid-template-columns:180px 1fr;gap:6px 12px;font-size:15px}
.kv dt{color:#666}.kv dd{margin:0}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:#666;border-bottom:2px solid #ddd;padding:6px}
td{border-bottom:1px solid #eee;padding:6px;vertical-align:top}
.note{font-size:13px;color:#666;margin-top:16px}
details{margin-top:6px}summary{cursor:pointer;color:#0b5ed7;font-size:13px}
.src{font-size:12px;color:#999}
</style></head><body>
<h1>Building Lookup</h1>
<div class="sub">Enter any Cook County address - building details from free public government data.</div>
<form method="post" action="/lookup">
<input type="text" name="address" placeholder="e.g. 233 S Wacker Dr, Chicago, IL 60606" value="{{q}}" required>
<button type="submit">Look up</button>
</form>
{% if error %}<div class="err">{{error}}</div>{% endif %}
{% if results %}
<div class="layout">
<div class="leftcol">
{% if aerial %}
<div class="card"><h2>Property photo (aerial)</h2>
<div style="position:relative;max-width:800px">
<img src="{{aerial}}" alt="Aerial photo of the property" style="width:100%;display:block;border-radius:6px">
<img src="{{outline}}" alt="" style="position:absolute;top:0;left:0;width:100%;pointer-events:none">
</div>
<div class="note">Aerial imagery from Cook County GIS with parcel boundaries overlaid — it may be a few years old, so recent changes might not show.</div></div>
{% endif %}
{% if streetview %}
<div class="card"><h2>Street-level view</h2>
<div class="note">Street-level photos can't be embedded without paid map keys, but these open the exact spot with one click:</div>
<p><a href="https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={{streetview}}" target="_blank" style="display:block;padding:10px 22px;background:#0b5ed7;color:#fff;border-radius:6px;text-decoration:none;margin-bottom:8px;text-align:center">Google Street View</a><a href="https://www.bing.com/maps?cp={{streetviewbing}}&lvl=18" target="_blank" style="display:block;padding:10px 22px;background:#0b5ed7;color:#fff;border-radius:6px;text-decoration:none;margin-bottom:8px;text-align:center">Bing Maps</a><a href="https://www.mapillary.com/app/?lat={{svlat}}&lng={{svlng}}&z=17" target="_blank" style="display:block;padding:10px 22px;background:#0b5ed7;color:#fff;border-radius:6px;text-decoration:none;text-align:center">Mapillary (open source)</a></p></div>
{% endif %}
</div>
<div class="rightcol">
{% for result in results %}
<div class="card"><h2>{{result.matched}}</h2>
{% if results|length > 1 %}<div class="note" style="margin-top:0">Parcel {{loop.index}} of {{results|length}} at this address.</div>{% endif %}
<dl class="kv">
<dt>Property ID (PIN)</dt><dd>{{result.pin}}</dd>
<dt>Municipality</dt><dd>{{result.municipality}}</dd>
<dt>Property class</dt><dd>{{result.class_desc}}</dd>
</dl></div>
{% if result.kind %}
<div class="card"><h2>{{result.kind}}</h2>
<dl class="kv">{% for l,v in result.chars %}<dt>{{l}}</dt><dd>{{v}}</dd>{% endfor %}</dl></div>
{% endif %}
<div class="card"><h2>Assessed value history</h2>
{% if result.assessed %}
<table><tr><th>Year</th><th>Class</th><th>Total assessed</th></tr>
{% for v in result.assessed %}<tr><td>{{v.year}}</td><td>{{v.class}}</td><td>${{v.total}}</td></tr>{% endfor %}
</table>
{% else %}<div class="note">Assessed values are temporarily unavailable — the county data portal isn't responding right now. Try again later.</div>{% endif %}
<div class="note">These values are estimated by the Assessor's <a href="{{result.model_url}}" target="_blank">{{result.model_name}}</a> — the public computer model that predicts what the property would sell for. The code is open source.</div></div>
{% if result.sales %}
<div class="card"><h2>Recent sales</h2>
<table><tr><th>Date</th><th>Price</th><th>Deed</th><th>Seller</th><th>Buyer</th></tr>
{% for s in result.sales %}<tr><td>{{s.date}}</td><td>${{s.price}}</td><td>{{s.deed}}</td><td>{{s.seller}}</td><td>{{s.buyer}}</td></tr>{% endfor %}
</table></div>
{% endif %}
<div class="card"><h2>Ownership</h2>
<div class="note">No free public API lists the current owner by name — the county sites that have it block automated lookups. The best free source is the most recent buyer on record:</div>
{% if result.sales %}<dl class="kv"><dt>Most recent buyer</dt><dd>{{result.sales[0].buyer}} — bought {{result.sales[0].date}} for ${{result.sales[0].price}} ({{result.sales[0].deed}}, doc ref in sales table)</dd></dl>
<div class="note">This is the last recorded buyer, which is usually but not always the current owner.</div>
{% else %}<div class="note">No sales on record for this PIN, so no buyer name is available from free sources.</div>{% endif %}
<div class="note">For the official taxpayer name, look up this PIN on the county sites (same ones CookViewer links to):</div>
<dl class="kv"><dt>PIN</dt><dd>{{result.pin}}</dd></dl>
<p><a href="https://www.cookcountyassessor.com/pin/{{result.pin}}" target="_blank" style="display:inline-block;padding:10px 22px;background:#0b5ed7;color:#fff;border-radius:6px;text-decoration:none;margin-right:8px">Assessor's page for this PIN</a><a href="https://www.cookcountytreasurer.com/" target="_blank" style="display:inline-block;padding:10px 22px;background:#0b5ed7;color:#fff;border-radius:6px;text-decoration:none">Treasurer's site</a></p></div>
<div class="card"><h2>Deeds & recorded documents</h2>
<div class="note">The deed copies themselves aren't free — the Cook County Clerk sells them per document on their site, and there's no free download. Search this PIN on the Clerk's site to find and purchase them:</div>
<dl class="kv"><dt>PIN to search</dt><dd>{{result.pin}}</dd></dl>
<p><button onclick="navigator.clipboard.writeText('{{result.pin}}');this.textContent='PIN copied — paste it on the Clerk site'" style="display:inline-block;padding:10px 22px;background:#6c757d;color:#fff;border:0;border-radius:6px;margin-right:8px;cursor:pointer">Copy PIN</button><a href="https://crs.cookcountyclerkil.gov/Search" target="_blank" style="display:inline-block;padding:10px 22px;background:#0b5ed7;color:#fff;border-radius:6px;text-decoration:none">Open the Clerk's recordings search</a></p>
<div class="note">Tip: on the Clerk's site choose PIN search and paste the PIN — you'll get the full list of recorded documents (deeds, mortgages, liens) with the option to purchase copies. The sales table above already lists document numbers, dates, prices, and parties for recent transfers, free.</div></div>
{% endfor %}
{% if permits is not none %}
<div class="card"><h2>Chicago building permits (nearby)</h2>
{% if permits %}<table><tr><th>Issued</th><th>Type</th><th>Address</th><th>Description</th><th>Reported cost</th></tr>
{% for p in permits %}<tr><td>{{p.date}}</td><td>{{p.type}}</td><td>{{p.address}}</td><td>{{p.desc}}</td><td>{{p.cost}}</td></tr>{% endfor %}
</table>{% else %}<div class="note">No permits found nearby.</div>{% endif %}</div>
<div class="card"><h2>Chicago building violations (nearby)</h2>
{% if violations %}<table><tr><th>Date</th><th>Address</th><th>Violation</th><th>Status</th></tr>
{% for v in violations %}<tr><td>{{v.date}}</td><td>{{v.addr}}</td><td>{{v.desc}}<details><summary>details</summary><div class="note"><b>Inspector:</b> {{v.comments}}<br><b>Ordinance:</b> {{v.code}}<br><b>Bureau:</b> {{v.bureau}}</div></details></td><td>{{v.status}}</td></tr>{% endfor %}
</table><div class="note">These are within about 150 meters and may belong to neighboring properties — check the address column.</div>{% else %}<div class="note">No violations found nearby.</div>{% endif %}</div>
{% endif %}
</div>
</div>
<div class="src">Sources: U.S. Census Geocoder, Cook County GIS &amp; Open Data Portal, City of Chicago Open Data Portal. Data may lag behind county/city updates.</div>
{% endif %}
</body></html>"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(PAGE, q="", error=None, results=None, permits=None, violations=None, aerial=None, outline=None, streetview=None)


@app.route("/lookup", methods=["POST"])
def lookup():
    q = request.form.get("address", "").strip()
    if not q:
        return render_template_string(PAGE, q=q, error="Enter an address.", results=None, permits=None, violations=None, aerial=None, outline=None, streetview=None)

    geo, err = geocode(q)
    if err:
        return render_template_string(PAGE, q=q, error=err, results=None,
                                       permits=None, violations=None,
                                       aerial=None, outline=None, streetview=None)

    parcels, err = find_parcels(geo["lat"], geo["lng"], geo["matched"])
    if err:
        return render_template_string(PAGE, q=q, error=err, results=None,
                                       permits=None, violations=None,
                                       aerial=None, outline=None, streetview=None)

    in_chicago = parcels[0]["municipality"].lower() == "chicago"
    with ThreadPoolExecutor(max_workers=10) as ex:
        jobs = [{"parcel": p,
                 "f_chars": ex.submit(get_characteristics, p["pin"]),
                 "f_values": ex.submit(get_values, p["pin"]),
                 "f_sales": ex.submit(get_sales, p["pin"])} for p in parcels]
        f_permits = ex.submit(get_permits, geo["lat"], geo["lng"]) if in_chicago else None
        f_viol = ex.submit(get_violations, geo["lat"], geo["lng"]) if in_chicago else None
        f_photo = ex.submit(get_parcel_photo, parcels[0]["pin"])
        results = []
        for j in jobs:
            p = j["parcel"]
            kind, chars = j["f_chars"].result()
            model_name, model_url = model_link(p["bldg_class"])
            results.append({"matched": geo["matched"], "pin": p["pin"],
                            "municipality": p["municipality"],
                            "class_desc": p.get("class_info") or class_description(p["bldg_class"]),
                            "kind": kind, "chars": chars,
                            "assessed": j["f_values"].result(),
                            "sales": j["f_sales"].result(),
                            "model_name": model_name, "model_url": model_url})
        permits = f_permits.result() if f_permits else None
        violations = f_viol.result() if f_viol else None
        aerial, outline = f_photo.result()

    lat, lng = geo["lat"], geo["lng"]
    return render_template_string(PAGE, q=q, error=None, results=results,
                                   permits=permits, violations=violations,
                                   aerial=aerial, outline=outline,
                                   streetview=f"{lat},{lng}",
                                   streetviewbing=f"{lat}~{lng}",
                                   svlat=lat, svlng=lng)


def main():
    if getattr(sys, "frozen", False):
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
