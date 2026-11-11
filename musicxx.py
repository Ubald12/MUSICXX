#!/usr/bin/env python3
"""MUSICXX: a local chat bot. Paste Spotify / YouTube / SoundCloud links (songs or playlists),
get .wav files in Desktop/OK. Run: python3 musicxx.py   (self-check: python3 musicxx.py --test)"""
import csv, glob, html, importlib.util, io, itertools, json, os, queue, re, subprocess, sys, threading, time, webbrowser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote_plus, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
WINDOWS = os.name == "nt"
# Windows often moves the visible Desktop into OneDrive
_onedrive = Path.home() / "OneDrive" / "Desktop"
OUT = (_onedrive if WINDOWS and _onedrive.is_dir() else Path.home() / "Desktop") / "OK"  # every song lands here, flat
LOGO = Path(__file__).with_name("logo.png")
# Tools: this Python's own bin/Scripts folder (installer setup), the ffmpeg spotdl downloads, then Homebrew/pipx
os.environ["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), str(Path.home() / ".spotdl"),
                                      f"{Path.home()}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin",
                                      os.environ.get("PATH", "")])
os.environ.update(NO_COLOR="1", TERM="dumb", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")


def tool_python(module, fallback):
    """This Python if it has `module` (installer setup), else the interpreter of the original Mac setup."""
    return Path(sys.executable) if importlib.util.find_spec(module) else fallback
jobs, ids = [], itertools.count(1)


CHARTS = {  # key: (name, Shazam chart CSVs, merged in order). Shazam updates these weekly.
    "rnb": ("R&B", ["genre/world/randb-soul"]),
    "afro": ("Afrobeats", ["genre/world/afrobeats", "top-200/nigeria"]),  # afrobeats chart has 50; Nigeria's chart fills to 100
    "rap": ("US Rap", ["genre/united-states/hip-hop-rap"]),
}
chart_cache = {}


def chart(key):
    """Top 100 [{artist, title}] for a chart, cached for an hour. Empty list if Shazam is unreachable."""
    if key in chart_cache and time.time() - chart_cache[key][0] < 3600:
        return chart_cache[key][1]
    songs, seen = [], set()
    for path in CHARTS[key][1]:
        text = subprocess.run(["curl", "-sfL", "--max-time", "20", "-A", "Mozilla/5.0",
                               f"https://www.shazam.com/services/charts/csv/{path}"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        for row in csv.reader(io.StringIO(text)):
            if len(row) == 3 and row[0].isdigit() and (row[1], row[2]) not in seen:
                seen.add((row[1], row[2]))
                songs.append({"artist": row[1], "title": row[2]})
    songs = songs[:100]
    if songs:
        chart_cache[key] = (time.time(), songs)
    return songs


# Spotify search runs inside spotdl's own Python, which ships the "spotapi" library spotdl uses
SPOTDL_PY = tool_python("spotapi", Path.home() / "Library/Application Support/pipx/venvs/spotdl/bin/python")
SPOTIFY_SEARCH = r'''
import json, sys
from spotapi import Song
out = []
for it in Song().query_songs(sys.argv[1], limit=20)["data"]["searchV2"]["tracksV2"]["items"]:
    d = it["item"]["data"]
    art = d["albumOfTrack"]["coverArt"]["sources"]
    out.append({"url": "https://open.spotify.com/track/" + d["uri"].split(":")[-1], "title": d["name"],
                "artist": ", ".join(a["profile"]["name"] for a in d["artists"]["items"]),
                "duration": d["duration"]["totalMilliseconds"] / 1000, "thumb": art[0]["url"] if art else ""})
print(json.dumps(out))
'''


def search(platform, q):
    """Up to 20 results [{url, title, artist, duration, thumb}] from Spotify, YouTube or SoundCloud."""
    if platform == "spotify":
        # the query goes in argv after -c, so it can never be read as a Python option
        r = subprocess.run([str(SPOTDL_PY), "-I", "-c", SPOTIFY_SEARCH, q], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
        return json.loads(r.stdout or "[]")
    # SoundCloud: the plain search buries edits under the original, so also search "<q> edit" / "<q> remix" and merge
    terms = (["ytsearch20:" + q] if platform == "youtube" else
             ["scsearch40:" + q, f"scsearch25:{q} edit", f"scsearch25:{q} remix"])
    with ThreadPoolExecutor(len(terms)) as pool:
        runs = pool.map(lambda t: subprocess.run(["yt-dlp", "--flat-playlist", "-J", t],
                                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40).stdout, terms)
        entries = [e for out in runs for e in (json.loads(out or "{}").get("entries") or [])]
    out, seen = [], set()
    for e in entries:
        url = e.get("webpage_url") or e.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        thumbs = e.get("thumbnails") or []
        thumb = (f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg" if platform == "youtube" else
                 next((t["url"] for t in thumbs if t.get("id") == "t300x300"), thumbs[-1]["url"] if thumbs else ""))
        out.append({"url": url, "title": e.get("title") or "", "artist": e.get("channel") or e.get("uploader") or "",
                    "duration": e.get("duration"), "thumb": thumb, "plays": e.get("view_count"),
                    "likes": e.get("like_count"), "date": e.get("timestamp")})
    if platform == "soundcloud":  # the extra "edit"/"remix" searches also match unrelated edits: keep titles with every word
        words = re.findall(r"\w+", q.lower())
        out = [r for r in out if all(w in f"{r['title']} {r['artist']}".lower() for w in words)] or out
    return out


def spotdl(*queries):
    """spotdl takes Spotify links or plain "Artist - Title" searches."""
    return ["spotdl", "download", *queries, "--format", "wav", "--simple-tui",
            "--output", f"{OUT}/{{artists}} - {{title}}.{{output-ext}}"]


def command(url):
    """Downloader command for a link, or None if it isn't one."""
    if "spotify.com/" in url or url.startswith("spotify:"):
        return spotdl(url)
    if url.startswith(("http://", "https://")):
        return ytdlp(url)
    return None


def ytdlp(url, name="%(uploader)s - %(title)s", *extra):
    # --no-playlist: a song link that also carries &list= grabs only the song; pure playlist/set links still get everything
    return ["yt-dlp", "-x", "--audio-format", "wav", "--no-playlist", "--newline", "--ignore-errors",
            "-P", str(OUT), "-o", f"{name}.%(ext)s", *extra, url]


def fetch(url):
    return subprocess.run(["curl", "-sfL", "--max-time", "20", "-A", "Mozilla/5.0", url],
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def can_fallback(url, log):
    """Single songs we can re-find on YouTube Music when the normal download fails. SoundCloud only when the
    track is DRM-locked (official releases): for anything else, e.g. a DJ edit, YouTube would give the wrong version."""
    return "spotify.com/track/" in url or ("soundcloud.com/" in url and "/sets/" not in url and "DRM" in log)


def fallback(url):
    """spotdl can't fetch some Spotify tracks ("Could not get song info") and big-label SoundCloud tracks are
    DRM-locked. Get artist + title from the public page / oEmbed instead and take the top YouTube Music
    song result. None if no title is found."""
    if "soundcloud.com/" in url:
        info = json.loads(fetch("https://soundcloud.com/oembed?format=json&url=" + quote_plus(url)) or "{}")
        artist, title = info.get("author_name", ""), info.get("title", "")
        title = title[: -len(" by " + artist)] if artist and title.endswith(" by " + artist) else title
    else:
        page = fetch(url)
        meta = dict(re.findall(r'<meta (?:property|name)="(og:title|music:musician_description)" content="([^"]*)"', page))
        artist, title = html.unescape(meta.get("music:musician_description", "")), html.unescape(meta.get("og:title", ""))
    if not title:
        return None
    name = f"{artist} - {title}" if artist else title
    search = "https://music.youtube.com/search?q=" + quote_plus(f"{artist} {title}") + "#songs"
    # yt-dlp output templates treat % specially and / as a folder
    return ytdlp(search, name.replace("%", "%%").replace("/", "-"), "--playlist-items", "1")


SONG = re.compile(r'\[ExtractAudio\] Destination: (.+)|Downloaded "(.+)":|Skipping (.+) \(file already exists\)')
TOTAL = re.compile(r"Downloading item \d+ of (\d+)|^\d+/(\d+) complete")
PCT = re.compile(r"^\[download\]\s+([\d.]+)%")


def parse(job, line):
    """Update a job's progress from one line of yt-dlp / spotdl output."""
    job["log"] = line[-300:]
    m = TOTAL.search(line)
    if m:
        job["total"] = int(m.group(1) or m.group(2))
    m = PCT.search(line)
    if m:
        job["pct"] = float(m.group(1))
    m = SONG.search(line)  # spotdl "already exists" counts too, so a re-sent playlist doesn't report "0 saved"
    if m:
        name = Path(m.group(1)).stem if m.group(1) else m.group(2) or m.group(3)
        job["songs"].append(name)
        job["saved"], job["pct"] = len(job["songs"]), 0
        return name, m.group(1)  # (song, file path if the tool printed one)


# ---- key / BPM / genre analysis, one song at a time in the background
VENV_PY = tool_python("essentia", Path.home() / "Library/Application Support/MUSICXX/venv/bin/python")
ANALYZER = Path(__file__).with_name("analyze.py")
analysis, info_lock = queue.Queue(), threading.Lock()


def set_info(job, name, **values):
    # replace the dict instead of mutating it, so /jobs can serialise it from another thread mid-update
    with info_lock:
        job["info"] = {**job["info"], name: {**job["info"].get(name, {}), **values}}


def queue_analysis(job, name, path):
    set_info(job, name, state="queued")
    analysis.put((job, name, path))


def song_file(name, path):
    """The .wav for a reported song. spotdl reports 'First Artist - Title' but names the file with all artists."""
    for p in ([Path(path)] if path else []) + [OUT / f"{name}.wav"]:
        if p.is_file():
            return p
    title = name.split(" - ", 1)[-1]
    hits = sorted(OUT.glob("*" + glob.escape(f" - {title}.wav")), key=lambda f: f.stat().st_mtime)
    return hits[-1] if hits else None


def genre(name):
    """Primary genre from Apple's catalog (iTunes Search API); retries with only the first artist."""
    clean = re.sub(r"[(\[][^)\]]*[)\]]", "", name).strip()  # drop "(feat. …)", "[Official Video]"
    artist, _, title = clean.partition(" - ")
    terms = [f"{artist} {title}", f"{re.split(r',|&| x | feat', artist)[0]} {title}"] if title else [clean]
    for term in terms:
        try:
            hits = json.loads(fetch("https://itunes.apple.com/search?entity=song&limit=1&term=" + quote_plus(term)) or "{}")
        except ValueError:
            continue
        if hits.get("results"):
            return hits["results"][0].get("primaryGenreName", "")
    return ""


def analyzer():
    while True:
        job, name, path = analysis.get()
        set_info(job, name, state="analyzing")
        result = {}
        try:
            f = song_file(name, path)
            r = subprocess.run([str(VENV_PY), "-I", str(ANALYZER), str(f)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            result = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:  # background worker: one bad file must not stop analysis of the rest
            pass
        set_info(job, name, state="done", genre=genre(name), **result)


def execute(job, cmd):
    """Run one downloader command, feeding its output into the job. Returns the exit code."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        job["log"] = f"Missing tool: {e.filename}"
        return 1
    later = []
    for line in p.stdout:
        if line.strip():
            found = parse(job, line.strip())
            # spotdl reports a song once its file is finished; yt-dlp names the file *before* converting it
            if found and found[1]:
                later.append(found)
            elif found:
                queue_analysis(job, *found)
    code = p.wait()
    for found in later:
        queue_analysis(job, *found)
    return code


def run(job):
    code = execute(job, job["cmd"])
    if not job["saved"] and can_fallback(job["url"], job["log"]):
        job["log"] = f"{'Spotify' if 'spotify' in job['url'] else 'SoundCloud'} won't give this song. Getting it from YouTube Music…"
        cmd = fallback(job["url"])
        code = execute(job, cmd) if cmd else 1
    job["status"] = "done" if code == 0 or job["saved"] else "failed"


def reveal(song=None):
    """Show a song (or the OK folder) in Finder / File Explorer / the Linux file manager."""
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(song)] if song else ["open", str(OUT)])
    elif WINDOWS:
        subprocess.run(["explorer", f"/select,{song}"] if song else ["explorer", str(OUT)])
    else:
        subprocess.run(["xdg-open", str(song.parent if song else OUT)])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.send(PAGE, "text/html; charset=utf-8")
        elif self.path == "/jobs":
            self.send(json.dumps([{k: v for k, v in j.items() if k != "cmd"} for j in jobs]))
        elif self.path.startswith("/search?"):
            qs = parse_qs(urlsplit(self.path).query)
            platform, q = qs.get("p", [""])[0], qs.get("q", [""])[0].strip()[:200]
            if platform not in ("spotify", "youtube", "soundcloud") or not q:
                return self.send('{"error":"bad search"}', code=400)
            try:
                self.send(json.dumps(search(platform, q)))
            except (subprocess.TimeoutExpired, ValueError, KeyError, OSError):
                self.send('{"error":"search failed"}', code=502)
        elif self.path.startswith("/chart/") and self.path[7:] in CHARTS:
            key = self.path[7:]
            songs = chart(key)
            self.send(json.dumps({"name": CHARTS[key][0], "songs": songs}), code=200 if songs else 502)
        elif self.path == "/logo.png" and LOGO.is_file():
            self.send(LOGO.read_bytes(), "image/png")
        else:
            self.send("{}", code=404)

    def do_POST(self):
        # JSON-only: a random website can't send this cross-origin without a preflight we never answer
        if self.headers.get("Content-Type") != "application/json":
            return self.send('{"error":"json only"}', code=415)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            return self.send('{"error":"bad request"}', code=400)
        if self.path == "/quit":
            self.send("{}")
            return threading.Thread(target=server.shutdown).start()
        if self.path == "/open":
            # Path(...).name strips any directories, so this can only point inside OUT
            song = OUT / (Path(str(body.get("name", ""))).name + ".wav")
            reveal(song if body.get("name") and song.is_file() else None)
            return self.send("{}")
        if self.path != "/download":
            return self.send("{}", code=404)
        if body.get("chart") in CHARTS:
            # songs come from our own cached chart, never from the request, so nothing untrusted reaches spotdl
            name, songs = CHARTS[body["chart"]][0], chart(body["chart"])
            ranks = body.get("ranks") or range(1, len(songs) + 1)
            picked = [(r, songs[r - 1]) for r in ranks if isinstance(r, int) and 1 <= r <= len(songs)]
            if not picked:
                return self.send('{"error":"chart unavailable"}', code=502)
            label = (f"Top {len(picked)} {name}" if len(picked) > 1 else
                     f"{name} #{picked[0][0]}: {picked[0][1]['artist']} - {picked[0][1]['title']}")
            job = {"id": next(ids), "url": label, "chart": True, "status": "running", "saved": 0, "songs": [],
                   "total": len(picked), "pct": 0, "info": {}, "log": "Finding songs…",
                   "cmd": spotdl(*(f"{s['artist']} - {s['title']}".lstrip("-") for _, s in picked))}
            threading.Thread(target=run, args=(job,), daemon=True).start()
            jobs.append(job)
            return self.send(json.dumps([job["id"]]))
        added = []
        for url in str(body.get("text", "")).split():
            job = {"id": next(ids), "url": url, "cmd": command(url), "status": "running",
                   "saved": 0, "songs": [], "total": 0, "pct": 0, "info": {}, "log": "Starting…"}
            if not job["cmd"]:
                job.update(status="failed", log="That's not a link I can download.")
            else:
                threading.Thread(target=run, args=(job,), daemon=True).start()
            jobs.append(job)
            added.append(job["id"])
        self.send(json.dumps(added))


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MUSICXX</title><link rel="icon" href="/logo.png"><style>
:root{--bg:#1c1c1e;--bg2:#2c2c2e;--panel:#242426;--hover:#323234;--line:#38383a;--text:#f5f5f7;--muted:#98989d;
--red:#e0362c;--red2:#e0362c;--ok:#30d158;--bad:#ff453a;--sp:#1db954;--yt:#ff3b30;--sc:#ff7700;--shadow:0 4px 16px rgba(0,0,0,.35)}
@media (prefers-color-scheme:light){:root{--bg:#f5f5f7;--bg2:#e8e8ed;--panel:#fff;--hover:#f0f0f2;--line:#d9d9de;--text:#1d1d1f;--muted:#6e6e73;--ok:#1f9d48;--bad:#d70015;--shadow:0 2px 10px rgba(0,0,0,.08)}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;color:var(--text);background:var(--bg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;display:flex;flex-direction:column}
button{font:inherit;border:0;cursor:pointer;color:inherit;background:none}
button:focus-visible,textarea:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--red);outline-offset:2px}
svg.i{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;vertical-align:-3px}
header{display:flex;align-items:center;gap:12px;padding:10px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
header img{width:26px;height:auto}
.brand{flex:1;line-height:1.15}.brand b{font-size:15px;letter-spacing:.5px}.brand small{display:block;color:var(--muted);font-size:12px}
.ghost{display:inline-flex;align-items:center;gap:6px;padding:7px 10px;border-radius:6px;color:var(--muted);font-size:13px}.ghost:hover{background:var(--hover);color:var(--text)}
main{flex:1;overflow-y:auto}
#chat{max-width:760px;margin:0 auto;padding:24px 18px 30px;display:flex;flex-direction:column;gap:12px}
#hero{text-align:center;padding:8vh 10px 10px}
#hero img{width:84px}
#hero h1{font-size:24px;font-weight:600;margin:18px 0 6px}#hero p{color:var(--muted);margin:0 0 16px}
.chips{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:5px;font-size:12px;font-weight:500;background:var(--bg2);color:var(--text)}
.chip i{width:7px;height:7px;border-radius:50%}.sp i{background:var(--sp)}.yt i{background:var(--yt)}.sc i{background:var(--sc)}.ot{color:var(--muted)}
.row{display:flex;gap:10px;animation:fade .2s ease-out both}@keyframes fade{from{opacity:0}}
.av{width:24px;height:auto;align-self:flex-start;margin-top:12px}
.card{flex:0 1 580px;min-width:0;padding:14px 16px;border-radius:10px;background:var(--panel);border:1px solid var(--line)}
.status{display:flex;align-items:center;gap:8px;font-weight:600}
.spin{width:14px;height:14px;border-radius:50%;border:2px solid var(--line);border-top-color:var(--muted);animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(1turn)}}
.done{color:var(--ok);display:inline-flex;align-items:center;gap:6px}.failed{color:var(--bad)}
.check{display:inline-grid;place-items:center;width:16px;height:16px;border-radius:50%;background:var(--ok);color:#fff;font-size:10px}
.bar{height:4px;border-radius:2px;background:var(--bg2);margin:10px 0 6px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--red);transition:width .5s}
.bar.indef i{width:30%!important;animation:indef 1.2s ease-in-out infinite}@keyframes indef{from{transform:translateX(-100%)}to{transform:translateX(330%)}}
.meta{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.songs{list-style:none;margin:10px 0 0;padding:0;display:grid;border-top:1px solid var(--line)}
.songs li{border-bottom:1px solid var(--line)}
.songs li button{width:100%;text-align:left;display:flex;align-items:center;gap:8px;padding:7px 4px;font-size:13.5px}
.songs li button:hover{background:var(--hover)}.songs li button span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.songs li button em{font-style:normal;font-size:12px;color:var(--muted);opacity:0}.songs li button:hover em{opacity:1}
.src{margin-bottom:8px}
.tags{display:flex;flex-wrap:wrap;gap:5px;padding:0 4px 8px 28px}
.tag{font-size:11.5px;font-weight:500;padding:1px 7px;border-radius:4px;background:var(--bg2);color:var(--text)}
.tag.cam{color:#fff;background:hsl(var(--h) 45% 42%);font-weight:600}
.tag.gen{color:var(--muted)}.tag.wait{color:var(--muted);background:none;padding-left:0}
.note{color:var(--muted);font-size:12px}
.more{font-size:12.5px;color:var(--red);padding:6px 4px}
.actions{display:flex;gap:8px;margin-top:10px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;border-radius:6px;font-size:13px;font-weight:500;background:var(--bg2);color:var(--text)}.pill:hover{background:var(--hover);box-shadow:inset 0 0 0 1px var(--line)}
footer{padding:10px 18px 16px;border-top:1px solid var(--line);background:var(--panel)}
#f{max-width:760px;margin:0 auto;display:flex;align-items:center;gap:8px;padding:5px;border-radius:8px;background:var(--bg);border:1px solid var(--line)}
#f:focus-within{border-color:var(--muted)}
textarea{flex:1;font:inherit;border:0;background:none;color:var(--text);resize:none;padding:8px 6px;max-height:120px;height:36px;outline:none!important}
#detect{margin-left:4px}#detect:empty{display:none}
.send{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 14px;border-radius:6px;background:var(--red);color:#fff;font-weight:600;flex:none}
.send:hover{filter:brightness(1.08)}.send:disabled{opacity:.4;cursor:default;filter:none}
.hint{max-width:760px;margin:6px auto 0;text-align:center;color:var(--muted);font-size:11.5px}
#drop{position:fixed;inset:0;z-index:9;display:none;place-items:center;background:rgba(0,0,0,.6);font-size:18px;font-weight:600;color:#fff}
#drop div{padding:36px 56px;border:2px dashed #fff;border-radius:12px}
body.dragging #drop{display:grid}
nav{display:flex;gap:2px;padding:2px;border-radius:8px;background:var(--bg2)}
nav button{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:6px;font-size:13px;font-weight:500;color:var(--muted)}
nav button:hover{color:var(--text)}nav button.on{background:var(--panel);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.2)}
nav sup{padding:0 6px;border-radius:9px;background:var(--red);color:#fff;font-size:11px;line-height:16px}nav sup:empty{display:none}
.ch{color:var(--text)}
#charts{display:none;flex:1;overflow-y:auto}body.charts #charts{display:block}body.charts #main,body.charts footer{display:none}
.wrap{max-width:760px;margin:0 auto;padding:24px 18px 40px}
.genres{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.genre{min-height:84px;padding:14px 16px;border-radius:10px;text-align:left;display:flex;flex-direction:column;justify-content:flex-end;background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--c)}
.genre:hover{background:var(--hover)}.genre.on{box-shadow:inset 0 0 0 1px var(--c);background:var(--hover)}
.genre b{font-size:17px;font-weight:600}.genre small{color:var(--muted);font-size:12.5px}
.genre[data-k=rnb]{--c:#8e5cd9}.genre[data-k=afro]{--c:#2fa45a}.genre[data-k=rap]{--c:#3d6ed8}
.chead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:24px 0 12px}
.chead h2{margin:0;font-size:19px;font-weight:600}.chead small{display:block;color:var(--muted);font-size:12px;font-weight:400}
.big{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:6px;background:var(--red);color:#fff;font-weight:600;white-space:nowrap}.big:hover{filter:brightness(1.08)}
.tracks{list-style:none;margin:0;padding:0;border-radius:10px;overflow:hidden;border:1px solid var(--line);background:var(--panel)}
.tracks li{display:flex;align-items:center;gap:12px;padding:8px 14px;border-top:1px solid var(--line)}
.tracks li:first-child{border-top:0}.tracks li:hover{background:var(--hover)}
.rank{width:28px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.ti{flex:1;min-width:0}.ti b,.ti span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ti b{font-weight:500}.ti span{color:var(--muted);font-size:13px}
.dl,.ico{width:30px;height:30px;border-radius:6px;display:grid;place-items:center;flex:none;color:var(--muted);text-decoration:none}
.dl:hover,.ico:hover{background:var(--bg2);color:var(--text)}.dl.ok,.ico.ok{color:var(--ok)}
.tracks li.skel{height:52px;background:linear-gradient(90deg,transparent,var(--hover),transparent);background-size:200% 100%;animation:sk 1.2s infinite}@keyframes sk{to{background-position:-200% 0}}
#toast{position:fixed;left:50%;bottom:100px;z-index:10;padding:9px 14px;border-radius:8px;background:#333336;color:#fff;font-size:13px;box-shadow:var(--shadow);opacity:0;transform:translateX(-50%);transition:opacity .2s;pointer-events:none}
#toast.show{opacity:1}
#explore{display:none;flex:1;overflow-y:auto}body.explore #explore{display:block}body.explore #main,body.explore footer{display:none}
.plats{display:flex;gap:2px;padding:2px;border-radius:8px;background:var(--bg2);width:max-content;max-width:100%}
.plat{display:flex;align-items:center;gap:7px;padding:6px 14px;border-radius:6px;font-weight:500;color:var(--muted)}
.plat i{width:8px;height:8px;border-radius:50%;background:var(--b)}.plat:hover{color:var(--text)}
.plat[data-p=spotify]{--b:var(--sp)}.plat[data-p=youtube]{--b:var(--yt)}.plat[data-p=soundcloud]{--b:var(--sc)}
.plat.on{background:var(--panel);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.2)}
.sbox{display:flex;align-items:center;gap:8px;margin:14px 0 10px;padding:4px 4px 4px 12px;border-radius:8px;background:var(--panel);border:1px solid var(--line);color:var(--muted)}
.sbox:focus-within{border-color:var(--muted)}
.sbox input::-webkit-search-cancel-button{filter:grayscale(1)}
.sbox input{flex:1;min-width:0;font:inherit;font-size:15px;border:0;background:none;color:var(--text);outline:none!important;padding:7px 0}
.sugg{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px}
.res{border-radius:8px;overflow:hidden;background:var(--panel);border:1px solid var(--line)}
.res:hover{background:var(--hover)}.res.playing{border-color:var(--red)}
.art{position:relative;aspect-ratio:1;background:var(--bg2);cursor:pointer;overflow:hidden}.grid.yt .art{aspect-ratio:16/9}
.art img{width:100%;height:100%;object-fit:cover;display:block}
.play{position:absolute;inset:0;display:grid;place-items:center;background:rgba(0,0,0,.3);opacity:0;transition:opacity .15s}
.res:hover .play,.res.playing .play{opacity:1}
.play span{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;background:#fff;color:#111}
.play svg.i{width:18px;height:18px;fill:currentColor;stroke:none;margin-left:2px}
.res.playing .play span{background:var(--red);color:#fff}
.fbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:-6px 0 16px}.fbar:empty{display:none}
.fbar .pill small{color:var(--muted);font-weight:400;margin-left:2px}.fbar .pill.on{background:var(--text);color:var(--bg)}.fbar .pill.on small{color:inherit;opacity:.7}
.fbar select{margin-left:auto;font:inherit;font-size:13px;padding:6px 8px;border-radius:6px;color:var(--text);background:var(--panel);border:1px solid var(--line)}
.kind{position:absolute;left:6px;top:6px;padding:1px 6px;border-radius:4px;background:rgba(0,0,0,.72);color:#fff;font-size:10.5px;font-weight:600;letter-spacing:.3px}
.ti .stats{font-size:11.5px;margin-top:1px}
.dur{position:absolute;right:6px;bottom:6px;padding:1px 6px;border-radius:4px;background:rgba(0,0,0,.72);color:#fff;font-size:11.5px;font-weight:500}
.rb{display:flex;align-items:center;gap:6px;padding:8px 6px 8px 10px}.rb .ti b{font-size:13.5px}
.res.skel .art,.res.skel .rb{background:linear-gradient(90deg,transparent,var(--hover),transparent);background-size:200% 100%;animation:sk 1.2s infinite}
.empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:40px 10px}
#player{position:fixed;right:20px;bottom:20px;z-index:8;width:380px;max-width:calc(100vw - 32px);display:none;border-radius:10px;overflow:hidden;background:var(--panel);border:1px solid var(--line);box-shadow:0 10px 30px rgba(0,0,0,.35)}
#player.show{display:block}body:not(.explore):not(.charts) #player{bottom:110px}
.ph{display:flex;align-items:center;gap:4px;padding:8px 8px 8px 14px}.ph .ti b{font-size:13.5px}
#pframe iframe{display:block;width:100%;border:0}
@media (max-width:560px){.brand small,.hint,#folder{display:none}#hero h1{font-size:21px}.genres{grid-template-columns:1fr}.genre{min-height:64px}
nav button{padding:5px 9px}.grid{grid-template-columns:repeat(2,1fr);gap:10px}#player{right:16px;bottom:16px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header><img src="/logo.png" alt=""><div class="brand"><b>MUSICXX</b><small>Spotify · YouTube · SoundCloud → .wav</small></div>
<nav><button id="tabD" class="on"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>Downloads<sup id="busy"></sup></button><button id="tabE"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>Explore</button><button id="tabC"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>Charts</button></nav>
<button class="ghost" id="folder"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Open OK folder</button><button class="ghost" id="quit" title="Stop MUSICXX"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v9M6.3 6.3a8 8 0 1 0 11.4 0"/></svg>Quit</button></header>
<main id="main"><div id="chat"><div id="hero"><img src="/logo.png" alt="MUSICXX logo"><h1>Drop a link. Get a .wav.</h1>
<p>Songs, playlists and albums save straight to <b>Desktop/OK</b>, with their key, BPM and genre.</p>
<div class="chips"><span class="chip sp"><i></i>Spotify</span><span class="chip yt"><i></i>YouTube</span><span class="chip sc"><i></i>SoundCloud</span></div>
<p style="margin-top:22px"><button class="pill" data-charts><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>Browse the top 100 R&amp;B, Afrobeats &amp; US Rap</button></p></div></div></main>
<section id="charts"><div class="wrap">
<div class="genres"><button class="genre" data-k="rnb"><b>R&amp;B</b><small>Top 100 worldwide</small></button>
<button class="genre" data-k="afro"><b>Afrobeats</b><small>Top 100</small></button>
<button class="genre" data-k="rap"><b>US Rap</b><small>Top 100 in the US</small></button></div>
<div class="chead"><h2><span id="cname">Loading…</span><small>Trending on Shazam · updated weekly</small></h2><button class="big" id="all"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>Download all</button></div>
<ul class="tracks" id="tracks"></ul></div></section>
<section id="explore"><div class="wrap">
<div class="plats"><button class="plat" data-p="spotify"><i></i>Spotify</button><button class="plat" data-p="youtube"><i></i>YouTube</button><button class="plat" data-p="soundcloud"><i></i>SoundCloud</button></div>
<form class="sbox" id="sf"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="q" type="search" aria-label="Search" autocomplete="off"><button class="big">Search</button></form>
<div class="sugg" id="sugg"></div><div class="fbar" id="fbar"></div><div class="grid" id="grid"></div></div></section>
<div id="player" role="region" aria-label="Player"><div class="ph"><div class="ti"><b id="pt"></b><span id="pa"></span></div>
<button class="ico" id="pdl" title="Download as .wav"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg></button><a class="ico" id="popen" target="_blank" rel="noopener" title="Open on the site"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg></a><button class="ico" id="px" title="Close (Esc)"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div><div id="pframe"></div></div>
<div id="toast" role="status"></div>
<footer><form id="f"><span id="detect"></span><textarea id="t" rows="1" placeholder="Paste a Spotify, YouTube or SoundCloud link…" aria-label="Links"></textarea>
<button type="button" class="pill" id="paste">Paste</button><button class="send" id="go" disabled><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>Download</button></form>
<div class="hint">Enter to download · paste several links at once · or drag a link anywhere</div></footer>
<div id="drop"><div>Drop the link to download</div></div>
<script>
const DL='<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>',OK='<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5 9-10"/></svg>',$=id=>document.getElementById(id),chat=$('chat'),t=$('t'),go=$('go'),detect=$('detect'),main=$('main');
const cards={},last={},status={},open=new Set();
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const post=(p,b)=>fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
function kind(u){const l=u.toLowerCase();
  if(/spotify\.com\/|^spotify:/.test(l))return['sp','Spotify',/playlist/.test(l)?'Playlist':/album/.test(l)?'Album':'Song'];
  if(/youtube\.com|youtu\.be/.test(l))return['yt','YouTube',/\/playlist\?/.test(l)?'Playlist':'Song'];
  if(/soundcloud\.com/.test(l))return['sc','SoundCloud',/\/sets\//.test(l)?'Playlist':'Song'];
  return /^https?:\/\//.test(l)?['ot','Link','']:null}
const chip=k=>k?`<span class="chip ${k[0]}"><i></i>${k[1]}${k[2]?' · '+k[2]:''}</span>`:'<span class="chip ot">Not a link</span>';
const short=u=>u.replace(/^https?:\/\/(www\.|open\.|m\.)?/,'').replace(/\?si=.*$/,'');
const plural=n=>n+(n==1?' song':' songs');
function card(j){
  const n=j.saved,k=kind(j.url);let head,bar='',meta='';
  if(j.status=='running'){
    head=`<span class="spin"></span>Downloading…`;
    const p=j.total?Math.min(100,n/j.total*100):j.pct;
    bar=`<div class="bar ${p?'':'indef'}"><i style="width:${p}%"></i></div>`;
    meta=(j.total?`${n} / ${j.total} songs`:j.pct?`${j.pct.toFixed(0)}%`:'')+(j.log?' · '+esc(j.log):'');
  }else if(j.status=='done')head=`<span class="done"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5 9-10"/></svg>${plural(n)} saved to Desktop/OK</span>`;
  else{head=`<span class="failed">✕ Couldn't download${n?' everything ('+plural(n)+' saved)':''}</span>`;meta=esc(j.log)}
  const all=open.has(j.id),list=all?j.songs:j.songs.slice(-5);
  const src=`<div class="src">${j.chart?'<span class="chip ch">Chart</span>':chip(k)}</div>`;
  const songs=list.length?`<ul class="songs">${list.map(s=>`<li><button data-song="${esc(s)}"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg><span>${esc(s)}</span><em>Show file</em></button>${tags(j.info&&j.info[s])}</li>`).join('')}</ul>`+
    (j.songs.length>5?`<button class="more" data-more="${j.id}">${all?'Show less':'Show all '+j.songs.length}</button>`:''):'';
  const acts=j.status=='done'?`<div class="actions"><button class="pill" data-folder><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Open folder</button></div>`:'';
  return `${src}<div class="status">${head}</div>${bar}${meta?`<div class="meta">${meta}</div>`:''}${songs}${acts}`;
}
function tags(i){  // key / BPM / genre for one song
  if(!i)return'';
  if(i.state!='done')return`<div class="tags"><b class="tag wait">${i.state=='analyzing'?'Analyzing key & BPM…':'Waiting to analyze…'}</b></div>`;
  const t=[];
  if(i.camelot)t.push(`<b class="tag cam" style="--h:${(parseInt(i.camelot)-1)*30}" title="Camelot key">${esc(i.camelot)}</b>`,`<b class="tag" title="Key">${esc(i.key)}</b>`);
  if(i.bpm)t.push(`<b class="tag" title="Tempo (estimate)">${i.bpm} BPM</b>`);
  if(i.genre)t.push(`<b class="tag gen" title="Genre">${esc(i.genre)}</b>`);
  return`<div class="tags">${t.join('')||'<b class="tag">Couldn\'t analyze this one</b>'}</div>`}
function notify(j){if(document.hidden&&window.Notification&&Notification.permission=='granted')
  new Notification(j.status=='done'?'MUSICXX ✓ Download finished':'MUSICXX ✕ Download failed',{body:j.status=='done'?plural(j.saved)+' saved to Desktop/OK':short(j.url),icon:'/logo.png'})}
function render(jobs){
  if(jobs.length)$('hero')?.remove();
  let added=false;
  for(const j of jobs){
    if(!cards[j.id]){
      chat.insertAdjacentHTML('beforeend',`<div class="row"><img class="av" src="/logo.png" alt=""><div class="card" id="c${j.id}"></div></div>`);
      cards[j.id]=$('c'+j.id);added=true;
    }
    const key=JSON.stringify(j)+open.has(j.id);
    if(last[j.id]!==key){last[j.id]=key;cards[j.id].innerHTML=card(j)}
    if(status[j.id]=='running'&&j.status!='running')notify(j);
    status[j.id]=j.status;
  }
  if(added)main.scrollTop=main.scrollHeight;
  const busy=jobs.filter(j=>j.status=='running').length;
  document.title=busy?`(${busy}) Downloading · MUSICXX`:'MUSICXX';
  $('busy').textContent=busy||'';
}
const askNotify=()=>{if(window.Notification&&Notification.permission=='default')Notification.requestPermission()};
function toast(m){const e=$('toast');e.textContent=m;e.classList.add('show');clearTimeout(e.t);e.t=setTimeout(()=>e.classList.remove('show'),2600)}
let ck='rnb';const cdata={},queued=new Set();
function tab(name){  // '' = Downloads, 'explore', 'charts'
  for(const c of ['charts','explore'])document.body.classList.toggle(c,name==c);
  $('tabD').classList.toggle('on',!name);$('tabE').classList.toggle('on',name=='explore');$('tabC').classList.toggle('on',name=='charts');
  if(name=='charts')loadChart(ck);else if(name=='explore')$('q').focus();else t.focus()}
// ---- Explore: search Spotify / YouTube / SoundCloud, preview in the official player, download
const PL={spotify:'Spotify',youtube:'YouTube',soundcloud:'SoundCloud'};
const store={get(k,d){try{return JSON.parse(localStorage.getItem(k))??d}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const SUGG=['Burna Boy','Tems','Rema','Asake','Wizkid','SZA','Drake','Kendrick Lamar','Summer Walker','Fally Ipupa'];
let plat=store.get('plat','spotify');if(!PL[plat])plat='spotify';
let results=[],seq=0,deb,cur=null;const got=new Set();
const fmt=s=>s?`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`:'';
const site=u=>/spotify/.test(u)?'Spotify':/soundcloud/.test(u)?'SoundCloud':'YouTube';
function setPlat(p){plat=p;store.set('plat',p);
  document.querySelectorAll('.plat').forEach(b=>b.classList.toggle('on',b.dataset.p==p));
  $('q').placeholder=p=='soundcloud'?'Type a song to see every edit, remix & bootleg on SoundCloud…':`Search ${PL[p]} for songs and artists…`;
  if($('q').value.trim())doSearch()}
// SoundCloud versions, guessed from the title. A track can be several (e.g. a mashup edit).
const TYPES=[['edit','Edits','EDIT',/\b(re-?)?edit\b|\bvip\b/i],['remix','Remixes','REMIX',/remix|\brmx\b/i],
  ['boot','Bootlegs & Flips','BOOTLEG',/bootleg|\bflip\b|rework|\bblend\b|\bcover\b/i],
  ['speed','Sped up / Slowed','SPED UP',/sped|speed ?up|slowed|reverb|nightcore|\bfast\b/i],
  ['mash','Mashups','MASHUP',/mash ?-?up|\s[x×]\s/i]];
const typesOf=s=>TYPES.filter(t=>t[3].test(s.title)).map(t=>t[0]);
const compact=n=>new Intl.NumberFormat('en',{notation:'compact'}).format(n);
let fkey='all',fsort='rel',rplat='';
function view(){  // indexes into results, after the filter and sort
  let idx=results.map((_,i)=>i);
  if(fkey=='orig')idx=idx.filter(i=>!typesOf(results[i]).length);else if(fkey!='all')idx=idx.filter(i=>typesOf(results[i]).includes(fkey));
  const by={plays:s=>s.plays||0,likes:s=>s.likes||0,new:s=>s.date||0}[fsort];
  if(by)idx.sort((a,b)=>by(results[b])-by(results[a]));
  return idx}
function renderFbar(){const fb=$('fbar');
  if(rplat!='soundcloud'||!results.length){fb.innerHTML='';return}
  const cnt=k=>k=='all'?results.length:results.filter(s=>k=='orig'?!typesOf(s).length:typesOf(s).includes(k)).length;
  fb.innerHTML=[['all','All'],...TYPES.map(t=>[t[0],t[1]]),['orig','Originals']].filter(([k])=>k=='all'||cnt(k))
    .map(([k,l])=>`<button class="pill${fkey==k?' on':''}" data-f="${k}">${l} <small>${cnt(k)}</small></button>`).join('')+
    `<select id="fsort" aria-label="Sort results"><option value="rel">Best match</option><option value="plays">Most played</option><option value="likes">Most liked</option><option value="new">Newest</option></select>`;
  $('fsort').value=fsort}
function renderGrid(){const idx=view();
  $('grid').innerHTML=idx.length?idx.map((i,pos)=>card2(results[i],i,pos)).join(''):'<div class="empty">Nothing in this filter.</div>'}
function renderSugg(){const recent=store.get('recent',[]);
  $('sugg').innerHTML=recent.map(r=>`<button class="pill" data-q="${esc(r)}"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>${esc(r)}</button>`).join('')+
    SUGG.filter(s=>!recent.some(r=>r.toLowerCase()==s.toLowerCase())).slice(0,10-recent.length).map(s=>`<button class="pill" data-q="${esc(s)}">${esc(s)}</button>`).join('')}
function card2(s,i,pos=i){const g=got.has(s.url),ty=TYPES.find(t=>t[0]==typesOf(s)[0]);
  const stats=s.plays!=null?`<span class="stats">▶ ${compact(s.plays)}${s.likes!=null?' · ♥ '+compact(s.likes):''}</span>`:'';
  return `<div class="res${cur&&cur.url==s.url?' playing':''}" data-i="${i}" style="animation-delay:${Math.min(pos,12)*30}ms">
  <div class="art" data-play="${i}" title="Play preview">${s.thumb?`<img src="${esc(s.thumb)}" alt="" loading="lazy">`:''}${ty?`<span class="kind">${ty[2]}</span>`:''}<div class="play"><span><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4l13 8-13 8z"/></svg></span></div>${s.duration?`<span class="dur">${fmt(s.duration)}</span>`:''}</div>
  <div class="rb"><div class="ti"><b title="${esc(s.title)}">${esc(s.title)}</b><span>${esc(s.artist)}</span>${stats}</div>
  <button class="ico${g?' ok':''}" data-get="${i}" title="Download as .wav">${g?OK:DL}</button></div></div>`}
async function doSearch(){
  clearTimeout(deb);const q=$('q').value.trim();if(!q)return;
  if(/^https?:\/\//.test(q)){send(q);$('q').value='';toast('Link sent to Downloads');return}
  const my=++seq,grid=$('grid'),p=plat;
  grid.className='grid'+(p=='youtube'?' yt':'');
  grid.innerHTML='<div class="res skel"><div class="art"></div><div class="rb"><div class="ti"><b>&nbsp;</b><span>&nbsp;</span></div></div></div>'.repeat(8);
  let r;
  try{const res=await fetch(`/search?p=${p}&q=${encodeURIComponent(q)}`);if(!res.ok)throw 0;r=await res.json()}
  catch(e){if(my==seq)grid.innerHTML=`<div class="empty">Couldn't search ${PL[p]} right now. Try again in a moment.</div>`;return}
  if(my!=seq)return;  // a newer search started meanwhile
  results=r;rplat=p;fkey='all';
  const recent=store.get('recent',[]).filter(x=>x.toLowerCase()!=q.toLowerCase());recent.unshift(q);store.set('recent',recent.slice(0,5));renderSugg();
  renderFbar();
  if(r.length)renderGrid();else grid.innerHTML=`<div class="empty">No results on ${PL[p]} for “${esc(q)}”.</div>`;
}
function embed(u){let m;
  if(m=u.match(/spotify\.com\/track\/([A-Za-z0-9]+)/))return[`https://open.spotify.com/embed/track/${m[1]}?utm_source=generator&theme=0`,152];
  if(m=u.match(/[?&]v=([\w-]{11})/)||u.match(/youtu\.be\/([\w-]{11})/))return[`https://www.youtube-nocookie.com/embed/${m[1]}?autoplay=1&rel=0`,214];
  if(/soundcloud\.com/.test(u))return[`https://w.soundcloud.com/player/?url=${encodeURIComponent(u)}&auto_play=true&color=%23d63a2c&visual=true`,180];
  return null}
function markGot(){
  document.querySelectorAll('[data-get]').forEach(b=>{const g=got.has(results[b.dataset.get]?.url);b.classList.toggle('ok',g);b.innerHTML=g?OK:DL});
  const g=cur&&got.has(cur.url);$('pdl').classList.toggle('ok',!!g);$('pdl').innerHTML=g?OK:DL}
function play(s){const e=embed(s.url);if(!e)return;cur=s;
  $('pt').textContent=s.title;$('pa').textContent=s.artist;$('popen').href=s.url;$('popen').title='Open on '+site(s.url);
  $('pframe').innerHTML=`<iframe src="${esc(e[0])}" height="${e[1]}" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture" referrerpolicy="strict-origin-when-cross-origin" title="${esc(s.title)}"></iframe>`;
  $('player').classList.add('show');markGot();
  document.querySelectorAll('.res').forEach(r=>r.classList.toggle('playing',results[r.dataset.i]?.url==s.url))}
async function grab(s){if(!s||got.has(s.url))return;got.add(s.url);markGot();askNotify();
  await post('/download',{text:s.url});toast(`Downloading “${s.title}” to Desktop/OK`);poll()}
document.querySelectorAll('.plat').forEach(b=>b.onclick=()=>setPlat(b.dataset.p));
$('sf').onsubmit=e=>{e.preventDefault();doSearch()};
$('q').oninput=()=>{clearTimeout(deb);if($('q').value.trim().length>=3&&!/^https?:/.test($('q').value))deb=setTimeout(doSearch,700)};
$('fbar').onclick=e=>{const b=e.target.closest('[data-f]');if(b){fkey=b.dataset.f;renderFbar();renderGrid()}};
$('fbar').onchange=e=>{if(e.target.id=='fsort'){fsort=e.target.value;renderGrid()}};
$('sugg').onclick=e=>{const b=e.target.closest('[data-q]');if(b){$('q').value=b.dataset.q;doSearch()}};
$('grid').onclick=e=>{const p=e.target.closest('[data-play]'),g=e.target.closest('[data-get]');
  if(p)play(results[p.dataset.play]);else if(g)grab(results[g.dataset.get])};
$('pdl').onclick=()=>grab(cur);
$('px').onclick=()=>{$('player').classList.remove('show');$('pframe').innerHTML='';cur=null;
  document.querySelectorAll('.res.playing').forEach(r=>r.classList.remove('playing'))};
addEventListener('keydown',e=>{
  if(e.key=='Escape'&&cur)$('px').click();
  if(e.key=='/'&&document.body.classList.contains('explore')&&document.activeElement!=$('q')){e.preventDefault();$('q').focus()}});
setPlat(plat);renderSugg();
async function loadChart(k){
  ck=k;document.querySelectorAll('.genre').forEach(g=>g.classList.toggle('on',g.dataset.k==k));
  const tr=$('tracks');
  if(!cdata[k]){
    tr.innerHTML='<li class="skel"></li>'.repeat(8);$('cname').textContent='Loading…';
    try{const r=await fetch('/chart/'+k);if(!r.ok)throw 0;cdata[k]=await r.json()}
    catch(e){if(ck==k){$('cname').textContent='Chart unavailable';tr.innerHTML='<li>Couldn\'t load this chart. Check your internet and try again.</li>'}return}
  }
  if(ck!=k)return;
  const d=cdata[k];
  $('cname').textContent=`Top ${d.songs.length} ${d.name}`;$('all').innerHTML=DL+`Download all ${d.songs.length}`;
  tr.innerHTML=d.songs.map((s,i)=>{const q=queued.has(k+(i+1));
    return `<li style="animation-delay:${Math.min(i,20)*15}ms"><span class="rank">${i+1}</span><div class="ti"><b>${esc(s.title)}</b><span>${esc(s.artist)}</span></div>
    <button class="dl${q?' ok':''}" data-rank="${i+1}" aria-label="Download ${esc(s.title)}">${q?OK:DL}</button></li>`}).join('');
}
document.querySelectorAll('.genre').forEach(g=>g.onclick=()=>loadChart(g.dataset.k));
$('tabD').onclick=()=>tab('');$('tabE').onclick=()=>tab('explore');$('tabC').onclick=()=>tab('charts');
$('tracks').onclick=async e=>{const b=e.target.closest('.dl');if(!b||b.classList.contains('ok'))return;
  askNotify();b.classList.add('ok');b.innerHTML=OK;queued.add(ck+b.dataset.rank);
  await post('/download',{chart:ck,ranks:[+b.dataset.rank]});toast('Added to Downloads');poll()};
$('all').onclick=async()=>{const d=cdata[ck];if(!d)return;const n=d.songs.length;
  if(!confirm(`Download all ${n} ${d.name} songs to Desktop/OK?\n\nThat's about ${(n*0.04).toFixed(1)} GB of WAV files and can take 15–30 minutes.`))return;
  askNotify();for(let i=1;i<=n;i++)queued.add(ck+i);
  await post('/download',{chart:ck});tab('');toast(`Downloading ${n} songs…`);poll()};
async function poll(){try{render(await (await fetch('/jobs')).json())}catch(e){}}
function update(){
  const links=t.value.trim().split(/\s+/).filter(Boolean);
  go.disabled=!links.length;
  detect.innerHTML=links.length?chip(kind(links[0]))+(links.length>1?` <span class="chip ot">+${links.length-1}</span>`:''):'';
  t.style.height='40px';t.style.height=Math.min(t.scrollHeight,120)+'px';
}
async function send(text){
  if(!text.trim())return;
  askNotify();
  await post('/download',{text});t.value='';update();poll();
}
$('f').onsubmit=e=>{e.preventDefault();send(t.value)};
t.oninput=update;
t.onkeydown=e=>{if(e.key=='Enter'&&!e.shiftKey){e.preventDefault();$('f').requestSubmit()}};
$('paste').onclick=async()=>{try{t.value=(await navigator.clipboard.readText()).trim();update();t.focus()}catch(e){t.focus()}};
chat.onclick=e=>{const b=e.target.closest('button');if(!b)return;
  if(b.dataset.song!==undefined)post('/open',{name:b.dataset.song});
  else if(b.dataset.more){const id=+b.dataset.more;open.has(id)?open.delete(id):open.add(id);poll()}
  else if(b.hasAttribute('data-folder'))post('/open');
  else if(b.hasAttribute('data-charts'))tab('charts')};
$('folder').onclick=()=>post('/open');
$('quit').onclick=async()=>{
  if(Object.values(status).includes('running')&&!confirm('Downloads are still running. Quit anyway?'))return;
  await post('/quit');document.body.innerHTML='<div style="margin:auto;text-align:center"><h2>MUSICXX stopped</h2><p style="color:var(--muted)">Open the app again to restart.</p></div>'};
let depth=0;
addEventListener('dragenter',e=>{e.preventDefault();depth++;document.body.classList.add('dragging')});
addEventListener('dragleave',()=>{if(--depth<=0){depth=0;document.body.classList.remove('dragging')}});
addEventListener('dragover',e=>e.preventDefault());
addEventListener('drop',e=>{e.preventDefault();depth=0;document.body.classList.remove('dragging');
  send(e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text/plain'))});
addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key=='v'&&document.activeElement!==t&&!/charts|explore/.test(document.body.className))t.focus()});
setInterval(poll,1000);poll();t.focus();if(location.hash=='#charts')tab('charts');else if(location.hash.startsWith('#explore')){tab('explore');  // #explore/[platform/]QUERY searches right away
  let q=decodeURIComponent(location.hash.slice(9));const m=q.match(/^(spotify|youtube|soundcloud)\/(.*)$/);
  if(m){q=m[2];setPlat(m[1])}if(q){$('q').value=q;doSearch()}}
</script></body></html>"""


UPDATE_URL = "https://raw.githubusercontent.com/Ubald12/MUSICXX/main/"


def self_update():
    """Replace musicxx.py/analyze.py with the latest from GitHub. True if anything changed.
    Skipped in the git checkout, so it never overwrites work in progress."""
    here = Path(__file__).resolve().parent
    if (here / ".git").exists() or os.environ.get("MUSICXX_UPDATED"):
        return False
    changed = False
    for name in ("musicxx.py", "analyze.py"):
        new = fetch(UPDATE_URL + name)
        try:
            compile(new, name, "exec")  # never install a truncated or broken download
        except (SyntaxError, ValueError):
            continue
        f = here / name
        if new.strip() and (not f.exists() or f.read_text(encoding="utf-8") != new):
            f.write_text(new, encoding="utf-8")
            changed = True
    return changed


def self_test():
    assert command("hello") is None
    sp = command("https://open.spotify.com/playlist/abc")
    assert sp[0] == "spotdl" and sp[-1] == f"{OUT}/{{artists}} - {{title}}.{{output-ext}}"
    yt = command("https://soundcloud.com/a/sets/b")
    assert yt[0] == "yt-dlp" and yt[-1].endswith("/sets/b") and str(OUT) in yt
    job = {"songs": [], "saved": 0, "total": 0, "pct": 0}
    for line in ["[download] Downloading item 2 of 12", "[download]  45.3% of 3.5MiB",
                 f"[ExtractAudio] Destination: {OUT}/A - B.wav", 'Downloaded "C - D": https://x',
                 "Skipping E - F (file already exists) (duplicate)", "3/10 complete"]:
        parse(job, line)
    assert job["songs"] == ["A - B", "C - D", "E - F"] and job["saved"] == 3 and job["total"] == 10, job
    print("self-test ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(self_test())
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # localhost only
    except OSError:  # already running: just show it
        webbrowser.open(f"http://localhost:{PORT}")
        sys.exit()
    if self_update():  # restart on the new code
        server.server_close()
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())], env={**os.environ, "MUSICXX_UPDATED": "1"})
        sys.exit()
    threading.Thread(target=analyzer, daemon=True).start()
    print(f"MUSICXX running at http://localhost:{PORT}  (close this window to stop)")
    webbrowser.open(f"http://localhost:{PORT}")
    server.serve_forever()
