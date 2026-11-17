# What's Up? — notes for Claude

A daily audio brief, published as a phone web app. Each weekday a GitHub
Actions build fetches news and market data, has Claude write six topic
segments at two lengths, voices them, encrypts a private "My Day" plan from
the owner's calendar, and publishes everything to GitHub Pages.

- Repo: `toothfairysandbox-prog/Daily-Podcast` (public). Site:
  `https://toothfairysandbox-prog.github.io/Daily-Podcast/`
- The owner is not a developer. Explain in plain words, and say what you
  pushed and why.

## Map

| file | what it is |
|---|---|
| `topics.py` | the topic menu: feeds, running orders, cover policy. Add a topic here and nowhere else |
| `build.py` | the build: topics in parallel, ~26 speech passes, `episodes.json` (schema 3), RSS |
| `segment_prompt.md` | the show's voice and length limits |
| `sources.py` | RSS fetching and market data (yfinance, Treasury curve) |
| `cover.py` | news photo or designed cover per topic |
| `tts.py` | edge-tts with fallbacks; `fit_duration` enforces time limits |
| `weather.py`, `weather_places.txt` | spoken forecast per listed town (template, Open-Meteo) |
| `myday.py`, `myday_prompt.md` | the encrypted calendar tab, and the birthdays reminder |
| `notify.py` | morning push notification |
| `web/` | the app: `index.html`, `sw.js`, `manifest.webmanifest`, `splash.webp` |
| `.github/workflows/daily-brief.yml` | the build; inputs `force`, `check_feeds`, `notify_only`, `only_topics` |

## Shipping a change

1. `python build.py --dry-run` (with `DRY_RUN_SILENT_AUDIO=1` and
   `PUBLIC_DIR=public_test`) proves the pipeline without API calls.
2. Anything under `web/` changed: bump `VERSION` in `web/sw.js`, or phones
   keep the old cached app.
3. Commit, push to `main`.
4. Rebuild: `gh workflow run daily-brief.yml -f force=true` if `gh` is
   installed, otherwise ask the owner to run it from the Actions tab. A
   plain run skips if today's brief already exists; that is deliberate.
5. Check the result on the `gh-pages` branch (`episodes.json`,
   `myday/index.json`), not just the green tick.

## Rules learned the hard way

- **Never let a data path fail quietly.** A dead price feed once produced
  plausible-sounding briefs for weeks. Failures must be loud in the log.
- **The site and build log are public.** No secrets in the repo, and
  `myday.py` must never log calendar content: errors by class name only,
  since HTTP errors contain the private calendar URL.
- **Untrusted text goes through `env:`, never `${{ }}` inside `run:`**
  (shell injection). Headlines and dispatch inputs are untrusted.
- **Every network call is bounded** (yfinance 25 s, Anthropic 180 s,
  edge-tts 240 s, step timeouts). Unbounded calls once hung a run for 20 min.
- **Tool schemas don't guarantee types**: coerce with `as_list()`.
- **The service worker must not touch audio or ranged requests**, and
  fetches `episodes.json` and `/myday/` network-first.
- **One `<audio>` element for the stitched brief**, src swapped between
  segments: iOS grants play permission per element.
- **Lengths:** every topic is recorded at four cuts (30/60/120/180 s). The
  app's `pickCuts()` gives each chosen topic its shortest cut, then spends
  the slider's remaining time on the highest-`weight` topics. The voice reads
  about 152 wpm; `tts.fit_duration` enforces each cut's limit.
- **No cut may greet or sign off.** Any cut can be stitched after the spoken
  date and between other topics.
- **Never re-encode audio to save space.** A 32 kbps pass made the voice
  sound muffled; audio ships as the speech service made it.
- **Old app versions must keep working against new data.** Phones cache the
  shell, so `episodes.json` keeps the previous format's fields (`short`,
  `long`, top-level `audio`) alongside the new ones.
- **Personal parts** (weather near a town, birthdays) are spoken only for
  the owner today. Preferences live in `localStorage` under `prefs`, shaped
  like the per-listener record a future voice server would store.
- **Feeds:** check with the `check_feeds` workflow input, on the runner only.
  Reuters, AP, ESPN, VentureBeat and Yahoo Finance's RSS are dead or
  blocked from runners; don't re-add them.
- **Timing of the morning run** comes from an external scheduler
  (cron-job.org, 07:40 America/Denver) calling `workflow_dispatch`. GitHub's
  own `schedule` fires hours late and is only a backstop.
- Secrets (`ANTHROPIC_API_KEY`, `VAPID_PRIVATE_KEY`, `PUSH_SUBSCRIPTION`,
  `CALENDAR_ICS_URL`, `MYDAY_KEY`) are the owner's to set. Never ask for
  their values or print them.
- `.git/claude-token` is a GitHub token the owner saved for Claude. It is
  inside `.git`, so it is never committed. Never print it.
