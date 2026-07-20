#!/usr/bin/env python3
"""
Instagram-Reels-Publisher über die Meta Graph API.

WARUM DAS: Terminierter Massen-Upload von IG-Reels ist headless über die
Business-Suite-UI nicht möglich (CSP blockt in-page-Fetch/Blob; das CDP-
file_upload-Werkzeug akzeptiert nur Chat-Anhänge; die IG-Content-Publishing-API
kennt kein natives Scheduling). Dieser Publisher umgeht das: er postet SOFORT
über die API und wird per Cron/launchd 2x/Tag (12:00 + 18:00) gefeuert — das
ergibt exakt „2 am Tag" ohne Business-Suite.

Videos sind bereits öffentlich (shorts-tmp), Captions/Reihenfolge kommen aus
jobs-ig-de.json / jobs-ig-en.json. Der Publisher merkt sich in state-ig.json,
was schon gepostet ist, und nimmt beim nächsten Lauf die nächsten dran.

VORAUSSETZUNGEN (einmalig, von Igor):
  1. Meta-App (developers.facebook.com) mit Produkten „Instagram Graph API".
  2. Beide IG-Konten sind Business/Creator-Accounts und mit je einer FB-Seite
     verbunden (gptagency_dach -> Gptagency.io-Seite; gptagency -> EN-Seite).
  3. Ein Long-Lived User Access Token mit Scopes:
        instagram_basic, instagram_content_publish,
        pages_show_list, pages_read_engagement, business_management
     (z. B. über den Graph API Explorer erzeugen, dann in ein Long-Lived Token
     tauschen: GET /oauth/access_token?grant_type=fb_exchange_token...).
  4. Token in die Umgebung legen:  export META_TOKEN="EAAB..."

DANN:
  python3 ig_publisher.py --discover        # zeigt die IG-User-IDs beider Konten
  python3 ig_publisher.py --dry-run         # zeigt, was als Nächstes dran wäre
  python3 ig_publisher.py --run --n 2       # postet die nächsten 2 fälligen (pro Konto)

Cron (2x/Tag, 12:00 + 18:00) z. B.:
  0 12,18 * * *  cd <pfad> && META_TOKEN="EAAB..." python3 ig_publisher.py --run --n 1

Der Publisher postet KEINE Passwörter und macht nichts Irreversibles ohne --run.
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
    # 1) Umgebungsvariable, 2) lokale Token-Datei (von Igor befüllt, nicht im Repo)
    tok = os.environ.get("META_TOKEN")
    if tok:
        return tok.strip()
    tokfile = os.path.expanduser("~/.gptagency_meta_token")
    if os.path.exists(tokfile):
        return open(tokfile).read().strip()
    sys.exit("FEHLER: Kein Token. Entweder export META_TOKEN=... "
             "oder Token in ~/.gptagency_meta_token schreiben.")


def api(path, params=None, method="GET", data=None):
    token = get_token()
    params = dict(params or {})
    params["access_token"] = token
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        sys.exit(f"API-FEHLER {e.code} bei {method} {path}:\n{detail}")


def discover():
    """Listet alle FB-Seiten des Tokens + je verbundenes IG-Konto (Handle + ID)."""
    pages = api("me/accounts", {"fields": "name,instagram_business_account{username,id}"})
    out = {}
    print("Gefundene Seiten / IG-Konten:")
    for p in pages.get("data", []):
        iga = p.get("instagram_business_account")
        if iga:
            print(f"  Seite {p['name']!r:40}  IG @{iga['username']}  id={iga['id']}")
            out[iga["username"]] = iga["id"]
        else:
            print(f"  Seite {p['name']!r:40}  (kein IG verbunden)")
    return out


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {}


def save_state(s):
    json.dump(s, open(STATE, "w"), ensure_ascii=False, indent=1)


def publish_one(ig_id, job):
    """Erstellt einen Reels-Container und veröffentlicht ihn (sofort)."""
    cre = api(f"{ig_id}/media", method="POST", data={
        "media_type": "REELS",
        "video_url": job["url"],
        "caption": job["caption"],
        "share_to_feed": "true",
    })
    cid = cre["id"]
    # Auf FINISHED warten (Video-Transcoding bei Meta)
    for _ in range(60):
        st = api(cid, {"fields": "status_code,status"})
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            sys.exit(f"Container-Fehler bei {job['datei']}: {st.get('status')}")
        time.sleep(5)
    else:
        sys.exit(f"Timeout beim Transcoding von {job['datei']}")
    pub = api(f"{ig_id}/media_publish", method="POST", data={"creation_id": cid})
    return pub["id"]


def run(n, dry, ids):
    state = load_state()
    for handle, jobs_path in ACCOUNTS.items():
        jobs = json.load(open(jobs_path))
        done = set(state.get(handle, []))
        due = [j for j in jobs if j["datei"] not in done][:n]
        if not due:
            print(f"@{handle}: nichts mehr offen ({len(done)}/{len(jobs)} gepostet).")
            continue
        ig_id = ids.get(handle)
        if not ig_id:
            print(f"@{handle}: keine IG-ID gefunden (discover prüfen) — übersprungen.")
            continue
        for j in due:
            if dry:
                print(f"[DRY] @{handle}  {j['datei']}  (URL ok: {j['url'].split('/')[-1]})")
                continue
            mid = publish_one(ig_id, j)
            state.setdefault(handle, []).append(j["datei"])
            save_state(state)
            print(f"[OK]  @{handle}  {j['datei']}  -> media {mid}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="IG-User-IDs anzeigen")
    ap.add_argument("--dry-run", action="store_true", help="nur zeigen, nichts posten")
    ap.add_argument("--run", action="store_true", help="wirklich posten")
    ap.add_argument("--n", type=int, default=1, help="wie viele je Konto (Default 1)")
    a = ap.parse_args()
    if a.discover:
        discover(); return
    ids = discover()
    print()
    if a.run:
        run(a.n, dry=False, ids=ids)
    else:
        run(a.n, dry=True, ids=ids)
        print("\n(Nur Vorschau. Für echtes Posten: --run)")


if __name__ == "__main__":
    main()
