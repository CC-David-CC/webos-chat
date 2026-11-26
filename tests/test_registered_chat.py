import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
import uuid

from server.chat import ChatStore
from webos_storage.policy import Denied, Principal


class RegisteredChatTest(unittest.TestCase):
    def setUp(self):
        private = TemporaryDirectory()
        self.addCleanup(private.cleanup)
        root = Path(private.name)
        self.accounts_db = root / 'accounts.sqlite3'
        with sqlite3.connect(self.accounts_db) as db:
            db.executescript('''
                CREATE TABLE accounts (username TEXT PRIMARY KEY, subject TEXT UNIQUE);
                CREATE TABLE account_policy (subject TEXT PRIMARY KEY, disabled INTEGER, generation INTEGER);
                CREATE TABLE registered_sessions (record TEXT NOT NULL, deadline INTEGER NOT NULL);
            ''')
        self.chat = ChatStore(root / 'chat-state', self.accounts_db, clock=lambda: 10)
        self.alex = self.register('alex')
        self.casey = self.register('casey')
        self.jordan = self.register('jordan')

    def register(self, username):
        principal = Principal('user:' + str(uuid.uuid4()), str(uuid.uuid4()), 'registered', {}, 100)
        record = {'sid': principal.sid, 'sub': principal.sub, 'name': username,
                  'kind': 'registered', '_account_generation': 0}
        with sqlite3.connect(self.accounts_db) as db:
            db.execute('INSERT INTO accounts VALUES (?, ?)', (username, principal.sub))
            db.execute('INSERT INTO account_policy VALUES (?, 0, 0)', (principal.sub,))
            db.execute('INSERT INTO registered_sessions VALUES (?, 100)', (json.dumps(record),))
        return principal

    def request(self, principal, operation, **fields):
        return self.chat.dispatch(principal, {'resource': 'chat', 'operation': operation, **fields})

    def direct(self):
        self.request(self.alex, 'friend-request', peer=self.casey.sub)
        self.request(self.casey, 'friend-accept', peer=self.alex.sub)
        return self.request(self.alex, 'direct', peer=self.casey.sub)['id']

    def send(self, conversation, body='Hello Casey', client_id=None):
        return self.request(self.alex, 'send', conversation=conversation,
                            clientId=client_id or str(uuid.uuid4()), body=body)['id']

    def test_private_search_and_request_direction(self):
        self.request(self.casey, 'display-name', name='Cáséy Ellis')
        result = self.request(self.alex, 'search', query='ÉY ELLIS')['people']
        self.assertEqual(result, [{'subject': self.casey.sub, 'username': 'casey', 'displayName': 'Cáséy Ellis'}])
        self.assertEqual(self.request(self.alex, 'search', query='alex')['people'], [])
        self.request(self.alex, 'friend-request', peer=self.casey.sub)
        self.assertEqual(self.request(self.casey, 'snapshot')['contacts'][0]['relationship'], 'received')
        with self.assertRaises(Denied):
            self.request(self.alex, 'friend-accept', peer=self.casey.sub)
        with self.assertRaises(Denied):
            self.request(self.alex, 'direct', peer=self.casey.sub)
        self.request(self.casey, 'friend-remove', peer=self.alex.sub)
        self.assertEqual(self.request(self.alex, 'snapshot')['contacts'], [])

    def test_membership_friendship_and_read_cursor(self):
        conversation = self.direct()
        self.assertEqual(self.request(self.casey, 'direct', peer=self.alex.sub)['id'], conversation)
        revision = self.chat.live_revision(self.casey.sub, self.casey.sid)
        message = self.send(conversation)
        self.assertGreater(self.chat.live_revision(self.casey.sub, self.casey.sid), revision)
        self.assertTrue(self.request(self.casey, 'snapshot')['conversations'][0]['unread'])
        for operation, fields in [
            ('messages', {'after': 0, 'before': 0}),
            ('send', {'clientId': str(uuid.uuid4()), 'body': 'unauthorized'}),
            ('read', {'message': message}),
        ]:
            with self.assertRaises(Denied):
                self.request(self.jordan, operation, conversation=conversation, **fields)
        self.request(self.casey, 'read', conversation=conversation, message=message)
        self.assertFalse(self.request(self.casey, 'snapshot')['conversations'][0]['unread'])
        self.assertTrue(self.request(self.alex, 'snapshot')['conversations'][0]['unread'])
        self.request(self.casey, 'friend-remove', peer=self.alex.sub)
        self.assertEqual(self.request(self.casey, 'messages', conversation=conversation,
                                      after=0, before=0)['messages'][0]['text'], 'Hello Casey')
        with self.assertRaises(Denied):
            self.send(conversation)

    def test_history_and_idempotency_survive_reopening_the_database(self):
        conversation = self.direct()
        client_id = str(uuid.uuid4())
        first = self.send(conversation, client_id=client_id)
        self.assertEqual(self.send(conversation, client_id=client_id), first)
        with self.assertRaises(Denied):
            self.send(conversation, 'Different text', client_id=client_id)
        ids = [first] + [self.send(conversation, f'message {i}') for i in range(55)]
        self.chat = ChatStore(self.chat.path.parent, self.accounts_db, clock=lambda: 10)
        page = self.request(self.casey, 'messages', conversation=conversation, after=0, before=0)
        self.assertTrue(page['more'])
        self.assertEqual([m['id'] for m in page['messages']], ids[-50:])
        older = self.request(self.casey, 'messages', conversation=conversation,
                             after=0, before=ids[-50])
        self.assertFalse(older['more'])
        self.assertEqual([m['id'] for m in older['messages']], ids[:-50])
        newer = self.request(self.casey, 'messages', conversation=conversation, after=first, before=0)
        self.assertTrue(newer['more'])
        self.assertEqual([m['id'] for m in newer['messages']], ids[1:51])

    def test_live_state_tracks_related_accounts_leaving_the_directory(self):
        self.send(self.direct())
        revision, peers = self.chat.live_state(self.alex.sub, self.alex.sid)
        self.assertEqual(peers, (self.casey.sub,))
        with sqlite3.connect(self.accounts_db) as db:
            db.execute('UPDATE account_policy SET disabled=1 WHERE subject=?', (self.jordan.sub,))
        self.assertEqual(self.chat.live_state(self.alex.sub, self.alex.sid), (revision, peers))

        with sqlite3.connect(self.accounts_db) as db:
            db.execute('UPDATE account_policy SET disabled=1 WHERE subject=?', (self.casey.sub,))
        self.assertEqual(self.chat.live_state(self.alex.sub, self.alex.sid), (revision, ()))
        with self.assertRaises(Denied):
            self.chat.live_state(self.casey.sub, self.casey.sid)

        with sqlite3.connect(self.accounts_db) as db:
            db.execute('DELETE FROM accounts WHERE subject=?', (self.casey.sub,))
        replacement = self.register('casey')
        self.assertEqual(self.chat.live_state(self.alex.sub, self.alex.sid), (revision, ()))
        self.assertEqual(self.request(replacement, 'snapshot')['conversations'], [])

    def test_revoked_session_and_reused_username(self):
        conversation = self.direct()
        self.send(conversation)
        guest = Principal('anon:' + str(uuid.uuid4()), str(uuid.uuid4()), 'anonymous', {}, 100)
        with self.assertRaises(Denied):
            self.request(guest, 'search', query='casey')
        with sqlite3.connect(self.accounts_db) as db:
            db.execute('UPDATE account_policy SET generation = 1 WHERE subject = ?', (self.casey.sub,))
        with self.assertRaises(Denied):
            self.chat.live_revision(self.casey.sub, self.casey.sid)
        with self.assertRaises(Denied):
            self.request(self.casey, 'snapshot')
        with sqlite3.connect(self.accounts_db) as db:
            db.execute('DELETE FROM account_policy WHERE subject = ?', (self.casey.sub,))
            db.execute('DELETE FROM accounts WHERE subject = ?', (self.casey.sub,))
        replacement = self.register('casey')
        self.assertNotEqual(replacement.sub, self.casey.sub)
        self.assertEqual(self.request(replacement, 'snapshot')['conversations'], [])
        self.assertEqual(self.request(self.alex, 'snapshot')['conversations'], [])
        with self.assertRaises(Denied):
            self.request(replacement, 'messages', conversation=conversation, after=0, before=0)

    def test_retirement_archives_for_the_survivor_and_purges_after_both_expire(self):
        self.request(self.casey, 'display-name', name='Private name')
        conversation = self.direct()
        self.send(conversation, 'From the survivor')
        self.request(self.casey, 'send', conversation=conversation, clientId=str(uuid.uuid4()), body='From the departed')
        self.chat.retire_account(self.casey.sub)
        self.chat.retire_account(self.casey.sub)
        with sqlite3.connect(self.accounts_db) as db:
            db.execute('DELETE FROM accounts WHERE subject=?', (self.casey.sub,))
        archived = self.request(self.alex, 'snapshot')
        self.assertEqual(archived['contacts'], [])
        self.assertTrue(archived['conversations'][0]['archived'])
        self.assertEqual(archived['conversations'][0]['peer']['displayName'], 'Former member')
        history = self.request(self.alex, 'messages', conversation=conversation, after=0, before=0)
        self.assertEqual([message['text'] for message in history['messages']], ['From the survivor', 'From the departed'])
        with self.assertRaisesRegex(Denied, 'read-only'):
            self.send(conversation)
        replacement = self.register('casey')
        self.assertEqual(self.request(replacement, 'snapshot')['conversations'], [])
        with self.assertRaises(Denied):
            self.request(replacement, 'messages', conversation=conversation, after=0, before=0)
        self.chat.retire_account(self.alex.sub)
        with self.chat.connection(directory=False) as db:
            for table in ('conversations', 'members', 'departed_members', 'messages', 'profiles', 'friendships'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
