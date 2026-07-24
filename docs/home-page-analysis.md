# Home page (`/admin`) analysis — improving user NPS

Scope: the page a human actually lands on when running this tool — `GET /admin`, generated
from the `ADMIN_HTML` template in `src/server.py:739-1372`. The overlay pages are broadcast
output, not a UI surface, so they are only referenced where they affect the admin experience.

Everything below was verified by running the server locally (`python -m src.server`),
probing the routes, and screenshotting the page in Chromium at 1280px and 390px.

---

## 1. What the page is today

Three stacked cards in a fixed 520px column:

| Card | Purpose | State |
|---|---|---|
| **Connection** | 3 login tabs (email code / Yostar token / gateway token) + "Track Results From" | Swaps to a read-only "Connected as …" panel once logged in |
| **Scoring** | UMA ×4, starting points, oka; writes back to both YAML configs and recalculates | Always visible |
| **Overlay** | Four hardcoded `http://localhost:8765/...` URLs as plain text | Static |

Client behaviour: `loadConfig()` + `checkStatus()` on load, then `checkStatus()` every 15s
(`src/server.py:1366-1369`). No WebSocket. No live data of any kind.

**The mental model mismatch:** users come to this page for a *live tournament broadcast*.
Their job-to-be-done is "make the overlay show correct scores, and know it is still working."
The page is built as a *configuration form*. It answers "how do I log in" and never answers
"is it working right now" — which is the question the user has every 30 seconds for the
next four hours.

---

## 2. Findings, ordered by NPS impact

### 2.1 Critical — the page cannot tell the user whether it is working

**No live tracking state at all.** Once connected, the page shows a nickname, an account ID,
a tracking start time, and an "observer level". It never shows: contest name/ID, the tracked
roster, how many games have been picked up, when the last successful poll happened, or a
single score. The operator has to open the overlay in another tab and eyeball it to know
whether the tool is alive. This is the single largest source of "I don't trust it" sentiment.

**Poll failures are invisible.** `GameTracker._poll_loop` swallows every exception into a log
line (`src/tracker.py:692-697`). The API session can expire, the contest can 403, the network
can drop — the home page keeps showing a green dot and "Connected as …" indefinitely.
`connection_status["connected"]` is only ever set at login time
(`src/server.py:276-299`) and is never invalidated by downstream failure.
*A green light that cannot turn red is worse than no light.*

**Silent no-op when `contest_id` is missing.** `_initialize_connected_trackers` only starts a
tracker `if contest_id:` (`src/server.py:319-336`). With `contest_id: 0` or absent, login
succeeds, the page goes green, and nothing is ever tracked. No warning anywhere.

**Demo mode is indistinguishable from real mode.** `_initialize_demo_trackers()`
(`src/server.py:302-305`) builds trackers with no client, so the overlay renders the full
roster at 0.0 before anyone logs in. On stream that looks like real data. The admin page
never says "you are not connected — nothing is being tracked."

**Observer status is jargon.** `observerSummary()` renders `reported level 2`
(`src/server.py:1057-1061`). Nothing tells the user what level is required, whether their
account can actually observe this contest, or what to do if it can't. When
`observer_error` is set, the amber error text sits directly under a green "Connected" dot —
contradictory signals in the same box (confirmed visually).

### 2.2 High — dead ends that force a restart or a re-login

**"Disconnect & Reconfigure" does not disconnect.** `onclick="showSetup()"`
(`src/server.py:817`) only hides a div. There is no `/api/disconnect` route — the server stays
logged in and polling, and a page refresh silently restores the connected view. Worse, the
button is the *only* path to the tracking-start field, so a user who just wants to adjust the
start time is pushed through a full re-login (email → code → verify) with a live event running.

**Tracking start time cannot be changed without re-authenticating.** It is only sent as a
parameter of `/api/connect` and `/api/verify_code`. Retroactively fixing "I started tracking
30 minutes too late" — an extremely common tournament-day need — means tearing down the
session. A `POST /api/tracking_start` that restarts the trackers in place would remove this
entirely.

**Everything except scoring requires editing YAML and restarting the process.** Contest ID,
roster, teams, `games_count`, poll interval and server region are all file-only. The page
that is supposed to run the tournament can change six numbers and nothing else.

**Manual result entry exists in the backend but has no UI.** `handle_admin_command` supports
`add_result` and `reset_player` over `/ws` (`src/server.py:458-482`), and
`GameTracker.manual_add_result` / `manual_reset_player` are implemented and tested. The admin
page never opens a WebSocket (verified: zero `new WebSocket` in the template). So the built-in
recovery path for "the API missed a game mid-broadcast" is unreachable to the person who needs
it. This is the highest value/lowest effort feature on the page — the hard part is already done.

