"""Helpers shared by every web module."""

from collections.abc import Callable, Iterable
import logging
import re

from flask import Blueprint, Response, request

LOGGER = logging.getLogger('opensnap.web')


def html_response(content: str) -> Response:
    """Return an HTML page."""

    return Response(content, mimetype='text/html')


def text_response(content: str = '') -> Response:
    """Return a plain-text body."""

    return Response(content, mimetype='text/plain')


def add_routes(
    blueprint: Blueprint,
    paths: Iterable[str],
    view: Callable[..., Response],
    *,
    methods: tuple[str, ...] = ('GET',),
) -> None:
    """Serve `view` on every path; endpoints are named after their paths."""

    for path in paths:
        endpoint = re.sub(r'\W', '_', path.strip('/')) or 'root'
        blueprint.add_url_rule(path, endpoint=endpoint, view_func=view, methods=list(methods))


def page_view(page: str) -> Callable[..., Response]:
    """Return a view that serves one fixed HTML page."""

    return lambda **_kwargs: html_response(page)


def dump_request(title: str) -> None:
    """Log the full request; client web traffic is still being reverse engineered."""

    # Keep the cached body so Flask can still populate `request.form`.
    raw_body = request.get_data(cache=True)
    LOGGER.info('%s', title)
    LOGGER.info('  method: %s', request.method)
    LOGGER.info('  path: %s', request.path)
    LOGGER.info('  full_path: %s', request.full_path)
    LOGGER.info('  url: %s', request.url)
    LOGGER.info('  remote_addr: %s', request.remote_addr)
    LOGGER.info('  host: %s', request.host)
    LOGGER.info('  query: %s', {key: request.args.getlist(key) for key in request.args})
    LOGGER.info('  form: %s', {key: request.form.getlist(key) for key in request.form})
    LOGGER.info('  headers: %s', dict(request.headers.items()))
    LOGGER.info('  body_len: %d', len(raw_body))
    LOGGER.info('  body_preview: %r', raw_body.decode('utf-8', errors='replace'))
