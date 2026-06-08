Movie trailer downloader
========================

Install requirements:

    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1

The installer checks what is missing, uses winget first, falls back to
Chocolatey when needed, and installs/updates:

    Python 3.14
    yt-dlp[default]
    FFmpeg
    Deno

    python -m pip install -U yt-dlp
    winget install Gyan.FFmpeg
    winget install DenoLand.Deno

Manual install commands are still shown above for troubleshooting, but the
preferred setup path is install.ps1. In the GUI, use the Run tab button named
"Install / Repair Dependencies".

FFmpeg must be available on PATH for the preferred output pipeline. The script
downloads the best available video/audio that yt-dlp can access, then converts
the result to H.264/AAC MP4 and normalizes trailer audio with FFmpeg.

Deno is recommended for YouTube EJS challenge solving. Node.js also works, but
yt-dlp currently requires Node 22 or newer for this path.

Preview first:

    python movie_trailer_downloader.py --root C:\movies --dry-run --include-vevo

Run for real:

    python movie_trailer_downloader.py --root C:\movies --include-vevo

Use a specific results/history file:

    python movie_trailer_downloader.py --root C:\movies --results-file trailer-results.json --include-vevo

Re-download everything, including movie folders that already have trailers:

    python movie_trailer_downloader.py --root C:\movies --include-vevo --redownload-existing

Generate a reusable cookies file:

    python movie_trailer_downloader.py --extract-cookies-from-browser edge --cookies-file youtube-cookies.txt

Use that cookies file:

    python movie_trailer_downloader.py --root C:\movies --cookies-file youtube-cookies.txt --include-vevo

Open the GUI:

    python movie_trailer_downloader.py

    or:

    python movie_trailer_downloader.py --gui

GUI controls:

    Preview
        Run without downloading or renaming.

    Download
        Start the trailer scan/download run.

    Install / Repair Dependencies
        Check and install missing tools with install.ps1.

    Cancel Task
        Cancels the current yt-dlp/FFmpeg operation, restores any .old trailer
        backup, cleans the temp folder, and keeps the app open.

    Exit
        Closes the app when idle. If a task is running, asks whether to cancel
        the active task first and keeps the app open.

Useful options:

    --check-deps
        Print dependency status and exit.

    --install-deps
        Run the bundled install.ps1 dependency bootstrapper and exit.

    --include-vevo
        Add VEVO-flavoured searches where available.

    --cookies-file youtube-cookies.txt
        Use a Netscape-format cookies file. If the file exists, the downloader
        prefers it over direct browser cookie extraction.

    --results-file trailer-results.json
        Store successful trailer results in a JSON file. The default is
        trailer-results.json beside the script.

    --extract-cookies-from-browser edge
        Generate the cookies file from a browser, then exit. Supported values
        include edge, chrome, chromium, firefox, brave, vivaldi, and opera.

    --limit 2
        Process only the first two movie folders while testing.

    --redownload-existing
        Total re-download mode. Without this option, folders that already have
        a current Movie (2026)-trailer.* file are skipped so the script can be
        restarted safely and only new movie folders are processed.

    --ignore-success-history
        Do not use trailer-results.json when deciding what to skip.

    --search-delay 2
        Seconds to wait between YouTube search queries. Default: 2. This is
        not applied to every internal yt-dlp HTTP request.

    --candidate-attempts 5
        Number of ranked trailer candidates to try before giving up on a movie.
        The first successful download is saved; the rest are skipped.

    --movie-delay 5
        Seconds to wait between movie folders that actually searched YouTube.
        Skipped folders do not wait. Default: 5.

    --download-sleep-min 3 --download-sleep-max 8
        Let yt-dlp sleep before downloads using a random delay in this range.
        Defaults: 3 to 8 seconds.

    --ffmpeg-threads 2
        Maximum FFmpeg threads for MP4 conversion. Lower values reduce CPU/RAM
        use and keep the computer responsive. Default: 2.

    --ffmpeg-preset veryfast
        FFmpeg x264 conversion preset. Faster presets use less CPU but can make
        slightly larger files. Default: veryfast.

    --ffmpeg-crf 22
        FFmpeg x264 quality value. Lower is higher quality/larger/slower; higher
        is smaller/lighter. Typical range is 18 to 23. Default: 22.

    --js-runtime node
        JavaScript runtime used by yt-dlp for YouTube challenges. Node is used
        automatically when Node 22 or newer is found on PATH. Deno is preferred
        when available. You can also provide a direct runtime path, for example
        --js-runtime node:C:\Tools\node\node.exe. Multiple runtimes can be
        comma-separated.

    --remote-components ejs:github
        Lets yt-dlp fetch current EJS challenge solver components from GitHub.
        Leave blank to disable this.

    --cookies-from-browser edge
        Let yt-dlp use browser cookies if YouTube asks for sign-in or age checks.
        You can also use chrome instead of edge.
        Leave this blank unless it is needed. On some Windows systems, browser
        cookie decryption can fail with a DPAPI error; the script will retry
        without browser cookies when that happens.