**A partial config kills the server with a raw traceback.** `_make_tracker` and `/api/config`
both index `config["games_count"]` unguarded (`src/server.py:171`, `src/server.py:530`).
Verified: a config missing that key raises `KeyError: 'games_count'` during startup, so the
process never boots — a first-run user gets a Python traceback instead of a page telling them
what to fix.

### 2.3 High — the OBS handoff is manual and error-prone

The Overlay card prints four `http://localhost:8765/...` strings as inert `<code>` text
(`src/server.py:934-937`). They are:

- **not clickable** and **not copyable** — every one has to be retyped or hand-selected into
  an OBS browser source;
- **hardcoded**, so they are simply wrong whenever the admin page is opened from anywhere
  other than the host machine (the server binds `0.0.0.0` by default, so a second machine on
  the LAN is an expected setup) or on a non-default port;
- **unexplained** — no recommended canvas size, no note that a browser source needs
  transparency, no "open preview" link to sanity-check a layout before going live.

Deriving them from `window.location.origin` with a copy button and an "open preview" link is
a ~20-line change that removes a guaranteed friction point from every single session.

### 2.4 Medium — form mechanics

- **No `<form>` element anywhere** (verified: zero `<form` tags). Enter does not submit. A
  user types a 6-digit code, presses Enter, nothing happens — a classic detractor moment
  during a timed verification window.
- **No autofocus, no `autocomplete`, no `inputmode`.** The code field is
  `type="text" maxlength="10"` (`src/server.py:852`) for a 6-digit numeric code, so phones
  get a full alphabetic keyboard.
- **No resend / countdown on the verification code**, and no way back if the mail is slow
  other than "Back" → resend from scratch.
- **The login tab is not remembered.** Users returning with a Yostar token re-click the same
  tab every session.
- **The saved UID is never prefilled.** `admin_page()` does
  `ADMIN_HTML.replace("__DEFAULT_UID__", str(uid))` (`src/server.py:736`) but the placeholder
  does not exist in the template — dead code. `config.yaml` already stores `yostar_uid`.
- **Raw exception strings are shown as user-facing errors** (`str(e)` from
  `try_connect`/`api_send_code`) and injected via `innerHTML`. Users see things like
  `403, message='Attempt to decode JSON with unexpected mimetype: text/plain', url=...`.
  Map the common failures to plain language, keep the raw text behind a "details" toggle.
- **Convoluted status condition.** `src/server.py:1123`:
  `if (s.error && !el.classList.contains('hidden') === false)` — `!x === false` reduces to
  `x`, so this only surfaces errors while step 2 is hidden. Almost certainly not the intent,
  and unreadable either way.
- **Scoring card:** no presets for common rulesets, no unsaved-changes indicator, no
  confirmation before a save that silently recalculates every finished game, and no mention
  that it writes **both** `config.yaml` and `config.semifinals.yaml` (`src/server.py:609-623`).

### 2.5 Medium — layout, responsiveness, accessibility

- **Mobile is broken.** At 390px the tab row (`display:flex`, three fixed-padding tabs)
  refuses to shrink and forces horizontal page scroll; inputs run off the right edge
  (verified by screenshot). The only media query handles the scoring grid
  (`src/server.py:795-797`). Fix: `flex-wrap: wrap` + `min-width: 0` on the tabs.
- **Desktop wastes ~60% of the viewport.** A single 520px column pinned left on a 1280px
  screen. A two-column layout at ≥900px would let a live status panel sit beside the forms
  without any scrolling.
- **Tabs are `<div onclick>`** (`src/server.py:773-780`) — not focusable, no `role="tab"`,
  no keyboard support, no `:focus-visible` styling anywhere on the page.
- **Contrast.** `.help { color: #555 }` on `#0e0e14` is roughly 3:1 — below WCAG AA for body
  text. Several help strings are patched inline to `#bbb`, which suggests the base value was
  already known to be too dim.
- **Flash of wrong state on every load.** The setup form is rendered visible, then hidden
  ~100ms later when `/api/status` resolves. Connected users see a login form flicker every
  refresh. Render a neutral loading state instead.
- **No favicon** (`/favicon.ico` → 404) and the tab title is the static `Tracker Admin`,
  while the `<h1>` says `Mahjong Soul Tracker`. For a page that lives in a pinned tab all
  day, a status-reflecting title (`● Tracking — 12 games`) is a free, constant reassurance
  signal.
