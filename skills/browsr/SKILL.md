---
name: browsr
description: Use when testing, inspecting, or debugging a localhost web UI from Claude Code running inside Herdr. Routes Chrome DevTools work to the Browsr tab group belonging to the current Herdr workspace instead of launching a separate test browser.
---

# Browsr

Use the `chrome-devtools` MCP for localhost browser work. It attaches to the
shared, visible Browsr Chromium. Do not use Playwright or launch another browser.

Before the first page-scoped browser action:

1. Run `/home/g00rek/Projects/browsr/bridge.py workspace-tabs`. This returns only
   the tabs owned by `HERDR_WORKSPACE_ID`.
2. Call `list_pages` in `chrome-devtools` and select the `pageId` whose URL
   exactly matches one of the returned workspace tabs.
3. Pass that `pageId` to every page-scoped DevTools tool. Never operate on a
   page absent from the `workspace-tabs` result.

If the workspace has no appropriate page, determine the local development URL
from the project or server output and run:

```bash
/home/g00rek/Projects/browsr/bridge.py open-url http://localhost:PORT
```

Then repeat `workspace-tabs` and `list_pages`. Do not use `new_page` for a
localhost page because it creates an ungrouped tab. Popups opened by an existing
workspace page may be used after confirming they appear in `workspace-tabs`.

Several Claude sessions may share Browsr concurrently. A tab selected for this
workspace remains this session's target even if the user focuses another Herdr
workspace. Re-resolve the page only if it closes or the MCP reconnects.

Use `chrome-mine`, not Browsr, for authenticated admin consoles and the user's
personal logged-in sessions. Browsr is an isolated development profile.
