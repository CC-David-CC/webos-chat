export function chatTransport(host, subject, signal) {
  function check(context) {
    if (signal.aborted || !context.valid() || host.storage.signedInSubject() !== subject) {
      throw new Error('Sign in again to use Chat.');
    }
  }

  async function request(operation, fields = {}) {
    const context = await host.storage.accountFileContext();
    check(context);
    const result = await context.files.request(operation, { resource: 'chat', ...fields });
    check(context);
    return result;
  }

  async function listen(onChange, onStatus) {
    let retry = 1000;
    while (!signal.aborted && host.storage.signedInSubject() === subject) {
      try {
        const { ticket } = await request('listen');
        if (!/^[A-Za-z0-9_-]{43}$/.test(ticket)) throw new Error('Invalid Chat ticket.');
        const response = await fetch('/api/chat/live', {
          method: 'POST',
          headers: { Authorization: 'Codi-Chat ' + ticket },
          credentials: 'omit',
          cache: 'no-store',
          signal,
        });
        if (!response.ok || !response.headers.get('content-type')?.startsWith('application/x-ndjson')) {
          throw new Error('Live updates are unavailable.');
        }
        let remaining = '';
        let previous = null;
        for await (const chunk of response.body.pipeThrough(new TextDecoderStream())) {
          retry = 1000;
          remaining += chunk;
          if (remaining.length > 1024) throw new Error('Invalid Chat update.');
          let boundary;
          while ((boundary = remaining.indexOf('\n')) !== -1) {
            const revision = remaining.slice(0, boundary);
            remaining = remaining.slice(boundary + 1);
            if (!/^\d{1,16}$/.test(revision)) throw new Error('Invalid Chat revision.');
            if (revision !== previous) {
              previous = revision;
              onChange();
            }
            onStatus('Connected');
          }
        }
      } catch (error) {
        if (signal.aborted) return;
      }
      if (signal.aborted) return;
      onStatus('Reconnecting…');
      await new Promise(resolve => setTimeout(resolve, retry));
      retry = Math.min(retry * 2, 15000);
    }
  }

  return { request, listen };
}
