---
name: browsr
description: Use for EVERY browser action from Claude Code inside Herdr — localhost UIs and logged-in admin consoles alike (Google Cloud, Cloudflare, PostHog, Meta, AppsFlyer, GitHub settings). Routes Chrome DevTools work to the Browsr tab group of the current Herdr workspace; never his daily Chrome (chrome-mine), never a separate test browser.
---

# Browsr

Use the `chrome-devtools` MCP for localhost browser work. It attaches to the
shared, visible Browsr Chromium. Do not use Playwright or launch another browser.

Before the first page-scoped browser action:

1. Run `~/projects/browsr/bridge.py workspace-tabs`. This returns only
   the tabs owned by `HERDR_WORKSPACE_ID`.
2. Call `list_pages` in `chrome-devtools` and select the `pageId` whose URL
   exactly matches one of the returned workspace tabs.
3. Pass that `pageId` to every page-scoped DevTools tool. Never operate on a
   page absent from the `workspace-tabs` result.

If the workspace has no appropriate page, determine the local development URL
from the project or server output and run:

```bash
~/projects/browsr/bridge.py open-url http://localhost:PORT
```

Then repeat `workspace-tabs` and `list_pages`. Do not use `new_page` for a
localhost page because it creates an ungrouped tab. Popups opened by an existing
workspace page may be used after confirming they appear in `workspace-tabs`.

Several Claude sessions may share Browsr concurrently. A tab selected for this
workspace remains this session's target even if the user focuses another Herdr
workspace. Re-resolve the page only if it closes or the MCP reconnects.

Every browser action goes through Browsr, authenticated admin consoles included
(Google Cloud, Cloudflare, PostHog, Meta, AppsFlyer, GitHub settings). `bridge.py
open-url` only opens localhost, so for an external console navigate a tab that is
already in this workspace's group and send it back to its localhost URL afterwards.
A login wall there is reported in one line, never typed through. Never fall back to
`chrome-mine` (his daily Chrome): each attach asks him to allow remote debugging and
hangs when he is away. It is also denied by a permission rule in
`~/.claude/settings.json`.
