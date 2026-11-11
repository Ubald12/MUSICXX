#!/usr/bin/env python3
"""MUSICXX: a local chat bot. Paste Spotify / YouTube / SoundCloud links (songs or playlists),
get .wav files in Desktop/OK. Run: python3 musicxx.py   (self-check: python3 musicxx.py --test)"""
import base64, csv, glob, html, importlib.util, io, itertools, json, os, queue, re, signal, subprocess, sys, tempfile, threading, time, webbrowser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote_plus, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
WINDOWS = os.name == "nt"
# Windows often moves the visible Desktop into OneDrive
_onedrive = Path.home() / "OneDrive" / "Desktop"
DEFAULT_OUT = (_onedrive if WINDOWS and _onedrive.is_dir() else Path.home() / "Desktop") / "OK"
DATA = Path.home() / ".musicxx"  # settings, library info, synced playlists
FORMATS = ("wav", "aiff", "mp3", "flac")
AUDIO = tuple("." + f for f in FORMATS)


def load(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(name, data):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(DATA / name)  # atomic: a crash mid-write never leaves half a file


settings = {"folder": str(DEFAULT_OUT), "format": "wav", "open_when_done": False, "clipboard": True, **load("settings.json", {})}
OUT = Path(settings["folder"])  # every song lands here, flat
LOGO = Path(__file__).with_name("logo.png")
# Tools: this Python's own bin/Scripts folder (installer setup), the ffmpeg spotdl downloads, then Homebrew/pipx
os.environ["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), str(Path.home() / ".spotdl"),
                                      f"{Path.home()}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin",
                                      os.environ.get("PATH", "")])
os.environ.update(NO_COLOR="1", TERM="dumb", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")


def tool_python(module, fallback):
    """This Python if it has `module` (installer setup), else the interpreter of the original Mac setup."""
    return Path(sys.executable) if importlib.util.find_spec(module) else fallback


def ensure(module, package=None):
    """True if `module` can be imported. Self-update brings new code but not new libraries, so a missing one is
    installed into the installer's venv (never into a system Python)."""
    if importlib.util.find_spec(module):
        return True
    if sys.prefix == sys.base_prefix:
        return False
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", package or module], capture_output=True)
    importlib.invalidate_caches()
    return importlib.util.find_spec(module) is not None


jobs, ids = [], itertools.count(1)
PRIVATE = ("cmd", "retry", "proc", "before")  # job fields the page never sees


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


# Song recognition (Shazam) runs in the Python that has the "shazamio" library
MAC_VENV_PY = Path.home() / "Library/Application Support/MUSICXX/venv/bin/python"
RECOGNIZE = r'''
import asyncio, json, sys
from shazamio import Shazam
t = asyncio.run(Shazam().recognize(sys.argv[1])).get("track") or {}
print(json.dumps({"title": t.get("title", ""), "artist": t.get("subtitle", ""), "cover": (t.get("images") or {}).get("coverart", "")}))
'''


def shazam_python():
    ensure("shazamio")
    return tool_python("shazamio", MAC_VENV_PY)


def recognize(audio):
    """Identify a short recording (any format ffmpeg reads). {title, artist, cover}, or None if no match."""
    with tempfile.TemporaryDirectory() as d:
        src, wav = Path(d, "rec"), Path(d, "rec.wav")
        src.write_bytes(audio)
        if subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(wav)]).returncode:
            return None
        r = subprocess.run([str(shazam_python()), "-I", "-c", RECOGNIZE, str(wav)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)
    try:
        hit = json.loads(r.stdout)
    except ValueError:
        return None
    return hit if hit.get("title") else None


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


def dl_format():
    """What the downloaders write. Neither can write AIFF, so AIFF is converted from WAV after the download."""
    return "wav" if settings["format"] == "aiff" else settings["format"]


def spotdl(*queries, archive=None):
    """spotdl takes Spotify links or plain "Artist - Title" searches."""
    fmt = dl_format()
    return ["spotdl", "download", *queries, "--format", fmt, *(["--bitrate", "320k"] if fmt == "mp3" else []),
            "--simple-tui", *(["--archive", str(archive)] if archive else []),
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
    return ["yt-dlp", "-x", "--audio-format", dl_format(), *(["--audio-quality", "0"] if dl_format() == "mp3" else []), "--no-playlist", "--newline", "--ignore-errors",
            "-P", str(OUT), "-o", f"{name}.%(ext)s", *extra, url]


def fetch(url, raw=False):
    out = subprocess.run(["curl", "-sfL", "--max-time", "20", "-A", "Mozilla/5.0", url], capture_output=True).stdout
    return out if raw else out.decode("utf-8", "replace")


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
    return ytmusic(artist, title)


def ytmusic(artist, title):
    """Download the top YouTube Music song result for artist + title."""
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


def song_file(name, path=None):
    """The audio file for a reported song. spotdl reports 'First Artist - Title' but names the file with all artists."""
    for p in ([Path(path)] if path else []) + [OUT / (name + e) for e in AUDIO]:
        if p.is_file():
            return p
    title = name.split(" - ", 1)[-1]
    hits = sorted((f for e in AUDIO for f in OUT.glob("*" + glob.escape(f" - {title}{e}"))), key=lambda f: f.stat().st_mtime)
    return hits[-1] if hits else None


def lookup(name):
    """Genre from Apple's catalog (iTunes Search API), plus album / year / cover art when the match is clearly the
    same song. Retries with only the first artist."""
    clean = re.sub(r"[(\[][^)\]]*[)\]]", "", name).strip()  # drop "(feat. …)", "[Official Video]"
    artist, _, title = clean.partition(" - ")
    terms = [f"{artist} {title}", f"{re.split(r',|&| x | feat', artist)[0]} {title}"] if title else [clean]
    norm = lambda t: re.sub(r"\W+", "", t.lower())
    for term in terms:
        try:
            hits = json.loads(fetch("https://itunes.apple.com/search?entity=song&limit=1&term=" + quote_plus(term)) or "{}")["results"]
        except (ValueError, KeyError):
            continue
        if hits:
            h, out = hits[0], {"genre": hits[0].get("primaryGenreName", "")}
            if norm(title)[:12] and norm(title)[:12] in norm(h.get("trackName", "")):
                out.update(album=h.get("collectionName", ""), year=(h.get("releaseDate") or "")[:4],
                           cover=(h.get("artworkUrl100") or "").replace("100x100bb", "600x600bb"))
            return out
    return {}


def short_key(key):
    return key.replace(" minor", "m").replace(" major", "")  # "Ab minor" -> "Abm", the form DJ apps read


def write_tags(f, info):
    """Artist / title / album / genre / key / BPM / cover inside the file, so DJ software and players show them."""
    if not ensure("mutagen"):
        return
    import mutagen
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, COMM, TALB, TBPM, TCON, TDRC, TIT2, TKEY, TPE1
    artist, title = (f.stem.split(" - ", 1) + [""])[:2] if " - " in f.stem else ("", f.stem)
    key, bpm = short_key(info.get("key", "")), str(info.get("bpm") or "")
    cover = fetch(info["cover"], raw=True) if info.get("cover") else b""
    audio = mutagen.File(f)
    if audio is None:
        return
    if isinstance(audio, FLAC):
        for k, v in {"title": title, "artist": artist, "album": info.get("album"), "genre": info.get("genre"),
                     "date": info.get("year"), "bpm": bpm, "initialkey": key}.items():
            if v:
                audio[k] = v
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.data = 3, "image/jpeg", cover
            audio.clear_pictures()
            audio.add_picture(pic)
    else:  # WAV, AIFF and MP3 all carry ID3 tags
        if audio.tags is None:
            audio.add_tags()
        for frame, v in ((TIT2, title), (TPE1, artist), (TALB, info.get("album")), (TCON, info.get("genre")),
                         (TDRC, info.get("year")), (TBPM, bpm), (TKEY, key)):
            if v:
                audio.tags.add(frame(encoding=3, text=v))
        if info.get("camelot"):  # Mixed In Key style, shown in the comment column of most DJ apps
            audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=f"{info['camelot']} - {info['key']} - {bpm} BPM"))
        if cover:
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    audio.save()


