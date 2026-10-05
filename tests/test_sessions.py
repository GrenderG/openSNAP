"""Session registry behavior tests."""

import unittest

from opensnap.core.sessions import SessionHandoff, SessionRegistry
from opensnap.protocol.models import Endpoint


def _handoff(user_id: int, endpoint: Endpoint, *, serial: int = 1, sequence_number: int = 0) -> SessionHandoff:
    return SessionHandoff(
        serial=serial,
        session_id=0x1000 + user_id,
        user_id=user_id,
        username=f'u{user_id}',
        endpoint=endpoint,
        game_identifier='automodellista',
        sequence_number=sequence_number,
    )


class SessionRegistryTests(unittest.TestCase):
    """Tests for session membership queries."""

    def test_list_lobby_members_filters_by_lobby(self) -> None:
        registry = SessionRegistry()

        session_one = registry.adopt(_handoff(1, Endpoint(host='10.0.0.1', port=1001)))
        session_two = registry.adopt(_handoff(2, Endpoint(host='10.0.0.2', port=1002)))
        session_three = registry.adopt(_handoff(3, Endpoint(host='10.0.0.3', port=1003)))

        registry.set_lobby(session_one.session_id, 7)
        registry.set_lobby(session_two.session_id, 7)
        registry.set_lobby(session_three.session_id, 8)

        members = registry.list_lobby_members(7)
        member_ids = {session.session_id for session in members}
        self.assertEqual(member_ids, {session_one.session_id, session_two.session_id})

    def test_accept_incoming_rejects_duplicate_or_older_sequences(self) -> None:
        registry = SessionRegistry()
        session = registry.adopt(_handoff(7, Endpoint(host='10.0.0.7', port=7007)))

        self.assertTrue(registry.accept_incoming(session.session_id, 1))
        self.assertFalse(registry.accept_incoming(session.session_id, 1))
        self.assertFalse(registry.accept_incoming(session.session_id, 0))
        self.assertTrue(registry.accept_incoming(session.session_id, 2))

    def test_rebind_endpoint_moves_session_lookup_to_new_endpoint(self) -> None:
        registry = SessionRegistry()
        old_endpoint = Endpoint(host='10.0.0.8', port=8008)
        new_endpoint = Endpoint(host='10.0.0.8', port=8010)
        session = registry.adopt(_handoff(7, old_endpoint))

        rebound = registry.rebind_endpoint(session.session_id, new_endpoint)

        self.assertIsNotNone(rebound)
        self.assertIsNone(registry.get_by_endpoint(old_endpoint))
        self.assertEqual(registry.get_by_endpoint(new_endpoint), rebound)

    def test_remove_drops_session_and_endpoint_binding(self) -> None:
        registry = SessionRegistry()
        endpoint = Endpoint(host='10.0.0.9', port=9009)
        session = registry.adopt(_handoff(7, endpoint))

        registry.remove(session.session_id)

        self.assertIsNone(registry.get(session.session_id))
        self.assertIsNone(registry.get_by_endpoint(endpoint))


    def test_adopt_continues_the_handoff_sequence(self) -> None:
        registry = SessionRegistry()
        session = registry.adopt(_handoff(4, Endpoint(host='10.0.0.4', port=2000), sequence_number=2))

        # Unreliable numbering continues after the bootstrap's login success.
        self.assertEqual(registry.allocate_sequence(session.session_id, 0), 3)
        self.assertEqual(registry.allocate_sequence(session.session_id, 0x8000), 0)

    def test_new_login_from_the_same_endpoint_supersedes_the_old_session(self) -> None:
        registry = SessionRegistry()
        endpoint = Endpoint(host='10.0.0.5', port=2000)
        old = registry.adopt(_handoff(5, endpoint))
        registry.set_lobby(old.session_id, 3)
        new = registry.adopt(_handoff(6, endpoint, serial=2))

        self.assertIsNone(registry.get(old.session_id))
        self.assertEqual(registry.get_by_endpoint(endpoint), new)
        self.assertEqual(registry.count_users_in_lobby(3), 0)


if __name__ == '__main__':
    unittest.main()
