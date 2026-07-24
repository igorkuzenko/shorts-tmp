#!/usr/bin/env python3
"""
Instagram- + Facebook-Reels-Publisher über die Meta Graph API.

WARUM DAS: Terminierter Massen-Upload von IG-Reels ist headless über die
Business-Suite-UI nicht möglich (CSP blockt in-page-Fetch/Blob; das CDP-
file_upload-Werkzeug akzeptiert nur Chat-Anhänge; die IG-Content-Publishing-API
kennt kein natives Scheduling). Dieser Publisher umgeht das: er postet SOFORT
über die API und wird per Cron (GitHub Actions) 2x/Tag gefeuert — das ergibt
exakt „2 am Tag" ohne Business-Suite.

Jedes fällige Video geht an BEIDE Flächen:
  - Instagram-Reel  (POST /{ig-id}/media -> /media_publish)
  - Facebook-Reel   (POST /{page-id}/video_reels, start/upload/finish)
FB ist best-effort: schlägt es fehl, wird nur gewarnt, IG bleibt maßgeblich.

Videos sind bereits öffentlich (shorts-tmp), Captions/Reihenfolge kommen aus
jobs-ig-de.json / jobs-ig-en.json. Fortschritt in state-ig.json.

Token (Long-Lived User Token mit instagram_basic, instagram_content_publish,
pages_show_list, pages_read_engagement, business_management, pages_manage_posts):
  export META_TOKEN="EAAB..."   ODER   ~/.gptagency_meta_token

  python3 ig_publisher.py --discover     # Konten + Seiten anzeigen
  python3 ig_publisher.py --dry-run      # zeigen, was als Nächstes dran wäre
  python3 ig_publisher.py --run --n 1    # nächste(s) fällige(s) posten (IG+FB)
"""
import json, os, sys, time, argparse, urllib.request, urllib.parse, urllib.error

GRAPH = "https://graph.facebook.com/v21.0"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state-ig.json")

# Welche Job-Datei zu welchem IG-Handle gehört
ACCOUNTS = {
    "gptagency_dach": os.path.join(HERE, "jobs-ig-de.json"),
    "gptagency":      os.path.join(HERE, "jobs-ig-en.json"),
}


def get_token():
    tok = os.environ.get("META_TOKEN")
    if tok:
        return tok.strip()
    tokfile = os.path.expanduser("~/.gptagency_meta_token")
    if os.path.exists(tokfile):
        return open(tokfile).read().strip()
    sys.exit("FEHLER: Kein Token. Entweder export META_TOKEN=... "
             "oder Token in ~/.gptagency_meta_token schreiben.")


class ApiError(Exception):
    """Graph-API-Fehler. Bewusst eine Exception (kein sys.exit): nur so greift
    der best-effort-Guard um publish_fb_reel — SystemExit erbt von
    BaseException und rutscht durch jedes `except Exception` hindurch."""


def api(path, params=None, method="GET", data=None, token=None):
    tok = token or get_token()
    params = dict(params or {})
    params["access_token"] = tok
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise ApiError(f"API-FEHLER {e.code} bei {method} {path}:\n{detail}")


def accounts_map():
    """handle -> {ig_id, page_id, page_token, page_name} für die verknüpften Konten."""
    pages = api("me/accounts", {"fields": "name,access_token,instagram_business_account{username,id}"})
    m = {}
    for p in pages.get("data", []):
        iga = p.get("instagram_business_account")
        if iga and iga.get("username") in ACCOUNTS:
            m[iga["username"]] = {
                "ig_id": iga["id"], "page_id": p["id"],
                "page_token": p["access_token"], "page_name": p["name"],
            }
    return m


def discover():
    """Zeigt alle Seiten + verbundene IG-Konten (nur Anzeige)."""
    pages = api("me/accounts", {"fields": "name,instagram_business_account{username,id}"})
    print("Gefundene Seiten / IG-Konten:")
    for p in pages.get("data", []):
        iga = p.get("instagram_business_account")
        if iga:
            print(f"  Seite {p['name']!r:40}  IG @{iga['username']}  ig={iga['id']}  page={p['id']}")
        else:
            print(f"  Seite {p['name']!r:40}  (kein IG verbunden)")


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {}


def save_state(s):
    json.dump(s, open(STATE, "w"), ensure_ascii=False, indent=1)


