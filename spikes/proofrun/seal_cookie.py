"""The T2/T3 fallback: seal a session cookie with the cell's own keyring when there is no real
login yet (SSC-064 not done). Every result taken with such a cookie says the real login was not
exercised.

Run at the repository root, so ``ssc_edge`` is importable:

    gcloud kms decrypt ... --plaintext-file=- \\
      | uv run python spikes/proofrun/seal_cookie.py --host <slug>.<label>.<apps domain> \\
          --org <org_...> --sub <usr_...> --keyring -

The keyring is read from a file or stdin, used in memory and never written; the cookie goes
straight into the kit's cookie jar (mode 600) with source ``sealed`` and is never printed. The
person must hold a grant on the app, and ``--org`` must be the cell's ``org_id``.
"""

import argparse
import sys
import time
from pathlib import Path

from ssc_edge.keys import parse_keyring
from ssc_edge.session import MAX_SESSION_SECONDS, Session, SessionCodec, new_sid

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proofrun.common import CookieJar, fence  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True, help="the app's public host")
    parser.add_argument("--org", required=True, help="the cell's org_id")
    parser.add_argument("--sub", required=True, help="a usr_ id granted on the app")
    parser.add_argument("--keyring", required=True, help="the plain keyring JSON file, or -")
    parser.add_argument("--hours", type=float, default=MAX_SESSION_SECONDS / 3600)
    parser.add_argument("--name", default="SSC proof run")
    parser.add_argument("--email", default="")
    args = parser.parse_args(argv)
    fence(*argv)
    raw = sys.stdin.buffer.read() if args.keyring == "-" else Path(args.keyring).read_bytes()
    keys = parse_keyring(raw)
    now = int(time.time())
    session = Session(
        sid=new_sid(),
        sub=args.sub,
        org=args.org,
        name=args.name,
        email=args.email,
        iat=now,
        exp=now + int(args.hours * 3600),
    )
    host = args.host.lower()
    value = SessionCodec(keys.session, active=keys.session_kid).seal(session, host)
    CookieJar().put(host, value, "sealed")
    print(f"sealed a session for {host}, valid {args.hours:.1f} h, saved to the cookie jar")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
