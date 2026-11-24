export const people = Object.freeze({
  alex: 'Alex Morgan',
  casey: 'Casey Ellis',
  jordan: 'Jordan Lane',
});

const initial = () => ({
  conversations: {
    general: {
      id: 'general', name: '# general', kind: 'channel',
      members: Object.keys(people),
      messages: [{ id: 1, author: 'jordan', text: 'Welcome to the project workspace.', at: Date.now() }],
      read: {},
    },
    writing: {
      id: 'writing', name: '# writing', kind: 'channel',
      members: ['alex', 'casey'],
      messages: [], read: {},
    },
  },
});

export function createChatStore(namespace) {
  const eventBus = new BroadcastChannel(namespace);
  let closed = false;
  const database = new Promise((resolve, reject) => {
    const request = indexedDB.open(namespace, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore('chat').add(initial(), 'state');
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });

  function validateUser(user) {
    if (!Object.hasOwn(people, user)) throw new Error('Choose a registered demo account.');
  }

  function memberConversation(state, id, user) {
    const conversation = Object.hasOwn(state.conversations, id) ? state.conversations[id] : null;
    if (!conversation?.members.includes(user)) throw new Error('This conversation is unavailable.');
    return conversation;
  }

  async function transaction(mode, operation) {
    const db = await database;
    return new Promise((resolve, reject) => {
      const tx = db.transaction('chat', mode);
      const store = tx.objectStore('chat');
      const request = store.get('state');
      let outcome;
      let failure;
      request.onsuccess = () => {
        try {
          const state = request.result;
          outcome = operation(state);
          if (mode === 'readwrite' && outcome.changed) store.put(state, 'state');
        } catch (error) {
          failure = error;
          tx.abort();
        }
      };
      tx.oncomplete = () => {
        if (outcome.changed && !closed) eventBus.postMessage('updated');
        resolve(outcome.value);
      };
      tx.onabort = () => reject(failure || tx.error || new Error('Chat storage could not be updated.'));
      tx.onerror = () => reject(tx.error);
    });
  }

  function subscribe(callback, signal) {
    eventBus.addEventListener('message', callback, { signal });
  }

  function conversationsFor(user) {
    validateUser(user);
    return transaction('readonly', state => ({
      value: Object.values(state.conversations).filter(item => item.members.includes(user)),
    }));
  }

  function openDirect(user, recipient) {
    validateUser(user);
    validateUser(recipient);
    if (user === recipient) throw new Error('Choose a different account.');
    const members = [user, recipient].sort();
    const id = 'dm:' + members.join(':');
    return transaction('readwrite', state => {
      if (Object.hasOwn(state.conversations, id)) return { value: id };
      state.conversations[id] = { id, kind: 'direct', members, messages: [], read: {} };
      return { value: id, changed: true };
    });
  }

  function sendMessage(user, id, draft) {
    validateUser(user);
    const text = draft.trim();
    if (!text || text.length > 2000) throw new Error('Enter a message between 1 and 2,000 characters.');
    return transaction('readwrite', state => {
      const conversation = memberConversation(state, id, user);
      const message = {
        id: (conversation.messages.at(-1)?.id ?? 0) + 1,
        author: user,
        text,
        at: Date.now(),
      };
      conversation.messages.push(message);
      conversation.read[user] = message.id;
      return { value: message.id, changed: true };
    });
  }

  function markRead(user, id, message) {
    validateUser(user);
    return transaction('readwrite', state => {
      const conversation = memberConversation(state, id, user);
      const last = conversation.messages.at(-1)?.id ?? 0;
      const position = Math.min(message, last);
      if (position <= (conversation.read[user] ?? 0)) return {};
      conversation.read[user] = position;
      return { changed: true };
    });
  }
  function close() {
    closed = true;
    eventBus.close();
    void database.then(db => db.close(), () => {});
  }
  return { subscribe, conversationsFor, openDirect, sendMessage, markRead, close };
}