def publish_ig(ig_id, job):
    """IG-Reel: Container erstellen, auf FINISHED warten, veröffentlichen."""
    data = {
        "media_type": "REELS",
        "video_url": job["url"],
        "caption": job["caption"],
        "share_to_feed": "true",
    }
    if job.get("cover"):
        data["cover_url"] = job["cover"]
    cre = api(f"{ig_id}/media", method="POST", data=data)
    cid = cre["id"]
    for _ in range(60):
        st = api(cid, {"fields": "status_code,status"})
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise ApiError(f"IG-Container-Fehler bei {job['datei']}: {st.get('status')}")
        time.sleep(5)
    else:
        raise ApiError(f"IG-Timeout beim Transcoding von {job['datei']}")
    pub = api(f"{ig_id}/media_publish", method="POST", data={"creation_id": cid})
    return pub["id"]


def publish_fb_reel(page_id, page_token, video_url, caption):
    """Facebook-Page-Reel: start -> upload (file_url header) -> finish."""
    start = api(f"{page_id}/video_reels", {"upload_phase": "start"}, method="POST", token=page_token)
    vid = start["video_id"]
    upload_url = start["upload_url"]
    req = urllib.request.Request(upload_url, method="POST")
    req.add_header("Authorization", f"OAuth {page_token}")
    req.add_header("file_url", video_url)
    with urllib.request.urlopen(req, timeout=180) as r:
        r.read()
    api(f"{page_id}/video_reels", {
        "upload_phase": "finish", "video_id": vid,
        "video_state": "PUBLISHED", "description": caption,
    }, method="POST", token=page_token)
    return vid


def run(n, dry, accts, only=None):
    state = load_state()
    failures = []
    for handle, jobs_path in ACCOUNTS.items():
        if only and handle != only:
            continue
        jobs = json.load(open(jobs_path))
        done = set(state.get(handle, []))
        due = [j for j in jobs if j["datei"] not in done][:n]
        if not due:
            print(f"@{handle}: nichts mehr offen ({len(done)}/{len(jobs)} gepostet).")
            continue
        info = accts.get(handle)
        if not info:
            print(f"@{handle}: keine Konto-Info gefunden (discover prüfen) — übersprungen.")
            continue
        for j in due:
            if dry:
                print(f"[DRY] @{handle}  {j['datei']}  -> IG + FB ({info['page_name']})")
                continue
            try:
                mid = publish_ig(info["ig_id"], j)
            except Exception as e:
                # IG ist maßgeblich, aber nur für DIESES Konto: das Video bleibt
                # unverbucht und ist beim nächsten Lauf wieder dran. Das jeweils
                # andere Konto darf davon nicht mit ausfallen.
                print(f"[FEHLER-IG] @{handle}  {j['datei']}: {e}")
                failures.append(handle)
                break
            print(f"[OK-IG]  @{handle}  {j['datei']}  -> media {mid}")
            try:
                fid = publish_fb_reel(info["page_id"], info["page_token"], j["url"], j["caption"])
                print(f"[OK-FB]  @{handle}  {j['datei']}  -> reel {fid}")
            except Exception as e:
                print(f"[WARN-FB] @{handle}  {j['datei']}: {e}")
            state.setdefault(handle, []).append(j["datei"])
            save_state(state)
    if failures:
        # Nach dem Speichern: der Workflow committet den State trotzdem
        # (Persist state laeuft mit if: always()).
        sys.exit(f"IG fehlgeschlagen bei: {', '.join(sorted(set(failures)))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="Konten/Seiten anzeigen")
    ap.add_argument("--dry-run", action="store_true", help="nur zeigen, nichts posten")
    ap.add_argument("--run", action="store_true", help="wirklich posten (IG + FB)")
    ap.add_argument("--n", type=int, default=1, help="wie viele je Konto (Default 1)")
    ap.add_argument("--only", choices=sorted(ACCOUNTS), default=None,
                    help="nur dieses Konto bedienen — zum Angleichen, wenn ein "
                         "Konto nach einem Teilausfall hinterherhinkt")
    a = ap.parse_args()
    if a.discover:
        discover(); return
    accts = accounts_map()
    if a.run:
        run(a.n, dry=False, accts=accts, only=a.only)
    else:
        run(a.n, dry=True, accts=accts, only=a.only)
        print("\n(Nur Vorschau. Für echtes Posten: --run)")


if __name__ == "__main__":
    main()
