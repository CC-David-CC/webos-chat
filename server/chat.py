"""Store registered-account conversations and enforce Chat access in SQLite."""

from contextlib import closing, contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from webos_storage.policy import Denied

PAGE = 50
MAX_RELATIONSHIPS = 200
MAX_CONVERSATIONS = 500
UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
OPERATIONS = {
    'snapshot': (),
    'search': ('query',),
    'display-name': ('name',),
    'friend-request': ('peer',),
    'friend-accept': ('peer',),
    'friend-remove': ('peer',),
    'direct': ('peer',),
    'messages': ('conversation', 'after', 'before'),
    'send': ('conversation', 'clientId', 'body'),
    'read': ('conversation', 'message'),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    subject TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    folded TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS friendships (
    first TEXT NOT NULL,
    second TEXT NOT NULL,
    requester TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
    CHECK (first < second AND requester IN (first, second)),
    PRIMARY KEY (first, second)
);
CREATE INDEX IF NOT EXISTS friendships_second ON friendships(second);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS direct_pairs (
    first TEXT NOT NULL,
    second TEXT NOT NULL,
    conversation TEXT NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
    CHECK (first < second),
    PRIMARY KEY (first, second)
);
CREATE TABLE IF NOT EXISTS members (
    conversation TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    last_read INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation, subject)
);
CREATE INDEX IF NOT EXISTS members_subject ON members(subject);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation TEXT NOT NULL,
    sender TEXT NOT NULL,
    client_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created INTEGER NOT NULL,
    UNIQUE (conversation, sender, client_id),
    FOREIGN KEY (conversation, sender) REFERENCES members(conversation, subject) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation, id);
CREATE TABLE IF NOT EXISTS updates (
    subject TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
);
"""

# This view deliberately exposes only active account identities to Chat queries.
DIRECTORY = """
CREATE TEMP VIEW people AS
SELECT a.subject, a.username, coalesce(p.name, a.username) AS display_name,
       coalesce(p.folded, a.username) AS folded
