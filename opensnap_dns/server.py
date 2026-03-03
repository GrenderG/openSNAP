"""Standalone UDP DNS server for openSNAP."""

from __future__ import annotations

import fnmatch
import logging
import socket

from dnslib import A, DNSRecord, QTYPE, RCODE, RR

from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging
from opensnap_dns.config import DnsServerConfig, default_dns_server_config


class SnapDnsServer:
    """Blocking UDP DNS server with static-record responses."""

    def __init__(self, *, config: DnsServerConfig) -> None:
        self._config = config
        self._logger = logging.getLogger('opensnap.dns')
        self._stopped = False
        self._exact_entries: dict[str, str] = {}
        self._wildcard_entries: list[tuple[str, str]] = []
        for raw_domain, raw_target in config.entries.items():
            domain = raw_domain.strip().rstrip('.').lower()
            if not domain:
                continue
            if '*' in domain:
                self._wildcard_entries.append((domain, raw_target))
                continue
            self._exact_entries[domain] = raw_target

    def run(self) -> None:
        """Run DNS loop until stopped."""

        try:
            dns_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as exc:
            self._logger.error('Failed to create DNS UDP socket: %s', exc)
            raise

        with dns_socket:
            self._enable_reuse_address(dns_socket)
            try:
                dns_socket.bind((self._config.host, self._config.port))
            except OSError as exc:
                self._logger.error(
                    'Failed to bind DNS UDP socket on %s:%d: %s',
                    self._config.host,
                    self._config.port,
                    exc,
                )
                raise

            self._logger.info(
                'DNS socket bound at %s:%d.',
                self._config.host,
                self._config.port,
            )
            while not self._stopped:
                dns_socket.settimeout(0.5)
                try:
                    payload, (host, port) = dns_socket.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break

                self._logger.debug(
                    'Received DNS datagram from %s:%d (%d byte(s)).',
                    host,
                    port,
                    len(payload),
                )
                response = self._build_response(payload)
                if response is None:
                    continue
                dns_socket.sendto(response, (host, port))

    def stop(self) -> None:
        """Request graceful DNS-loop stop."""

        self._stopped = True

    def _enable_reuse_address(self, dns_socket: socket.socket) -> None:
        """Enable address reuse to reduce restart/bind failures across platforms."""

        try:
            dns_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError as exc:
            self._logger.warning('Failed to enable SO_REUSEADDR on DNS socket: %s', exc)

    def _build_response(self, payload: bytes) -> bytes | None:
        """Build one DNS response datagram for a request payload."""

        try:
            request = DNSRecord.parse(payload)
        except Exception as exc:  # pragma: no cover - dnslib parser exception types vary.
            self._logger.warning('Ignoring malformed DNS datagram: %s', exc)
            return None

        if not request.questions:
            reply = request.reply()
            reply.header.rcode = RCODE.FORMERR
            return reply.pack()

        reply = request.reply()
        reply.header.aa = 1

        has_answer = False
        for question in request.questions:
            domain = str(question.qname).strip().rstrip('.').lower()
            self._logger.info('Received DNS request for: %s', domain)
            if question.qtype not in (QTYPE.A, QTYPE.ANY):
                continue

            answer_ips = self._resolve_answer_ips(domain)
            for answer_ip in answer_ips:
                reply.add_answer(
                    RR(
                        rname=question.qname,
                        rtype=QTYPE.A,
                        rclass=1,
                        ttl=self._config.ttl,
                        rdata=A(answer_ip),
                    )
                )
                has_answer = True

        if has_answer:
            reply.header.rcode = RCODE.NOERROR
        else:
            reply.header.rcode = RCODE.NXDOMAIN

        return reply.pack()

    def _resolve_answer_ips(self, domain: str) -> list[str]:
        """Resolve A-record answer list from static entries or system resolver."""

        static_ip = self._resolve_static_ip(domain)
        if static_ip is not None:
            return [static_ip]
        return self._resolve_system_ips(domain)

    def _resolve_static_ip(self, domain: str) -> str | None:
        """Resolve one A-record from static exact/wildcard entries."""

        direct = self._exact_entries.get(domain)
        if direct is not None:
            return direct

        for pattern, target in self._wildcard_entries:
            if fnmatch.fnmatchcase(domain, pattern):
                return target
        return None

    def _resolve_system_ips(self, domain: str) -> list[str]:
        """Resolve domain through host system DNS resolver."""

        try:
            infos = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_DGRAM)
        except socket.gaierror:
            return []

        resolved: list[str] = []
        for info in infos:
            ip = info[4][0]
            if ip not in resolved:
                resolved.append(ip)
        if resolved:
            self._logger.debug(
                'Resolved %s via system DNS resolver: %s.',
                domain,
                ', '.join(resolved),
            )
        return resolved


def main() -> None:
    """CLI entrypoint for DNS service."""

    load_env_file()
    configure_logging(service_name='dns')
    logger = logging.getLogger('opensnap.dns')
    config = default_dns_server_config()
    server = SnapDnsServer(config=config)
    logger.info(
        'Starting openSNAP DNS on %s:%d with %d static entries.',
        config.host,
        config.port,
        len(config.entries),
    )
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, shutting down DNS service.')
    except OSError:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
