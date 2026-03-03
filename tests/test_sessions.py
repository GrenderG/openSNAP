"""Session registry behavior tests."""

import unittest

from opensnap.core.accounts import build_account
from opensnap.core.sessions import SessionRegistry
from opensnap.protocol.models import Endpoint


class SessionRegistryTests(unittest.TestCase):
    """Tests for session membership queries."""

    def test_list_lobby_members_filters_by_lobby(self) -> None:
        registry = SessionRegistry()
        user_one = build_account(user_id=1, username='u1', password_record='p', seed='s', team='')
        user_two = build_account(user_id=2, username='u2', password_record='p', seed='s', team='')
        user_three = build_account(user_id=3, username='u3', password_record='p', seed='s', team='')

        session_one = registry.create_or_replace(Endpoint(host='10.0.0.1', port=1001), user_one)
        session_two = registry.create_or_replace(Endpoint(host='10.0.0.2', port=1002), user_two)
        session_three = registry.create_or_replace(Endpoint(host='10.0.0.3', port=1003), user_three)

        registry.set_lobby(session_one.session_id, 7)
        registry.set_lobby(session_two.session_id, 7)
        registry.set_lobby(session_three.session_id, 8)

        members = registry.list_lobby_members(7)
        member_ids = {session.session_id for session in members}
        self.assertEqual(member_ids, {session_one.session_id, session_two.session_id})

    def test_accept_incoming_rejects_duplicate_or_older_sequences(self) -> None:
        registry = SessionRegistry()
        user = build_account(user_id=7, username='u7', password_record='p', seed='s', team='')
        session = registry.create_or_replace(Endpoint(host='10.0.0.7', port=7007), user)

        self.assertTrue(registry.accept_incoming(session.session_id, 1))
        self.assertFalse(registry.accept_incoming(session.session_id, 1))
        self.assertFalse(registry.accept_incoming(session.session_id, 0))
        self.assertTrue(registry.accept_incoming(session.session_id, 2))

    def test_rebind_endpoint_moves_session_lookup_to_new_endpoint(self) -> None:
        registry = SessionRegistry()
        user = build_account(user_id=8, username='u8', password_record='p', seed='s', team='')
        old_endpoint = Endpoint(host='10.0.0.8', port=8008)
        new_endpoint = Endpoint(host='10.0.0.8', port=8010)
        session = registry.create_or_replace(old_endpoint, user)

        rebound = registry.rebind_endpoint(session.session_id, new_endpoint)

        self.assertIsNotNone(rebound)
        self.assertIsNone(registry.get_by_endpoint(old_endpoint))
        self.assertEqual(registry.get_by_endpoint(new_endpoint), rebound)


if __name__ == '__main__':
    unittest.main()
