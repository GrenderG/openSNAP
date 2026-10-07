"""Repository-level launcher for openSNAP services."""

import argparse

from opensnap.env_loader import load_env_file


def main() -> None:
    """Dispatch to bootstrap, game, web, DNS, or companion app service launcher."""

    load_env_file()

    parser = argparse.ArgumentParser(description='openSNAP service launcher.')
    parser.add_argument(
        'service',
        nargs='?',
        choices=('bootstrap', 'game', 'web', 'dns', 'app'),
        default='game',
        help='Service to launch: game (default), bootstrap, web, dns, or app.',
    )
    parser.add_argument(
        'app',
        nargs='?',
        help='Companion app to launch when service=app (for example: capcom).',
    )
    parser.add_argument(
        '--web-plugin',
        dest='web_plugin',
        default=None,
        help=(
            'Optional web profile override when service=web '
            '(for example: generic, automodellista, automodellista_beta1).'
        ),
    )
    args = parser.parse_args()
    if args.app is not None and args.service != 'app':
        parser.error(f'unexpected argument {args.app!r} for service {args.service}.')

    if args.service == 'web':
        try:
            from opensnap_web.server import main as run_web_server
        except ModuleNotFoundError as exc:
            if exc.name == 'flask':
                raise SystemExit(
                    'Flask is not installed. Run `pip install -r requirements.txt` first.'
                ) from exc
            raise

        run_web_server(web_plugin=args.web_plugin)
        return

    if args.service == 'app':
        from opensnap_app import APPS, run_app

        if args.app is None:
            parser.error(f'service app needs an app name: {", ".join(APPS)}.')
        run_app(args.app)
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
