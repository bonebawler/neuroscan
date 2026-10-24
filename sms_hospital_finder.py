"""ResponSMS - emergency hospital referral for Quezon City over (simulated) SMS.

Pipeline:  build_data() -> Store -> Session.send() / handle_sms()
  1. build_data()   one-time: clean the DOH facility CSV, geocode hospitals, load barangay polygons
  2. Store          loads hospitals_clean.csv, barangays.csv and qc_barangays.geojson
  3. rank()         filter by service rules, normalized weighted sum (capacity, proximity, freshness)
  4. Session        multi-turn SMS conversation (menu, symptom, location, English/Tagalog)
  5. handle_sms()   stateless one-shot version of the same flow

Needs: pandas numpy rapidfuzz shapely.  Optional env var: GOOGLE_MAPS_API_KEY.
Data lives in ./qc_hospital_sms/data (override with env var QC_SMS_ROOT).
"""
import os, re, json, math, time, textwrap, html as _html
import urllib.parse, urllib.request
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
from shapely.geometry import Point, shape
from shapely.ops import unary_union

# =====================================================================
# 1. CONFIG
# =====================================================================
HERE = Path(__file__).resolve().parent

def _load_dotenv(path):
    """Tiny .env reader (KEY=value lines) so you don't need python-dotenv."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_dotenv(HERE / ".env")

try:                                        # still works inside Google Colab
    from google.colab import userdata
    GOOGLE_MAPS_API_KEY = userdata.get("GOOGLE_MAPS_API_KEY") or ""
except Exception:
    GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()

ROOT = Path(os.environ.get("QC_SMS_ROOT", HERE / "qc_hospital_sms"))
DATA = ROOT / "data"

AS_OF = date.today()
FRESH_HORIZON_DAYS = 730
NEUTRAL_CAPACITY = 0.5
ROAD_FACTOR = 1.35
ROAD_KMH_FALLBACK = 25
SMS_MAX_CHARS = 480
FALLBACK = "If life-threatening call 911 now or go to the nearest ER."
VERBOSE = True            # print which geocoder answered

WEIGHTS = {
    "stroke":           dict(cap=.30, prox=.55, fresh=.15),
    "trauma":           dict(cap=.30, prox=.55, fresh=.15),
    "cardiac":          dict(cap=.30, prox=.55, fresh=.15),
    "obstetric":        dict(cap=.40, prox=.45, fresh=.15),
    "pediatric":        dict(cap=.40, prox=.45, fresh=.15),
    "respiratory":      dict(cap=.35, prox=.50, fresh=.15),
    "seizure":          dict(cap=.35, prox=.50, fresh=.15),
    "poisoning":        dict(cap=.35, prox=.50, fresh=.15),
    "severe_bleeding":  dict(cap=.35, prox=.50, fresh=.15),
    "burns":            dict(cap=.35, prox=.50, fresh=.15),
    "allergic_reaction":dict(cap=.35, prox=.50, fresh=.15),
    "default":          dict(cap=.35, prox=.50, fresh=.15),
}

SERVICE_RULES = {
    "stroke":            dict(min_level=2, specialty_ok=[]),
    "trauma":            dict(min_level=2, specialty_ok=["ortho"]),
    "obstetric":         dict(min_level=2, specialty_ok=[]),
    "cardiac":           dict(min_level=3, specialty_ok=["cardiac"]),
    "pediatric":         dict(min_level=2, specialty_ok=["pediatric"]),
    "respiratory":       dict(min_level=2, specialty_ok=["pulmonary"]),
    "seizure":           dict(min_level=2, specialty_ok=[]),
    "poisoning":         dict(min_level=2, specialty_ok=[]),
    "severe_bleeding":   dict(min_level=2, specialty_ok=[]),
    "burns":             dict(min_level=2, specialty_ok=[]),
    "allergic_reaction": dict(min_level=2, specialty_ok=[]),
}

SPECIALTY_BY_NAME = {
    "HEART CENTER": "cardiac", "LUNG CENTER": "pulmonary",
    "KIDNEY": "renal", "UROLOGY": "urology",
    "ORTHOPEDIC": "ortho", "CHILDREN": "pediatric",
}

EMERGENCY_KEYWORDS = {
    "stroke": ["stroke", "slurred", "paralyzed", "paralysis",
               "na-stroke", "nabingi", "paralisado", "hindi makagalaw",
               "hindi makapagsalita", "tumba kalahati ng katawan"],
    "cardiac": ["heart attack", "cardiac", "chest pain",
                "atake sa puso", "masakit ang dibdib", "sakit sa puso"],
    "obstetric": ["pregnant", "obstetric", "labor", "childbirth",
                  "pregnancy", "buntis", "nanganganak", "manganganak",
                  "panganganak", "bagong panganak"],
    "pediatric": ["pediatric", "pedia", "child", "baby", "infant",
                  "bata", "sanggol", "anak", "paslit"],
    "trauma": ["trauma", "accident", "injury", "fracture",
               "crash", "hit by", "aksidente", "nabangga", "bangga",
               "saksak", "sinaksak", "baril", "binaril", "sugat",
               "nahulog", "napilay"],
    "respiratory": ["difficulty breathing", "cant breathe",
                    "shortness of breath", "asthma", "wheezing",
                    "hirap huminga", "hindi makahinga", "hika",
                    "kinakapos ng hininga"],
    "seizure": ["seizure", "convulsion", "epilepsy",
                "kombulsyon", "kumbulsyon", "nangingisay"],
    "poisoning": ["poisoning", "overdose", "swallowed poison",
                  "nilason", "nalason", "nakainom ng lason", "nakalunok ng lason"],
    "severe_bleeding": ["severe bleeding", "hemorrhage", "bleeding heavily",
                        "dumudugo", "hindi tumitigil ang dugo",
                        "sobrang dugo", "sumusuka ng dugo"],
    "burns": ["burn", "burned", "scalded", "nasunog", "napaso"],
    "allergic_reaction": ["allergic reaction", "anaphylaxis", "swollen throat",
                          "hives", "allergy", "namamaga ang lalamunan"],
}

EMERGENCY_PRIORITY = ["obstetric", "severe_bleeding", "cardiac", "respiratory",
                      "stroke", "seizure", "poisoning", "burns",
                      "allergic_reaction", "trauma", "pediatric"]

NON_EMERGENCY_KEYWORDS = {
    "fever": ["fever", "feverish", "lagnat", "nilalagnat"],
    "cough_cold": ["cough", "cold", "sore throat", "flu", "ubo", "sipon", "trangkaso"],
    "stomach_ache": ["stomach ache", "abdominal pain", "nausea", "diarrhea",
                     "sakit ng tiyan", "masakit ang tiyan", "pagtatae"],
    "headache": ["headache", "migraine", "sakit ng ulo", "masakit ang ulo"],
    "minor_injury": ["minor injury", "sprain", "scratch",
                     "gasgas", "maliit na sugat", "pilay"],
    "checkup": ["check-up", "checkup", "consultation", "appointment",
                "pacheck-up", "patingin", "konsulta"],
    "vaccination": ["vaccine", "vaccination", "immunization", "bakuna", "turok"],
}
NON_EMERGENCY_PRIORITY = ["fever", "cough_cold", "stomach_ache", "headache",
                          "minor_injury", "checkup", "vaccination"]
NON_EMERGENCY_ADVICE = {
    "fever": dict(en="Rest, fluids, monitor temp.", tl="Magpahinga, uminom ng tubig."),
    "cough_cold": dict(en="Rest, warm fluids.", tl="Magpahinga, mainit-init na tubig."),
    "stomach_ache": dict(en="Hydrate.", tl="Uminom ng tubig."),
    "headache": dict(en="Rest in dark room.", tl="Magpahinga sa madilim na lugar."),
    "minor_injury": dict(en="Clean wound.", tl="Hugasan ang sugat."),
    "checkup": dict(en="Book at barangay health center.", tl="Mag-book sa health center."),
    "vaccination": dict(en="Visit barangay health center.", tl="Pumunta sa health center."),
}

CARE_TYPES = ["Health Center", "Rural Health Unit", "Polyclinic",
              "Birthing Home", "Lying-in Clinic", "Dispensary"]

TAGALOG_MARKERS = {"po","opo","ako","ikaw","siya","kami","tayo","kayo",
                   "ang","mga","ay","at","may","wala","para","kung",
                   "naman","lang","din","rin","dito","doon","hindi","oo","salamat"}
TAGALOG_STRONG = {"buntis","nanganganak","lagnat","ubo","sipon","sakit",
                  "tiyan","ulo","hika","nangingisay","nilason","dumudugo",
                  "nasunog","napaso","bakuna","patingin","konsulta","sugat",
                  "aksidente","nabangga","atake","lolo","lola","anak",
                  "sanggol","bata","po","opo","puso","dibdib","dugo",
                  "malapit","saan","tulong","saklolo"}

EN = {
    "menu_title":   "ResponSMS - Quezon City emergency hospital referral. I find the nearest suitable hospitals for your emergency, by road distance.",
    "emg_header":   "EMERGENCY (reply with the number or the word):",
    "nonemg_header":"NON-EMERGENCY (self-care tips + nearest hospitals):",
    "menu_lines":   ["  1 STROKE           2 TRAUMA",
                     "  3 OBSTETRIC        4 CARDIAC",
                     "  5 PEDIATRIC        6 RESPIRATORY",
                     "  7 SEIZURE          8 POISONING",
                     "  9 SEVERE BLEEDING 10 BURNS",
                     " 11 ALLERGIC REACTION",
                     " 12 LAGNAT (fever)  13 UBO/SIPON",
                     " 14 SAKIT NG TIYAN  15 SAKIT NG ULO",
                     " 16 PILAY/GASGAS    17 CHECK-UP",
                     " 18 BAKUNA"],
    "menu_example": "Example: send 'STROKE near NIP', or send a number (e.g. 1) and then your place. Send 'help' to see this menu again, 'reset' to start over.",
    "menu_place":   "Your place can be a barangay, landmark, street, or coordinates as lat,lon (e.g. 14.6760,121.0437) - I will look it up on the map.",
    "ask_loc":      "Where are you or the patient? Send the barangay, a landmark or street name, or coordinates as lat,lon (e.g. 14.6760,121.0437).",
    "ask_symptom":  "{loc} noted. What is the emergency? Send a number or word: STROKE, TRAUMA, OBSTETRIC, CARDIAC, PEDIATRIC, RESPIRATORY, SEIZURE, POISONING, SEVERE BLEEDING, BURNS or ALLERGIC REACTION. Send 'help' for the full menu.",
    "unknown_loc":  "Could not find that place. Check the spelling, try the barangay name or a well-known landmark with its street, or send coordinates as lat,lon.",
    "oos":          "That location is outside Quezon City, which this service covers. If this is life-threatening, call 911 now.",
    "got_it":       "Got it: {etype}.",
    "headline":     "{etype} near {loc}. Nearest suitable hospitals (by road):",
    "line_emg":     " {i}) {name} L{lvl} ~{dist} km (~{mins} min) {cap}{beds}.",
    "line_emg_beds":", {beds} beds(licensed)",
    "cap":          {"reported":"cap reported","outdated":"cap OUTDATED",
                     "unknown":"cap UNKNOWN","simulated":"cap SIMULATED"},
    "tail":         " Distances by road. Beds shown are LICENSED, NOT confirmed available - call ahead.",
    "fallback":     FALLBACK,
    "no_eligible":  "No hospital in my data meets the service requirements for this emergency. Do not wait for a better match.",
    "nonemg_head":  "{etype} near {loc} - this sounds non-emergency.",
    "nonemg_advice":"Self-care while you arrange care: {advice}",
    "nonemg_fac":   "Nearest hospitals if you need to be seen (distance by road):",
    "line_care":    " {i}) {name} [{ftype}] {barangay} ~{dist} km (~{mins} min).",
    "nonemg_tail":  "Go to the ER right away if symptoms worsen or you develop trouble breathing, chest pain, confusion or fainting.",
    "no_care":      "No nearby clinic found in my data. Visit your barangay health center.",
    "outside_note": " (You are outside Quezon City - these hospitals may be far. Call 911 if life-threatening.)",
    "links_header": "Map links (tap for directions):",
    "cap_legend":   "Key: cap reported = bed count from a valid license; OUTDATED = license expired; UNKNOWN = no bed data; L2/L3 = service level. Beds are licensed, not available.",
}
TL = {
    "menu_title":   "ResponSMS - referral ng ospital sa Quezon City",
    "emg_header":   "EMERGENCY:",
    "nonemg_header":"HINDI EMERGENCY:",
    "menu_lines":   ["  1 STROKE           2 TRAUMA",
                     "  3 OBSTETRIC        4 CARDIAC",
                     "  5 PEDIATRIC        6 RESPIRATORY",
                     "  7 SEIZURE          8 POISONING",
                     "  9 SEVERE BLEEDING 10 BURNS",
                     " 11 ALLERGIC REACTION",
                     " 12 LAGNAT           13 UBO/SIPON",
                     " 14 SAKIT NG TIYAN  15 SAKIT NG ULO",
                     " 16 PILAY/GASGAS    17 CHECK-UP",
                     " 18 BAKUNA"],
    "menu_example": "Halimbawa: STROKE malapit sa NIP, o help",
    "menu_place":   "Pwede ring magpadala ng lugar - hahanapin ko sa Google Maps.",
    "ask_loc":      "Saan? Magpadala ng barangay o lugar.",
    "ask_symptom":  "{loc}. Ano ang emergency? Magpadala ng STROKE, TRAUMA, OBSTETRIC, CARDIAC, PEDIATRIC, etc., o 'help' para sa menu.",
    "unknown_loc":  "Hindi ko makita ang lugar. Magpadala ng barangay, landmark, o lat,lon.",
    "oos":          "Ang lugar ay labas ng Quezon City.",
    "got_it":       "Nakuha ko: {etype}.",
    "headline":     "{etype} malapit sa {loc}. Pinakamalapit (sa kalsada):",
    "line_emg":     " {i}) {name} L{lvl} ~{dist} km (~{mins} min) {cap}{beds}.",
    "line_emg_beds":", {beds} kama (licensed)",
    "cap":          {"reported":"cap reported","outdated":"cap OUTDATED",
                     "unknown":"cap UNKNOWN","simulated":"cap SIMULATED"},
    "tail":         " Tantiya lang. HINDI kumpirmado kung may bakante - tumawag muna.",
    "fallback":     "Kung emergency, tumawag sa 911 o pumunta sa pinakamalapit na ER.",
    "no_eligible":  "Walang angkop na ospital. Huwag maghintay.",
    "nonemg_head":  "{etype} malapit sa {loc} - hindi emergency.",
    "nonemg_advice":"Pangangalaga: {advice}",
    "nonemg_fac":   "Pinakamalapit na pagpapatingin:",
    "line_care":    " {i}) {name} [{ftype}] {barangay} ~{dist} km (~{mins} min).",
    "nonemg_tail":  "Pumunta sa ER kung lumala. Kung emergency, tumawag sa 911.",
    "no_care":      "Walang malapit na klinika. Pumunta sa health center.",
    "outside_note": " (Labas ng QC - tumawag sa 911 kung emergency.)",
    "links_header": "Mga link ng mapa (pindutin para sa direksyon):",
    "cap_legend":   "Susi: cap reported = bilang ng kama mula sa valid na lisensya; OUTDATED = expired ang lisensya; UNKNOWN = walang datos ng kama; L2/L3 = antas ng serbisyo. Licensed na kama, hindi bakante.",
}
LANG = {"en": EN, "tl": TL}

MENU_CHOICES = {
    "1":("emg","stroke"), "2":("emg","trauma"), "3":("emg","obstetric"),
    "4":("emg","cardiac"), "5":("emg","pediatric"), "6":("emg","respiratory"),
    "7":("emg","seizure"), "8":("emg","poisoning"), "9":("emg","severe_bleeding"),
    "10":("emg","burns"), "11":("emg","allergic_reaction"),
    "12":("nonemg","fever"), "13":("nonemg","cough_cold"),
    "14":("nonemg","stomach_ache"), "15":("nonemg","headache"),
    "16":("nonemg","minor_injury"), "17":("nonemg","checkup"),
    "18":("nonemg","vaccination"),
}

GREETINGS = {"help","hi","hello","menu","start","hey","tulong","saklolo",
             "good morning","good afternoon","good evening"}
RESET_WORDS = {"reset","restart","new","clear","ulit"}


# =====================================================================
# 2. SMALL HELPERS (links, text cleaning, hospital-table parsing)
# =====================================================================
def google_maps_link(name, lat, lon):
    q = urllib.parse.quote(f"{name} @{lat:.6f},{lon:.6f}")
    return f"https://maps.google.com/?q={q}"

_SHORT_CACHE = {}
def shorten_url(url, timeout=4):
    if url in _SHORT_CACHE:
        return _SHORT_CACHE[url]
    try:
        api = "https://tinyurl.com/api-create.php?url=" + urllib.parse.quote(url, safe="")
        with urllib.request.urlopen(api, timeout=timeout) as r:
            short_url = r.read().decode().strip()
        if short_url.startswith("http"):
            _SHORT_CACHE[url] = short_url
            return short_url
    except Exception:
        pass
    _SHORT_CACHE[url] = url
    return url

def _clean(s):
    """Fix HTML entities and the '?A' -> N-tilde encoding artifacts in the DOH CSV."""
    s = _html.unescape(str(s))
    s = s.replace("?A", "\u00d1").replace("?a", "\u00f1")
    return s.strip()

def parse_beds(v):
    try: return int(str(v).replace(",", "").strip())
    except (ValueError, TypeError): return 0

def parse_level(v):
    m = re.search(r"Level\s*(\d)", str(v))
    return int(m.group(1)) if m else None

def classify_specialty(name):
    u = str(name).upper()
    for kw, tag in SPECIALTY_BY_NAME.items():
        if kw in u: return tag
    return "general"

def haversine_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * 6371.0088 * math.asin(math.sqrt(a))

def minmax(x):
    x = np.asarray(x, float)
    lo, hi = np.nanmin(x), np.nanmax(x)
    return np.ones_like(x) if hi - lo < 1e-12 else (x - lo) / (hi - lo)


# =====================================================================
# 3. GEOCODER  (Google -> Photon -> Nominatim, best effort)
# =====================================================================
_GEO_CACHE = {}
_NOM_LAST = [0.0]

def _try_google(query):
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        payload = {
            "textQuery": query,
            "maxResultCount": 1,
            "locationBias": {"circle": {
                "center": {"latitude": 14.676, "longitude": 121.0437},
                "radius": 25000.0}},
        }
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": "places.displayName,places.location",
            })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        if data.get("places"):
            p = data["places"][0]
            return {"lat": float(p["location"]["latitude"]),
                    "lon": float(p["location"]["longitude"]),
                    "name": (p.get("displayName") or {}).get("text", query),
                    "source": "google"}
    except Exception:
        pass
    return None

def _try_photon(query):
    try:
        url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode({
            "q": query + " Quezon City", "limit": 1, "lang": "en"})
        req = urllib.request.Request(url, headers={"User-Agent": "ResponSMS/3.2"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        feats = data.get("features", [])
        if not feats:
            return None
        f = feats[0]
        lon, lat = f["geometry"]["coordinates"]
        props = f.get("properties", {})
        name = props.get("name") or props.get("street") or query
        return {"lat": float(lat), "lon": float(lon), "name": name, "source": "photon"}
    except Exception:
        return None

def _try_nominatim(query):
    elapsed = time.time() - _NOM_LAST[0]
    if elapsed < 1.1:                       # Nominatim usage policy: max 1 request/second
        time.sleep(1.1 - elapsed)
    _NOM_LAST[0] = time.time()
    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
            "q": query + ", Quezon City, Philippines",
            "format": "jsonv2", "limit": 1, "countrycodes": "ph"})
        req = urllib.request.Request(url, headers={"User-Agent": "ResponSMS/3.2"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read().decode())
        if rows:
            return {"lat": float(rows[0]["lat"]),
                    "lon": float(rows[0]["lon"]),
                    "name": rows[0].get("display_name", query).split(",")[0],
                    "source": "nominatim"}
    except Exception:
        pass
    return None

def geocode_place(query):
    """Returns {lat, lon, name, source} or None. Misses are not cached."""
    q = query.strip().lower()
    if not q:
        return None
    if q in _GEO_CACHE:
        return _GEO_CACHE[q]
    for name, fn in (("google", _try_google),
                     ("photon", _try_photon),
                     ("nominatim", _try_nominatim)):
        hit = fn(query)
        if hit:
            _GEO_CACHE[q] = hit
            if VERBOSE:
                print(f"  [geo:{name}] '{query}' -> {hit['lat']:.4f},{hit['lon']:.4f}")
            return hit
    return None


# =====================================================================
# 4. DATA BUILD  (run once; produces the three files Store loads)
# =====================================================================
GEO_URL = ("https://raw.githubusercontent.com/faeldon/philippines-json-maps/master/2023/"
           "geojson/municities/hires/bgysubmuns-municity-1381300000.0.1.json")

def find_qc_csv():
    dirs = [d for d in (HERE, Path.cwd(), DATA, HERE / "data", Path("/content"),
                        Path("/mnt/user-data/uploads")) if d.exists()]
    for d in dirs:
        for pat in ("QCFacis*.csv", "*facilit*.csv", "*qc_facis*.csv"):
            hits = sorted(d.glob(pat))
            if hits:
                return hits[0]
    import importlib.util
    if importlib.util.find_spec("google") and importlib.util.find_spec("google.colab"):
        from google.colab import files          # Colab only: ask for an upload
        print(">>> Upload 'QCFacis - Sheet1.csv' now <<<")
        return Path(list(files.upload().keys())[0])
    seen = sorted({c.name for d in dirs[:2] for c in d.glob("*.csv")})
    raise SystemExit(
        "Could not find the DOH facility CSV (looked for QCFacis*.csv).\n"
        f"  Put it in {HERE}\n"
        "  or run:  python sms_hospital_finder.py build /path/to/your.csv\n"
        f"  CSV files I can see in the script folder / current folder: {seen or 'none'}")

def build_data(csv_path=None, regeocode=False):
    """DOH facility CSV -> hospitals_clean.csv + barangays.csv + qc_barangays.geojson."""
    DATA.mkdir(parents=True, exist_ok=True)
    csv_path = Path(csv_path) if csv_path else find_qc_csv()
    print(f"CSV: {csv_path}")
    try:
        raw = pd.read_csv(csv_path)
    except UnicodeDecodeError:           # Excel-saved CSVs are often cp1252
        raw = pd.read_csv(csv_path, encoding="latin-1")
    print(f"Rows: {len(raw)}")

    hosp = raw[raw["Health Facility Type"].astype(str).str.strip() == "Hospital"].copy()
    print(f"Hospitals in CSV: {len(hosp)}")

    hosp["name"]                = hosp["Facility Name"].map(_clean).str.upper()
    hosp["ownership"]           = hosp["Ownership Major Classification"]
    hosp["level"]               = hosp["Service Capability"].map(parse_level)
    hosp["licensed_beds"]       = hosp["Bed Capacity"].map(parse_beds)
    hosp["license_valid_until"] = pd.to_datetime(hosp["License Validity Date"],
                                                 format="%m/%d/%Y", errors="coerce")
    hosp["barangay"]            = hosp["Barangay Name"].map(_clean).str.upper()
    hosp["specialty"]           = hosp["name"].map(classify_specialty)
    hosp["phone"]               = hosp["Landline Number"]
    hosp["full_address"] = (
        hosp["Street Name and #"].map(_clean) + ", " +
        hosp["Building name and #"].map(_clean) + ", " +
        hosp["Barangay Name"].map(_clean) + ", Quezon City, Philippines"
    ).str.replace(r",\s*,", ", ", regex=True).str.replace(r"^,\s*", "", regex=True)
    hosp["uncertainty_km"] = 0.05

    # barangay polygons
    geo_path = DATA / "qc_barangays.geojson"
    if not geo_path.exists():
        print("Downloading barangay polygons...")
        urllib.request.urlretrieve(GEO_URL, geo_path)
    with open(geo_path, encoding="utf-8") as fh:
        gj = json.load(fh)
    brg_rows = []
    for f in gj["features"]:
        geom = shape(f["geometry"])
        cen = geom.centroid
        if not geom.contains(cen):
            cen = geom.representative_point()
        brg_rows.append(dict(barangay=_clean(f["properties"]["adm4_en"]).upper(),
                             lat=cen.y, lon=cen.x))
    barangays = pd.DataFrame(brg_rows).drop_duplicates("barangay")
    print(f"Barangays: {len(barangays)}")

    # geocode hospitals (cached to CSV)
    geo_cache = DATA / "hospitals_geocoded.csv"
    if geo_cache.exists() and not regeocode:
        geo_df = pd.read_csv(geo_cache)
        print(f"Using geocode cache: {len(geo_df)} rows")
    else:
        rows = []
        for i, (_, r) in enumerate(hosp.iterrows(), 1):
            query = f"{r['name']}, {r['full_address']}"
            print(f"[{i}/{len(hosp)}] {r['name'][:55]}")
            hit = geocode_place(query)
            if hit:
                rows.append(dict(name=r["name"], lat=hit["lat"], lon=hit["lon"],
                                 source=hit.get("source", "?"), query=query))
                print(f"    -> {hit['lat']:.5f}, {hit['lon']:.5f} ({hit.get('source')})")
            else:
                rows.append(dict(name=r["name"], lat=None, lon=None, source="miss", query=query))
                print("    -> MISS")
            time.sleep(0.35)
        geo_df = pd.DataFrame(rows)
        geo_df.to_csv(geo_cache, index=False)
        print(f"Cached to {geo_cache}")

    coord_map = {str(r["name"]).upper(): (float(r["lat"]), float(r["lon"]))
                 for _, r in geo_df.iterrows() if pd.notna(r["lat"])}
    hosp["lat"] = hosp["name"].map(lambda n: coord_map.get(n, (None, None))[0])
    hosp["lon"] = hosp["name"].map(lambda n: coord_map.get(n, (None, None))[1])

    missed = hosp["lat"].isna().sum()       # fall back to the barangay centroid
    if missed:
        print(f"WARNING: {missed} hospitals had no geocode hit - using barangay centroid")
        brg_cent = barangays.set_index("barangay")[["lat", "lon"]]
        for idx, r in hosp[hosp["lat"].isna()].iterrows():
            if r["barangay"] in brg_cent.index:
                hosp.at[idx, "lat"] = float(brg_cent.loc[r["barangay"], "lat"])
                hosp.at[idx, "lon"] = float(brg_cent.loc[r["barangay"], "lon"])
    hosp = hosp.dropna(subset=["lat", "lon"]).copy()

    # capacity status: unknown -> outdated (beds listed, license expired) -> reported (license valid)
    hosp["capacity_status"] = "unknown"
    hosp.loc[hosp["licensed_beds"] > 0, "capacity_status"] = "outdated"
    valid_until = pd.to_datetime(hosp["license_valid_until"], errors="coerce")
    hosp.loc[(hosp["licensed_beds"] > 0) & (valid_until >= pd.Timestamp(AS_OF)),
             "capacity_status"] = "reported"

    age = (pd.Timestamp(AS_OF) - valid_until).dt.days
    hosp["freshness"] = (1 - age.clip(lower=0) / FRESH_HORIZON_DAYS).clip(0, 1).fillna(0.0)

    hospitals = hosp[["name", "level", "ownership", "barangay", "specialty",
                      "lat", "lon", "licensed_beds", "capacity_status",
                      "license_valid_until", "freshness", "phone",
                      "uncertainty_km", "full_address"]].copy()
    hospitals.to_csv(DATA / "hospitals_clean.csv", index=False)
    barangays.to_csv(DATA / "barangays.csv", index=False)
    print(f"\nReady: {len(hospitals)} hospitals with coordinates")
    return hospitals, barangays


# =====================================================================
# 5. STORE
# =====================================================================
class Store:
    def __init__(self, data_dir=None):
        d = Path(data_dir) if data_dir else DATA
        if not (d / "hospitals_clean.csv").exists():
            raise FileNotFoundError(
                f"No data in {d}. Put QCFacis - Sheet1.csv next to the script, then run:\n"
                "    python sms_hospital_finder.py build")
        self.hospitals = pd.read_csv(d / "hospitals_clean.csv",
                                     parse_dates=["license_valid_until"])
        self.barangays = pd.read_csv(d / "barangays.csv")
        with open(d / "qc_barangays.geojson", encoding="utf-8") as fh:
            gj = json.load(fh)
        self.qc_poly = unary_union([shape(f["geometry"]) for f in gj["features"]]).buffer(0.002)
        self.last_geo = {}
        self.as_of = AS_OF
        self._gaz = None


# =====================================================================
# 6. LOCATION RESOLUTION
# =====================================================================
def _gazetteer(store):
    if store._gaz is None:
        entries = []
        for _, b in store.barangays.iterrows():
            entries.append((str(b.barangay).lower(), float(b.lat), float(b.lon), "barangay"))
        for _, h in store.hospitals.iterrows():
            entries.append((str(h["name"]).lower(), float(h.lat), float(h.lon), "hospital"))
        store._gaz = entries
    return store._gaz

def _fuzzy_local(text, store, threshold=80):
    t = re.sub(r"\s+", " ", text.lower()).strip()
    gaz = _gazetteer(store)
    for name, lat, lon, kind in gaz:
        if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", t):
            return lat, lon, name.title()
    tokens = t.split()
    grams = [" ".join(tokens[i:i+k])
             for k in (1, 2, 3, 4)
             for i in range(len(tokens) - k + 1)
             if len(" ".join(tokens[i:i+k])) >= 4]
    names = [g[0] for g in gaz]
    best = None
    for gram in grams:
        m = process.extractOne(gram, names, scorer=fuzz.ratio)
        if m and m[1] >= threshold:
            idx = names.index(m[0])
            if best is None or m[1] > best[0]:
                best = (m[1], gaz[idx])
    if best:
        return best[1][1], best[1][2], best[1][0].title()
    return None

def nearest_barangay(lat, lon, store):
    best_name, best_dist = None, float("inf")
    for _, b in store.barangays.iterrows():
        d = haversine_km(lat, lon, float(b.lat), float(b.lon))
        if d < best_dist:
            best_name, best_dist = str(b.barangay), d
    return best_name, best_dist

def _resolve_point(lat, lon, store, label):
    brgy, _ = nearest_barangay(lat, lon, store)
    geo = {"label": label, "nearest_barangay": brgy.title() if brgy else None}
    display = label if label and len(label) <= 25 else (brgy.title() if brgy else label)
    return ("OK", display, lat, lon, geo)

_STOPWORDS = {
    "sa","po","ng","ni","at","near","malapit","dito","ko","namin","kami","ako",
    "si","niya","yung","ung","please","pls","brgy","bgy","barangay",
    "may","mayroon","meron","na","pa","para","din","rin","lang","naman",
    "emergency","tulong","saklolo","help","menu",
}

def _strip_symptoms(text):
    """Remove symptom keywords and filler words so only the place name is left."""
    t = text.lower()
    t = re.sub(r"\([^)]*\)", " ", t)
    all_kws = []
    for kws in EMERGENCY_KEYWORDS.values():
        all_kws.extend(kws)
    for kws in NON_EMERGENCY_KEYWORDS.values():
        all_kws.extend(kws)
    all_kws.sort(key=len, reverse=True)     # longest first so multi-word phrases go before their parts
    for kw in all_kws:
        t = re.sub(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", " ", t)
    t = re.sub(r"[^\w\s.,\-/]", " ", t)
    words = [w for w in t.split()
             if w and w not in _STOPWORDS and (len(w) > 2 or w in ("up", "qc"))]
    return " ".join(words).strip()

def _finish(res, outside):
    geo = res[4] or {}
    geo["outside_qc"] = bool(outside)
    return (res[0], res[1], res[2], res[3], geo)

def resolve_location(text, store):
    """-> (status, display_name, lat, lon, geo).  status is "OK" or "UNKNOWN_LOCATION".
    geo["no_text"] is True when the message held no place text at all (e.g. just "stroke"),
    so callers can tell "no place given" apart from "place given but not found"."""
    coord = re.search(r"(-?\d{1,2}\.\d{3,})\s*[,;\s]\s*(-?\d{2,3}\.\d{3,})", text)
    if coord:
        lat, lon = float(coord.group(1)), float(coord.group(2))
        outside = not store.qc_poly.contains(Point(lon, lat))
        return _finish(_resolve_point(lat, lon, store, f"{lat:.4f},{lon:.4f}"), outside)

    loc_q = _strip_symptoms(text)
    if not loc_q:
        return ("UNKNOWN_LOCATION", None, None, None, {"no_text": True})

    for _, b in store.barangays.iterrows():             # exact barangay
        name_lc = str(b.barangay).lower()
        if re.search(r"(?<![a-z0-9])" + re.escape(name_lc) + r"(?![a-z0-9])", loc_q):
            return _finish(_resolve_point(float(b.lat), float(b.lon), store,
                                          str(b.barangay).title()), False)

    local = _fuzzy_local(loc_q, store, threshold=80)    # typo-tolerant local match
    if local:
        outside = not store.qc_poly.contains(Point(local[1], local[0]))
        return _finish(_resolve_point(local[0], local[1], store, local[2]), outside)

    hit = geocode_place(loc_q)                          # remote chain
    if hit:
        outside = not store.qc_poly.contains(Point(hit["lon"], hit["lat"]))
        return _finish(_resolve_point(hit["lat"], hit["lon"], store, hit["name"]), outside)

    return ("UNKNOWN_LOCATION", None, None, None, {})


# =====================================================================
# 7. RANKING ENGINE
# =====================================================================
OSRM_BASE = "https://router.project-osrm.org"
_ROUTE_CACHE = {}

def road_distances(origin_lat, origin_lon, dests, timeout=8):
    """[(km, minutes), ...] from OSRM, or None if the service is unreachable."""
    if not dests:
        return None
    key = (round(origin_lat, 3), round(origin_lon, 3), len(dests))
    if key in _ROUTE_CACHE:
        return _ROUTE_CACHE[key]
    try:
        coords = f"{origin_lon},{origin_lat}"
        for lat, lon in dests:
            coords += f";{lon},{lat}"
        url = f"{OSRM_BASE}/table/v1/driving/{coords}?sources=0&annotations=distance,duration"
        req = urllib.request.Request(url, headers={"User-Agent": "ResponSMS/3.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        if data.get("code") != "Ok":
            return None
        dist_m = data["distances"][0][1:]
        dur_s = data["durations"][0][1:]
        result = [(d / 1000.0 if d is not None else None,
                   s / 60.0 if s is not None else None)
                  for d, s in zip(dist_m, dur_s)]
        _ROUTE_CACHE[key] = result
        return result
    except Exception:
        return None

def _fill_road(cand, olat, olon):
    """Add road_km / road_min; any missing value falls back to haversine * ROAD_FACTOR."""
    dests = list(zip(cand.lat.astype(float), cand.lon.astype(float)))
    road = road_distances(olat, olon, dests)
    n = len(cand)
    km = np.full(n, np.nan)
    mins = np.full(n, np.nan)
    if road:
        for i, r in enumerate(road):
            if r and r[0] is not None: km[i] = r[0]
            if r and r[1] is not None: mins[i] = r[1]
    fb_km = cand["haversine_km"].values.astype(float) * ROAD_FACTOR
    fb_min = fb_km / ROAD_KMH_FALLBACK * 60
    cand["road_km"] = np.where(np.isnan(km), fb_km, km)         # positional, not index-aligned
    cand["road_min"] = np.where(np.isnan(mins), fb_min, mins)
    return cand

def eligible(h, emergency):
    r = SERVICE_RULES.get(emergency, SERVICE_RULES["stroke"])
    gen = (h["specialty"] == "general") & (h["level"] >= r["min_level"])
    spec = h["specialty"].isin(r["specialty_ok"])
    return h[gen | spec].copy()

def rank(store, lat, lon, emergency, top_k=3):
    w = WEIGHTS.get(emergency, WEIGHTS["default"])
    cand = eligible(store.hospitals, emergency)
    if cand.empty:
        return dict(status="NO_ELIGIBLE", results=[])
    cand["haversine_km"] = [haversine_km(lat, lon, float(a), float(b))
                            for a, b in zip(cand.lat, cand.lon)]
    P = 1 - minmax(cand["haversine_km"])
    Craw = np.log(cand["licensed_beds"].clip(lower=1).values.astype(float))
    known = (cand["capacity_status"] != "unknown").values
    Cs = np.full(len(cand), NEUTRAL_CAPACITY)       # unknown capacity gets a neutral score, never a high one
    if known.any():
        Cs[known] = minmax(Craw[known])
    F = cand["freshness"].values.astype(float)
    cand["score"] = w["cap"] * Cs + w["prox"] * P + w["fresh"] * F
    cand = cand.sort_values(["score", "level", "haversine_km", "name"],
                            ascending=[False, False, True, True])
    top = cand.head(max(top_k * 2, top_k + 2)).copy()   # road distances only for the shortlist
    top = _fill_road(top, lat, lon)
    top = top.sort_values(["score", "level", "road_km", "name"],
                          ascending=[False, False, True, True]).head(top_k)
    out = []
    for _, r in top.iterrows():
        out.append(dict(
            name=str(r["name"]), level=int(r["level"]),
            ownership=str(r["ownership"]),
            barangay=str(r["barangay"]), specialty=str(r["specialty"]),
            lat=float(r["lat"]), lon=float(r["lon"]),
            dist_km=round(float(r["road_km"]), 1),
            dist_min=int(round(float(r["road_min"]))),
            dist_haversine_km=round(float(r["haversine_km"]), 1),
            cap_label=str(r["capacity_status"]),
            licensed_beds=(int(r["licensed_beds"]) if r["capacity_status"] != "unknown" else None),
            score=round(float(r["score"]), 4),
        ))
    return dict(status="OK", results=out)


# =====================================================================
# 8. MESSAGE UNDERSTANDING + REPLY BUILDING
# =====================================================================
def detect_language(text):
    words = set(re.findall(r"[a-z\-]+", (text or "").lower()))
    if words & TAGALOG_STRONG: return "tl"
    if len(words & TAGALOG_MARKERS) >= 1: return "tl"
    return "en"

def _fuzzy_match(text, keyword_list, single=84, multi=82):
    t = (text or "").lower()
    tokens = re.findall(r"[a-z\-]+", t)
    for kw in keyword_list:
        if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", t):
            return True
        parts = kw.split()
        if len(parts) == 1:
            if any(fuzz.ratio(tok, kw) >= single for tok in tokens if len(tok) >= 4):
                return True
        else:
            phrases = [" ".join(tokens[i:i+len(parts)])
                       for i in range(max(0, len(tokens) - len(parts) + 1))]
            if any(fuzz.ratio(ph, kw) >= multi for ph in phrases):
                return True
    return False

def detect_emergency(text):
    hits = [etype for etype, kws in EMERGENCY_KEYWORDS.items() if _fuzzy_match(text, kws)]
    for e in EMERGENCY_PRIORITY:
        if e in hits: return e
    return None

def detect_non_emergency(text):
    hits = [etype for etype, kws in NON_EMERGENCY_KEYWORDS.items() if _fuzzy_match(text, kws)]
    for e in NON_EMERGENCY_PRIORITY:
        if e in hits: return e
    return None

_SHORT_RULES = [
    (r",? (INC|CORPORATION|CORP)\.?$", ""),
    (r"MEDICAL CENTER", "MC"),
    (r"GENERAL HOSPITAL", "GH"),
    (r"HOSPITAL", "HOSP"),
    (r"PHILIPPINE", "PHIL"),
    (r"MEMORIAL", "MEM"),
]
def short(n, k=26):
    for p, s in _SHORT_RULES:
        n = re.sub(p, s, str(n))
    return n[:k].strip()

def build_emergency_reply(emergency, loc, lat, lon, store, lang, outside_qc=False):
    """-> (sms_text, results, status, follow_up_map_links)"""
    T = LANG[lang]
    r = rank(store, lat, lon, emergency, top_k=3)
    if r["status"] != "OK":
        return T["no_eligible"] + " " + T["fallback"], [], "NO_ELIGIBLE", ""

    loc_disp = loc.title() if loc and not str(loc)[0].isdigit() else (loc or "QC")
    head = T["headline"].format(etype=emergency.upper(), loc=loc_disp)

    def line(i, x):
        cap = T["cap"].get(x["cap_label"], x["cap_label"])
        beds = T["line_emg_beds"].format(beds=x["licensed_beds"]) if x["licensed_beds"] else ""
        return T["line_emg"].format(i=i, name=short(x["name"]), lvl=x["level"],
                                    dist=x["dist_km"], mins=x["dist_min"],
                                    cap=cap, beds=beds)

    note = T["outside_note"] if outside_qc else ""      # counted in the fit check so it is never silently dropped
    msg = ""
    for k in (3, 2, 1):                     # drop hospitals until the SMS fits
        body = "".join(line(i + 1, x) for i, x in enumerate(r["results"][:k]))
        msg = head + body + T["tail"] + " " + T["fallback"] + note
        if len(msg) <= SMS_MAX_CHARS: break
    else:
        msg = msg[:SMS_MAX_CHARS]

    links = [T["links_header"]]
    for i, x in enumerate(r["results"][:3], 1):
        raw = google_maps_link(x["name"], x["lat"], x["lon"])
        links.append(f"{i}) {short(x['name'], 18)} {shorten_url(raw)}")
    follow = "\n".join(links)
    with_key = follow + "\n" + T["cap_legend"]
    follow = (with_key if len(with_key) <= SMS_MAX_CHARS else follow)[:SMS_MAX_CHARS]
    return msg, r["results"], "OK", follow

def build_non_emergency_reply(care_type, loc, lat, lon, store, lang):
    T = LANG[lang]
    advice = NON_EMERGENCY_ADVICE.get(care_type, {}).get(lang, "")
    loc_disp = loc.title() if loc and not str(loc)[0].isdigit() else (loc or "QC")
    parts = [
        T["nonemg_head"].format(etype=care_type.replace("_", " ").upper(), loc=loc_disp) + "\n",
        T["nonemg_advice"].format(advice=advice) + "\n",
    ]
    r = rank(store, lat, lon, "stroke", top_k=2)    # nearest capable hospitals as the fallback care option
    if r["status"] == "OK" and r["results"]:
        parts.append(T["nonemg_fac"] + "\n")
        for i, x in enumerate(r["results"], 1):
            parts.append(f" {i}) {short(x['name'])} [{x['barangay'].title()}] ~{x['dist_km']} km\n")
    parts.append(T["nonemg_tail"] + " " + T["fallback"])
    return "".join(parts), [], "OK"


# =====================================================================
# 9. STATELESS ONE-SHOT HANDLER
# =====================================================================
def parse_message(text, store):
    if not text or not text.strip():
        return dict(status="EMPTY")
    e = detect_emergency(text)
    ne = detect_non_emergency(text) if not e else None
    st, loc, lat, lon, geo = resolve_location(text, store)
    if not e and not ne and st != "OK":
        return dict(status="MISSING_EMERGENCY_AND_LOCATION")
    if not e and not ne:
        return dict(status="MISSING_EMERGENCY", location=loc, lat=lat, lon=lon, geo=geo)
    if st != "OK":
        return dict(status="MISSING_LOCATION", emergency=e, non_emergency=ne)
    return dict(status="OK", emergency=e, non_emergency=ne,
                location=loc, lat=lat, lon=lon, geo=geo)

def handle_sms(text, store, use_simulated=False):
    t0 = time.perf_counter()
    results, follow_up = [], ""
    try:
        if store is None:
            raise RuntimeError("hospital DB not available")
        parsed = parse_message(text, store)
        store.last_geo = parsed.get("geo") or {}
        lang = detect_language(text or "")
        status = parsed["status"]
        if status == "OK" and parsed.get("emergency"):
            outside = (parsed.get("geo") or {}).get("outside_qc", False)
            reply, results, final, follow_up = build_emergency_reply(
                parsed["emergency"], parsed["location"],
                parsed["lat"], parsed["lon"], store, lang, outside_qc=outside)
            if final != "OK":
                status = final
        elif status == "OK" and parsed.get("non_emergency"):
            reply, results, _ = build_non_emergency_reply(
                parsed["non_emergency"], parsed["location"],
                parsed["lat"], parsed["lon"], store, lang)
        elif status == "OK":
            reply = LANG[lang]["ask_loc"]
        else:
            prompts = {
                "EMPTY": "No message." if lang == "en" else "Walang mensahe.",
                "MISSING_LOCATION": LANG[lang]["ask_loc"],
                "UNKNOWN_LOCATION": LANG[lang]["unknown_loc"],
                "MISSING_EMERGENCY": LANG[lang]["ask_loc"],
                "MISSING_EMERGENCY_AND_LOCATION": LANG[lang]["ask_loc"],
                "OUT_OF_SCOPE": LANG[lang]["oos"],
                "NO_ELIGIBLE": LANG[lang]["no_eligible"],
                "SYSTEM_ERROR": "System error." if lang == "en" else "May problema.",
            }
            reply = prompts.get(status, "?") + " " + LANG[lang]["fallback"]
    except Exception as ex:
        status, reply, parsed, follow_up = "SYSTEM_ERROR", "SYSTEM ERROR. " + FALLBACK, {"error": str(ex)}, ""
    return dict(status=status, reply=reply, parsed=parsed, results=results,
                follow_up=follow_up, reply_chars=len(reply),
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 2))


# =====================================================================
# 10. MULTI-TURN SESSION
# =====================================================================
def _build_menu(lang):
    T = LANG[lang]
    lines = [T["menu_title"], "", T["emg_header"]]
    lines += T["menu_lines"][:6]
    lines += ["", T["nonemg_header"]]
    lines += T["menu_lines"][6:]
    lines += ["", T["menu_example"], T["menu_place"], "", T["fallback"]]
    return "\n".join(lines)

def _wrap(text, width=58):
    if not width:
        return str(text)
    out = []
    for para in str(text).split("\n"):
        if not para.strip():
            out.append("")
        else:
            out.extend(textwrap.wrap(para, width=width) or [""])
    return "\n".join(out)

class Session:
    """One conversation (one phone number / one browser). wrap_width=None disables hard line wraps."""
    def __init__(self, store, wrap_width=58):
        self.store = store
        self.wrap_width = wrap_width
        self.lang = "tl"
        self.reset()

    def reset(self):
        self.emergency = None
        self.non_emergency = None
        self.coords = None
        self.location = None
        self.geo = {}
        self.last_follow_up = ""
        self.turn = 0

    def reply(self, text):
        self.turn += 1
        return _wrap(text, self.wrap_width)

    def _set_location(self, loc, lat, lon, geo):
        self.coords, self.location, self.geo = (lat, lon), loc, geo or {}
        self.store.last_geo = self.geo

    def send(self, text):
        try:
            return self._send(text)
        except Exception:               # never crash an emergency conversation
            self.last_follow_up = ""
            msg = "System error. " if self.lang == "en" else "May problema. "
            return self.reply(msg + LANG[self.lang]["fallback"])

    def _send(self, text):
        self.last_follow_up = ""
        text = (text or "").strip()
        low = text.lower()
        self.lang = detect_language(text)
        T = LANG[self.lang]

        if low in RESET_WORDS:
            self.reset()
            return self.reply(_build_menu(self.lang))

        if low in GREETINGS or low == "":
            self.reset()
            greet = "Kumusta!" if self.lang == "tl" else "Hi."
            return self.reply(greet + "\n" + _build_menu(self.lang))

        if low in MENU_CHOICES:
            kind, value = MENU_CHOICES[low]
            if kind == "emg":
                self.emergency, self.non_emergency = value, None
            else:
                self.non_emergency, self.emergency = value, None
            if self.coords:
                return self._answer()
            return self.reply(T["got_it"].format(etype=value.upper()) + "\n" + T["ask_loc"])

        e = detect_emergency(text)
        ne = detect_non_emergency(text) if not e else None
        st, loc, lat, lon, geo = resolve_location(text, self.store)
        no_text = bool((geo or {}).get("no_text"))

        # symptom in this message (emergency wins over non-emergency)
        if e or ne:
            if e:
                self.emergency, self.non_emergency, label = e, None, e
            else:
                self.non_emergency, self.emergency, label = ne, None, ne
            if st == "OK":
                self._set_location(loc, lat, lon, geo)
                return self._answer()
            if no_text and self.coords:         # "stroke" alone after a location was given earlier
                return self._answer()
            # a place WAS typed but not found: say so, never silently reuse an old location
            prompt = T["ask_loc"] if no_text else T["unknown_loc"]
            return self.reply(T["got_it"].format(etype=label.upper()) + "\n" + prompt)

        # location only
        if st == "OK":
            self._set_location(loc, lat, lon, geo)
            if self.emergency or self.non_emergency:    # symptom already chosen (e.g. via menu)
                return self._answer()
            return self.reply(T["ask_symptom"].format(loc=loc))

        # neither symptom nor a findable place
        if st == "UNKNOWN_LOCATION":
            pending = self.emergency or self.non_emergency
            if pending:
                prompt = T["ask_loc"] if no_text else T["unknown_loc"]
                return self.reply(T["got_it"].format(etype=pending.upper()) + "\n" + prompt)
            return self.reply(T["unknown_loc"])

        return self.reply(_build_menu(self.lang))

    def _answer(self):
        if self.emergency:
            return self._answer_emergency()
        return self._answer_non_emergency()

    def _answer_emergency(self):
        lat, lon = self.coords
        reply, _, _, follow = build_emergency_reply(
            self.emergency, self.location, lat, lon, self.store, self.lang,
            outside_qc=self.geo.get("outside_qc", False))
        self.last_follow_up = follow
        return self.reply(reply)

    def _answer_non_emergency(self):
        lat, lon = self.coords
        msg = build_non_emergency_reply(self.non_emergency, self.location,
                                        lat, lon, self.store, self.lang)[0]
        return self.reply(msg)


if __name__ == "__main__":
    # `python sms_hospital_finder.py build`  -> build the data files
    # `python sms_hospital_finder.py`        -> console chat
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build_data(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        s = Session(Store())
        print("ResponSMS console. Type 'help'. Ctrl+C to quit.")
        while True:
            try:
                msg = input("> ")
            except (EOFError, KeyboardInterrupt):
                break
            print(s.send(msg))
            if s.last_follow_up:
                print(s.last_follow_up)