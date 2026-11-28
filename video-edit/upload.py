"""YouTube upload for finished FirstNaz videos (YouTube Data API v3).

  ./edit upload login                    one-time Google sign-in (opens the browser)
  ./edit upload PATH [--dry-run]         upload every out/main.mp4 and out/short.mp4 under PATH

PATH is one project, a series folder, or a whole family (projects/E4). Projects are taken in
folder order, so playlists fill in series order.

What gets uploaded comes from each project's work/youtube.json (Claude writes it; edit freely):
  {"main":  {"title": "...", "description": "...", "tags": [...], "playlist": "E4",
             "privacy": "private", "video_id": null},
   "short": {...same...}}
If it's missing, the title is the folder name ("1 - Know God" -> "Know God") and privacy is private.
out/music-credit.txt is appended to the description automatically.

Each upload writes its video_id back to youtube.json, so a rerun skips what's done. YouTube's daily
API quota allows only a handful of uploads; when it runs out the command stops cleanly: rerun it
tomorrow and it carries on.

Credentials live outside the repo (it's public): ~/.firstnaz-video/youtube/client_secret.json
(from Google Cloud, see SKILL.md) and token.json (written by `login`).
"""
import json, re, sys, time
from pathlib import Path

CRED_DIR = Path.home() / ".firstnaz-video" / "youtube"
SECRET, TOKEN = CRED_DIR / "client_secret.json", CRED_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube"]  # upload + playlists
CATEGORY = "29"  # Nonprofits & Activism
KINDS = {"main": "main.mp4", "short": "short.mp4"}


def _need_libs():
    try:
        import googleapiclient, google_auth_oauthlib  # noqa: F401
    except ImportError:
        sys.exit("YouTube libraries missing. Run: ~/.firstnaz-video/venv/bin/pip install google-api-python-client google-auth-oauthlib")


def login():
    _need_libs()
    from google_auth_oauthlib.flow import InstalledAppFlow
    CRED_DIR.mkdir(parents=True, exist_ok=True); CRED_DIR.chmod(0o700)
    if not SECRET.exists():
        sys.exit(f"Put the OAuth client file from Google Cloud at {SECRET} first (see SKILL.md, YouTube upload).")
    creds = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES).run_local_server(port=0, prompt="consent")
    TOKEN.write_text(creds.to_json()); TOKEN.chmod(0o600)
    print(f"Signed in. Token saved to {TOKEN}")


def service():
    _need_libs()
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    if not TOKEN.exists():
        sys.exit("Not signed in to YouTube. Run: ./edit upload login")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception:
            sys.exit("YouTube sign-in expired (test-mode sign-ins last 7 days). Run: ./edit upload login")
        TOKEN.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _num(p):  # "3 - Evangelism" sorts by its number, then name
    m = re.match(r"\s*(\d+)", p.name)
    return (int(m.group(1)) if m else 10**6, p.name.lower())


