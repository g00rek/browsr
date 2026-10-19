# Security

Browsr runs a separate Chromium profile meant for local development. Do not
sign personal accounts into it or use it as a daily browser.

## The DevTools port is the real boundary

Its DevTools endpoint listens on `127.0.0.1` only, on a fixed port (`39222`).
A fixed port is a deliberate trade: agent sessions resolve the address once at
startup, so a moving port strands them on every browser restart.

Be clear about what that costs. A loopback TCP port carries no user check, so
this is **not** limited to processes running as you. Any local account, and any
sandboxed application that was granted network access but no access to your
home directory — a Flatpak or Snap with `--share=network`, a container on the
host network — can reach it. Chromium's own defences do not help: the Host
header check only stops DNS rebinding, and the Origin check only stops web
pages. A local process sends neither and is let in.

What it can then do is everything: read every cookie in the profile in one
call, navigate tabs, and run script inside any page that is open. So the
sentence above about not signing personal accounts in is not housekeeping
advice — it is the whole of the protection. If you do use Browsr for a
logged-in console, as this project's own agent skill suggests, understand that
you are relying on every other local process and account being trustworthy.

The Unix socket between Herdr and the browser is created with `0600`
permissions and is genuinely restricted to you. The DevTools port is the
weaker of the two, and therefore the one that defines the boundary.

If that trade is wrong for you, run Browsr on a machine or account where you
control what else runs, or build Chromium's `--remote-debugging-pipe` into a
fork — at the cost of the fixed address that the agent tooling depends on.

## The rest

Native messaging accepts only the Browsr extension, pinned by the id recorded
in `bridge.py`. That id is derived from a public key published in this
repository, so it identifies the extension rather than authenticating it; the
host manifest is written only into Browsr's own browser profile, never into
the browser you use yourself.

URL routing deliberately accepts HTTP(S) to `localhost`, `127.0.0.1` and
`[::1]` and nothing else, checked once in `bridge.py` and again in the
extension immediately before the tab is opened.

The cleanup action removes the plugin's own files. It never deletes the browser
profile, because that holds browsing history and sessions; it reports where the
profile is and leaves the decision to you.

Please report security problems privately to the repository owner rather than
publishing a working exploit.
