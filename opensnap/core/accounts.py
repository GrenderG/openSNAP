"""Account directory services."""

from dataclasses import dataclass

from opensnap.config import UserConfig


@dataclass(slots=True)
class Account:
    """User account state."""

    user_id: int
    username: str
    # TODO: Move away from cleartext password storage while preserving login compatibility.
    password: str
    # TODO: Consider per-account random seeds instead of one static default seed.
    seed: str
    team: str


class AccountDirectory:
    """In-memory account lookup service."""

    def __init__(self, users: tuple[UserConfig, ...]) -> None:
        self._by_name: dict[str, Account] = {}
        self._by_id: dict[int, Account] = {}
        for user in users:
            account = Account(
                user_id=user.user_id,
                username=user.username,
                password=user.password,
                seed=user.seed,
                team=user.team,
            )
            self._by_name[account.username] = account
            self._by_id[account.user_id] = account

    def get_by_name(self, username: str) -> Account | None:
        """Get account by username."""

        return self._by_name.get(username)

    def get_by_id(self, user_id: int) -> Account | None:
        """Get account by user id."""

        return self._by_id.get(user_id)

    def set_team(self, user_id: int, team: str) -> None:
        """Set account team string."""

        account = self._by_id.get(user_id)
        if account is not None:
            account.team = team
