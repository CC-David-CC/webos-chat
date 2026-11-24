# WebOS Chat local prototype

Serve this directory over loopback and open http://127.0.0.1:8782 in a browser.

```sh
python3 -m http.server 8782 --bind 127.0.0.1
```

Open two tabs using the same browser profile. Choose Alex Morgan in one and Casey Ellis in the other. Open a DM with the other person from the recipient picker, or switch to # writing. Jordan Lane can see # general. Anon gets the sign-in screen.

IndexedDB keeps conversations, membership, ordered messages and each demo account’s read position under the local origin. BroadcastChannel updates the other tabs when a message arrives. Demo account selection and the current conversation live in session storage. The demo uses synthetic identities and serves static files. Real WebOS sign-in, cross-device delivery and file sharing are described in the parent WebOS Chat plan.
