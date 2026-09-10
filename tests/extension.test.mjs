import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { makeChrome } from './chrome_stub.mjs';

// The worker is a script that only ever touches the browser through the global
// `chrome`, so running its source with `chrome` bound as an argument gives every
// test a genuinely fresh worker. Importing it repeatedly would not: Node hands
// back the same module instance, side effects and all.
const SOURCE = readFileSync(
	fileURLToPath(new URL('../extension/service-worker.js', import.meta.url)),
	'utf8'
);
const SESSION = '/home/tester/.config/herdr/herdr.sock';
// Herdr's session id and the workspace id are joined by a unit separator.
const key = workspaceId => `${SESSION}\u001f${workspaceId}`;

/** Boot one fresh service worker against a fresh in-memory browser. */
async function boot(model) {
	const harness = makeChrome(model);
	// eslint-disable-next-line no-new-func
	new Function('chrome', SOURCE)(harness.chrome);
	for (let attempt = 0; attempt < 200 && !harness.port; attempt += 1) {
		await new Promise(resolve => setTimeout(resolve, 1));
	}
	assert.ok(harness.port, 'the worker never opened its native port');
	return harness;
}

function created(workspaceId, label) {
	return { type: 'workspace', event: 'created', session_id: SESSION, workspace_id: workspaceId, label };
}

test('a workspace that already owns a group in another window is not given a second one', async () => {
	// The reboot shape: a stray launch left a second window behind, and the
	// group Herdr made earlier sits in the window that is not the managed one.
	const harness = await boot({
		windows: [1, 2],
		tabs: [{ id: 50, windowId: 2, groupId: 500 }],
		groups: [{ id: 500, windowId: 2, title: 'TasteRay' }],
		storage: {
			herdrBrowserState: {
				windowId: 1,
				groups: { [`${SESSION}w16`]: 500 },
				lastActiveTabs: {},
			},
		},
	});

	await harness.deliver(created('w16', 'TasteRay'));

	assert.deepEqual(harness.strip(), [{ title: 'TasteRay', windowId: 2, tabs: 1 }]);
});

test('syncing the same workspaces twice leaves the strip unchanged', async () => {
	const harness = await boot({ windows: [1] });

	for (const round of [1, 2]) {
		await harness.deliver(created('w16', 'TasteRay'));
		await harness.deliver(created('w1M', 'ray-cards-labels'));
		await harness.deliver(created('w1R', 'tr'));
		assert.equal(harness.strip().length, 3, `round ${round} changed the strip`);
	}

	assert.deepEqual(
		harness.strip().map(group => group.title),
		['TasteRay', 'ray-cards-labels', 'tr']
	);
});

test('two workspaces sharing a label keep one group each', async () => {
	const harness = await boot({ windows: [1] });

	await harness.deliver(created('w16', 'TasteRay'));
	await harness.deliver(created('w22', 'TasteRay'));

	assert.deepEqual(
		harness.strip().map(group => group.title),
		['TasteRay', 'TasteRay']
	);
});

test('closing a workspace closes its group', async () => {
	const harness = await boot({ windows: [1] });
	await harness.deliver(created('w16', 'TasteRay'));
	await harness.deliver(created('w1M', 'ray-cards-labels'));

	await harness.deliver({
		type: 'workspace',
		event: 'closed',
		session_id: SESSION,
		workspace_id: 'w16',
	});

	assert.deepEqual(
		harness.strip().map(group => group.title),
		['ray-cards-labels']
	);
	assert.equal(harness.state.tabs.filter(tab => tab.groupId !== -1).length, 1);
});

test('opening a page from an agent does not rename the group', async () => {
	const harness = await boot({ windows: [1] });
	await harness.deliver(created('w1R', 'tr'));

	// The agent shell knows the workspace id and nothing else.
	await harness.deliver({
		type: 'open_url',
		session_id: SESSION,
		workspace_id: 'w1R',
		url: 'http://localhost:5173/',
		focus: false,
	});

	assert.deepEqual(harness.strip().map(group => group.title), ['tr']);
	assert.ok(harness.state.tabs.some(tab => tab.url === 'http://localhost:5173/'));
});

function workspaceSet(session, ...pairs) {
	return {
		type: 'workspace_set',
		session_id: session,
		workspaces: pairs.map(([workspace_id, label]) => ({ workspace_id, label })),
	};
}

test('a sync takes away the group of a workspace Herdr no longer has', async () => {
	// The event that would have closed it arrived while the bridge was down and
	// is never resent, so only the full list can put this right.
	const harness = await boot({ windows: [1] });
	await harness.deliver(workspaceSet(SESSION, ['w16', 'TasteRay'], ['w1R', 'tr'], ['w9', 'tmp']));
	assert.equal(harness.strip().length, 3);

	await harness.deliver(workspaceSet(SESSION, ['w16', 'TasteRay'], ['w1R', 'tr']));

	assert.deepEqual(harness.strip().map(group => group.title), ['TasteRay', 'tr']);
});

test('a sync leaves another Herdr session alone', async () => {
	const other = '/home/tester/.config/herdr/other.sock';
	const harness = await boot({ windows: [1] });
	await harness.deliver(workspaceSet(other, ['w1', 'their-app']));
	await harness.deliver(workspaceSet(SESSION, ['w16', 'TasteRay']));

	await harness.deliver(workspaceSet(SESSION, ['w16', 'TasteRay']));

	assert.deepEqual(harness.strip().map(group => group.title), ['their-app', 'TasteRay']);
});

test('a sync that lists nothing takes nothing away', async () => {
	// Herdr failing to answer must not read as "the user has no workspaces".
	const harness = await boot({ windows: [1] });
	await harness.deliver(workspaceSet(SESSION, ['w16', 'TasteRay']));

	await harness.deliver(workspaceSet(SESSION));

	assert.deepEqual(harness.strip().map(group => group.title), ['TasteRay']);
});

test('group ids left over from a previous browser run are dropped, not carried', async () => {
	const harness = await boot({
		windows: [1],
		storage: {
			herdrBrowserState: {
				windowId: 99,
				groups: { [`${SESSION}w16`]: 777, [`${SESSION}w1M`]: 778 },
				lastActiveTabs: { [`${SESSION}w16`]: 6001 },
			},
		},
	});

	await harness.deliver(created('w16', 'TasteRay'));

	const stored = harness.state.storage.herdrBrowserState;
	assert.equal(harness.strip().length, 1);
	assert.deepEqual(Object.values(stored.groups), harness.state.groups.map(group => group.id));
	assert.ok(!Object.values(stored.lastActiveTabs).includes(6001), 'a dead tab id survived');
});
