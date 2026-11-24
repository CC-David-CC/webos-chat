import { startChat } from './app.mjs';

export function mount(host) {
  const doc = host.document ?? document;
  const element = doc.createElement('section');
  element.className = 'window-content webos-chat-demo';
  const subject = host.storage?.signedInSubject?.();
  const lifetime = new AbortController();
  const root = element.attachShadow({ mode: 'open' });
  const status = doc.createElement('p');
  const style = doc.createElement('link');
  style.rel = 'stylesheet';
  style.href = new URL('./style.css', import.meta.url);
  root.append(style, status);
  status.textContent = subject ? 'Opening Chat…' : 'Sign in to use Chat';

  if (subject) void open();
  async function open() {
    try {
      const response = await fetch(new URL('./view.html', import.meta.url), { signal: lifetime.signal });
      if (!response.ok) throw new Error('Chat is unavailable.');
      const html = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (lifetime.signal.aborted || host.storage.signedInSubject() !== subject) return;
      status.remove();
      root.append(...html.body.children);
      startChat(root, {
        namespace: 'webos-chat-demo:' + subject,
        signal: lifetime.signal,
        async delivery(label) {
          const options = { method: 'POST', signal: lifetime.signal, credentials: 'omit' };
          try {
            const result = await fetch('/webos-demo-message', options);
            label.textContent = await result.text() === 'no connection' ? 'no connection' : 'Delivery check unavailable';
          } catch (error) {
            if (!lifetime.signal.aborted) label.textContent = 'Delivery check unavailable';
          }
        },
      });
    } catch (error) {
      if (!lifetime.signal.aborted) status.textContent = 'Chat is unavailable.';
    }
  }

  return { element, dispose() { lifetime.abort(); } };
}