def projects(root):
    root = Path(root).resolve()
    if (root / "out").is_dir(): return [root]
    out = []
    for d in sorted((d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")), key=_num):
        out += projects(d) if not (d / "out").is_dir() else [d]
    return out


def default_meta(proj, kind):
    title = re.sub(r"^\s*\d+\s*-\s*", "", proj.name)
    return {"title": title + (" #Shorts" if kind == "short" else ""), "description": "", "tags": [],
            "playlist": None, "privacy": "private", "video_id": None}


def load_meta(proj):
    f = proj / "work" / "youtube.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_meta(proj, meta):
    (proj / "work" / "youtube.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")


def full_description(proj, m):
    d = (m.get("description") or "").strip()
    credit = proj / "out" / "music-credit.txt"
    if credit.exists():
        d = (d + "\n\nMusic: " + credit.read_text().strip()).strip()
    return d


def check_meta(m):
    probs = []
    if not m["title"] or len(m["title"]) > 100: probs.append("title must be 1-100 characters")
    if re.search(r"[<>]", m["title"] + (m.get("description") or "")): probs.append("no < or > allowed in title/description")
    if len(m.get("description") or "") > 4800: probs.append("description too long")
    if m.get("privacy") not in ("private", "unlisted", "public"): probs.append("privacy must be private/unlisted/public")
    return probs


class QuotaHit(Exception): pass


def _call(req):
    from googleapiclient.errors import HttpError
    for attempt in range(5):
        try:
            return req.execute()
        except HttpError as e:
            reason = str(e)
            if "quotaExceeded" in reason or "uploadLimitExceeded" in reason: raise QuotaHit(reason)
            if e.resp.status in (500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt * 3); continue
            raise


def upload_file(yt, path, m, desc):
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
    body = {"snippet": {"title": m["title"], "description": desc, "tags": m.get("tags") or [], "categoryId": CATEGORY},
            "status": {"privacyStatus": m["privacy"], "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False}}
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=False)
    resp, tries = None, 0
    while resp is None:
        try:
            st, resp = req.next_chunk()
            if st: print(f"\r    {int(st.progress() * 100)}%", end="", flush=True)
            tries = 0
        except HttpError as e:
            if "quotaExceeded" in str(e) or "uploadLimitExceeded" in str(e): raise QuotaHit(str(e))
            if e.resp.status in (500, 502, 503, 504) and tries < 5:
                tries += 1; time.sleep(2 ** tries); continue
            raise
        except (ConnectionError, TimeoutError, OSError):
            if tries >= 5: raise
            tries += 1; time.sleep(2 ** tries)
    print("\r    100%")
    return resp["id"]


class Playlists:
    def __init__(self, yt):
        self.yt, self.ids = yt, None

    def get(self, title, privacy):
        if self.ids is None:
            self.ids, tok = {}, None
            while True:
                r = _call(self.yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=tok))
                for p in r.get("items", []): self.ids.setdefault(p["snippet"]["title"], p["id"])
                tok = r.get("nextPageToken")
                if not tok: break
        if title not in self.ids:
            r = _call(self.yt.playlists().insert(part="snippet,status", body={
                "snippet": {"title": title}, "status": {"privacyStatus": privacy}}))
            self.ids[title] = r["id"]; print(f"  made playlist '{title}' ({privacy})")
        return self.ids[title]

    def add(self, pid, vid):
        _call(self.yt.playlistItems().insert(part="snippet", body={
            "snippet": {"playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": vid}}}))


def run(root, dry=False):
    todo = []
    for proj in projects(root):
        meta = load_meta(proj)
        for kind, fname in KINDS.items():
            f = proj / "out" / fname
            if not f.exists(): continue
            m = {**default_meta(proj, kind), **(meta.get(kind) or {})}
            todo.append((proj, kind, f, m))
    if not todo: sys.exit(f"No out/main.mp4 or out/short.mp4 under {root}")
    bad = [(p, k, check_meta(m)) for p, k, _, m in todo if check_meta(m)]
    for p, k, probs in bad: print(f"  {p.name} {k}: {'; '.join(probs)}")
    if bad: sys.exit("Fix work/youtube.json first.")

    pending = [t for t in todo if not t[3].get("video_id") or (t[3].get("playlist") and not t[3].get("in_playlist"))]
    print(f"{len(todo)} videos, {len(todo) - len(pending)} already done, {len(pending)} to go")
    if dry:
        for proj, kind, f, m in pending:
            mb = f.stat().st_size / 1048576
            print(f"  [{m['privacy']}] {m['title']}  ({kind}, {mb:.0f} MB)" + (f"  -> playlist '{m['playlist']}'" if m.get("playlist") else ""))
        return
    yt = service(); pl = Playlists(yt); done = 0
    try:
        for proj, kind, f, m in pending:
            print(f"  {m['title']}  ({kind})")
            if not m.get("video_id"):
                m["video_id"] = upload_file(yt, f, m, full_description(proj, m))
                meta = load_meta(proj); meta[kind] = m; save_meta(proj, meta)
                print(f"    https://youtu.be/{m['video_id']}")
            if m.get("playlist") and not m.get("in_playlist"):
                pl.add(pl.get(m["playlist"], m["privacy"]), m["video_id"])
                m["in_playlist"] = True
                meta = load_meta(proj); meta[kind] = m; save_meta(proj, meta)
            done += 1
    except QuotaHit:
        left = len(pending) - done
        print(f"\nYouTube's daily limit reached after {done} this run; {left} left. Rerun the same command tomorrow to carry on.")
        sys.exit(3)
    print(f"\nDone: {done} uploaded/added this run. Check them in YouTube Studio (they're {', '.join(sorted({t[3]['privacy'] for t in pending}))}).")


def main(args):
    if not args: sys.exit(__doc__)
    if args[0] == "login": return login()
    run(args[0], dry="--dry-run" in args)
