import { chatTransport } from './transport.mjs';

const VIEW = `
  <header class="top">
    <h1>WebOS Chat</h1>
    <div class="account">
      <span id="connection" role="status">Connecting…</span>
      <details id="profile">
        <summary id="identity">Account</summary>
        <form id="profile-form">
          <label for="display-name">Display name</label>
          <div><input id="display-name" maxlength="64" placeholder="Use username"><button>Save</button></div>
        </form>
      </details>
    </div>
  </header>
  <p id="notice" role="alert" hidden></p>
  <main class="chat">
    <aside>
      <nav aria-label="People"><button id="people">Find people and manage friends</button></nav>
      <section>
        <h2>Direct messages</h2>
        <nav id="threads" aria-label="Direct messages"></nav>
      </section>
    </aside>
    <section id="directory" class="directory">
      <h2>Friends</h2>
      <form id="search-form">
        <label for="query">Search registered users</label>
        <div><input id="query" type="search" maxlength="64" placeholder="Username or display name" required><button>Search</button></div>
      </form>
      <section id="results-panel" hidden>
        <h3>Search results</h3><div id="results"></div>
      </section>
      <section>
        <h3>Friend requests</h3><div id="requests"></div>
      </section>
      <section>
        <h3>Your friends</h3><div id="friends"></div>
      </section>
    </section>
    <section class="conversation" hidden>
      <header>
        <button id="back" type="button">Back</button>
        <div><h2 id="conversation-title"></h2><p id="members"></p></div>
      </header>
      <div id="messages" role="log" aria-label="Messages" tabindex="0">
        <button id="older" type="button" hidden>Load older messages</button>
        <div id="history"></div>
      </div>
      <form id="compose">
        <label for="message">Message</label>
        <p id="hint" class="empty" hidden>Sending is available between accepted friends.</p>
        <div><input id="message" maxlength="4000" autocomplete="off" required><button>Send</button></div>
      </form>
    </section>
  </main>`;

export function mount(host) {
  const doc = host.document ?? document;
  const element = doc.createElement('section');
  element.className = 'window-content webos-chat';
  const root = element.attachShadow({ mode: 'open' });
  for (const file of ['style.css', 'registered.css']) {
    const link = doc.createElement('link');
    link.rel = 'stylesheet';
    link.href = new URL(file, import.meta.url);
    root.append(link);
  }
  const section = doc.createElement('div');
  section.className = 'registered';
  root.append(section);
  const lifetime = new AbortController();
  const { signal } = lifetime;
  const subject = host.storage?.signedInSubject?.();
  if (!subject) {
    section.textContent = 'Sign in to use Chat.';
  } else {
    section.innerHTML = VIEW;
    start(section, chatTransport(host, subject, signal), doc, signal);
  }
  return { element, dispose: () => lifetime.abort() };
}

