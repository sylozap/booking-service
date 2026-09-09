#!/usr/bin/env python
"""Generate an RS256 key pair for ``auth`` and print it where it belongs.

The service signs access tokens with an RSA private key it is given; it never
generates one for itself, because a service that invents a key on startup
issues tokens the rest of the platform cannot verify, and every replica would
invent a different one.

Usage::

    # Look at a new key pair and its kid
    python scripts/gen_keys.py

    # Write the private half to a file for a mounted Secret, print the kid
    python scripts/gen_keys.py --out secrets/auth-signing-key.pem

    # The same, readable by a container that runs as another user
    python scripts/gen_keys.py --out secrets/auth-signing-key.pem --mode 644

    # A single line to paste into .env as JWT_PRIVATE_KEY
    python scripts/gen_keys.py --env

**The output is a secret and nothing here writes it into the repository.**
``--out`` refuses a path inside the working tree that git is watching, and the
default is to print, so the value lands in a terminal the operator controls
rather than in a file someone commits by accident. ``scripts/check-secrets.sh``
is the second line of defence, not the first.

Rotating a key is not this script's job and needs no special support: generate
a new pair, give it to the service, restart. The new public half is registered
alongside the old one, both are served from ``/.well-known/jwks.json``, and the
tokens signed by the outgoing key keep verifying until they expire. The old row
is deactivated once no token can still carry its ``kid`` -- fifteen minutes
after the last pod running it was replaced.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: S404 - runs git to refuse writing a key into the repo
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Importable because the script runs inside the workspace environment, which is
# how the Makefile and the developer both invoke it.
from barber_auth.adapters.rsa_signer import RsaTokenSigner

KEY_SIZE_BITS = 4096
# 65537. The standard public exponent: large enough to be safe against the
# small-exponent attacks that killed 3, small enough to keep verification --
# which every service does on every request -- cheap.
PUBLIC_EXPONENT = 65537


def main() -> int:
    """Generate a pair, then print or store it."""
    arguments = _parse_arguments()

    private_pem = _generate_private_pem()
    signer = RsaTokenSigner(private_pem)

    if arguments.out is not None:
        _write_private_key(arguments.out, private_pem, arguments.mode)
        print(
            f"private key written to {arguments.out} (mode {arguments.mode:04o})",
            file=sys.stderr,
        )
    elif arguments.env:
        # \n escapes rather than real newlines: pydantic-settings reads the
        # value back as one line, and a multi-line value in .env is not
        # portable across the tools that parse it.
        print(f"JWT_PRIVATE_KEY={private_pem.strip().replace(chr(10), chr(92) + 'n')}")
    else:
        print(private_pem, end="")

    print(f"kid={signer.kid}", file=sys.stderr)
    print(signer.public_pem, end="", file=sys.stderr)
    return 0


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the private key to this file instead of stdout",
    )
    destination.add_argument(
        "--env",
        action="store_true",
        help="print the private key as a single JWT_PRIVATE_KEY=... line",
    )
    parser.add_argument(
        "--mode",
        type=_octal_mode,
        default=0o600,
        help="file mode for --out, written the way chmod takes it (default 600)",
    )
    return parser.parse_args()


def _octal_mode(value: str) -> int:
    """Read a file mode the way chmod is given one.

    Octal without needing the ``0o`` prefix, because the number is copied from
    a chmod line far more often than from Python.
    """
    try:
        mode = int(value, 8)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an octal file mode") from None

    if not 0 <= mode <= 0o777:
        raise argparse.ArgumentTypeError(f"{value!r} is outside 0-777")

    return mode


def _generate_private_pem() -> str:
    """A fresh 4096-bit RSA private key, unencrypted PKCS#8 PEM.

    Unencrypted because the thing that protects it is the Secret it is stored
    in and the file mode it is written with; a passphrase would have to live
    next to it in the same environment to be usable at startup, which protects
    nothing and adds a way for the service to fail to start.
    """
    key = rsa.generate_private_key(public_exponent=PUBLIC_EXPONENT, key_size=KEY_SIZE_BITS)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _write_private_key(path: Path, private_pem: str, mode: int = 0o600) -> None:
    """Store the key, refusing a location git would pick up."""
    if _is_tracked_or_untracked_in_git(path):
        raise SystemExit(
            f"{path} is inside the working tree and not ignored by git; "
            "write the key outside the repository or add the path to .gitignore"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    # Created at 0600 and widened only afterwards, but still before a single
    # byte of the key exists on disk: a chmod after the write would leave a
    # window in which the key is readable by everyone, and that window is all
    # an attacker needs. The widening cannot be folded into the open() flags
    # instead, because the umask of the caller silently strips bits from the
    # mode given there -- a developer running with umask 077 would get a 0600
    # key back and no indication that the mode they asked for was ignored.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        if mode != 0o600:
            os.fchmod(handle.fileno(), mode)
        handle.write(private_pem)


def _is_tracked_or_untracked_in_git(path: Path) -> bool:
    """Whether git would show this path in ``git status``.

    ``check-ignore`` answers yes for a path git is told to leave alone, which
    is the only case where writing a key inside the tree is acceptable. A path
    outside the repository makes git fail, and that is also fine.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, path is not a shell string
            ["git", "check-ignore", "--quiet", str(path)],  # noqa: S607
            check=False,
            capture_output=True,
        )
    except OSError:
        # No git on this machine: nothing to protect the key from either.
        return False

    inside_repository = _is_inside_repository(path)
    ignored = result.returncode == 0
    return inside_repository and not ignored


def _is_inside_repository(path: Path) -> bool:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False

    if result.returncode != 0:
        return False

    root = Path(result.stdout.strip()).resolve()
    return root in path.resolve().parents


if __name__ == "__main__":
    raise SystemExit(main())
