# Porting Browsr to the Herdr desktop app

Research notes and a plan, written 2026-11-04 against `herdr-app 0.1.12`,
`hc 0.1.17`, with Herdr TUI 0.9.5 still installed and running.

## The two programs are separate products

`herdr-app` is a new native desktop app built on **herdr-core** (`hc`), not a
new version of the TUI. They do not share a server, a config directory or
agents. The TUI keeps `~/.config/herdr`; the app uses `~/.config/herdr-app`.
Both can run at once, and on this machine both do.

The app has **no plugin system**. Everything Browsr relies on today —
`herdr-plugin.toml`, `[[actions]]`, `[[startup]]`, `[[events]]`, `[[panes]]`,
`[[link_handlers]]`, `herdr plugin install`, the marketplace at
herdr.dev/plugins — does not exist there. The app is extended with **mods**:
JavaScript programs that draw panels, run commands and call `exec`.

## What survives untouched

The browser half of Browsr does not care which Herdr is in front:

- the dedicated Chromium profile and how it is launched,
- the extension that owns the tab groups,
- the native-messaging host and the control socket,
- the fixed DevTools port and `browsr-chrome-mcp.py`.

## What has no counterpart and must be replaced

| Today (TUI) | In the app |
|---|---|
| `herdr-plugin.toml` | nothing; a mod folder plus `keys.json` entries |
| `[[startup]] bridge.py watch` | a mod copy, kept alive as a window or a layout block |
| `HerdrLink`, polling `workspace.list` 5×/s | `render()` re-runs on focus change; no polling at all |
| `herdr workspace list` | `herdr-app ls` (JSON: id, name, folder, machine, active) |
| `panel.py` in a Herdr pane | a mod panel in JavaScript |
| `[[link_handlers]]` for localhost URLs | no equivalent found |
| `herdr plugin install g00rek/browsr` | copy `mods/browsr/` into the app home |
| topic `herdr-plugin`, herdr.dev/plugins | topic `herdr-mod`, herdr.dev/mods |

## The mechanism the port rests on

From the installed guide (`~/.config/herdr-app/AGENTS.md`):

> `render(cx)` describes the UI and runs again after `cx.notify()` and
> whenever Herdr's live data changes (spaces, agents, terminals, focus,
> theme, shared state). Don't poll those.

So the thing that was hardest under the TUI is free here. The TUI never told
a plugin about a switch made in its own interface, which is why Browsr asks
five times a second over a held-open socket. A mod is simply re-rendered.

A mod stays alive "while its block is in the layout, shown or not", and its
timers keep running while hidden. A mod that is never shown never starts, so
Browsr's needs one of:

- a kept window — `"place": "none"` plus
  `"overlay": {"kind": "window", "key": "browsr", "keep": true}`, which
  reopens on the next start. This is what the author's own `omarchy-theme`
  mod does, and it is the closest thing to a background plugin.
- a block in the layout, which means editing a personal layout.

## What was built (2026-11-04)

Following the space is **not** done by a panel. Every shipped mod declares
`"place": "none"` and is placed by a layout; a mod is alive only while it is
on screen, and the browser has to keep up whether or not anyone is looking
at one. So:

- `bridge.py watch` now also follows the space under the app. It reads
  `view.json`, which the app rewrites on every switch, and syncs on the
  change and on nothing else. Measured: three switches, three syncs, nothing
  on the other ninety-seven ticks.
- `launch` starts that watcher, because the app has no startup hook. A second
  one exits on the lock.
- `mods/browsr/` is the panel only: `place: none` and a popup, the shape
  `picker` and `machine-add` use. `keys.json` suggests `alt-b`.
- `bridge.py --app` / `--tui` pins which Herdr is being asked. The app runs
  programs in the person's own environment, where nothing says which Herdr
  is calling, and guessing wrong is destructive.

## Shape of the port

1. `mods/browsr/main.js` + `mod.json` in the repo, copied into the app home.
   It watches `focused().space`, and on a change calls
   `exec("<browsr>/bridge.py", ["focus", id, name])`. It also draws the panel
   that `panel.py` draws today: browser up, link answering, extension fresh,
   groups against spaces, and the keys that act on them.
2. `bridge.py` grows a second source of truth for "what are the spaces and
   which is in front": `herdr-app ls` beside the existing TUI socket. The
   session id that keys groups in the extension becomes the app's home path
   when running under the app, so TUI groups and app groups never collide.
3. `keys.json` fragment shipped with the mod for showing the browser.
4. `RECIPE.md` and `preview.png` at the repo root level the mods index wants,
   and the repo topic `herdr-mod`.

## Open questions

- **Both or only the app.** Keeping the TUI integration means two
  integrations in one repo for one behaviour. Dropping it strands the
  marketplace listing and anyone who installed it there.
- **Clicking a localhost link in the app** has no handler to register. Unless
  the app routes URLs some other way, that feature is TUI-only.
- **Remote spaces.** The app has first-class SSH machines; a space can live on
  another machine. Browsr drives a browser on this one. Decide whether a
  remote space gets a group at all.

## Facts worth keeping

- `herdr-app ls` and `herdr-app status` answer in about 3.4 ms, against the
  8-second timeout budget the TUI CLI needed. Cheap enough to call on demand.
- `~/.config/herdr-app/view.json` holds `active` (the focused space id) and is
  rewritten on every switch — a file-watch trigger for anything outside the app.
- `herdr-app check <mod folder>` loads a mod and renders it once; it is the
  mod equivalent of a test run and must pass before calling a mod done.
- `herdr-app shot [file.png]` saves the last rendered frame, which is how to
  look at a mod without asking the author to describe his screen.