function start(view, transport, doc, signal) {
  const by = selector => view.querySelector(selector);
  const on = (node, event, fn) => node.addEventListener(event, fn, { signal });
  const find = by('#people');
  const back = by('#back');
  const notice = by('#notice');
  const profile = by('#profile');
  const identity = by('#identity');
  const input = by('#display-name');
  const query = by('#query');
  const directory = by('#directory');
  const conversation = by('.conversation');
  const message = by('#message');
  const log = by('#messages');
  const history = by('#history');
  const older = by('#older');
  const compose = by('#compose');
  const sendButton = compose.querySelector('button');
  const drafts = new Map();
  let state;
  let selected = null;
  let selectionGeneration = 0;
  let loaded = [];
  let more = false;
  let loading = false;
  let sending = false;
  let reading = false;
  let requestedRead = 0;
  let searchSequence = 0;
  let results;
  let syncing = false;
  let getUpdate = false;

  function report(text = '') {
    notice.hidden = !text;
    notice.textContent = text;
  }

  function run(fn) {
    return async event => {
      event?.preventDefault();
      report();
      try {
        await fn();
      } catch (error) {
        if (!signal.aborted) report(error.message || 'Chat is unavailable.');
      }
    };
  }

  function button(label, action) {
    const node = doc.createElement('button');
    node.type = 'button';
    node.textContent = label;
    on(node, 'click', run(action));
    return node;
  }

  function text(tag, value, name = '') {
    const node = doc.createElement(tag);
    node.textContent = value;
    if (name) node.className = name;
    return node;
  }

  const label = person => person.displayName === person.username ? person.username : `${person.displayName} (@${person.username})`;
  const contact = id => state.contacts.find(person => person.subject === id);
  const thread = () => state?.conversations.find(item => item.id === selected);
  const atBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 20;

  function renderSidebar() {
    find.setAttribute('aria-current', String(!selected));
    const items = state.conversations.map(item => {
      const name = item.peer.displayName;
      const open = button(name, () => openThread(item.id));
      open.setAttribute('aria-current', String(selected === item.id));
      if (item.archived) open.append(text('span', 'Read-only', 'badge'));
      if (item.unread) open.append(text('span', 'Unread', 'badge'));
      return open;
    });
    by('#threads').replaceChildren(...items);
  }

  async function friendship(operation, person) {
    await transport.request(operation, { peer: person.subject });
    await refresh();
  }

  function personRow(person) {
    const row = text('article', '', 'person');
    row.append(text('strong', label(person)));
    const actions = text('div', '', 'person-actions');
    row.append(actions);
    const relation = contact(person.subject)?.relationship;
    const action = (name, operation) => button(name, () => friendship(operation, person));
    if (relation === 'friend') {
      actions.append(button('Message', async () => {
        const { id } = await transport.request('direct', { peer: person.subject });
        await refresh();
        await openThread(id);
      }), action('Remove friend', 'friend-remove'));
    } else if (relation === 'received') {
      actions.append(action('Accept', 'friend-accept'), action('Decline', 'friend-remove'));
    } else if (relation === 'sent') {
      actions.append(text('span', 'Request sent'), action('Cancel request', 'friend-remove'));
    } else {
      actions.append(action('Add friend', 'friend-request'));
    }
    return row;
  }

  function renderDirectory() {
    const friends = state.contacts.filter(person => person.relationship === 'friend');
    const requests = state.contacts.filter(person => person.relationship !== 'friend');
    by('#friends').replaceChildren(...(friends.length ? friends.map(personRow) : [text('p', 'No friends yet.', 'empty')]));
    by('#requests').replaceChildren(...(requests.length ? requests.map(personRow) : [text('p', 'No pending requests.', 'empty')]));
    if (results) {
      const people = results.map(person => contact(person.subject) ?? person);
      by('#results-panel').hidden = false;
      by('#results').replaceChildren(...(people.length ? people.map(personRow) : [text('p', 'No matching users.', 'empty')]));
    }
  }

  function renderConversation() {
    const current = thread();
    directory.hidden = !!current;
    conversation.hidden = !current;
    if (!current) return;
    by('#conversation-title').textContent = current.peer.displayName;
    by('#members').textContent = current.archived ? 'Archived conversation' : '@' + current.peer.username;
    const maySend = !current.archived && contact(current.peer.subject)?.relationship === 'friend';
    by('#hint').textContent = current.archived ? 'This account expired. You can still read the conversation.' :
      'You can send direct messages to accepted friends.';
    by('#hint').hidden = maySend;
    message.disabled = !maySend;
    sendButton.disabled = !maySend || sending;
  }

  function renderHistory(scroll) {
    const current = thread();
    if (!current) {
      history.replaceChildren();
      older.hidden = true;
      return;
    }
    const rows = loaded.map(row => {
      const entry = text('article', '', 'message');
      const header = doc.createElement('header');
      const sender = row.sender === state.me.subject ? state.me : current.peer;
      const date = new Date(row.created * 1000);
      const time = text('time', date.toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }));
      time.dateTime = date.toISOString();
      header.append(text('strong', sender.displayName), time);
      entry.append(header, text('p', row.text));
      return entry;
    });
    history.replaceChildren(...(rows.length ? rows : [text('p', 'No messages yet.', 'empty')]));
    older.hidden = !more;
    if (scroll) log.scrollTop = log.scrollHeight;
  }

  async function markRead() {
    const last = loaded.at(-1)?.id;
    const current = thread();
    if (!current?.unread || !last || reading || last === requestedRead ||
        doc.visibilityState !== 'visible' || !atBottom() || !log.getClientRects().length) return;
    requestedRead = last;
    reading = true;
    try {
      await transport.request('read', { conversation: current.id, message: last });
    } catch (error) {
      if (requestedRead === last) requestedRead = 0;
    } finally {
      reading = false;
    }
  }

  async function readNew(id, generation) {
    const scroll = !loaded.length || atBottom();
    let added = false;
    let page;
    do {
      const after = loaded.at(-1)?.id ?? 0;
      page = await transport.request('messages', { conversation: id, after, before: 0 });
      if (generation !== selectionGeneration || signal.aborted) return;
      if (!after) more = page.more;
      if (page.messages.length) {
        loaded.push(...page.messages);
        added = true;
      }
      if (!after) break;
    } while (page.more);
    if (added || !loaded.length) renderHistory(scroll);
    await markRead();
  }

  async function openThread(id) {
    if (selected === id) return;
    if (selected) {
      const prior = drafts.get(selected);
      drafts.set(selected, { text: message.value, retry: prior?.retry });
    }
    selected = id;
    const generation = ++selectionGeneration;
    loaded = [];
    more = false;
    requestedRead = 0;
    message.value = id ? drafts.get(id)?.text ?? '' : '';
    renderSidebar();
    renderConversation();
    renderHistory(true);
    if (id && thread()) {
      await refresh();
      if (generation === selectionGeneration && !message.disabled) message.focus();
    }
  }

  async function refresh() {
    getUpdate = true;
    if (syncing) return syncing;
    syncing = (async () => {
      while (getUpdate && !signal.aborted) {
        getUpdate = false;
        const previous = state;
        state = await transport.request('snapshot');
        identity.textContent = label(state.me);
        if (!profile.open) input.value = state.me.displayName === state.me.username ? '' : state.me.displayName;
        let resultsChanged = false;
        const threadsChanged = JSON.stringify(previous?.conversations) !== JSON.stringify(state.conversations);
        if (threadsChanged && results && previous) {
          const active = new Set(state.conversations.filter(item => !item.archived).map(item => item.id));
          const departed = new Set(previous.conversations.filter(item => !item.archived && !active.has(item.id))
            .map(item => item.peer.subject));
          const visible = results.filter(person => !departed.has(person.subject));
          resultsChanged = visible.length !== results.length;
          results = visible;
        }
        if (selected && !thread()) {
          const hadFocus = conversation.contains(view.getRootNode().activeElement);
          await openThread(null);
          if (hadFocus) find.focus();
          report('This conversation is no longer available.');
        } else if (threadsChanged) {
          renderSidebar();
        }
        if (resultsChanged || !previous || JSON.stringify(previous.contacts) !== JSON.stringify(state.contacts)) renderDirectory();
        renderConversation();
        const peer = thread()?.peer;
        const previousPeer = previous?.conversations.find(item => item.id === selected)?.peer;
        if (loaded.length && peer && previousPeer?.displayName !== peer.displayName) renderHistory(false);
        if (selected && !loading) await readNew(selected, selectionGeneration);
      }
    })();
    try {
      await syncing;
    } catch (error) {
      if (!signal.aborted) report(error.message || 'Chat is unavailable.');
    } finally {
      syncing = false;
      if (getUpdate && !signal.aborted) void refresh();
    }
  }

  on(find, 'click', run(() => state && openThread(null)));
  on(back, 'click', run(async () => {
    await openThread(null);
    find.focus();
  }));
  on(by('#profile-form'), 'submit', run(async () => {
    await transport.request('display-name', { name: input.value });
    profile.open = false;
    await refresh();
  }));
  on(by('#search-form'), 'submit', run(async () => {
    if (!state) await refresh();
    if (!state) return;
    const sequence = ++searchSequence;
    const found = await transport.request('search', { query: query.value });
    if (sequence !== searchSequence || signal.aborted) return;
    results = found.people;
    renderDirectory();
  }));
  on(older, 'click', run(async () => {
    if (loading || !selected || !loaded.length) return;
    loading = true;
    older.disabled = true;
    const id = selected;
    const generation = selectionGeneration;
    try {
      const page = await transport.request('messages', { conversation: id, before: loaded[0].id, after: 0 });
      if (generation !== selectionGeneration) return;
      const height = log.scrollHeight;
      const offset = log.scrollTop;
      loaded.unshift(...page.messages);
      more = page.more;
      renderHistory(false);
      log.scrollTop = offset + log.scrollHeight - height;
    } finally {
      loading = false;
      older.disabled = false;
      void refresh();
    }
  }));
  on(compose, 'submit', run(async () => {
    if (!selected || message.disabled || sending) return;
    const id = selected;
    const body = message.value.trim();
    if (!body) return;
    const prior = drafts.get(id)?.retry;
    const retry = prior?.body === body ? prior : { body, clientId: crypto.randomUUID() };
    drafts.set(id, { text: message.value, retry });
    sending = true;
    renderConversation();
    try {
      await transport.request('send', { conversation: id, ...retry });
      if (selected === id && message.value.trim() === body) message.value = '';
      drafts.delete(id);
      await refresh();
    } finally {
      sending = false;
      renderConversation();
    }
  }));
  on(log, 'scroll', () => void markRead());
  on(doc, 'visibilitychange', () => void markRead());
  on(doc.defaultView, 'focus', () => void markRead());
  find.setAttribute('aria-current', 'true');
  void refresh();
  void transport.listen(() => void refresh(), text => { by('#connection').textContent = text; });
}
