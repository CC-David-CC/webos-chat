import { createChatStore, people } from './store.mjs';

export function startChat(root, { namespace = 'webos-chat-demo', signal, delivery } = {}) {
  const find = id => root.querySelector('#' + id);
  const { conversationsFor, markRead, openDirect, sendMessage, subscribe, close } = createChatStore(namespace);
  const account = find('account');
  const sessionKey = namespace + ':account';
  const channelList = find('channels');
  const directList = find('directs');
  const log = find('messages');
  const recipient = find('recipient');
  const composer = find('compose');
  const input = find('message');
  const time = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });

  let currentId = '';
  let renderSequence = 0;

  function make(tag, text, className) {
    const item = document.createElement(tag);
    if (text) item.textContent = text;
    if (className) item.className = className;
    return item;
  }

  function option(id, name) {
    const item = make('option', name);
    item.value = id;
    return item;
  }

  function isUser(id) {
    return Object.hasOwn(people, id);
  }

  function report(error) {
    const label = find('error');
    label.textContent = error?.message ?? '';
    label.hidden = !error;
  }

  function title(item, user) {
    return item.kind === 'channel' ? item.name : people[item.members.find(id => id !== user)];
  }

  function conversationButton(item, user) {
    const label = title(item, user);
    const button = make('button', label);
    button.type = 'button';
    button.dataset.conversation = item.id;
    if (item.id === currentId) button.setAttribute('aria-current', 'true');
    const unread = item.messages.filter(message => message.id > (item.read[user] ?? 0) && message.author !== user).length;
    if (unread && item.id !== currentId) {
      button.append(make('span', String(unread), 'badge'));
      button.setAttribute('aria-label', label + ', ' + unread + ' unread');
    }
    button.addEventListener('click', () => selectConversation(item.id));
    return button;
  }

  function renderMessages(item) {
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    const scrollPosition = log.scrollTop;
    const previousCount = log.children.length;
    const rows = item?.messages.map(message => {
      const entry = make('article', '', 'message');
      const header = make('header');
      const date = new Date(message.at);
      const stamp = make('time', time.format(date));
      stamp.dateTime = date.toISOString();
      header.append(make('strong', people[message.author]), stamp);
      entry.append(header, make('p', message.text));
      return entry;
    }) ?? [];
    log.replaceChildren(...(rows.length ? rows : [make('p', 'No messages yet.', 'empty')]));
    if (rows.length >= previousCount && atBottom) log.scrollTop = log.scrollHeight;
    else log.scrollTop = scrollPosition;
  }

  async function refresh() {
    const sequence = ++renderSequence;
    const user = account.value;
    if (!isUser(user) || signal.aborted) return;
    try {
      const conversations = await conversationsFor(user);
      if (sequence !== renderSequence || signal.aborted) return;
      const selected = conversations.find(item => item.id === currentId) ?? conversations[0];
      currentId = selected?.id ?? '';
      channelList.replaceChildren(...conversations.filter(item => item.kind === 'channel').map(item => conversationButton(item, user)));
      directList.replaceChildren(...conversations.filter(item => item.kind === 'direct').map(item => conversationButton(item, user)));
      find('name').textContent = selected ? title(selected, user) : 'Conversations';
      find('members').textContent = selected?.members.map(id => people[id]).join(', ') ?? '';
      renderMessages(selected);
      composer.hidden = !selected;
      const last = selected?.messages.at(-1)?.id ?? 0;
      if (last > (selected?.read[user] ?? 0) && document.visibilityState === 'visible') {
        await markRead(user, currentId, last);
      }
    } catch (error) {
      if (sequence === renderSequence) report(error);
    }
  }

  function selectConversation(id) {
    currentId = id;
    log.replaceChildren();
    input.value = '';
    sessionStorage.setItem(sessionKey + ':' + account.value, id);
    report();
    void refresh();
  }

  function applyAccount() {
    const user = account.value;
    const signedIn = isUser(user);
    ++renderSequence;
    sessionStorage.setItem(sessionKey, user);
    currentId = signedIn ? sessionStorage.getItem(sessionKey + ':' + user) || '' : '';
    log.replaceChildren();
    channelList.replaceChildren();
    directList.replaceChildren();
    composer.hidden = true;
    input.value = '';
    find('delivery').textContent = '';
    find('name').textContent = 'Conversations';
    find('members').textContent = '';
    find('signed-out').hidden = signedIn;
    find('chat').hidden = !signedIn;
    report();
    if (!signedIn) return;
    recipient.replaceChildren(option('', 'Choose a person'), ...Object.entries(people)
      .filter(([id]) => id !== user).map(([id, name]) => option(id, name)));
    void refresh();
  }

  account.replaceChildren(option('', 'Anon'), ...Object.entries(people).map(([id, name]) => option(id, name)));
  const rememberedUser = sessionStorage.getItem(sessionKey);
  account.value = isUser(rememberedUser) ? rememberedUser : '';
  account.addEventListener('change', applyAccount, { signal });
  document.addEventListener('visibilitychange', () => void refresh(), { signal });
  subscribe(() => void refresh(), signal);
  signal.addEventListener('abort', close, { once: true });

  find('new-direct').addEventListener('submit', async event => {
    event.preventDefault();
    const user = account.value;
    try {
      const id = await openDirect(user, recipient.value);
      if (account.value !== user) return;
      selectConversation(id);
      recipient.value = '';
    } catch (error) {
      report(error);
    }
  }, { signal });

  composer.addEventListener('submit', async event => {
    event.preventDefault();
    const user = account.value;
    const id = currentId;
    const button = composer.querySelector('button');
    button.disabled = true;
    try {
      await sendMessage(user, id, input.value);
      if (account.value !== user || currentId !== id) return;
      input.value = '';
      input.focus();
      report();
      await refresh();
      if (delivery && !signal.aborted) await delivery(find('delivery'));
    } catch (error) {
      report(error);
    } finally {
      button.disabled = false;
    }
  }, { signal });

  applyAccount();
}
