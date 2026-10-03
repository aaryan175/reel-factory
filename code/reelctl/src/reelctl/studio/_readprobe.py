"""The read test, as a program with no reelctl imports. Run by path, never as a package.

``preflight.probe_input`` runs this in a child interpreter whenever the path it is testing
could *block* rather than fail — typically any path on an external volume. The
child exists for exactly one reason: a read stuck in ``openat`` waiting for a TCC consent
prompt cannot be interrupted from inside the process that issued it, so a thread would leak
forever where a process can simply be killed. Observed in practice: a daemon interpreter in
launchd context blocked on a footage root on an external volume rather than failing.

It imports only ``json``, ``os`` and ``sys`` so the child costs a bare interpreter start,
and it is spawned with ``-I -S`` so it does not process site packages to get there.

Protocol: exit 0 means readable; exit 3 means the filesystem refused, with
``{"errno": <int>, "error": "<message>"}`` on stdout; any other exit is the probe itself
failing and is reported as such rather than as a verdict about the path.
"""

import json
import os
import sys

REFUSED = 3


def read_test(path):
    """Touch the bytes. Raises whatever the filesystem raises; returns nothing on success.

    Directories are listed and files are read one byte, because content is what macOS
    actually gates — the wedged volume answered ``stat`` on the same path in 0.00s while
    denying the listing, so a metadata check would have cleared every observed hang.
    Nothing here writes, creates or resolves anything.
    """
    if os.path.isdir(path):
        with os.scandir(path) as entries:
            for _ in entries:
                break
        return
    with open(path, "rb") as handle:
        handle.read(1)


def main(argv):
    if len(argv) != 2:  # pragma: no cover - the caller always passes exactly one path
        sys.stderr.write("usage: _readprobe.py <path>\n")
        return 2
    try:
        read_test(argv[1])
    except OSError as exc:
        sys.stdout.write(json.dumps({"errno": exc.errno, "error": str(exc)}))
        return REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