def to_aiff(f):
    """WAV -> AIFF (same quality, but AIFF holds tags every DJ app reads). Keeps the WAV if conversion fails."""
    out = f.with_suffix(".aiff")
    if subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(f), "-map", "0:a", "-c:a", "pcm_s16be", str(out)]).returncode == 0:
        f.unlink()
        return out
    out.unlink(missing_ok=True)
    return f


library = load("library.json", {})  # file name without extension -> {key, camelot, bpm, genre, album, year, cover, added}
lib_lock, pending = threading.Lock(), set()


def analyzer():
    """Background worker, one song at a time: convert (AIFF), analyze key + BPM, look up genre / cover, write tags."""
    while True:
        job, name, path = analysis.get()
        if job:
            set_info(job, name, state="analyzing")
        result, f = {}, None
        try:  # background worker: one bad file must not stop the rest
            f = song_file(name, path)
            if job and settings["format"] == "aiff" and f.suffix == ".wav":
                f = to_aiff(f)
            r = subprocess.run([str(VENV_PY), "-I", str(ANALYZER), str(f)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            result = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            pass
        result.update(lookup(f.stem if f else name))
        if f and f.is_file():
            try:
                write_tags(f, result)
            except Exception:
                pass
            with lib_lock:
                library[f.stem] = {**result, "added": library.get(f.stem, {}).get("added") or f.stat().st_mtime}
                save("library.json", library)
            pending.discard(f.stem)
        if job:
            set_info(job, name, state="done", **result)


def library_list():
    """Every song in the folder with its analysis. Songs not analyzed yet (e.g. added by hand) get queued."""
    out = []
    for f in (OUT.iterdir() if OUT.is_dir() else []):
        if f.suffix.lower() not in AUDIO or not f.is_file():
            continue
        st, i = f.stat(), library.get(f.stem)
        # skip files written in the last minute: a download may still be writing them
        if i is None and f.stem not in pending and time.time() - st.st_mtime > 60:
            pending.add(f.stem)
            analysis.put((None, f.stem, str(f)))
        artist, title = (f.stem.split(" - ", 1) if " - " in f.stem else ("", f.stem))
        out.append({**(i or {}), "name": f.stem, "file": f.name, "artist": artist, "title": title, "ext": f.suffix[1:].upper(),
                    "size": st.st_size, "added": (i or {}).get("added") or st.st_mtime, "state": "done" if i else "analyzing"})
    return out


def execute(job, cmd):
    """Run one downloader command, feeding its output into the job. Returns the exit code."""
    try:  # own process group, so Cancel can stop the tool *and* the ffmpeg it started
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                             **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}))
    except FileNotFoundError as e:
        job["log"] = f"Missing tool: {e.filename}"
        return 1
    job["proc"] = p
    if job.get("cancelled"):
        kill(p)
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


def kill(p):
    if p and p.poll() is None:
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGTERM)


def remove_partials(job):
    """After Cancel: delete files the job created but never finished. Only when no other download is running,
    so another job's files are never touched; files that existed before the job are never touched."""
    if any(j is not job and j["status"] == "running" for j in jobs):
        return
    keep = {f.stem for f in (song_file(n) for n in job["songs"]) if f}
    for f in OUT.iterdir():
        if f.is_file() and f.name not in job["before"] and f.stem not in keep:
            f.unlink(missing_ok=True)


def run(job):
    job["before"] = set(os.listdir(OUT))
    code = execute(job, job["cmd"])
    retry = job.get("retry")  # a prepared second try (recognized songs), else the per-link fallback
    if not job.get("cancelled") and not job["saved"] and (retry or can_fallback(job["url"], job["log"])):
        job["log"] = ("Trying YouTube Music…" if retry else
                      f"{'Spotify' if 'spotify' in job['url'] else 'SoundCloud'} won't give this song. Getting it from YouTube Music…")
        cmd = retry or fallback(job["url"])
        code = execute(job, cmd) if cmd else 1
        if not cmd:
            job["log"] = "Couldn't find this song on YouTube Music either."
    if job.get("cancelled"):
        job["status"] = "cancelled"
        return remove_partials(job)
    job["status"] = "done" if code == 0 or job["saved"] else "failed"
    if WINDOW:  # the browser page shows its own notifications
        notify("Download finished" if job["status"] == "done" else "Download failed",
               f"{job['saved']} song{'s' if job['saved'] != 1 else ''} saved" if job["status"] == "done" else job["url"])
    if settings["open_when_done"] and job["saved"]:
        reveal()


def start(url, cmd, **extra):
    """Create a download job and run it in the background."""
    job = {"id": next(ids), "url": url, "cmd": cmd, "status": "running", "saved": 0, "songs": [], "total": 0,
           "pct": 0, "info": {}, "log": "Starting…", **extra}
    if not cmd:
        job.update(status="failed", log="That's not a link I can download.")
    else:
        threading.Thread(target=run, args=(job,), daemon=True).start()
    jobs.append(job)
    return job


def notify(title, text):
    """A system notification (used in the app window, where web notifications don't work)."""
    try:
        if sys.platform == "darwin":  # text goes in argv, never into the script itself
            subprocess.run(["osascript", "-e", "on run a", "-e", "display notification (item 2 of a) with title (item 1 of a)",
                            "-e", "end run", "MUSICXX · " + title, text])
        elif not WINDOWS:
            subprocess.run(["notify-send", "MUSICXX · " + title, text])
    except OSError:
        pass


# ---- playlist sync: synced playlists are re-checked every few hours and new songs downloaded
syncs = load("sync.json", [])  # [{"url", "last"}]
PLAYLIST = re.compile(r"spotify\.com/(playlist|album)/|youtube\.com/playlist\?|soundcloud\.com/[^/?#]+/sets/")
SYNC_EVERY = 3 * 3600


def sync_now(entry):
    if any(j["url"] == entry["url"] and j["status"] == "running" for j in jobs):
        return
    url = entry["url"]  # archives remember what was fetched, so each sync only downloads new songs
    cmd = (spotdl(url, archive=DATA / "spotdl-archive.txt") if "spotify.com/" in url else
           ytdlp(url, "%(uploader)s - %(title)s", "--download-archive", str(DATA / "ytdlp-archive.txt")))
    start(url, cmd, sync=True)
    entry["last"] = time.time()
    save("sync.json", syncs)


def syncer():
    time.sleep(60)
    while True:
        for entry in list(syncs):
            if time.time() - entry.get("last", 0) > SYNC_EVERY:
                sync_now(entry)
        time.sleep(300)


# ---- clipboard: offer to download a Spotify / YouTube / SoundCloud link as soon as it's copied
LINK = re.compile(r"^https?://(open\.spotify\.com|(www\.|m\.|music\.)?youtube\.com|youtu\.be|(on\.|m\.)?soundcloud\.com)/\S+$")
clip, sent = {"id": 0, "url": ""}, set()


def read_clipboard():
    try:
        if sys.platform == "darwin":
            return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
        if WINDOWS:
            import ctypes
            u, k = ctypes.windll.user32, ctypes.windll.kernel32
            u.GetClipboardData.restype = k.GlobalLock.restype = ctypes.c_void_p
            k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [ctypes.c_void_p]
            if not u.OpenClipboard(None):
                return ""
            try:
                h = u.GetClipboardData(13)  # CF_UNICODETEXT
                ptr = k.GlobalLock(h) if h else None
                text = ctypes.wstring_at(ptr) if ptr else ""
                if ptr:
                    k.GlobalUnlock(h)
                return text
            finally:
                u.CloseClipboard()
        for cmd in (["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"]):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
            except OSError:
                continue
    except Exception:  # clipboard holds an image, is locked by another app, …
        pass
    return ""


