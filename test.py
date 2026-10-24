"""Tests for sms_hospital_finder.py

    python test.py          offline tests on a small built-in fixture (no data files, no network)
    python test.py --live   also runs network checks and tests against your real data
                            (qc_hospital_sms/data/*), if you have built it
"""
import contextlib, json, sys, tempfile, traceback
from pathlib import Path

import pandas as pd

import sms_hospital_finder as shf


# ---------------------------------------------------------------------
# fixture: 3 barangay squares + 6 made-up hospitals
# ---------------------------------------------------------------------
BARANGAYS = [  # name, centroid lat, lon  (each polygon is a 0.02-degree square around it)
    ("PINYAHAN", 14.6495, 121.0370),
    ("BAHAY TORO", 14.6750, 121.0230),
    ("PAYATAS", 14.7200, 121.1000),
]
HOSPITAL_COLS = ["name", "level", "ownership", "barangay", "specialty", "lat", "lon",
                 "licensed_beds", "capacity_status", "license_valid_until", "freshness",
                 "phone", "uncertainty_km", "full_address"]
HOSPITALS = [  # name, level, specialty, lat, lon, beds, status, freshness
    ("ALPHA GENERAL HOSPITAL", 3, "general",   14.6480, 121.0380, 200, "reported", 0.9),
    ("BETA MEDICAL CENTER",    2, "general",   14.6760, 121.0240, 100, "reported", 0.8),
    ("GAMMA HEART CENTER",     3, "cardiac",   14.6400, 121.0450, 150, "reported", 0.7),
    ("DELTA CHILDRENS HOSPITAL", 2, "pediatric", 14.6600, 121.0500, 80, "outdated", 0.2),
    ("EPSILON GENERAL HOSPITAL", 1, "general", 14.7210, 121.1010, 0, "unknown", 0.0),
    ("ZETA GENERAL HOSPITAL",  2, "general",   14.7190, 121.0990, 0, "unknown", 0.0),
]
FAKE_GEO = {  # stands in for Google/Photon/Nominatim during offline tests
    "nip": dict(lat=14.6490, lon=121.0375, name="NIP", source="fake"),
}
CENTER = (14.6495, 121.0370)          # Pinyahan centroid
ORTIGAS = (14.5873, 121.0615)         # outside the fixture's QC polygon


