// An in-memory stand-in for the slice of the Chromium extension API the service
// worker uses. The worker talks to the browser through nothing else, so a model
// that keeps windows, tabs and groups consistent is enough to drive it, and the
// native port below lets a test push the very messages Herdr pushes.

export function makeChrome({ storage = {}, windows = [1], tabs = [], groups = [] } = {}) {
	const state = {
		nextId: 1000,
		storage: structuredClone(storage),
		windows: windows.map(id => ({ id, type: 'normal' })),
		tabs: tabs.map(tab => ({ groupId: -1, active: false, url: 'chrome://newtab/', ...tab })),
		groups: groups.map(group => ({ title: '', collapsed: false, ...group })),
		removedGroups: [],
	};
	const id = () => ++state.nextId;
	const listeners = { tabsActivated: [], groupsRemoved: [] };
	const nativePorts = [];

	// Chromium drops a group as soon as its last tab leaves it. Several of the
	// worker's paths lean on that, so the model has to do it too.
	function collectEmptyGroups() {
		for (const group of [...state.groups]) {
			if (state.tabs.some(tab => tab.groupId === group.id)) continue;
			state.groups = state.groups.filter(item => item.id !== group.id);
			state.removedGroups.push(group);
			for (const listener of listeners.groupsRemoved) listener({ ...group });
		}
	}

	function matches(group, query) {
		if (query.windowId != null && group.windowId !== query.windowId) return false;
		if (query.title != null && group.title !== query.title) return false;
		return true;
	}

	const chrome = {
		storage: {
			local: {
				async get(key) {
					const keys = typeof key === 'string' ? [key] : Object.keys(key || state.storage);
					const result = {};
					for (const name of keys) {
						if (name in state.storage) result[name] = structuredClone(state.storage[name]);
					}
					return result;
				},
				async set(values) {
					Object.assign(state.storage, structuredClone(values));
				},
				async remove(key) {
					delete state.storage[key];
				},
			},
		},
		windows: {
			async getAll() {
				return state.windows.map(win => ({ ...win }));
			},
			async get(windowId) {
				const win = state.windows.find(item => item.id === windowId);
				if (!win) throw new Error(`No window with id ${windowId}`);
				return { ...win };
			},
			async create() {
				const win = { id: id(), type: 'normal' };
				state.windows.push(win);
				return { ...win };
			},
			async update(windowId, properties) {
				const win = state.windows.find(item => item.id === windowId);
				if (!win) throw new Error(`No window with id ${windowId}`);
				Object.assign(win, properties);
				return { ...win };
			},
		},
		tabs: {
			async create({ windowId, url = 'chrome://newtab/', active = true }) {
				const tab = { id: id(), windowId: windowId ?? state.windows[0].id, url, active, groupId: -1 };
				state.tabs.push(tab);
				return { ...tab };
			},
			async get(tabId) {
				const tab = state.tabs.find(item => item.id === tabId);
				if (!tab) throw new Error(`No tab with id ${tabId}`);
				return { ...tab };
			},
			async query(query = {}) {
				return state.tabs
					.filter(tab => {
						if (query.groupId != null && tab.groupId !== query.groupId) return false;
						if (query.windowId != null && tab.windowId !== query.windowId) return false;
						return true;
					})
					.map(tab => ({ ...tab }));
			},
			async update(tabId, properties) {
				const tab = state.tabs.find(item => item.id === tabId);
				if (!tab) throw new Error(`No tab with id ${tabId}`);
				Object.assign(tab, properties);
				if (properties.active) {
					for (const other of state.tabs) {
						if (other.windowId === tab.windowId && other.id !== tab.id) other.active = false;
					}
				}
				return { ...tab };
			},
			async group({ groupId, tabIds }) {
				const moving = tabIds.map(tabId => state.tabs.find(tab => tab.id === tabId)).filter(Boolean);
				let group = state.groups.find(item => item.id === groupId);
				if (groupId != null && !group) throw new Error(`No group with id ${groupId}`);
				if (!group) {
					group = { id: id(), title: '', collapsed: false, windowId: moving[0].windowId };
					state.groups.push(group);
				}
				// Grouping a tab into a group that lives elsewhere pulls the tab
				// into that window, exactly as Chromium does.
				for (const tab of moving) {
					tab.groupId = group.id;
					tab.windowId = group.windowId;
				}
				collectEmptyGroups();
				return group.id;
			},
			async remove(tabIds) {
				const ids = new Set(Array.isArray(tabIds) ? tabIds : [tabIds]);
				state.tabs = state.tabs.filter(tab => !ids.has(tab.id));
				collectEmptyGroups();
			},
			onActivated: { addListener: fn => listeners.tabsActivated.push(fn) },
		},
		tabGroups: {
			async get(groupId) {
				const group = state.groups.find(item => item.id === groupId);
				if (!group) throw new Error(`No group with id ${groupId}`);
				return { ...group };
			},
			async query(query = {}) {
				return state.groups.filter(group => matches(group, query)).map(group => ({ ...group }));
			},
			async update(groupId, properties) {
				const group = state.groups.find(item => item.id === groupId);
				if (!group) throw new Error(`No group with id ${groupId}`);
				Object.assign(group, properties);
				return { ...group };
			},
			onRemoved: { addListener: fn => listeners.groupsRemoved.push(fn) },
		},
		runtime: {
			connectNative() {
				const port = {
					messageListeners: [],
					disconnectListeners: [],
					sent: [],
					onMessage: { addListener: fn => port.messageListeners.push(fn) },
					onDisconnect: { addListener: fn => port.disconnectListeners.push(fn) },
					postMessage: message => port.sent.push(message),
				};
				nativePorts.push(port);
				return port;
			},
		},
	};

	return {
		chrome,
		state,
		listeners,
		get port() {
			return nativePorts[nativePorts.length - 1];
		},
		/** Push one Herdr message and resolve once the worker has answered it. */
		async deliver(message) {
			const requestId = message.request_id || `req-${id()}`;
			const answered = new Promise(resolve => {
				const original = this.port.postMessage;
				this.port.postMessage = reply => {
					original(reply);
					if (reply.type === 'response' && reply.request_id === requestId) {
						this.port.postMessage = original;
						resolve(reply);
					}
				};
			});
			for (const listener of this.port.messageListeners) {
				listener({ ...message, request_id: requestId });
			}
			return answered;
		},
		/** The tab strip as a person would read it: one entry per group. */
		strip() {
			return this.state.groups.map(group => ({
				title: group.title,
				windowId: group.windowId,
				tabs: this.state.tabs.filter(tab => tab.groupId === group.id).length,
			}));
		},
	};
}