def clipboard_watcher():
    last = read_clipboard().strip()  # whatever was copied before MUSICXX opened isn't offered
    while True:
        time.sleep(1.5)
        if not settings["clipboard"]:
            continue
        text = read_clipboard().strip()
        if text != last:
            last = text
            if LINK.match(text) and text not in sent:
                clip.update(id=clip["id"] + 1, url=text)


def update_tools():
    """Once a day, upgrade yt-dlp and spotdl: the sites change often and old versions stop working.
    Only in the installer's venv."""
    if sys.prefix == sys.base_prefix:
        return
    stamp = DATA / "tools-updated"
    if stamp.exists() and time.time() - stamp.stat().st_mtime < 86400:
        return
    # shortcut: a download started during the ~30 s upgrade could hit a half-installed tool; fine for a daily job
    while any(j["status"] == "running" for j in jobs):
        time.sleep(30)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]", "spotdl", "deno"], capture_output=True)
    if r.returncode == 0:
        DATA.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    for module in ("mutagen", "shazamio"):
        ensure(module)


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

    def send_file(self, f):
        """Stream an audio file, with byte ranges: the app window's player needs them to play and seek."""
        size = f.stat().st_size
        start, end = 0, size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):
            start, end = (int(m[1]), int(m[2] or end)) if m[1] else (max(0, size - int(m[2])), end)
            end = min(end, size - 1)
            if start > end:
                return self.send("{}", code=416)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", {".wav": "audio/wav", ".aiff": "audio/aiff", ".mp3": "audio/mpeg", ".flac": "audio/flac"}[f.suffix.lower()])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := fh.read(min(1 << 16, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):  # the player skipped ahead and dropped this request
            pass

    def do_GET(self):
        if self.path == "/":
            self.send(PAGE, "text/html; charset=utf-8")
        elif self.path == "/jobs":
            self.send(json.dumps([{k: v for k, v in j.items() if k not in PRIVATE} for j in jobs]))
        elif self.path == "/show":  # the app icon was clicked again while MUSICXX is running
            self.send("{}")
            show()
        elif self.path == "/clip":
            self.send(json.dumps(clip))
        elif self.path == "/clipboard":
            self.send(json.dumps({"text": read_clipboard().strip()}))
        elif self.path == "/library":
            self.send(json.dumps(library_list()))
        elif self.path == "/settings":
            self.send(json.dumps({**settings, "syncs": syncs, "window": bool(WINDOW)}))
        elif self.path.startswith("/file?"):
            # Path(...).name strips any directories, so only files directly inside OUT can be served
            f = OUT / Path(parse_qs(urlsplit(self.path).query).get("name", [""])[0]).name
            if f.suffix.lower() not in AUDIO or not f.is_file():
                return self.send("{}", code=404)
            self.send_file(f)
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
        size = int(self.headers.get("Content-Length") or 0)
        if size > 8_000_000:  # a 12-second recording is ~200 KB
            return self.send('{"error":"too large"}', code=413)
        try:
            body = json.loads(self.rfile.read(size) or "{}")
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            return self.send('{"error":"bad request"}', code=400)
        if self.path == "/quit":
            self.send("{}")
            return threading.Thread(target=quit_app).start()
        if self.path == "/open":
            # Path(...).name strips any directories, so this can only point inside OUT
            song = song_file(Path(str(body.get("name", ""))).name) if body.get("name") else None
            reveal(song)
            return self.send("{}")
        job = next((j for j in jobs if j["id"] == body.get("id")), None)
        if self.path == "/cancel" and job and job["status"] == "running":
            job["cancelled"] = True
            kill(job.get("proc"))
            return self.send("{}")
        if self.path == "/retry" and job and job["status"] in ("failed", "cancelled"):
            new = start(job["url"], command(job["url"]) or job["cmd"],
                        **{k: job[k] for k in ("chart", "shazam", "cover", "retry", "total", "sync") if k in job})
            return self.send(json.dumps([new["id"]]))
        if self.path == "/settings":
            return self.send(json.dumps(update_settings(body)))
        if self.path == "/choose-folder":
            return self.send(json.dumps({"folder": choose_folder()}))
        if self.path == "/sync":
            url = str(body.get("url", ""))
            if not (PLAYLIST.search(url) and command(url)):
                return self.send('{"error":"not a playlist"}', code=400)
            syncs[:] = [e for e in syncs if e["url"] != url] + ([{"url": url, "last": time.time()}] if body.get("on") else [])
            save("sync.json", syncs)
            return self.send(json.dumps(syncs))
        if self.path == "/sync-now":
            for entry in syncs:
                sync_now(entry)
            return self.send("{}")
        if self.path == "/listen-in-browser":  # the app window may not get the microphone; the browser always can
            webbrowser.open(f"http://localhost:{PORT}/#listen")
            return self.send("{}")
        if self.path == "/shazam":
            try:
                audio = base64.b64decode(str(body.get("audio", "")), validate=True)
            except ValueError:
                return self.send('{"error":"bad audio"}', code=400)
            hit = recognize(audio)
            if not hit:
                return self.send('{"error":"no match"}')
            # artist/title come from Shazam; strip leading dashes so spotdl can't read them as options
            name = f"{hit['artist']} - {hit['title']}".lstrip("- ")
            job = start(name, spotdl(name), shazam=True, cover=hit["cover"], retry=ytmusic(hit["artist"], hit["title"]))
            return self.send(json.dumps({**hit, "job": job["id"]}))
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
            job = start(label, spotdl(*(f"{s['artist']} - {s['title']}".lstrip("-") for _, s in picked)), chart=True, total=len(picked))
            return self.send(json.dumps([job["id"]]))
        urls = str(body.get("text", "")).split()
        sent.update(urls)  # don't offer these from the clipboard again
        self.send(json.dumps([start(url, command(url))["id"] for url in urls]))


def update_settings(body):
    """Apply the changes from the Settings screen, ignoring anything invalid. Returns the settings now in force."""
    global OUT
    if body.get("format") in FORMATS:
        settings["format"] = body["format"]
    for k in ("open_when_done", "clipboard"):
        if isinstance(body.get(k), bool):
            settings[k] = body[k]
    folder = body.get("folder")
    if isinstance(folder, str) and folder.strip():
        f = Path(folder.strip()).expanduser()
        if f.is_absolute():
            try:
                f.mkdir(parents=True, exist_ok=True)
                settings["folder"], OUT = str(f), f
            except OSError:
                pass
    save("settings.json", settings)
    return {**settings, "syncs": syncs, "window": bool(WINDOW)}


