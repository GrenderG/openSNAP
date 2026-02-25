"""Contracts for game-specific web routes."""

from dataclasses import dataclass
from typing import Callable, Protocol

from flask import Flask, Response

from opensnap_web.config import WebServerConfig


@dataclass(frozen=True, slots=True)
class WebRouteTools:
    """Shared helpers injected into game web modules."""

    dump_request: Callable[[str], None]
    html_response: Callable[[str], Response]


class GameWebModule(Protocol):
    """Interface implemented by game-specific web modules."""

    name: str

    def register_routes(self, app: Flask, config: WebServerConfig, tools: WebRouteTools) -> None:
        """Register game-specific HTTP routes."""
