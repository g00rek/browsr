---
name: browsr
description: Use for every browser action from Claude Code inside Herdr: localhost UIs, public pages and logged-in admin consoles (Google Cloud, Cloudflare, PostHog, Meta, AppsFlyer, GitHub settings). Routes Chrome DevTools work to the Browsr tab group of the current Herdr workspace.
---

# Browsr

`<browsr>` below is the directory the Browsr plugin was cloned into;
`herdr plugin list` prints it. Replace it when you install this skill.

Use the `chrome-devtools` MCP for localhost browser work. It attaches to the
shared, visible Browsr Chromium. Do not use Playwright or launch another browser.

Before the first page-scoped browser action:

1. Run `<browsr>/bridge.py workspace-tabs`. This returns only
   the tabs owned by `HERDR_WORKSPACE_ID`.
2. Call `list_pages` in `chrome-devtools` and select the `pageId` whose URL
   exactly matches one of the returned workspace tabs.
3. Pass that `pageId` to every page-scoped DevTools tool. Never operate on a
   page absent from the `workspace-tabs` result.

If the workspace has no appropriate page, determine the local development URL
from the project or server output and run:

```bash
<browsr>/bridge.py open-url http://localhost:PORT
```

Then repeat `workspace-tabs` and `list_pages`. Do not use `new_page` for a
localhost page because it creates an ungrouped tab. Popups opened by an existing
workspace page may be used after confirming they appear in `workspace-tabs`.

Several Claude sessions may share Browsr concurrently. A tab selected for this
workspace remains this session's target even if the user focuses another Herdr
workspace. Re-resolve the page only if it closes or the MCP reconnects.

This skill covers every browser action, logged-in consoles included (Google Cloud,
Cloudflare, PostHog, Meta, AppsFlyer, GitHub settings). `bridge.py open-url` opens
localhost only; for any other address, navigate a tab that is already in this
workspace's group, then send it back to its localhost URL. If a page asks to log in or
re-enter a password, report it in one line.
