const HOST = 'dev.herdr.browser';
const STATE_KEY = 'herdrBrowserState';

let port;
let reconnectTimer;
let messageQueue = Promise.resolve();
let managedWindowId;
const groups = new Map();
const lastActiveTabs = new Map();

function workspaceKey(sessionId, workspaceId) {
	return `${sessionId || 'default'}\u001f${workspaceId}`;
}

function normalizeLabel(label, workspaceId) {
	const clean = String(label || '')
		.trim()
		.replace(/\s+/g, ' ');
	return (clean || workspaceId || 'Herdr').slice(0, 80);
}

function isLocalUrl(raw) {
	try {
		const url = new URL(raw);
		return (
			(url.protocol === 'http:' || url.protocol === 'https:') &&
			['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
		);
	} catch {
		return false;
	}
}

async function loadState() {
	const stored = (await chrome.storage.local.get(STATE_KEY))[STATE_KEY] || {};
	managedWindowId = stored.windowId;
	for (const [workspaceId, groupId] of Object.entries(stored.groups || {})) {
		groups.set(workspaceId, Number(groupId));
	}
	for (const [workspaceId, tabId] of Object.entries(stored.lastActiveTabs || {})) {
		lastActiveTabs.set(workspaceId, Number(tabId));
	}
}

async function saveState() {
	await chrome.storage.local.set({
		[STATE_KEY]: {
			windowId: managedWindowId,
			groups: Object.fromEntries(groups),
			lastActiveTabs: Object.fromEntries(lastActiveTabs),
		},
	});
}

async function dedupeUnownedGroups() {
	const win = await getManagedWindow();
	const allGroups = await chrome.tabGroups.query({ windowId: win.id });
	const ownedIds = new Set(groups.values());
	const byTitle = new Map();
	for (const group of allGroups) {
		const title = group.title || '';
		if (!byTitle.has(title)) byTitle.set(title, []);
		byTitle.get(title).push(group);
	}

	for (const sameTitle of byTitle.values()) {
		const owned = sameTitle.filter(group => ownedIds.has(group.id));
		if (owned.length !== 1) continue;
		const canonical = owned[0];
		for (const duplicate of sameTitle) {
			if (duplicate.id === canonical.id || ownedIds.has(duplicate.id)) continue;
			const tabs = await chrome.tabs.query({ groupId: duplicate.id });
			if (tabs.length) {
				await chrome.tabs.group({
					groupId: canonical.id,
					tabIds: tabs.map(tab => tab.id),
				});
			}
		}
	}
}

async function getManagedWindow() {
	if (managedWindowId != null) {
		try {
			return await chrome.windows.get(managedWindowId);
		} catch {
			managedWindowId = undefined;
		}
	}
	const normal = (await chrome.windows.getAll({ windowTypes: ['normal'] }))[0];
	const win = normal || (await chrome.windows.create({ url: 'chrome://newtab/' }));
	managedWindowId = win.id;
	await saveState();
	return win;
}

async function validGroup(key) {
	const groupId = groups.get(key);
	if (groupId == null) return undefined;
	try {
		const group = await chrome.tabGroups.get(groupId);
		if (group.windowId === (await getManagedWindow()).id) return group;
	} catch {}
	groups.delete(key);
	return undefined;
}

async function findUnownedGroupByTitle(title) {
	const win = await getManagedWindow();
	const owned = new Set(groups.values());
	const candidates = await chrome.tabGroups.query({ windowId: win.id, title });
	return candidates.find(group => !owned.has(group.id));
}

async function mergeUnownedGroups(canonical, title) {
	const win = await getManagedWindow();
	const owned = new Set(groups.values());
	const candidates = await chrome.tabGroups.query({ windowId: win.id, title });
	for (const duplicate of candidates) {
		if (duplicate.id === canonical.id || owned.has(duplicate.id)) continue;
		const tabs = await chrome.tabs.query({ groupId: duplicate.id });
		if (tabs.length) {
			await chrome.tabs.group({
				groupId: canonical.id,
				tabIds: tabs.map(tab => tab.id),
			});
		}
	}
}

async function ensureGroup(sessionId, workspaceId, label) {
	const key = workspaceKey(sessionId, workspaceId);
	const title = normalizeLabel(label, workspaceId);
	// Migrate the short-lived 0.1.0 development state from workspace-only keys.
	if (!groups.has(key) && groups.has(workspaceId)) {
		groups.set(key, groups.get(workspaceId));
		groups.delete(workspaceId);
		if (lastActiveTabs.has(workspaceId)) {
			lastActiveTabs.set(key, lastActiveTabs.get(workspaceId));
			lastActiveTabs.delete(workspaceId);
		}
	}
	let group = await validGroup(key);
	if (!group) group = await findUnownedGroupByTitle(title);
	if (!group) {
		const win = await getManagedWindow();
		const tab = await chrome.tabs.create({ windowId: win.id, active: false });
		const groupId = await chrome.tabs.group({ tabIds: [tab.id] });
		group = await chrome.tabGroups.get(groupId);
	}
	groups.set(key, group.id);
	await mergeUnownedGroups(group, title);
	await chrome.tabGroups.update(group.id, { title, collapsed: true });
	await saveState();
	return group;
}

async function activateWorkspace(workspaceId, label, sessionId) {
	const key = workspaceKey(sessionId, workspaceId);
	const group = await ensureGroup(sessionId, workspaceId, label);
	const tabs = await chrome.tabs.query({ groupId: group.id });
	const remembered = lastActiveTabs.get(key);
	const tab = tabs.find(item => item.id === remembered) || tabs[0];
	if (tab) {
		await chrome.tabs.update(tab.id, { active: true });
		lastActiveTabs.set(key, tab.id);
	}
	for (const [otherKey, otherGroupId] of groups) {
		try {
			await chrome.tabGroups.update(otherGroupId, {
				collapsed: otherKey !== key,
			});
		} catch {}
	}
	await saveState();
}

async function openLocalUrl(message) {
	if (!isLocalUrl(message.url)) throw new Error('Only localhost URLs are routed');
	const key = workspaceKey(message.session_id, message.workspace_id);
	const group = await ensureGroup(
		message.session_id,
		message.workspace_id,
		message.workspace_label
	);
	const tabs = await chrome.tabs.query({ groupId: group.id });
	let tab = tabs.find(item => item.id === lastActiveTabs.get(key) && isLocalUrl(item.url));
	if (!tab) tab = tabs.find(item => isLocalUrl(item.url));
	// `focus: false` has to cover the tab as well as the window. Activating the tab
	// regardless meant an agent opening a page moved the user off whatever tab they
	// were on inside the window, even though the window itself was never raised.
	const activate = message.focus !== false;
	if (tab) {
		tab = await chrome.tabs.update(tab.id, { url: message.url, active: activate });
	} else {
		const win = await getManagedWindow();
		tab = await chrome.tabs.create({ windowId: win.id, url: message.url, active: activate });
		await chrome.tabs.group({ groupId: group.id, tabIds: [tab.id] });
	}
	lastActiveTabs.set(key, tab.id);
	// Expanding the group is visible too: with another workspace active this one is
	// collapsed on purpose, and expanding it rearranges the user's tab strip. A silent
	// open leaves the strip exactly as it was; the group expands when the workspace
	// is next activated.
	if (activate) {
		await chrome.tabGroups.update(group.id, { collapsed: false });
		await chrome.windows.update((await getManagedWindow()).id, { focused: true });
	}
	await saveState();
}

async function workspaceTabs(message) {
	const key = workspaceKey(message.session_id, message.workspace_id);
	const group = await validGroup(key);
	if (!group) return { workspace_id: message.workspace_id, tabs: [] };
	const tabs = await chrome.tabs.query({ groupId: group.id });
	return {
		workspace_id: message.workspace_id,
		group_id: group.id,
		group_title: group.title,
		tabs: tabs.map(tab => ({
			tab_id: tab.id,
			url: tab.url || '',
			title: tab.title || '',
			active: Boolean(tab.active),
		})),
	};
}

async function handleMessage(message) {
	switch (message.type) {
		case 'workspace':
			if (message.event === 'created')
				await ensureGroup(message.session_id, message.workspace_id, message.label);
			if (message.event === 'renamed') {
				const group = await validGroup(workspaceKey(message.session_id, message.workspace_id));
				if (group)
					await chrome.tabGroups.update(group.id, {
						title: normalizeLabel(message.label, message.workspace_id),
					});
			}
			if (message.event === 'focused')
				await activateWorkspace(message.workspace_id, message.label, message.session_id);
			if (message.event === 'closed') {
				const key = workspaceKey(message.session_id, message.workspace_id);
				groups.delete(key);
				lastActiveTabs.delete(key);
				await saveState();
			}
			return;
		case 'open_url':
			await openLocalUrl(message);
			return;
		case 'ping':
			return;
		case 'show': {
			const win = await getManagedWindow();
			await chrome.windows.update(win.id, { focused: true });
			return;
		}
		case 'workspace_tabs':
			return workspaceTabs(message);
		default:
			throw new Error(`Unknown bridge message: ${message.type}`);
	}
}

function sendResponse(message, ok, error, result) {
	if (!message.request_id || !port) return;
	port.postMessage({ type: 'response', request_id: message.request_id, ok, error, result });
}

function connect() {
	clearTimeout(reconnectTimer);
	try {
		port = chrome.runtime.connectNative(HOST);
		port.onMessage.addListener(message => {
			messageQueue = messageQueue
				.then(() => handleMessage(message))
				.then(result => sendResponse(message, true, undefined, result))
				.catch(error => sendResponse(message, false, String(error?.message || error)));
		});
		port.onDisconnect.addListener(() => {
			port = undefined;
			reconnectTimer = setTimeout(connect, 1000);
		});
		port.postMessage({ type: 'ready' });
	} catch {
		reconnectTimer = setTimeout(connect, 1000);
	}
}

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
	for (const [workspaceId, groupId] of groups) {
		try {
			const tab = await chrome.tabs.get(tabId);
			if (tab.groupId === groupId) {
				lastActiveTabs.set(workspaceId, tabId);
				await saveState();
				break;
			}
		} catch {}
	}
});

chrome.tabGroups.onRemoved.addListener(async group => {
	for (const [workspaceId, groupId] of groups) {
		if (groupId === group.id) {
			groups.delete(workspaceId);
			lastActiveTabs.delete(workspaceId);
		}
	}
	await saveState();
});

loadState()
	.then(dedupeUnownedGroups)
	.then(connect)
	.catch(() => connect());
