"""Game announcements posted to a webhook (Discord or any JSON consumer)."""

from opensnap.webhooks.base import GameWebhooks
from opensnap.webhooks.dispatcher import DayChangeTimer, WebhookDispatcher, WebhookEvent

__all__ = ['DayChangeTimer', 'GameWebhooks', 'WebhookDispatcher', 'WebhookEvent']
