"""A same-origin stream of account-specific change notices."""

import asyncio
from collections import Counter
from dataclasses import dataclass
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from aiohttp import web
from webos_storage.policy import Denied

PATH = '/api/chat/live'
TOKEN = re.compile('Codi-Chat ([A-Za-z0-9_-]{43})')
TICKET_SECONDS = 20


@dataclass(frozen=True)
class Ticket:
    subject: str
    session: str
    expires: float


class LiveChat:
    def __init__(self, store, origin):
        self.store = store
        self.origin = origin
        self.host = urlsplit(origin).netloc
        self.tickets = {}
        self.lock = threading.Lock()
        self.streams = Counter()
        self.stopping = False

    def issue(self, principal, request):
        if request != {'resource': 'chat', 'operation': 'listen'}:
            raise Denied('Invalid Chat request.')
        expires = min(time.time() + TICKET_SECONDS, principal.deadline)
        if principal.kind != 'registered' or expires <= time.time():
            raise Denied('Sign in to use Chat.')
        self.store.live_revision(principal.sub, principal.sid)
        with self.lock:
            now = time.time()
            self.tickets = {token: ticket for token, ticket in self.tickets.items() if ticket.expires > now}
            if len(self.tickets) >= 512 or sum(t.subject == principal.sub for t in self.tickets.values()) >= 8:
                raise Denied('Too many Chat windows. Try again shortly.')
            token = secrets.token_urlsafe(32)
            self.tickets[token] = Ticket(principal.sub, principal.sid, expires)
        return {'ticket': token}

    def claim(self, token):
        with self.lock:
            ticket = self.tickets.pop(token, None)
        if ticket is None or ticket.expires <= time.time():
            raise web.HTTPForbidden()
        return ticket

    async def shutdown(self, _app):
        self.stopping = True

    async def stream(self, request):
        if (request.raw_path != PATH or request.headers.getall('Origin', []) != [self.origin]
                or request.headers.getall('Host', []) != [self.host] or request.can_read_body):
            raise web.HTTPForbidden()
        header = request.headers.getall('Authorization', [])
        match = TOKEN.fullmatch(header[0]) if len(header) == 1 else None
        if not match:
            raise web.HTTPForbidden()
        ticket = self.claim(match[1])
        if self.streams.total() >= 128 or self.streams.get(ticket.subject, 0) >= 6:
            raise web.HTTPTooManyRequests()
        self.streams[ticket.subject] += 1
        response = web.StreamResponse(headers={
            'Content-Type': 'application/x-ndjson',
            'Cache-Control': 'no-store, no-transform',
            'X-Content-Type-Options': 'nosniff',
        })
        try:
            revision = await self.revision(ticket)
            await response.prepare(request)
            last_sent = 0
            previous = None
            while not self.stopping and request.transport and not request.transport.is_closing():
                now = time.monotonic()
                if previous != revision or now - last_sent >= 12:
                    async with asyncio.timeout(5):
                        await response.write(f'{revision}\n'.encode('ascii'))
                    previous, last_sent = revision, now
                await asyncio.sleep(1.5)
                revision = await self.revision(ticket)
        except Denied:
            if not response.prepared:
                raise web.HTTPForbidden() from None
        except (ConnectionError, TimeoutError):
            pass
        finally:
            self.streams[ticket.subject] -= 1
            if not self.streams[ticket.subject]:
                del self.streams[ticket.subject]
        return response

    async def revision(self, ticket):
        return await asyncio.to_thread(self.store.live_revision, ticket.subject, ticket.session)