def make_fixture(folder):
    folder = Path(folder)
    feats = []
    for name, lat, lon in BARANGAYS:
        h = 0.01
        ring = [[lon-h, lat-h], [lon+h, lat-h], [lon+h, lat+h], [lon-h, lat+h], [lon-h, lat-h]]
        feats.append({"type": "Feature", "properties": {"adm4_en": name},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}})
    (folder / "qc_barangays.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    pd.DataFrame(BARANGAYS, columns=["barangay", "lat", "lon"]).to_csv(folder / "barangays.csv", index=False)
    rows = []
    for name, lvl, spec, lat, lon, beds, status, fresh in HOSPITALS:
        rows.append([name, lvl, "Private", "PINYAHAN", spec, lat, lon, beds, status,
                     "2027-01-01", fresh, "", 0.05, "fixture"])
    pd.DataFrame(rows, columns=HOSPITAL_COLS).to_csv(folder / "hospitals_clean.csv", index=False)


@contextlib.contextmanager
def offline_patches():
    """No network: fake geocoder, no OSRM (haversine fallback), no URL shortener."""
    saved = (shf.geocode_place, shf.road_distances, shf.shorten_url, shf.VERBOSE)
    shf.geocode_place = lambda q: FAKE_GEO.get(q.strip().lower())
    shf.road_distances = lambda *a, **k: None
    shf.shorten_url = lambda u, timeout=4: u
    shf.VERBOSE = False
    try:
        yield
    finally:
        shf.geocode_place, shf.road_distances, shf.shorten_url, shf.VERBOSE = saved


def flat(s):
    """Collapse the hard line wraps so substring checks don't depend on where lines break."""
    return " ".join(str(s).split())


class Skip(Exception):
    pass


# ---------------------------------------------------------------------
# offline tests  (each takes the fixture store)
# ---------------------------------------------------------------------
def test_detect_emergency_english(store):
    assert shf.detect_emergency("my father had a stroke") == "stroke"
    assert shf.detect_emergency("severe bleeding on his leg") == "severe_bleeding"

def test_detect_emergency_tagalog(store):
    assert shf.detect_emergency("Nabangga ang kapatid ko") == "trauma"
    assert shf.detect_emergency("buntis po ako, nanganganak na") == "obstetric"
    assert shf.detect_emergency("nangingisay si lolo") == "seizure"

def test_detect_emergency_typos(store):
    assert shf.detect_emergency("strok na si papa") == "stroke"
    assert shf.detect_emergency("hert attack") == "cardiac"

def test_emergency_priority(store):
    assert shf.detect_emergency("chest pain and car accident") == "cardiac"   # cardiac outranks trauma

def test_non_emergency(store):
    assert shf.detect_non_emergency("may lagnat ako") == "fever"
    assert shf.detect_emergency("may lagnat ako") is None
    assert shf.detect_non_emergency("kailangan ng bakuna") == "vaccination"

def test_detect_language(store):
    assert shf.detect_language("buntis po ako") == "tl"
    assert shf.detect_language("my mother has chest pain") == "en"

def test_helpers(store):
    assert shf.parse_beds("1,200") == 1200 and shf.parse_beds("n/a") == 0
    assert shf.parse_level("Level 2") == 2 and shf.parse_level("") is None
    assert shf.classify_specialty("PHILIPPINE HEART CENTER") == "cardiac"
    assert shf.classify_specialty("NATIONAL KIDNEY INSTITUTE") == "renal"
    assert shf.classify_specialty("QUEZON CITY GENERAL HOSPITAL") == "general"

def test_haversine(store):
    assert abs(shf.haversine_km(0, 0, 0, 1) - 111.195) < 0.1
    assert shf.haversine_km(14.6, 121.0, 14.6, 121.0) == 0

def test_resolve_coordinates_inside_and_outside(store):
    st, _, lat, lon, geo = shf.resolve_location("stroke 14.6495, 121.0370", store)
    assert st == "OK" and geo["outside_qc"] is False
    st, _, _, _, geo = shf.resolve_location("stroke 14.5873, 121.0615", store)
    assert st == "OK" and geo["outside_qc"] is True

def test_resolve_barangay_exact_and_typo(store):
    st, name, lat, lon, _ = shf.resolve_location("stroke sa pinyahan", store)
    assert st == "OK" and name == "Pinyahan" and abs(lat - 14.6495) < 1e-6
    st, name, *_ = shf.resolve_location("trauma sa pinyhan", store)       # one letter missing
    assert st == "OK" and name == "Pinyahan"

def test_resolve_via_geocoder(store):
    st, name, lat, lon, _ = shf.resolve_location("stroke near NIP", store)
    assert st == "OK" and name == "NIP" and abs(lat - 14.6490) < 1e-6

def test_resolve_flags_missing_vs_unfound_place(store):
    st, *_, geo = shf.resolve_location("stroke", store)
    assert st == "UNKNOWN_LOCATION" and geo.get("no_text") is True        # nothing typed
    st, *_, geo = shf.resolve_location("stroke sa zzzxqplace", store)
    assert st == "UNKNOWN_LOCATION" and not geo.get("no_text")            # typed, not found

def test_rank_stroke_filters_and_orders(store):
    r = shf.rank(store, *CENTER, "stroke", top_k=3)
    names = [x["name"] for x in r["results"]]
    assert names == ["ALPHA GENERAL HOSPITAL", "BETA MEDICAL CENTER", "ZETA GENERAL HOSPITAL"], names
    assert "EPSILON GENERAL HOSPITAL" not in names     # level 1 is below the stroke minimum
    assert "GAMMA HEART CENTER" not in names           # cardiac specialist is not a stroke centre

def test_rank_cardiac_prefers_nearby_specialist(store):
    r = shf.rank(store, 14.6410, 121.0440, "cardiac", top_k=3)
    assert [x["name"] for x in r["results"]] == ["GAMMA HEART CENTER", "ALPHA GENERAL HOSPITAL"]

def test_rank_pediatric_includes_children_hospital(store):
    assert "DELTA CHILDRENS HOSPITAL" in set(shf.eligible(store.hospitals, "pediatric")["name"])
    names = [x["name"] for x in shf.rank(store, 14.6600, 121.0500, "pediatric", top_k=4)["results"]]
    assert "DELTA CHILDRENS HOSPITAL" in names

def test_unknown_capacity_never_looks_verified(store):
    r = shf.rank(store, *CENTER, "stroke", top_k=3)["results"]
    zeta = next(x for x in r if x["name"].startswith("ZETA"))
    assert zeta["cap_label"] == "unknown" and zeta["licensed_beds"] is None
    reply, *_ = shf.build_emergency_reply("stroke", "Pinyahan", *CENTER, store, "en")
    assert "cap UNKNOWN" in reply and "NOT confirmed" in reply

def test_road_fallback_when_routing_is_down(store):
    for x in shf.rank(store, *CENTER, "stroke", top_k=3)["results"]:
        assert x["dist_km"] >= x["dist_haversine_km"] and x["dist_min"] >= 0

def test_sms_length_limit_all_emergencies(store):
    for etype in shf.SERVICE_RULES:
        for lang in ("en", "tl"):
            msg, _, status, _ = shf.build_emergency_reply(etype, "Pinyahan", *CENTER, store, lang)
            assert len(msg) <= shf.SMS_MAX_CHARS, (etype, lang, len(msg))

def test_sms_length_worst_case(store):
    big = store.hospitals.copy()
    big["name"] = "QUEZON CITY GENERAL HOSPITAL ANNEX " + big["name"]
    big["licensed_beds"] = 1200
    big["capacity_status"] = "outdated"
    saved, store.hospitals = store.hospitals, big
    try:
        for etype in shf.SERVICE_RULES:
            msg, *_ = shf.build_emergency_reply(etype, "A" * 25, *CENTER, store, "en")
            assert len(msg) <= shf.SMS_MAX_CHARS and " 2)" in msg, (etype, len(msg))   # at least 2 hospitals
            msg, *_ = shf.build_emergency_reply(etype, "A" * 25, *CENTER, store, "en", outside_qc=True)
            assert len(msg) <= shf.SMS_MAX_CHARS and "outside Quezon City" in msg, (etype, len(msg))  # warning never dropped
    finally:
        store.hospitals = saved

def test_follow_up_has_links_and_capacity_key(store):
    _, _, _, follow = shf.build_emergency_reply("stroke", "Pinyahan", *CENTER, store, "en")
    assert "maps.google.com" in follow and len(follow) <= shf.SMS_MAX_CHARS

def test_outside_qc_note(store):
    sess = shf.Session(store, wrap_width=None)
    reply = sess.send("stroke 14.5873, 121.0615")
    assert "outside Quezon City" in reply

def test_session_location_then_symptom(store):
    s = shf.Session(store)
    assert "emergency" in flat(s.send("nip")).lower()          # asks for the symptom, not the place
    reply = flat(s.send("stroke"))                              # reuses the cached place
    assert "STROKE near Nip" in reply and "1)" in reply
    assert "maps.google.com" in s.last_follow_up

def test_session_menu_then_place(store):
    s = shf.Session(store)
    assert "Where are you" in flat(s.send("1"))
    reply = flat(s.send("pinyahan"))                            # must answer, not ask for a symptom again
    assert "STROKE near Pinyahan" in reply

def test_session_does_not_reuse_old_location_for_unfound_place(store):
    s = shf.Session(store)
    s.send("stroke sa pinyahan")
    reply = flat(s.send("trauma sa zzzxqplace"))
    assert "Could not find that place" in reply and "1)" not in reply

def test_session_non_emergency_tagalog(store):
    reply = flat(shf.Session(store).send("lagnat sa pinyahan")).lower()
    assert "hindi emergency" in reply and "pangangalaga" in reply and "1)" in reply

def test_session_help_and_reset(store):
    s = shf.Session(store)
    assert "ResponSMS" in s.send("help")
    s.send("stroke sa pinyahan")
    s.send("reset")
    assert s.coords is None and s.emergency is None

def test_session_survives_internal_error(store):
    saved = shf.rank
    shf.rank = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        reply = shf.Session(store).send("stroke sa pinyahan")
    finally:
        shf.rank = saved
    assert "911" in reply

def test_handle_sms_statuses(store):
    ok = shf.handle_sms("stroke near pinyahan", store)
    assert ok["status"] == "OK" and ok["results"] and ok["elapsed_ms"] >= 0
    assert shf.handle_sms("stroke", store)["status"] == "MISSING_LOCATION"
    assert shf.handle_sms("", store)["status"] == "EMPTY"
    failed = shf.handle_sms("stroke pinyahan", None)
    assert failed["status"] == "SYSTEM_ERROR" and "911" in failed["reply"]


# ---------------------------------------------------------------------
# live tests (python test.py --live)
# ---------------------------------------------------------------------
def live_photon(_):
    import urllib.parse, urllib.request
    hits, errors = 0, 0
    for q in ["Ateneo de Manila University Quezon City", "Ortigas Center Pasig",
              "Institute of Chemistry UP Diliman"]:
        url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode({"q": q, "limit": 1})
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "test/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                feats = json.loads(r.read().decode()).get("features", [])
            if feats:
                lon, lat = feats[0]["geometry"]["coordinates"]
                print(f"      {q[:45]:<45} -> {lat:.4f},{lon:.4f}  {feats[0]['properties'].get('name', '?')}")
                hits += 1
            else:
                print(f"      {q[:45]:<45} -> no hits")
        except Exception as e:
            errors += 1
            print(f"      {q[:45]:<45} -> ERR: {e}")
    if errors == 3:
        raise Skip("Photon unreachable (no network?)")
    assert hits >= 1

def _real_store():
    if not (shf.DATA / "hospitals_clean.csv").exists():
        raise Skip(f"no real data in {shf.DATA} (run: python sms_hospital_finder.py build)")
    return shf.Store()

def live_real_data_every_service_has_a_hospital(_):
    real = _real_store()
    assert len(real.hospitals) > 0 and len(real.barangays) > 0
    for etype in shf.SERVICE_RULES:
        assert shf.rank(real, 14.676, 121.0437, etype)["status"] == "OK", f"no eligible hospital for {etype}"

def live_real_data_nip_then_stroke(_):
    s = shf.Session(_real_store())
    s.send("nip")
    reply = flat(s.send("stroke"))
    assert "STROKE" in reply and "1)" in reply, reply


# ---------------------------------------------------------------------
def run(tests, store, patched):
    ok = fail = skipped = 0
    for fn in tests:
        label = fn.__name__.replace("_", " ")
        try:
            with (offline_patches() if patched else contextlib.nullcontext()):
                fn(store)
            print(f"[PASS] {label}"); ok += 1
        except Skip as s:
            print(f"[SKIP] {label}: {s}"); skipped += 1
        except Exception:
            print(f"[FAIL] {label}"); traceback.print_exc(limit=3); fail += 1
    return ok, fail, skipped


if __name__ == "__main__":
    offline = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    offline.sort(key=lambda f: f.__code__.co_firstlineno)
    with tempfile.TemporaryDirectory() as tmp:
        make_fixture(tmp)
        fixture_store = shf.Store(tmp)
        ok, fail, skipped = run(offline, fixture_store, patched=True)
    if "--live" in sys.argv:
        live = [v for k, v in globals().items() if k.startswith("live_")]
        live.sort(key=lambda f: f.__code__.co_firstlineno)
        print("\n--- live ---")
        o, f, s = run(live, None, patched=False)
        ok, fail, skipped = ok + o, fail + f, skipped + s
    print(f"\n{ok} passed, {fail} failed, {skipped} skipped")
    sys.exit(1 if fail else 0)  