# Security

Browsr runs a separate Chromium profile meant for local development. Do not
sign personal accounts into it or use it as a daily browser.

Its DevTools endpoint listens on `127.0.0.1` only, on a fixed port (`39222`).
A fixed port is a deliberate trade: agent sessions resolve the address once at
startup, so a moving port strands them on every browser restart. It also means
the port is predictable, and any process running as the same user can find it
and drive the browser. Treat local processes and anything you attach to Browsr
as trusted.

Native messaging accepts only the Browsr extension, pinned by the id recorded in
`bridge.py`, and the Unix socket between Herdr and the browser is created with
`0600` permissions. URL routing deliberately accepts HTTP(S) to `localhost`,
`127.0.0.1` and `[::1]` and nothing else.

The cleanup action removes the plugin's own files. It never deletes the browser
profile, because that holds browsing history and sessions; it reports where the
profile is and leaves the decision to you.

Please report security problems privately to the repository owner rather than
publishing a working exploit.
