from __future__ import annotations

import sys

USAGE = """usage: python -m loginproof <command>
  serve                 callback server on 127.0.0.1:8765 (open / to see the login links)
  directory [PROVIDER]  snapshot directory users and groups (GOOGLE, ENTRA, OKTA; default all configured)
  device [--recheck]    CLI device login, then the deactivation re-check
  join                  print the per-provider join verdict
  report                write RESULTS.md
"""


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        sys.exit(USAGE)
    cmd, rest = argv[0], argv[1:]
    if cmd == "serve":
        from loginproof.server import main as run

        run()
    elif cmd == "directory":
        from loginproof.directory import main as run

        run(rest or None)
    elif cmd == "device":
        from loginproof.device import main as run

        run(rest)
    elif cmd == "join":
        from loginproof.join import main as run

        run()
    elif cmd == "report":
        from loginproof.report import main as run

        run()
    else:
        sys.exit(USAGE)


main()
