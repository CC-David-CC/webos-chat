# WebOS Chat local prototype

Serve this directory over loopback and open http://127.0.0.1:8782 in a browser.

```sh
python3 -m http.server 8782 --bind 127.0.0.1
```

Open two tabs using the same browser profile. Choose Alex Morgan in one and Casey Ellis in the other. Open a DM with the other person from the recipient picker, or switch to # writing. Jordan Lane can see # general. Anon gets the sign-in screen.

IndexedDB keeps conversations, membership, ordered messages and each demo account’s read position under the local origin. BroadcastChannel updates the other tabs when a message arrives. Demo account selection and the current conversation live in session storage. The demo uses synthetic identities and serves static files. Real WebOS sign-in, cross-device delivery and file sharing are described in the parent WebOS Chat plan.

## Registered WebOS Chat

The existing desktop mounts `registered.mjs` from the viewer build with
`--webos-chat enabled`. The backend opts in with `WEBOS_REGISTERED_CHAT=1`.
Chat uses the signed account gateway for search, contacts and messages. Its
same-origin stream at `POST /api/chat/live` sends account-specific revision
notices. Browsers fetch new messages through the signed gateway.

The service creates `chat-state/chat.sqlite3` under its private state root.
The file stores display names, friendships, conversation membership, messages
and each member's read position. It reads enabled accounts and active sessions
from WebOS Accounts. The two stores remain separate. Keep Chat state with the
backed-up WebOS private state.

Run the backend tests with the WebOS Storage package on `PYTHONPATH`.

```sh
python -m unittest discover -s tests -v
```