FROM directory.accounts a JOIN directory.account_policy ap USING (subject)
LEFT JOIN profiles p USING (subject)
WHERE ap.disabled = 0
"""


def pair(subject, peer):
    return (subject, peer) if subject < peer else (peer, subject)


def public_person(row):
    return {'subject': row['subject'], 'username': row['username'], 'displayName': row['display_name']}


def identifier(value, prefix=''):
    if not isinstance(value, str) or not value.startswith(prefix) or not UUID.fullmatch(value[len(prefix):]):
        raise Denied('Invalid Chat identifier.')
    return value


def cursor(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise Denied('Invalid message position.')
    return value


class ChatStore:
    def __init__(self, directory, account_db, clock=time.time):
        directory = Path(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Owned private directory required for Chat')
        self.path = directory / 'chat.sqlite3'
        self.account_db = Path(account_db).absolute().as_uri() + '?mode=ro'
        self.clock = clock
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError('Owned private Chat database required') from None
        else:
            os.close(fd)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self, *, directory=True, write=False):
        with closing(sqlite3.connect(self.path, uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            if directory:
                db.execute('ATTACH DATABASE ? AS directory', (self.account_db,))
                db.execute(DIRECTORY)
            with db:
                db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield db

    def check_session(self, db, subject, session):
        now = int(self.clock())
        row = db.execute(
            """SELECT 1 FROM directory.registered_sessions s
               JOIN directory.account_policy p ON p.subject = ?
               JOIN directory.accounts a USING (subject)
               WHERE s.deadline > ? AND p.disabled = 0
                 AND json_extract(s.record, '$.sid') = ?
                 AND json_extract(s.record, '$.sub') = a.subject
                 AND json_extract(s.record, '$.name') = a.username
                 AND json_extract(s.record, '$.kind') = 'registered'
                 AND json_extract(s.record, '$._account_generation') = p.generation
               LIMIT 1""",
            (subject, now, session),
        ).fetchone()
        if row is None:
            raise Denied('Sign in to use Chat.')

    @staticmethod
    def revision(db, subject):
        row = db.execute('SELECT revision FROM updates WHERE subject = ?', (subject,)).fetchone()
        return row[0] if row else 0

    def live_revision(self, subject, session):
        with self.connection() as db:
            self.check_session(db, subject, session)
            return self.revision(db, subject)

    @staticmethod
    def touch(db, *subjects):
        db.executemany(
            '''INSERT INTO updates VALUES (?, 1)
               ON CONFLICT(subject) DO UPDATE SET revision = revision + 1''',
            ((subject,) for subject in set(subjects)),
        )

    def dispatch(self, principal, request):
        operation = request.get('operation')
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise Denied('Chat operation unavailable.')
        if set(request) != {'resource', 'operation', *OPERATIONS[operation]}:
            raise Denied('Invalid Chat request.')
        if principal.kind != 'registered' or principal.deadline <= self.clock():
            raise Denied('Sign in to use Chat.')
        with self.connection(write=operation not in ('snapshot', 'search', 'messages')) as db:
            self.check_session(db, principal.sub, principal.sid)
            subject = principal.sub
            fields = {name: request[name] for name in OPERATIONS[operation]}
            if operation.startswith('friend-'):
                return self.friendship(db, subject, identifier(fields['peer'], 'user:'), operation)
            match operation:
                case 'snapshot':
                    return self.snapshot(db, subject)
                case 'search':
                    return self.search(db, subject, fields['query'])
                case 'display-name':
                    return self.display_name(db, subject, fields['name'])
                case 'direct':
                    return self.direct(db, subject, identifier(fields['peer'], 'user:'))
            conversation = identifier(fields['conversation'])
            peer = self.conversation_peer(db, subject, conversation)
            if operation == 'messages':
                return self.messages(db, conversation, cursor(fields['after']), cursor(fields['before']))
            if operation == 'send':
                return self.send(db, subject, peer, conversation, identifier(fields['clientId']), fields['body'])
            return self.mark_read(db, subject, conversation, cursor(fields['message']))

    def snapshot(self, db, subject):
        profile = db.execute('SELECT * FROM people WHERE subject = ?', (subject,)).fetchone()
        related = db.execute(
            '''SELECT p.*, f.accepted, f.requester FROM friendships f
               JOIN people p ON p.subject = CASE WHEN f.first = ? THEN f.second ELSE f.first END
               WHERE f.first = ? OR f.second = ? ORDER BY p.username''', (subject,) * 3,
        )
        contacts = [
            {**public_person(row), 'relationship': 'friend' if row['accepted'] else
             ('sent' if row['requester'] == subject else 'received')}
            for row in related
        ]
        conversations = db.execute(
            '''SELECT m.conversation, p.*, m.last_read,
                      coalesce((SELECT max(id) FROM messages WHERE conversation = m.conversation), 0) AS latest
               FROM members m
               JOIN direct_pairs d USING (conversation)
               JOIN people p ON p.subject = CASE WHEN d.first = ? THEN d.second ELSE d.first END
               WHERE m.subject = ? ORDER BY latest DESC, p.username''',
            (subject, subject),
        )
        return {
            'me': public_person(profile),
            'revision': self.revision(db, subject),
            'contacts': contacts,
            'conversations': [
                {'id': row['conversation'], 'peer': public_person(row), 'unread': row['latest'] > row['last_read']}
                for row in conversations
            ],
        }

    @staticmethod
    def search(db, subject, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 64:
            raise Denied('Search for a username or display name.')
        query = query.strip().casefold()
        rows = db.execute(
            '''SELECT * FROM people WHERE subject <> ? AND (instr(username, ?) OR instr(folded, ?))
               ORDER BY (username = ?) DESC, username LIMIT 25''', (subject, query, query, query),
        )
        return {'people': [public_person(row) for row in rows]}

    def display_name(self, db, subject, name):
        if not isinstance(name, str):
            raise Denied('Invalid display name.')
        name = name.strip()
        if len(name) > 64 or any(not ch.isprintable() for ch in name):
            raise Denied('Use up to 64 printable characters for a display name.')
        if name:
            db.execute(
                '''INSERT INTO profiles VALUES (?, ?, ?)
                   ON CONFLICT(subject) DO UPDATE SET name = excluded.name, folded = excluded.folded''',
                (subject, name, name.casefold()),
            )
        else:
            db.execute('DELETE FROM profiles WHERE subject = ?', (subject,))
        peers = db.execute(
            '''SELECT CASE WHEN first = ? THEN second ELSE first END FROM friendships WHERE first = ? OR second = ?
               UNION SELECT CASE WHEN first = ? THEN second ELSE first END FROM direct_pairs WHERE first = ? OR second = ?''',
            (subject,) * 6,
        ).fetchall()
        self.touch(db, subject, *(row[0] for row in peers))
        return {'ok': True}

    @staticmethod
    def require_peer(db, subject, peer):
        if subject == peer or db.execute('SELECT 1 FROM people WHERE subject = ?', (peer,)).fetchone() is None:
            raise Denied('This Chat account is unavailable.')

    def friendship(self, db, subject, peer, operation):
        self.require_peer(db, subject, peer)
        key = pair(subject, peer)
        row = db.execute('SELECT requester, accepted FROM friendships WHERE first = ? AND second = ?', key).fetchone()
        if operation == 'friend-request':
            if row:
                return {'ok': True}
            for person in key:
                count = db.execute('SELECT count(*) FROM friendships WHERE first = ? OR second = ?', (person, person)).fetchone()[0]
                if count >= MAX_RELATIONSHIPS:
                    raise Denied('This account’s friend list is full.')
            db.execute('INSERT INTO friendships (first, second, requester) VALUES (?, ?, ?)', (*key, subject))
        elif operation == 'friend-accept':
            if row is None or row['requester'] != peer:
                raise Denied('This friend request is unavailable.')
            if row['accepted']:
                return {'ok': True}
            db.execute('UPDATE friendships SET accepted = 1 WHERE first = ? AND second = ?', key)
        else:
            db.execute('DELETE FROM friendships WHERE first = ? AND second = ?', key)
        self.touch(db, *key)
        return {'ok': True}

    @staticmethod
    def require_friend(db, key):
        if not db.execute('SELECT 1 FROM friendships WHERE first = ? AND second = ? AND accepted = 1', key).fetchone():
            raise Denied('You can send direct messages to accepted friends.')

    def direct(self, db, subject, peer):
        self.require_peer(db, subject, peer)
        key = pair(subject, peer)
        self.require_friend(db, key)
        row = db.execute('SELECT conversation FROM direct_pairs WHERE first = ? AND second = ?', key).fetchone()
        if row:
            return {'id': row['conversation']}
        for person in key:
            count = db.execute('SELECT count(*) FROM members WHERE subject = ?', (person,)).fetchone()[0]
            if count >= MAX_CONVERSATIONS:
                raise Denied('This account’s conversation list is full.')
        conversation = str(uuid.uuid4())
        db.execute('INSERT INTO conversations VALUES (?)', (conversation,))
        db.execute('INSERT INTO direct_pairs VALUES (?, ?, ?)', (*key, conversation))
        db.executemany('INSERT INTO members (conversation, subject) VALUES (?, ?)', ((conversation, account) for account in key))
        self.touch(db, *key)
        return {'id': conversation}

    @staticmethod
    def conversation_peer(db, subject, conversation):
        row = db.execute(
            '''SELECT p.subject FROM members m JOIN direct_pairs d USING (conversation)
               JOIN people p ON p.subject = CASE WHEN d.first = ? THEN d.second ELSE d.first END
               WHERE m.conversation = ? AND m.subject = ?''', (subject, conversation, subject),
        ).fetchone()
        if row is None:
            raise Denied('This conversation is unavailable.')
        return row['subject']

    @staticmethod
    def messages(db, conversation, after, before):
        if before and after:
            raise Denied('Choose one message position.')
        if after:
            rows = db.execute(
                '''SELECT id, sender, text, created FROM messages WHERE conversation = ? AND id > ?
                   ORDER BY id LIMIT ?''', (conversation, after, PAGE + 1),
            ).fetchall()
        else:
            rows = db.execute(
                '''SELECT id, sender, text, created FROM messages WHERE conversation = ? AND (? = 0 OR id < ?)
                   ORDER BY id DESC LIMIT ?''', (conversation, before, before, PAGE + 1),
            ).fetchall()
        selected = rows[:PAGE] if after else reversed(rows[:PAGE])
        return {'messages': [dict(row) for row in selected], 'more': len(rows) > PAGE}

    def send(self, db, subject, peer, conversation, client_id, body):
        key = pair(subject, peer)
        self.require_friend(db, key)
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 4000 or '\x00' in body:
            raise Denied('Use 1 to 4000 characters for a message.')
        body = body.strip()
        row = db.execute(
            'SELECT id, text FROM messages WHERE conversation = ? AND sender = ? AND client_id = ?',
            (conversation, subject, client_id),
        ).fetchone()
        if row:
            if row['text'] != body:
                raise Denied('Compose a new message before changing a retry.')
            return {'id': row['id']}
        message = db.execute(
            'INSERT INTO messages (conversation, sender, client_id, text, created) VALUES (?, ?, ?, ?, ?)',
            (conversation, subject, client_id, body, int(self.clock())),
        ).lastrowid
        self.touch(db, *key)
        return {'id': message}

    def mark_read(self, db, subject, conversation, message):
        if not db.execute('SELECT 1 FROM messages WHERE conversation = ? AND id = ?', (conversation, message)).fetchone():
            raise Denied('This message is unavailable.')
        updated = db.execute(
            'UPDATE members SET last_read = ? WHERE conversation = ? AND subject = ? AND last_read < ?',
            (message, conversation, subject, message),
        )
        if updated.rowcount:
            self.touch(db, subject)
        return {'ok': True}