- **`GET /` returns `{"detail":"Not Found"}`** — there is no root route (verified: 404).
  The literal home page of the app is a raw JSON error. One `RedirectResponse` fixes it.

### 2.6 Notes outside the page itself

- `config.yaml` is committed with what looks like a **real `yostar_token` and `yostar_uid`**.
  If those are live credentials they should be rotated and the file replaced with an example.
- `.venv/` is committed (1467 files), and it is a macOS venv whose `python3` symlinks into
  Xcode — it cannot work on Linux and makes the documented setup fail for anyone who trusts
  it. Add `.venv/`, `__pycache__/` to `.gitignore` and drop it from the index.
- Startup blocks on a Majsoul pre-connect inside the `startup` event
  (`src/server.py:1377-1389`). It fails fast when the host is unreachable, but a slow/hanging
  connect delays "application startup complete" and the admin page with it.
- **Zero test coverage of the admin page or its endpoints.** `tests/` covers the tracker,
  the client and config loading only.

---

## 3. Where the detractors come from

Three moments generate almost all negative sentiment. Fixes should be judged against them.

| Moment | What goes wrong today | Fix |
|---|---|---|
| **First run** | Broken venv in the repo, YAML editing, F12 console instructions, a `KeyError` crash on an incomplete config, and a green "connected" state that tracks nothing when `contest_id` is unset | 2.2 config guards, 2.1 contest warning, root redirect |
| **Going live (OBS setup)** | Hand-retyping four localhost URLs that are wrong from any other machine | 2.3 copyable, origin-derived URLs |
| **Mid-event failure** | Green dot while polling has been failing for 20 minutes; no manual override to patch a missed game; changing the tracking start means re-login | 2.1 live status + degraded state, 2.2 manual entry UI + tracking-start endpoint |

---

## 4. Recommended order of work

**Phase 1 — trust (largest NPS movement, ~1-2 days)**

1. Turn the page into a dashboard: live status card with contest ID, tracked roster/teams,
   games detected, last successful poll ("updated 4s ago"), and a compact score table.
   Reuse `/api/scores` + `/api/scores/team`, or subscribe to `/ws` for push updates.
2. Make failure visible: track last-poll success/failure in `GameTracker`, expose it on
   `/api/status`, and render three honest states — **Tracking** / **Degraded** (connected but
   polls failing, with the reason) / **Not connected**.
3. Warn loudly when `contest_id` is unset or the observer level is insufficient, in plain
   language with the fix.
4. Label demo mode explicitly, on both the admin page and the overlay preview.

**Phase 2 — remove dead ends (~1-2 days)**

5. Expose manual `add_result` / `reset_player` in the UI (backend already exists).
6. `POST /api/tracking_start` to change the cutoff without re-login; surface the field in the
   connected view.
7. Real `POST /api/disconnect`, or rename the button to "Reconfigure connection".
8. Origin-derived overlay URLs with copy buttons, open-preview links, and OBS notes.
9. Guard `games_count` and other required keys; fail with an actionable message instead of a
   `KeyError`, and show a config health line on the page.

**Phase 3 — polish (~1 day)**

10. Wrap inputs in `<form>` (Enter submits), autofocus, `inputmode="numeric"`,
    `maxlength="6"`, remember the last login tab, prefill the saved UID.
11. Keyboard-accessible tabs (`role="tab"`, arrow keys, focus rings), raise `.help` contrast.
12. Fix the mobile overflow; add a two-column desktop layout.
13. Loading state instead of the login-form flash; favicon; status-reflecting `<title>`.
14. Friendly error mapping with raw details behind a toggle.
15. Scoring presets, unsaved-changes indicator, and a note that both configs are written.
16. `GET /` → redirect to `/admin`.

**Phase 4 — bigger bets**

17. Edit contest ID / roster / teams / `games_count` from the UI, hot-reloaded without a
    process restart. This is what finally makes the tool self-service.
18. Extract `ADMIN_HTML` from `server.py` into `overlay/admin.html` (a 630-line HTML+JS blob
    inside a Python string has no syntax highlighting, no linting, and no tests) and add
    endpoint tests for the admin API surface.

---

## 5. Measuring the change

The three detractor moments above map to observable events; instrumenting them locally is
enough to see whether NPS movement is real:

- time from server start to first successful poll (first-run friction),
- share of sessions that ever reach a "Degraded" state and how long they stay there,
- whether manual result entry gets used (proxy for tracking accuracy gaps),
- overlay-URL copy clicks vs. sessions (proxy for OBS-setup friction).

If a survey is added, ask it after a tournament session ends, not on the config page.
