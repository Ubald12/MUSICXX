<p align="center"><img src="logo.png" width="120" alt="MUSICXX logo"></p>

# MUSICXX

Paste a Spotify, YouTube or SoundCloud link and get the song as a **.wav** file in a folder called **OK** on your Desktop.

- **Links:** songs, Spotify playlists and albums, YouTube playlists and SoundCloud sets.
- **🔎 Explore:** search Spotify, YouTube or SoundCloud inside the app, play songs before you download, and filter SoundCloud results by edits, remixes, bootlegs, mashups and sped-up versions.
- **🎙 Listen:** like Shazam. Play a song near your computer, and MUSICXX recognizes it and downloads it.
- **🔥 Charts:** the top 100 trending R&B, Afrobeats and US Rap songs, with download-all.
- **🎹 Key, BPM, genre and cover art** for every song, including the Camelot code for harmonic mixing, written inside the file so DJ software (Rekordbox, Serato) and music players show it.
- **📚 Library:** every song in your folder, searchable and sortable by key, BPM, genre or date, with a built-in player.
- **🔁 Synced playlists:** tick **Keep synced** on a downloaded playlist and new songs added to it download automatically.
- **📋 Copy a link anywhere** and MUSICXX offers to download it.
- **📱 Send from your phone:** scan a QR code and paste links on your phone; they download on your computer.
- **🎚 Stems:** split any song into vocals, drums, bass and other.
- **🎧 16-bar intros:** make a DJ version of any song with 16 bars of its own beat before the vocal comes in, so it's easy to mix in.
- **🎤 Vocal outros:** make a DJ version where the last 16 bars with singing are only the vocals (acapella), so you can bring the next song in underneath.
- **🎛 Mixing helper:** click a song in the Library to see every song that mixes with it, and get a suggested set.
- **💿 DJ apps:** playlists for Rekordbox, Serato and other apps are written automatically.
- **✓ Exact matches:** each Spotify song shows whether it's the exact same recording or the closest match found.

Works on **Windows 10/11, macOS and Linux** computers. Phones can't run it, but they can send it links (see **Send from your phone**).

## Install

### Easiest: one-file installer

Download the file for your computer from the [latest release](https://github.com/Ubald12/MUSICXX/releases/latest):

- **Mac with an Apple chip (M1 or newer):** `MUSICXX-mac-arm64.dmg`
- **Older Intel Mac:** `MUSICXX-mac-intel.dmg`
- **Windows 10/11:** `MUSICXX-Setup-Windows.exe`

Not sure which Mac you have? Click the Apple menu, then **About This Mac**. "Chip: Apple M…" means Apple chip.

- **Mac:** open the .dmg and drag **MUSICXX** into **Applications**. The first time you open it, macOS says it can't check it for malware, because it isn't from the App Store: click **Done**, open **System Settings**, then **Privacy & Security**, scroll down and click **Open Anyway** next to MUSICXX.
- **Windows:** run the .exe. If Windows shows *"Windows protected your PC"*, click **More info**, then **Run anyway**.

Everything MUSICXX needs is inside, so there's nothing else to install.

### Or: install from the ZIP

1. **Download:** [MUSICXX ZIP](https://github.com/Ubald12/MUSICXX/archive/refs/heads/main.zip), or use the green **Code** button, then **Download ZIP**. Unzip it.
2. **Run the installer** in the unzipped folder:
   - **Windows:** double-click **`install.bat`**. If Windows shows *"Windows protected your PC"*, click **More info**, then **Run anyway**. If Python isn't installed yet, the installer installs it for you.
   - **Mac:** use **`install.command`**. (`install.bat` is the Windows installer, so ignore it on a Mac.)
     1. Double-click **`install.command`**. macOS says it can't check the file for malware, because it isn't from the App Store. Click **Done**.
     2. Open **System Settings**, then **Privacy & Security**, and scroll to the bottom. Next to *"install.command was blocked"*, click **Open Anyway**, then **Open Anyway** again and enter your Mac password.

     If it says you need Python, install it from [python.org](https://www.python.org/downloads/) and run the installer again.
   - **Linux:** open a terminal in the folder and run `bash install.command`.
3. **Wait** about 2–5 minutes while it downloads the music tools.

When it finishes, MUSICXX opens in its own window, and a **MUSICXX** shortcut appears on your Desktop. On Mac it's also in Launchpad. Start it from that shortcut from now on. You can delete the unzipped folder.

**First download on a Mac:** macOS may ask whether MUSICXX can access your Desktop. Click **Allow**, or songs can't be saved.

## Using it

- **Download a link:** paste it in the box and press Enter. Songs save to **Desktop/OK**.
- **Explore:** choose a platform, search, and press ▶ to listen or ↓ to download.
- **Listen:** click **Listen** and allow the microphone. After about 11 seconds, the song is found and downloaded.
- **Charts:** choose a genre, then download one song or all 100.
- **Library:** search your songs, click a column to sort, ▶ to play. Click a song to highlight the songs that mix with it, then **Suggest a set**.
- **Stems:** in the Library, click the ☰ button on a song. The first time, MUSICXX downloads the separation tool (about 1 GB).
- **16-bar intro:** in the Library, click **+ 16-bar intro** on a song (or click the song, then **+ 16-bar intro** in the bar above the list). In about a minute, *Song (16 Bar Intro)* appears next to it: 16 bars of the song's beat with the vocals removed, as loud as the song, then the whole original song from its normal start. Not available on Windows.
- **Vocal outro:** click **+ Vocal outro** the same way. *Song (Vocal Outro)* plays normally, then its last 16 bars of singing are vocals only. You can do both on one song: make the intro, then a vocal outro of the intro version. Not available on Windows.
- **DJ apps:** playlists are saved in **OK/Playlists**. In Rekordbox, go to **Preferences › Advanced › rekordbox xml** and choose *MUSICXX for Rekordbox.xml*; the playlists appear under "rekordbox xml". Serato gets MUSICXX crates automatically (restart Serato to see them).
- **Phone:** in **Settings**, turn on **Send songs from my phone** and scan the QR code. Your phone must be on the same Wi-Fi.
- **Several downloads:** two run at a time and the rest wait their turn. Unfinished downloads continue the next time you open MUSICXX.
- **Cancel or retry:** click ✕ on a running download, or **Retry** on one that failed.
- **⚙️ Settings:** choose where songs save, the file format (WAV, AIFF, FLAC or MP3), and turn the clipboard offer on or off.
- **Stop MUSICXX:** close the window, or click **Quit**.

## Things to know

- **Spotify songs** are found on YouTube by their recording code, because Spotify's own audio is locked. Each song shows **✓ Exact recording**, **✓ Match**, or **⚠ Closest match** if it might be a different version.
- **Sound quality:** a WAV, AIFF or FLAC can't sound better than its source. YouTube and SoundCloud serve about 128–160 kbps, shown as "Source quality" on each download.
- **Locked SoundCloud tracks:** big-label tracks on SoundCloud are locked, so MUSICXX gets those from YouTube Music too.
- **BPM is an estimate.** Hip-hop sometimes reads at half or double the usual tempo.
- **Key and BPM on Windows:** the analysis library has no Windows version, so Windows shows only the genre.
- **Use it only for music you're allowed to download.**

## Updating

MUSICXX updates itself. Each time you open it, it gets the latest version from this page. Your songs in Desktop/OK aren't touched.

The download tools (yt-dlp and spotdl) also update themselves once a day, so downloads keep working when YouTube or Spotify change.
