"""Resident Evil Outbreak APP service."""

from opensnap_app.capcom.outbreak.service import (
    APP_BUILD,
    DATA_SUBDIRECTORY,
    PUBLIC_BETA_DATA_SUBDIRECTORY,
    PUBLIC_BETA_VERSION,
    AppService,
    read_app_service_config,
)

__all__ = [
    'APP_BUILD',
    'DATA_SUBDIRECTORY',
    'PUBLIC_BETA_DATA_SUBDIRECTORY',
    'PUBLIC_BETA_VERSION',
    'AppService',
    'read_app_service_config',
]
