# Browsr

A Herdr plugin that gives every workspace its own group of browser tabs, in a
browser of its own.

Switch project in Herdr and the browser follows: that project's tab group opens,
the rest fold away. The browser never steals your focus — it is simply already
on the right project when you look at it. Your own browser is left alone;
Browsr drives a separate Chromium profile with its own history and logins.

```
 ┌ Browsr ─────────────────────────────────────────────────────┐
 │  ▸ api      ▾ search       ▸ notes    ▸ infra    ▸ billing  │
 │             ├ localhost:5173                                 │
 │             └ Dashboard                                      │
 └──────────────────────────────────────────────────────────────┘
      ▲          ▲
      │          └ the workspace you are in, open
      └ the others, folded
```

There is also a panel inside Herdr that says what the browser is doing and lets
you act on it without leaving the terminal.

## Requirements

- Linux
- [Herdr](https://herdr.dev) 0.8.2 or newer
- Chromium
- Python 3.9 or newer (CI runs 3.12)
- Hyprland, for the one feature that raises the browser window. Everything else
  works on any window manager; that one key simply does nothing.
- `npx`, only if you want the Chrome DevTools MCP integration for coding agents

## Install

```bash
git clone https://github.com/g00rek/browsr ~/projects/browsr
herdr plugin link ~/projects/browsr
~/projects/browsr/bridge.py setup
```

That last line checks Chromium is present and registers the small helper that
lets the browser talk to Herdr. Installing from GitHub with `herdr plugin
install` runs it for you; linking a local checkout does not, so run it once
yourself. Either way it is harmless to repeat, and the plugin runs it again
whenever it starts the browser.

Add two keys to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+b"
type = "plugin_action"
command = "g00rek.browsr.launch"
description = "Show Browsr"

[[keys.command]]
key = "prefix+shift+b"
type = "plugin_action"
command = "g00rek.browsr.panel"
description = "Browsr panel"
```

Then `herdr server reload-config`.

## Using it

`prefix+b` shows Browsr, starting it the first time. **Nothing else ever starts
it.** An agent opening a page, a question about the tabs, the browser tooling
that comes up with an agent session — all of them work when Browsr is running
and politely decline when it is not. The browser is a window on your screen and
it appears when you ask for it.

From then on it keeps itself in step with Herdr: a new workspace gets a group, a
renamed one is renamed, a closed one takes its group with it, and switching
projects brings the right group forward — whether you switch with the keyboard,
the mouse or the command line.

Clicking an `http://localhost:…` link in Herdr sends it to the current
workspace's group instead of your own browser.

## The panel

`prefix+shift+b` opens a pane beside your work. The same key takes you to it
when it is open elsewhere, and closes it when you are already in it.

```
  BROWSR

  Browser        running
  Herdr link     answering
  Extension      up to date
  Windows        1

  Strip matches Herdr (5)
    api                    1 tab
    search                 2 tabs
    notes                  1 tab

  [p] show browser  [s] match Herdr  [r] restart browser
  [k] quit browser  [q] close panel
```

What the rows mean:

| Row | What it tells you |
|---|---|
| `Browser` | Whether Chromium is running at all. |
| `Herdr link` | Whether its extension is answering. The browser can be running while this is dead, and then tab groups quietly stop following — you also get a notification when that happens. |
| `Extension` | Whether the browser is running the extension that is on disk. Restarting is the only thing that refreshes it. |
| `Windows` | Should always be 1. More than one means something opened a stray window. |
| `Strip …` | Each group against Herdr's workspaces. A `leftover` group has no workspace; a workspace marked `no group` has no group. |

`[s] match Herdr` forces the strip back into agreement. You should not need it —
it happens on its own — but it is there when it has not.

## For coding agents

Three commands, meant to be run from inside a Herdr pane:

```bash
./bridge.py workspace-tabs                     # the tabs of this workspace, as JSON
./bridge.py open-url http://localhost:3000     # open a page without taking focus
./bridge.py status                             # everything the panel shows, as JSON
```

For Claude Code, a wrapper attaches the Chrome DevTools MCP to the running
Browsr instead of launching a browser of its own:

```bash
claude mcp add --scope user chrome-devtools ~/projects/browsr/browsr-chrome-mcp.py
```

`skills/browsr/SKILL.md` tells an agent how to pick the right tab. Copy it to
`~/.claude/skills/browsr/SKILL.md` and replace `<browsr>` with the directory you
cloned into.

## When something looks wrong

Open the panel first; it names all four of these.

**Tab groups stopped following.** The link is dead — the browser is up but its
extension is not answering. `[r] restart browser`. You also get a notification
when this lasts more than a minute, because it makes no other noise.

**The panel says the extension is older than the files.** You changed the
extension. Chromium keeps its own compiled copy and a plain restart of the
browser does not refresh it; `[r] restart browser` does, because the plugin
clears that copy when the files have changed.

**More than one window.** Something opened a stray window. It is harmless now —
groups are found wherever they are — but close it; the panel will go back to 1.

**A group with no workspace, or the other way round.** Press `[s] match Herdr`.
If it comes back, that is a bug worth reporting.

## Uninstall

```bash
herdr plugin action invoke cleanup --plugin g00rek.browsr
herdr plugin unlink g00rek.browsr
```

The cleanup action removes the two files that let Chromium talk to Herdr and the
plugin's state directory. It does **not** delete the browser profile — that
holds your history and logins — and prints where it is and how big, so you can
decide.

## How it works

```text
Herdr  ──events──┐                   ┌── asks Herdr which workspace is in front,
                 │                   │   five times a second over one connection
             bridge.py               │
                 │                   │
            Unix socket        native_host.py
                 │                   │
                 └────── native messaging ──────┐
                                                │
                            dedicated Chromium + extension
                                                │
      coding agent ── browsr-chrome-mcp.py ─────┘  (DevTools on 127.0.0.1:39222)
```

The extension owns the tab groups, because nothing outside a browser extension
can see them. The native host lives exactly as long as the extension is
connected and is what keeps the browser in step: Herdr does not call a plugin
when a workspace is switched in its own interface, so waiting to be told does
not work — it asks instead.

The DevTools port is fixed at `39222` so that restarting the browser does not
strand agent sessions that resolved the address once at startup. It is
deliberately not `9222`, which is what a personal Chrome answers on.

More on the security model: [SECURITY.md](SECURITY.md).

## Development

```bash
./scripts/check.sh
```

That runs the Python tests, the extension tests and the syntax checks. The
extension is driven through the same native port Herdr uses, against an
in-memory model of windows, tabs and groups; the socket handling is exercised
against real sockets.

## Licence

[MIT](LICENSE)