def choose_folder():
    """The system's folder picker. "" if cancelled or unavailable."""
    try:
        if WINDOW:
            import webview
            picked = WINDOW.create_file_dialog(getattr(webview, "FOLDER_DIALOG", 20), directory=str(OUT))
            return picked[0] if picked else ""
        if sys.platform == "darwin":
            return subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Save songs to:")'],
                                  capture_output=True, text=True).stdout.strip()
        if WINDOWS:
            ps = ("Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                  "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }")
            return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
        return subprocess.run(["zenity", "--file-selection", "--directory"], capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


WINDOW = None  # the native app window, when pywebview is available


def quit_app():
    server.shutdown()
    if WINDOW:
        WINDOW.destroy()


def show():
    """Bring the window to the front, or open the page in the browser when there is no window."""
    if not WINDOW:
        return webbrowser.open(f"http://localhost:{PORT}")
    WINDOW.restore()
    WINDOW.show()
    if sys.platform == "darwin":
        from AppKit import NSApp
        from PyObjCTools import AppHelper
        AppHelper.callAfter(lambda: NSApp.activateIgnoringOtherApps_(True))


def watch_busy():
    """Dock badge with the number of running downloads (Mac), and ask before closing the window mid-download."""
    shown = None
    while True:
        n = sum(j["status"] == "running" for j in jobs)
        if n != shown:
            shown = n
            if WINDOW:
                WINDOW.confirm_close = bool(n)
                if sys.platform == "darwin":
                    try:
                        from AppKit import NSApp
                        from PyObjCTools import AppHelper
                        AppHelper.callAfter(lambda: NSApp.dockTile().setBadgeLabel_(str(n) if n else None))  # AppKit: main thread only
                    except Exception:
                        pass
        time.sleep(1)


def open_window(url):
    """Show MUSICXX in its own window; blocks until it's closed. False if pywebview isn't usable here."""
    global WINDOW
    if not (sys.platform == "darwin" or WINDOWS):
        return False  # Linux needs extra GUI libraries; the browser works everywhere
    if not importlib.util.find_spec("webview"):
        threading.Thread(target=ensure, args=("webview", "pywebview"), daemon=True).start()  # ready from the next launch
        return False
    try:
        if sys.platform == "darwin":  # menu-bar name; otherwise macOS shows "Python"
            from Foundation import NSBundle
            NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "MUSICXX"
        import webview
        WINDOW = webview.create_window("MUSICXX", url, width=1180, height=820, min_size=(720, 560), background_color="#1e2124")
        webview.start(icon=str(LOGO))
        return True
    except Exception:
        WINDOW = None
        return False


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MUSICXX</title><link rel="icon" href="/logo.png"><style>
:root{--bg:#1e2124;--bg2:#33373c;--panel:#272a2e;--hover:#31353a;--line:#3b3f45;--text:#f5f5f7;--muted:#98989d;
--red:#e0362c;--red2:#e0362c;--ok:#30d158;--bad:#ff453a;--sp:#1db954;--yt:#ff3b30;--sc:#ff7700;--shadow:0 4px 16px rgba(0,0,0,.35)}
:root{color-scheme:dark;--metal:linear-gradient(180deg,#3a3e44,#2a2d32)}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;color:var(--text);background:repeating-linear-gradient(90deg,rgba(255,255,255,.015) 0 1px,transparent 1px 3px),linear-gradient(160deg,#2c3035 0%,#1a1c1f 50%,#26292e 100%) fixed;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;display:flex;flex-direction:column}
button{font:inherit;border:0;cursor:pointer;color:inherit;background:none}
button:focus-visible,textarea:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--red);outline-offset:2px}
svg.i{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;vertical-align:-3px}
header{display:flex;align-items:center;gap:12px;padding:10px 20px;border-bottom:1px solid #15171a;background:var(--metal);box-shadow:inset 0 1px rgba(255,255,255,.08);position:sticky;top:0;z-index:5}
header img{width:26px;height:auto}
.brand{flex:1;line-height:1.15}.brand b{font-size:15px;letter-spacing:.5px}.brand small{display:block;color:var(--muted);font-size:12px}
.ghost{display:inline-flex;align-items:center;gap:6px;padding:7px 10px;border-radius:6px;color:var(--muted);font-size:13px}.ghost:hover{background:var(--hover);color:var(--text)}
main{flex:1;overflow-y:auto}main:focus,#charts:focus,#explore:focus{outline:none}
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
footer{padding:10px 18px 16px;border-top:1px solid #15171a;background:var(--metal);box-shadow:inset 0 1px rgba(255,255,255,.08)}
#f{max-width:760px;margin:0 auto;display:flex;align-items:center;gap:8px;padding:5px;border-radius:8px;background:#1a1c1f;border:1px solid var(--line)}
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
/* 3D logo */
#hero .logo3d{perspective:700px;display:inline-block}
#hero img{animation:spin3d 7s linear infinite;transform-style:preserve-3d;filter:drop-shadow(0 14px 18px rgba(0,0,0,.55))}
@keyframes spin3d{0%{transform:rotateY(0) rotateX(10deg)}25%{transform:rotateY(90deg) rotateX(0) translateY(-6px)}50%{transform:rotateY(180deg) rotateX(-10deg)}75%{transform:rotateY(270deg) rotateX(0) translateY(-6px)}100%{transform:rotateY(360deg) rotateX(10deg)}}
header img{animation:sway 5s ease-in-out infinite}@keyframes sway{50%{transform:perspective(200px) rotateY(35deg)}}
/* Listen (Shazam) */
.listen{background:var(--red);color:#fff;font-weight:600}.listen:hover{background:var(--red);filter:brightness(1.08);box-shadow:none}
#hero .listen{padding:9px 16px;font-size:14px}
#rec{position:fixed;inset:0;z-index:20;display:none;place-items:center;background:rgba(10,11,13,.86)}#rec.show{display:grid}
#rec .box{text-align:center;width:min(360px,90vw)}
#rec .orb{position:relative;width:150px;height:150px;margin:0 auto 22px;display:grid;place-items:center;perspective:600px}
#rec .orb img{width:70px;position:relative;z-index:1;animation:spin3d 2.2s linear infinite}
#rec .orb i{position:absolute;inset:0;border-radius:50%;border:2px solid var(--red);opacity:0;animation:ring 2.4s ease-out infinite}
#rec .orb i:nth-child(2){animation-delay:.8s}#rec .orb i:nth-child(3){animation-delay:1.6s}
@keyframes ring{0%{transform:scale(.45);opacity:.9}100%{transform:scale(1.25);opacity:0}}
#rec.idle .orb i,#rec.idle .orb img{animation-play-state:paused}
#rec h2{margin:0 0 4px;font-size:20px;font-weight:600}#rec p{margin:0 0 18px;color:var(--muted)}
#rec .found{display:flex;align-items:center;gap:12px;text-align:left;margin:0 0 18px;padding:10px;border-radius:10px;background:var(--panel);border:1px solid var(--line)}
#rec .found img{width:56px;height:56px;border-radius:6px;object-fit:cover}#rec .found:empty{display:none}
#rec .row2{display:flex;gap:8px;justify-content:center}
.shz i{background:#08f}
/* scrolling promo bar */
#ticker{flex:none;height:34px;overflow:hidden;position:relative;border-top:1px solid #15171a;
background:repeating-linear-gradient(-45deg,rgba(255,255,255,.035) 0 10px,transparent 10px 20px),linear-gradient(180deg,#2e3136,#1c1e22);background-size:28px 28px,auto;animation:stripes 1.2s linear infinite}
@keyframes stripes{to{background-position:28px 0,0 0}}
#ticker .tk{display:flex;width:max-content;height:100%;align-items:center;animation:marq 26s linear infinite}
#ticker:hover .tk{animation-play-state:paused}
@keyframes marq{to{transform:translateX(-50%)}}
#ticker .seg{display:flex;align-items:center;gap:18px;padding-right:18px;white-space:nowrap}
#ticker b{font-size:12.5px;font-weight:800;letter-spacing:3px;background:linear-gradient(90deg,#d8dbe0,#fff,#9aa0a8);-webkit-background-clip:text;background-clip:text;color:transparent}
#ticker .eq{display:flex;gap:2px;align-items:flex-end;height:16px}#ticker .eq i{width:3px;background:var(--red);border-radius:1px;animation:eq .9s ease-in-out infinite alternate}
#ticker .eq i:nth-child(2){animation-delay:-.3s}#ticker .eq i:nth-child(3){animation-delay:-.6s}#ticker .eq i:nth-child(4){animation-delay:-.15s}#ticker .eq i:nth-child(5){animation-delay:-.45s}
@keyframes eq{from{height:3px}to{height:16px}}
#ticker .disc{width:18px;height:18px;border-radius:50%;background:radial-gradient(circle,var(--red) 0 3px,#111 3px 4px,transparent 4px),repeating-radial-gradient(circle,#222 0 1px,#3a3d42 1px 2px);animation:spin 2s linear infinite}
#ticker .dia{width:7px;height:7px;transform:rotate(45deg);background:var(--c)}
#ticker .wave{width:44px;height:14px;background:linear-gradient(90deg,var(--red) 50%,transparent 0) 0 50%/8px 2px repeat-x;mask:radial-gradient(circle at 4px 50%,#000 3px,transparent 3.5px) 0 0/8px 14px;-webkit-mask:radial-gradient(circle at 4px 50%,#000 3px,transparent 3.5px) 0 0/8px 14px;animation:wv .6s linear infinite}
@keyframes wv{to{background-position:8px 50%}}
#toast{bottom:134px}#player{bottom:54px}body:not(.explore):not(.charts) #player{bottom:144px}
/* card controls */
.status .x{margin-left:auto}.cv{width:40px;height:40px;border-radius:5px;object-fit:cover;float:right;margin:0 0 6px 10px}
.src{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.syn i{background:#bf5af2}
.songs li button img{width:22px;height:22px;border-radius:3px;object-fit:cover;flex:none}
.pill.on{background:var(--text);color:var(--bg)}
.cancelled{color:var(--muted)}
/* Library */
#library{display:none;flex:1;overflow-y:auto}body.library #library{display:block}body.library #main,body.library footer{display:none}
.wrap.wide{max-width:1100px}
.lbar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}.lbar .sbox{flex:1;margin:0;min-width:220px}.lbar small{color:var(--muted)}
.ltab{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
.ltab th{text-align:left;font-weight:500;color:var(--muted);font-size:12px;padding:8px 10px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
.ltab th[data-s]:hover{color:var(--text)}.ltab th.on{color:var(--text)}.ltab th.on::after{content:" ↓"}.ltab th.on.asc::after{content:" ↑"}
.ltab td{padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}.ltab tr:last-child td{border-bottom:0}.ltab tbody tr:hover{background:var(--hover)}
.ltab tr.playing td{background:var(--hover)}.ltab .tt{display:flex;align-items:center;gap:10px;min-width:0;max-width:380px}
.ltab .tt img,.ltab .tt .ph0{width:34px;height:34px;border-radius:4px;object-fit:cover;flex:none;background:var(--bg2)}
.ltab .tt b{display:block;font-weight:500;overflow:hidden;text-overflow:ellipsis}.ltab .tt span{display:block;color:var(--muted);overflow:hidden;text-overflow:ellipsis}
.ltab .num{font-variant-numeric:tabular-nums}.ltab .mut{color:var(--muted)}.ltab .acts{text-align:right}.ltab .acts .ico{display:inline-grid}
#player audio{display:block;width:100%;padding:0 10px 10px}
/* Settings */
#settings{position:fixed;inset:0;z-index:20;display:none;place-items:center;background:rgba(0,0,0,.55)}#settings.show{display:grid}
#settings .box{width:min(520px,92vw);max-height:88vh;overflow-y:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:0 20px 50px rgba(0,0,0,.5)}
#settings h2{margin:0;padding:14px 18px;font-size:16px;font-weight:600;border-bottom:1px solid var(--line);display:flex;align-items:center}#settings h2 .ico{margin-left:auto}
.set{padding:14px 18px;border-bottom:1px solid var(--line)}.set:last-child{border-bottom:0}
.set label.t{display:block;font-weight:600;margin-bottom:6px}.set p{margin:6px 0 0;color:var(--muted);font-size:12px}
.set .line{display:flex;gap:8px;align-items:center}.set input[type=text],.set select{flex:1;min-width:0;font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:7px 9px}
.set .chk{display:flex;gap:10px;align-items:center;margin:4px 0}.set .chk input{width:16px;height:16px;accent-color:var(--red)}
.syncs{list-style:none;margin:8px 0 0;padding:0}.syncs li{display:flex;gap:8px;align-items:center;padding:6px 0;border-top:1px solid var(--line);font-size:13px}
.syncs li span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.syncs li small{color:var(--muted)}
/* copied-link banner */
#clip{position:fixed;top:62px;right:16px;z-index:12;display:none;align-items:center;gap:10px;max-width:min(520px,92vw);padding:10px 10px 10px 14px;border-radius:10px;background:var(--panel);border:1px solid var(--line);box-shadow:0 10px 30px rgba(0,0,0,.4);animation:fade .2s}
#clip.show{display:flex}#clip .u{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:12.5px}#clip b{white-space:nowrap}
@media (max-width:560px){.brand small,.hint,#folder{display:none}#hero h1{font-size:21px}.genres{grid-template-columns:1fr}.genre{min-height:64px}
nav button{padding:5px 9px}.grid{grid-template-columns:repeat(2,1fr);gap:10px}#player{right:16px;bottom:16px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header><img src="/logo.png" alt=""><div class="brand"><b>MUSICXX</b><small>Spotify · YouTube · SoundCloud → .wav</small></div>
<nav><button id="tabD" class="on"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>Downloads<sup id="busy"></sup></button><button id="tabL"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4v16M9 4v16M14 4l6 16"/></svg>Library</button><button id="tabE"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>Explore</button><button id="tabC"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>Charts</button></nav>
<button class="ghost" id="folder" title="Open the folder your songs save to"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Open folder</button><button class="ghost" id="gear" title="Settings"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>Settings</button><button class="ghost" id="quit" title="Stop MUSICXX"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v9M6.3 6.3a8 8 0 1 0 11.4 0"/></svg>Quit</button></header>
<main id="main"><div id="chat"><div id="hero"><span class="logo3d"><img src="/logo.png" alt="MUSICXX logo"></span><h1>Drop a link. Get a <span class="fmt">.wav</span>.</h1>
<p>Songs, playlists and albums save straight to <b class="where">Desktop/OK</b>, with their key, BPM, genre and cover art.</p>
<div class="chips"><span class="chip sp"><i></i>Spotify</span><span class="chip yt"><i></i>YouTube</span><span class="chip sc"><i></i>SoundCloud</span></div>
<p style="margin-top:22px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap"><button class="pill listen" data-listen><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>Identify a song playing near you</button><button class="pill" data-charts><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>Browse the top 100 R&amp;B, Afrobeats &amp; US Rap</button></p></div></div></main>
<section id="charts"><div class="wrap">
<div class="genres"><button class="genre" data-k="rnb"><b>R&amp;B</b><small>Top 100 worldwide</small></button>
<button class="genre" data-k="afro"><b>Afrobeats</b><small>Top 100</small></button>
<button class="genre" data-k="rap"><b>US Rap</b><small>Top 100 in the US</small></button></div>
<div class="chead"><h2><span id="cname">Loading…</span><small>Trending on Shazam · updated weekly</small></h2><button class="big" id="all"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>Download all</button></div>
<ul class="tracks" id="tracks"></ul></div></section>
<section id="library"><div class="wrap wide">
<div class="lbar"><form class="sbox" onsubmit="return false"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="lq" type="search" placeholder="Search your songs by title, artist, genre or key…" aria-label="Search library" autocomplete="off"></form><small id="lcount"></small></div>
<table class="ltab"><thead><tr><th data-s="title">Title</th><th data-s="camelot">Key</th><th data-s="bpm">BPM</th><th data-s="genre">Genre</th><th data-s="ext">Format</th><th data-s="added" class="on">Added</th><th></th></tr></thead><tbody id="lrows"></tbody></table>
<div class="empty" id="lempty" hidden>No songs yet. Everything you download shows up here.</div></div></section>
<section id="explore"><div class="wrap">
<div class="plats"><button class="plat" data-p="spotify"><i></i>Spotify</button><button class="plat" data-p="youtube"><i></i>YouTube</button><button class="plat" data-p="soundcloud"><i></i>SoundCloud</button></div>
<form class="sbox" id="sf"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="q" type="search" aria-label="Search" autocomplete="off"><button class="big">Search</button></form>
<div class="sugg" id="sugg"></div><div class="fbar" id="fbar"></div><div class="grid" id="grid"></div></div></section>
<div id="player" role="region" aria-label="Player"><div class="ph"><div class="ti"><b id="pt"></b><span id="pa"></span></div>
<button class="ico" id="pdl" title="Download"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg></button><a class="ico" id="popen" target="_blank" rel="noopener" title="Open on the site"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg></a><button class="ico" id="px" title="Close (Esc)"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div><div id="pframe"></div></div>
<div id="toast" role="status"></div>
<footer><form id="f"><span id="detect"></span><textarea id="t" rows="1" placeholder="Paste a Spotify, YouTube or SoundCloud link…" aria-label="Links"></textarea>
<button type="button" class="pill" data-listen title="Listen to a song playing near you and download it"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>Listen</button><button type="button" class="pill" id="paste">Paste</button><button class="send" id="go" disabled><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>Download</button></form>
<div class="hint">Enter to download · paste several links at once · or drag a link anywhere</div></footer>
<div id="drop"><div>Drop the link to download</div></div>
<div id="clip" role="alert"><b>Copied link</b><span id="clipk"></span><span class="u" id="clipu"></span><button class="send" id="clipgo"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>Download</button><button class="ico" id="clipx" title="Dismiss"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>
<div id="settings" role="dialog" aria-modal="true" aria-labelledby="seth"><div class="box"><h2 id="seth">Settings<button class="ico" id="setx" title="Close (Esc)"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></h2>
<div class="set"><label class="t" for="sfolder">Save songs to</label><div class="line"><input type="text" id="sfolder" spellcheck="false"><button class="pill" id="schoose">Choose…</button></div></div>
<div class="set"><label class="t" for="sformat">File format</label><div class="line"><select id="sformat"><option value="wav">WAV · lossless</option><option value="aiff">AIFF · lossless, best for DJ software</option><option value="flac">FLAC · lossless, smaller files</option><option value="mp3">MP3 · 320 kbps, smallest</option></select></div>
<p>All formats get the artist, title, cover, key and BPM written inside the file. AIFF and FLAC show them in every DJ app; WAV tags are read by Serato and Rekordbox but not by Apple Music.</p></div>
<div class="set"><label class="chk"><input type="checkbox" id="sopen">Open the folder when a download finishes</label><label class="chk"><input type="checkbox" id="sclip">Offer to download links I copy</label></div>
<div class="set"><label class="t">Synced playlists</label><p>New songs added to these playlists download automatically every 3 hours while MUSICXX is open. Turn it on with <b>Keep synced</b> on a downloaded playlist.</p><ul class="syncs" id="ssyncs"></ul></div></div></div>
<div id="ticker" aria-hidden="true"><div class="tk"></div></div>
<div id="rec" role="dialog" aria-modal="true" aria-labelledby="rech"><div class="box"><div class="orb"><i></i><i></i><i></i><img src="/logo.png" alt=""></div>
<h2 id="rech">Listening…</h2><p id="recp">Hold your computer near the music.</p><div class="found" id="recf"></div>
<div class="row2"><button class="pill" id="recagain" hidden>Try again</button><button class="pill" id="recx">Cancel</button></div></div></div>
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
let cfg={folder:'',format:'wav',syncs:[],window:false};
const where=()=>cfg.folder?cfg.folder.split(/[\\/]/).filter(Boolean).slice(-2).join('/'):'Desktop/OK';
const small=u=>u?u.replace('600x600bb','100x100bb'):'';
const X='<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
function card(j){
  const n=j.saved,k=kind(j.url);let head,bar='',meta='';
  if(j.status=='running'){
    head=`<span class="spin"></span>Downloading…<button class="ico x" data-cancel="${j.id}" title="Cancel">${X}</button>`;
    const p=j.total?Math.min(100,n/j.total*100):j.pct;
    bar=`<div class="bar ${p?'':'indef'}"><i style="width:${p}%"></i></div>`;
    meta=(j.total?`${n} / ${j.total} songs`:j.pct?`${j.pct.toFixed(0)}%`:'')+(j.log?' · '+esc(j.log):'');
  }else if(j.status=='done')head=`<span class="done"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5 9-10"/></svg>${plural(n)} saved to ${esc(where())}</span>`;
  else if(j.status=='cancelled')head=`<span class="cancelled">Cancelled${n?' ('+plural(n)+' saved)':''}</span>`;
  else{head=`<span class="failed">✕ Couldn't download${n?' everything ('+plural(n)+' saved)':''}</span>`;meta=esc(j.log)}
  const all=open.has(j.id),list=all?j.songs:j.songs.slice(-5);
  const cover=j.cover||Object.values(j.info||{}).map(i=>i.cover).find(Boolean);
  const src=`<div class="src">${cover?`<img class="cv" src="${esc(small(cover))}" alt="">`:''}${j.shazam?'<span class="chip shz"><i></i>Identified by Shazam</span>':j.chart?'<span class="chip ch">Chart</span>':chip(k)}${j.sync?'<span class="chip syn"><i></i>Synced playlist</span>':''}</div>`;
  const songs=list.length?`<ul class="songs">${list.map(s=>`<li><button data-song="${esc(s)}">${j.info&&j.info[s]&&j.info[s].cover?`<img src="${esc(small(j.info[s].cover))}" alt="">`:'<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>'}<span>${esc(s)}</span><em>Show file</em></button>${tags(j.info&&j.info[s])}</li>`).join('')}</ul>`+
    (j.songs.length>5?`<button class="more" data-more="${j.id}">${all?'Show less':'Show all '+j.songs.length}</button>`:''):'';
  const pl=k&&/Playlist|Album/.test(k[2])&&!j.chart,on=cfg.syncs.some(e=>e.url==j.url);
  const sync=pl&&j.status!='running'?`<button class="pill${on?' on':''}" data-sync="${esc(j.url)}" title="Download new songs from this playlist automatically">${on?'✓ Synced':'Keep synced'}</button>`:'';
  const retry=/failed|cancelled/.test(j.status)?`<div class="actions"><button class="pill" data-retry="${j.id}">Retry</button>${sync}</div>`:'';
  const acts=retry||(j.status=='done'?`<div class="actions">${sync}<button class="pill" data-folder><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Open folder</button></div>`:'');
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
function notify(j){if(!cfg.window&&document.hidden&&window.Notification&&Notification.permission=='granted')
  new Notification(j.status=='done'?'MUSICXX ✓ Download finished':'MUSICXX ✕ Download failed',{body:j.status=='done'?plural(j.saved)+' saved to '+where():short(j.url),icon:'/logo.png'})}
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
function tab(name){  // '' = Downloads, 'library', 'explore', 'charts'
  for(const c of ['charts','explore','library'])document.body.classList.toggle(c,name==c);
  $('tabD').classList.toggle('on',!name);$('tabL').classList.toggle('on',name=='library');$('tabE').classList.toggle('on',name=='explore');$('tabC').classList.toggle('on',name=='charts');
  if(name=='charts')loadChart(ck);else if(name=='explore')$('q').focus();else if(name=='library'){loadLib();$('lq').focus()}else t.focus()}
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
  <button class="ico${g?' ok':''}" data-get="${i}" title="Download">${g?OK:DL}</button></div></div>`}
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
function play(s){const e=embed(s.url);if(!e)return;cur=s;$('pdl').hidden=$('popen').hidden=false;
  $('pt').textContent=s.title;$('pa').textContent=s.artist;$('popen').href=s.url;$('popen').title='Open on '+site(s.url);
  $('pframe').innerHTML=`<iframe src="${esc(e[0])}" height="${e[1]}" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture" referrerpolicy="strict-origin-when-cross-origin" title="${esc(s.title)}"></iframe>`;
  $('player').classList.add('show');markGot();
  document.querySelectorAll('.res').forEach(r=>r.classList.toggle('playing',results[r.dataset.i]?.url==s.url))}
async function grab(s){if(!s||got.has(s.url))return;got.add(s.url);markGot();askNotify();
  await post('/download',{text:s.url});toast(`Downloading “${s.title}” to ${where()}`);poll()}
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
$('tabD').onclick=()=>tab('');$('tabL').onclick=()=>tab('library');$('tabE').onclick=()=>tab('explore');$('tabC').onclick=()=>tab('charts');
$('tracks').onclick=async e=>{const b=e.target.closest('.dl');if(!b||b.classList.contains('ok'))return;
  askNotify();b.classList.add('ok');b.innerHTML=OK;queued.add(ck+b.dataset.rank);
  await post('/download',{chart:ck,ranks:[+b.dataset.rank]});toast('Added to Downloads');poll()};
$('all').onclick=async()=>{const d=cdata[ck];if(!d)return;const n=d.songs.length;
  if(!confirm(`Download all ${n} ${d.name} songs to ${where()}?\n\nThat's about ${(n*(cfg.format=='mp3'?0.01:cfg.format=='flac'?0.025:0.04)).toFixed(1)} GB of ${cfg.format.toUpperCase()} files and can take 15–30 minutes.`))return;
  askNotify();for(let i=1;i<=n;i++)queued.add(ck+i);
  await post('/download',{chart:ck});tab('');toast(`Downloading ${n} songs…`);poll()};
let clipSeen=null;
async function poll(){try{render(await (await fetch('/jobs')).json());
  const c=await (await fetch('/clip')).json();
  if(clipSeen===null)clipSeen=c.id;  // only links copied from now on
  else if(c.id>clipSeen){clipSeen=c.id;$('clipk').innerHTML=chip(kind(c.url));$('clipu').textContent=short(c.url);$('clip').dataset.url=c.url;$('clip').classList.add('show')}
}catch(e){}}
$('clipgo').onclick=()=>{send($('clip').dataset.url);$('clip').classList.remove('show');tab('');toast('Downloading the copied link')};
$('clipx').onclick=()=>$('clip').classList.remove('show');
// ---- Library: every song in the folder, with key / BPM / genre, searchable and sortable
let lib=[],lsort='added',lasc=false,lplaying='';
async function loadLib(){try{lib=await (await fetch('/library')).json();renderLib()}catch(e){}}
function renderLib(){
  const q=$('lq').value.trim().toLowerCase();
  let rows=lib.filter(s=>!q||[s.title,s.artist,s.genre,s.key,s.camelot,s.album].join(' ').toLowerCase().includes(q));
  const v=s=>lsort=='camelot'?(s.camelot?parseInt(s.camelot)*2+(s.camelot.endsWith('B')?1:0):99):s[lsort]??'';
  rows.sort((a,b)=>{const x=v(a),y=v(b);const c=typeof x=='number'&&typeof y=='number'?x-y:String(x).localeCompare(String(y));return lasc?c:-c});
  document.querySelectorAll('.ltab th[data-s]').forEach(th=>{th.classList.toggle('on',th.dataset.s==lsort);th.classList.toggle('asc',lasc)});
  $('lcount').textContent=`${plural(rows.length)}${q?' found':''} · ${where()}`;
  $('lempty').hidden=!!lib.length;
  $('lrows').innerHTML=rows.map(s=>`<tr class="${lplaying==s.file?'playing':''}"><td><div class="tt">${s.cover?`<img src="${esc(small(s.cover))}" alt="" loading="lazy">`:'<span class="ph0"></span>'}<div style="min-width:0"><b>${esc(s.title)}</b><span>${esc(s.artist)}</span></div></div></td>
  <td>${s.camelot?`<b class="tag cam" style="--h:${(parseInt(s.camelot)-1)*30}">${esc(s.camelot)}</b> <span class="mut">${esc(s.key)}</span>`:s.state=='analyzing'?'<span class="mut">Analyzing…</span>':''}</td>
  <td class="num">${s.bpm||''}</td><td class="mut">${esc(s.genre||'')}</td><td class="mut">${esc(s.ext)}</td><td class="mut num">${new Date(s.added*1000).toLocaleDateString()}</td>
  <td class="acts"><button class="ico" data-lplay="${esc(s.file)}" title="Play">${'<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4l13 8-13 8z"/></svg>'}</button><button class="ico" data-lshow="${esc(s.name)}" title="Show in folder">${'<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>'}</button></td></tr>`).join('')}
$('lq').oninput=renderLib;
document.querySelector('.ltab thead').onclick=e=>{const th=e.target.closest('th[data-s]');if(!th)return;
  if(lsort==th.dataset.s)lasc=!lasc;else{lsort=th.dataset.s;lasc=/title|genre|camelot|ext/.test(lsort)}renderLib()};
$('lrows').onclick=e=>{const p=e.target.closest('[data-lplay]'),f=e.target.closest('[data-lshow]');
  if(f)post('/open',{name:f.dataset.lshow});
  if(p){const s=lib.find(x=>x.file==p.dataset.lplay);if(!s)return;cur=null;lplaying=s.file;
    $('pt').textContent=s.title;$('pa').textContent=s.artist;$('pdl').hidden=$('popen').hidden=true;
    $('pframe').innerHTML=`<audio controls autoplay src="/file?name=${encodeURIComponent(s.file)}"></audio>`;$('player').classList.add('show');renderLib()}};
setInterval(()=>{if(document.body.classList.contains('library'))loadLib()},4000);  // analysis progress
// ---- Settings
function applyCfg(c){cfg=c;
  document.querySelectorAll('.where').forEach(e=>e.textContent=where());document.querySelectorAll('.fmt').forEach(e=>e.textContent='.'+cfg.format);
  $('sfolder').value=cfg.folder;$('sformat').value=cfg.format;$('sopen').checked=cfg.open_when_done;$('sclip').checked=cfg.clipboard;
  $('ssyncs').innerHTML=cfg.syncs.length?cfg.syncs.map(e=>`<li>${chip(kind(e.url))}<span title="${esc(e.url)}">${esc(short(e.url))}</span><small>${e.last?'checked '+new Date(e.last*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):''}</small><button class="pill" data-unsync="${esc(e.url)}">Stop</button></li>`).join('')+
    '<li><span></span><button class="pill" id="syncnow">Check all now</button></li>':'<li><span class="mut">None yet.</span></li>';
  for(const id in last)delete last[id];poll()}  // re-render cards (folder name, sync buttons)
const saveCfg=async b=>applyCfg(await (await post('/settings',b)).json());
fetch('/settings').then(r=>r.json()).then(applyCfg).catch(()=>{});
$('gear').onclick=()=>{$('settings').classList.add('show');$('sfolder').focus()};
$('setx').onclick=()=>$('settings').classList.remove('show');
$('settings').onclick=e=>{if(e.target.id=='settings')$('setx').click()};
$('sfolder').onchange=()=>saveCfg({folder:$('sfolder').value});
$('schoose').onclick=async()=>{const r=await (await post('/choose-folder')).json();if(r.folder)saveCfg({folder:r.folder})};
$('sformat').onchange=()=>saveCfg({format:$('sformat').value});
$('sopen').onchange=()=>saveCfg({open_when_done:$('sopen').checked});
$('sclip').onchange=()=>saveCfg({clipboard:$('sclip').checked});
$('ssyncs').onclick=async e=>{const u=e.target.closest('[data-unsync]');
  if(u){await post('/sync',{url:u.dataset.unsync,on:false});saveCfg({})}
  if(e.target.id=='syncnow'){await post('/sync-now');$('setx').click();tab('');toast('Checking synced playlists for new songs…');poll()}};
addEventListener('keydown',e=>{if(e.key=='Escape'&&$('settings').classList.contains('show'))$('setx').click()});
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
$('paste').onclick=async()=>{try{t.value=(await (await fetch('/clipboard')).json()).text;update()}catch(e){}t.focus()};
chat.onclick=e=>{const b=e.target.closest('button');if(!b)return;
  if(b.dataset.song!==undefined)post('/open',{name:b.dataset.song});
  else if(b.dataset.more){const id=+b.dataset.more;open.has(id)?open.delete(id):open.add(id);poll()}
  else if(b.hasAttribute('data-folder'))post('/open');
  else if(b.dataset.cancel)post('/cancel',{id:+b.dataset.cancel}).then(poll);
  else if(b.dataset.retry)post('/retry',{id:+b.dataset.retry}).then(poll);
  else if(b.dataset.sync){const on=!cfg.syncs.some(e=>e.url==b.dataset.sync);
    post('/sync',{url:b.dataset.sync,on}).then(()=>saveCfg({}));toast(on?'Synced: new songs will download automatically':'Stopped syncing this playlist')}
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
// Promo bar: one segment of text + moving graphics, doubled so the scroll loops seamlessly
{const eq='<span class="eq"><i></i><i></i><i></i><i></i><i></i></span>',d=c=>`<span class="dia" style="--c:${c}"></span>`;
 const seg=`<span class="seg"><b>DOWNLOAD ALL YOUR FAV TRACKS</b>${eq}${d('var(--sp)')}<span class="disc"></span><b>SPOTIFY · YOUTUBE · SOUNDCLOUD</b>${d('var(--yt)')}<span class="wave"></span>${d('var(--sc)')}${eq}<b>STRAIGHT TO .WAV</b><span class="disc"></span></span>`;
 $('ticker').firstElementChild.innerHTML=seg.repeat(4)}
// Listen: record ~11 s from the mic, let Shazam identify it, and the server starts the download
const REC_SECS=11;let recStream=null,recTimer=null,recCancel=false;
function recUI(state,h,p,found){const r=$('rec');r.className='show'+(state=='idle'?' idle':'');$('rech').textContent=h;$('recp').textContent=p;
  $('recf').innerHTML=found||'';$('recagain').hidden=state!='idle';$('recx').textContent=state=='idle'?'Close':'Cancel'}
function recStop(){clearInterval(recTimer);if(recStream)recStream.getTracks().forEach(t=>t.stop());recStream=null}
async function listen(){
  recCancel=false;recUI('rec','Listening…','Allow the microphone if your browser asks.');
  try{recStream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false}})}
  catch(e){if(cfg.window){post('/listen-in-browser');return recUI('idle','Listening in your browser','This window can\'t use the microphone, so Listen opened in your web browser.')}
    return recUI('idle','Microphone blocked','Allow microphone access for this page in your browser settings, then try again.')}
  const mr=new MediaRecorder(recStream),parts=[];mr.ondataavailable=e=>parts.push(e.data);
  let left=REC_SECS;$('recp').textContent=`Hold your computer near the music · ${left}s`;
  recTimer=setInterval(()=>{left--;if(left>0)$('recp').textContent=`Hold your computer near the music · ${left}s`;else mr.stop()},1000);
  mr.onstop=async()=>{recStop();if(recCancel)return;
    recUI('rec','Identifying…','Checking with Shazam.');
    const b64=await new Promise(ok=>{const fr=new FileReader();fr.onload=()=>ok(String(fr.result).split(',')[1]);fr.readAsDataURL(new Blob(parts,{type:mr.mimeType}))});
    let r;try{r=await (await post('/shazam',{audio:b64})).json()}catch(e){r={error:'offline'}}
    if(recCancel)return;
    if(r.error)return recUI('idle',"Couldn't recognize that song",'Try again closer to the speaker, while the song is playing.');
    recUI('idle','Found it!',`Downloading to ${where()}.`,`${r.cover?`<img src="${esc(r.cover)}" alt="">`:''}<div class="ti"><b>${esc(r.title)}</b><span>${esc(r.artist)}</span></div>`);
    tab('');poll();setTimeout(()=>{if($('rech').textContent=='Found it!')$('rec').className=''},2500)};
  mr.start()}
document.addEventListener('click',e=>{if(e.target.closest('[data-listen]'))listen()});
$('recagain').onclick=listen;
$('recx').onclick=()=>{recCancel=true;recStop();$('rec').className=''};
addEventListener('keydown',e=>{if(e.key=='Escape'&&$('rec').classList.contains('show'))$('recx').click()});
setInterval(poll,1000);poll();t.focus();if(location.hash=='#listen')listen();else if(location.hash=='#settings')$('gear').click();else if(location.hash=='#library')tab('library');else if(location.hash=='#charts')tab('charts');else if(location.hash.startsWith('#explore')){tab('explore');  // #explore/[platform/]QUERY searches right away
  let q=decodeURIComponent(location.hash.slice(9));const m=q.match(/^(spotify|youtube|soundcloud)\/(.*)$/);
  if(m){q=m[2];setPlat(m[1])}if(q){$('q').value=q;doSearch()}}
</script></body></html>"""


REPO = "Ubald12/MUSICXX"


def self_update():
    """Replace musicxx.py/analyze.py with the latest from GitHub. True if anything changed.
    Skipped in the git checkout, so it never overwrites work in progress."""
    here = Path(__file__).resolve().parent
    if (here / ".git").exists() or os.environ.get("MUSICXX_UPDATED"):
        return False
    # Fetch by commit id: raw.githubusercontent caches /main/ for 5 minutes and could hand back an older version
    sha = subprocess.run(["curl", "-sf", "--max-time", "10", "-H", "Accept: application/vnd.github.sha",
                          f"https://api.github.com/repos/{REPO}/commits/main"], capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False  # offline or rate-limited: run what we have
    changed = False
    for name in ("musicxx.py", "analyze.py"):
        new = fetch(f"https://raw.githubusercontent.com/{REPO}/{sha}/{name}")
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
    saved_format = settings["format"]
    try:
        settings["format"] = "aiff"
        assert "wav" in command("https://youtu.be/x") and "wav" in spotdl("a")  # AIFF is made from WAV afterwards
        settings["format"] = "mp3"
        assert spotdl("a")[spotdl("a").index("--bitrate") + 1] == "320k" and "--audio-quality" in command("https://youtu.be/x")
        assert spotdl("a", archive="x")[-3:-1] != ["--archive", "x"] and "--archive" in spotdl("a", archive="x")
    finally:
        settings["format"] = saved_format
    assert short_key("Ab minor") == "Abm" and short_key("C major") == "C"
    assert LINK.match("https://open.spotify.com/track/1") and LINK.match("https://soundcloud.com/a/b") and not LINK.match("https://example.com/x")
    assert PLAYLIST.search("https://open.spotify.com/playlist/1") and PLAYLIST.search("https://soundcloud.com/a/sets/b")
    assert not PLAYLIST.search("https://open.spotify.com/track/1")
    print("self-test ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(self_test())
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # localhost only
    except OSError:  # already running: bring it to the front
        try:
            subprocess.run(["curl", "-s", "--max-time", "5", f"http://127.0.0.1:{PORT}/show"])
        except OSError:
            webbrowser.open(f"http://localhost:{PORT}")
        sys.exit()
    if self_update():  # restart on the new code
        server.server_close()
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())], env={**os.environ, "MUSICXX_UPDATED": "1"})
        sys.exit()
    for task in (analyzer, syncer, clipboard_watcher, update_tools, watch_busy):
        threading.Thread(target=task, daemon=True).start()
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    url = f"http://localhost:{PORT}"
    print(f"MUSICXX running at {url}")
    if not open_window(url):
        webbrowser.open(url)
        serving.join()  # until Quit
