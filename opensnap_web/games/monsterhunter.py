"""Monster Hunter web routes."""

from flask import Flask

from opensnap_web.config import WebServerConfig
from opensnap_web.games.automodellista import register_signup_routes
from opensnap_web.games.base import WebRouteTools
from opensnap_web.signup import SqliteSignupService


class MonsterHunterWebModule:
    """Web endpoints used by Monster Hunter clients."""

    name = 'monsterhunter'

    def register_routes(self, app: Flask, config: WebServerConfig, tools: WebRouteTools) -> None:
        """Register Monster Hunter-specific web paths with shared legacy signup logic."""

        del config
        signup_service = SqliteSignupService()
        register_signup_routes(
            app,
            tools=tools,
            signup_service=signup_service,
            route_prefixes=('mhweb', 'mheuweb', 'reweb'),
            include_root_aliases=False,
        )