GUI cookies flow:

    1. Open the Settings tab.
    2. Choose the Cookies file path, for example youtube-cookies.txt.
    3. Pick Edge or Chrome under Extract cookies from.
    4. Click Extract Cookies.
    5. Leave Direct browser fallback blank unless the cookies file is not enough.

403 Forbidden handling:

    YouTube sometimes blocks yt-dlp API/search/download requests with
    "HTTP Error 403: Forbidden". The script automatically retries with
    alternate YouTube client profiles, and if cookies look stale it retries
    without them.

    If it still fails, refresh the cookies file from the GUI Settings tab or:

        python movie_trailer_downloader.py --extract-cookies-from-browser edge --cookies-file youtube-cookies.txt

YouTube n challenge / only images available:

    If yt-dlp reports "n challenge solving failed", install/update the EJS
    challenge solver support:

        python -m pip install -U "yt-dlp[default]"

    The script also sets yt-dlp to use Deno or Node when a supported runtime is
    installed, and it can use remote EJS components with ejs:github. If YouTube still reports
    "n challenge solving failed", install/update Deno or Node.js 22+, then
    leave JS runtime blank so the script can auto-detect it, or set JS runtime
    to deno / node:C:\path\to\node.exe. In the GUI, see Settings -> Cookies ->
    JS runtime and Remote EJS components.

    If candidate #1 still fails with "Only images are available" or "Requested
    format is not available", the script retries that candidate with alternate
    format selectors, then tries the next ranked candidates before giving up.

Best-quality MP4 conversion:

    The downloader no longer forces YouTube to provide MP4 during the download.
    It first asks yt-dlp for the best available streams, then FFmpeg converts
    the successful download to:

        Movie (2026)-trailer.mp4

    The FFmpeg output is H.264 video, AAC audio, yuv420p pixel format, faststart
    MP4, and loudnorm audio normalization. The default conversion profile is
    intentionally gentle: 2 FFmpeg threads, veryfast preset, and CRF 22. If
    FFmpeg is missing or conversion fails, the original downloaded file is saved
    instead and the log explains what happened.

CPU/RAM tuning:

    If conversion still uses too much CPU, set FFmpeg threads to 1 in the GUI
    Settings tab. If you want smaller files and do not mind heavier CPU use,
    raise the preset to fast or medium and lower CRF toward 18.

Rate limiting:

    The script uses polite pacing by default so YouTube sees fewer back-to-back
    requests. You can tune Search delay, Delay between movies, and Download
    sleep min/max in the GUI Settings tab. All timing values are seconds.

How files are named:

    C:\movies\Movie (2026)\Movie (2026)-trailer.mp4

Only one trailer is saved per movie folder. With FFmpeg installed, the final
file is normalized and converted to MP4.

Restart behavior:

    By default, successful downloads are written to trailer-results.json.
    On later runs, a verified success record with an existing trailer file is
    skipped before searching. Existing trailer files are also treated as
    complete and recorded into the results file.

    Use --redownload-existing, or tick Total re-download existing trailers in
    the GUI Settings tab, when you want to refresh the whole library.

Before downloading, existing trailer files are temporarily preserved:

    Movie (2026)-trailer.mp4 -> Movie (2026)-trailer.mp4.old

Matching .old trailer backups are deleted on rerun, and any backup created
during the current run is deleted after a new trailer downloads successfully.

Open or locked files:

    If Windows reports that an existing trailer is open in another program, the
    script skips that movie, continues with the rest of the queue, then retries
    the skipped movie once after the first pass is finished.

    If the file is still locked on the retry, the movie is skipped cleanly and
    the run finishes. Close the player or file browser preview that is using
    the trailer, then run the downloader again.

Notes:

    The script uses yt-dlp searches against public/free sources. YouTube is the
    main search backend because yt-dlp supports it well and it usually has
    official studio, Movieclips, Rotten Tomatoes, ONE Media, KinoCheck, IGN, and
    VEVO-style public uploads where available.
