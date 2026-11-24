import { startChat } from './app.mjs';

const lifecycle = new AbortController();
startChat(document, { signal: lifecycle.signal });
