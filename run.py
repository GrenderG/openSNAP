"""Repository-level launcher for openSNAP services."""

import argparse

from opensnap.env_loader import load_env_file


def main() -> None:
    """Dispatch to bootstrap, game, web, or DNS service launcher."""

    load_env_file()

    parser = argparse.ArgumentParser(description='openSNAP service launcher.')
    parser.add_argument(
        'service',
        nargs='?',
        choices=('bootstrap', 'game', 'web', 'dns'),
        default='game',
        help='Service to launch: game (default), bootstrap, web, or dns.',
    )
    args = parser.parse_args()

    if args.service == 'web':
        try:
            from opensnap_web.server import main as run_web_server
        except ModuleNotFoundError as exc:
            if exc.name == 'flask':
                raise SystemExit(
                    'Flask is not installed. Run `pip install -r requirements.txt` first.'
                ) from exc
            raise

        run_web_server()
        return

    if args.service == 'bootstrap':
        from opensnap.bootstrap_server import main as run_bootstrap_server

        run_bootstrap_server()
        return

    if args.service == 'dns':
        try:
            from opensnap_dns.server import main as run_dns_server
        except ModuleNotFoundError as exc:
            if exc.name == 'dnslib':
                raise SystemExit(
                    'dnslib is not installed. Run `pip install -r requirements.txt` first.'
                ) from exc
            raise

        run_dns_server()
        return

    from opensnap.game_server import main as run_game_server

    run_game_server()


if __name__ == '__main__':
    main()
