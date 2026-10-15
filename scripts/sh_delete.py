#!/usr/bin/env python3
"""Delete files and directories on Windows, with or without the recycle bin.

Motivation: Windows ships no ``trash`` command.  ``gio trash`` and ``trash-put``
are absent.  On hardened machines ``Add-Type`` may be unavailable, which rules
out the .NET and VisualBasic file APIs.  What remains is ``ctypes`` calling
``shell32.SHFileOperationW`` directly - which is what this script does.

It is cautious by design:

* **Dry run is the default.**  Nothing is deleted until you pass ``--execute``.
* Recycle bin entries are only ever touched if they are provably ``$I``/``$R``
  pairs inside the current user's bin, and they match a prefix you named.
* Deletion is batched and verified afterwards with ``os.path.exists``, because
  the return value of ``SHFileOperationW`` is not trustworthy on its own.

Usage
-----
    # Look before you leap
    python sh_delete.py delete "C:\\build\\cache" --mode recycle
    python sh_delete.py delete "C:\\build\\cache" --mode recycle --execute

    # A manifest is one path per line, '#' starts a comment
    python sh_delete.py delete --manifest cleanup.txt --mode permanent --execute

    # Inspect the recycle bin for entries created by deleting a directory
    python sh_delete.py scan --prefix "C:\\build\\cache"

    # Reclaim the space (this is the part people forget)
    python sh_delete.py purge --prefix "C:\\build\\cache" --execute
"""

import argparse
import ctypes
import os
import struct
import sys
import time
from ctypes import wintypes

# ----------------------------------------------------------------------
# Win32 plumbing
# ----------------------------------------------------------------------

FO_DELETE = 3

FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040  # present -> recycle bin, absent -> permanent
FOF_NOERRORUI = 0x0400
FOF_NOCONFIRMMKDIR = 0x0200


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def sh_delete(paths, to_recycle=False):
    """Delete ``paths``.  Returns ``(return_code, aborted)``.

    ``pFrom`` must be a double-null-terminated list of absolute paths with
    backslash separators.

    That list is built as a real wide-character buffer rather than a Python
    string, and not merely for tidiness: assigning a string containing ``"\\0"``
    to a ``c_wchar_p`` field **raises ``ValueError: embedded null character`` on
    CPython 3.9 and earlier**, while 3.12+ accepts it silently.  Writing the
    obvious one-liner therefore produces code that works on the author's
    interpreter and fails on older ones.  The buffer form behaves identically on
    every version.

    ``buffer`` must stay referenced until ``SHFileOperationW`` returns, so it is
    bound to a local name rather than constructed inline.
    """
    if not paths:
        return 0, False

    buffer = ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")

    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(buffer, wintypes.LPCWSTR)
    operation.pTo = None
    operation.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR
    if to_recycle:
        operation.fFlags |= FOF_ALLOWUNDO

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    return result, bool(operation.fAnyOperationsAborted)


# ----------------------------------------------------------------------
# recycle bin inspection
# ----------------------------------------------------------------------

# $I metadata layout (format version 2), all little-endian:
#   [0:8]   header, 0x02
#   [8:16]  original size, QWORD
#   [16:24] deletion time, FILETIME
#   [24:28] path length in wide characters (includes the trailing null)
#   [28:]   original full path, UTF-16LE, null-terminated
_METADATA_HEADER = struct.Struct("<QqQ")


def recycle_bin_path():
    """The current user's recycle bin directory, or None."""
    sid = _current_sid()
    if not sid:
        return None
    path = os.path.join("C:\\$Recycle.Bin", sid)
    return path if os.path.isdir(path) else None


def _current_sid():
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32

    # Every one of these needs an explicit restype: ctypes defaults to c_int,
    # which truncates the 64-bit values on x64 and fails silently.
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)
    ]
    advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)
    ]

    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None
    try:
        needed = ctypes.c_uint32(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
            return None
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_pointer), ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def _canonical(path):
    """A normalised, long-name, case-folded form for reliable path comparison.

    This exists because of a real trap.  Windows writes the **long** form of a
    path into the recycle bin metadata even when the delete was issued against
    an 8.3 short name.  Schematically:

        delete issued for : C:\\Users\\LONGPATH~1.USR\\AppData\\Local\\Temp\\tmpXXXX\\victim
        stored in $I      : C:\\Users\\LongUserName.USERHOST\\AppData\\Local\\Temp\\tmpXXXX\\victim

    Same directory, different strings.  Prefix matching on the raw values
    therefore rejects every entry.  The visible symptom is that ``scan``
    reports nothing, so ``purge`` becomes a quiet no-op and the disk space is
    never reclaimed - the exact failure this whole module exists to prevent.

    ``os.path.realpath`` resolves the short name, and keeps working even when
    the tail of the path no longer exists, which is precisely our situation:
    the target has already been deleted.
    """
    expanded = os.path.expandvars(str(path))
    return os.path.realpath(os.path.normpath(expanded)).lower()


def read_metadata(path):
    """Parse an ``$I`` file into ``(original_size, deleted_at, original_path)``."""
    with open(path, "rb") as handle:
        data = handle.read(28 + 65536)
    if len(data) < 28:
        return None

    _version, size, filetime = _METADATA_HEADER.unpack(data[0:24])
    (char_count,) = struct.unpack("<I", data[24:28])
    raw = data[28 : 28 + char_count * 2]
    original = raw.decode("utf-16-le", "ignore").rstrip("\x00")

    # FILETIME is 100-nanosecond ticks since 1601-01-01.
    seconds = filetime / 10_000_000 - 11644473600
    try:
        deleted_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
    except (OSError, ValueError, OverflowError):
        deleted_at = "?"

    return size, deleted_at, original


def scan_recycle_bin(prefix=None):
    """List recycle bin entries, optionally filtered by original path prefix.

    Both sides of the comparison go through :func:`_canonical`, so 8.3 short
    names and letter case cannot cause a silent miss.  Matching then covers both
    ``...\\dir`` and ``...\\dir\\`` forms: filtering on only one of them drops
    either the directory itself or its contents, which is how a cleanup ends up
    half-finished.
    """
    bin_path = recycle_bin_path()
    if not bin_path:
        return []

    patterns = None
    if prefix:
        normalised = _canonical(prefix).rstrip("\\/")
        patterns = (normalised, normalised + os.sep)

    entries = []
    for name in os.listdir(bin_path):
        if not name.startswith("$I"):
            continue
        metadata_path = os.path.join(bin_path, name)
        try:
            parsed = read_metadata(metadata_path)
        except OSError:
            continue
        if not parsed:
            continue
        size, deleted_at, original = parsed

        if patterns and not _canonical(original).startswith(patterns):
            continue

        identifier = name[2:].split(".")[0]
        entries.append(
            {
                "id": identifier,
                "metadata": metadata_path,
                "payload": _find_payload(bin_path, identifier),
                "size": size,
                "deletedAt": deleted_at,
                "originalPath": original,
            }
        )
    return entries


def _find_payload(bin_path, identifier):
    """Locate the ``$R`` file or directory that pairs with an ``$I`` entry."""
    direct = os.path.join(bin_path, "$R" + identifier)
    if os.path.exists(direct):
        return direct
    # The payload may carry an extension, e.g. $RABC123.txt
    for name in os.listdir(bin_path):
        if name.startswith("$R" + identifier):
            return os.path.join(bin_path, name)
    return None


def _is_inside_recycle_bin(path):
    """Guard against ever deleting anything that is not a bin entry.

    Compared in canonical form for the same reason as :func:`scan_recycle_bin`:
    the bin path itself can carry a short name or unexpected letter case.
    """
    bin_path = recycle_bin_path()
    if not bin_path or not path:
        return False
    return _canonical(path).startswith(_canonical(bin_path) + os.sep)


def _is_bin_entry(path):
    name = os.path.basename(path)
    return (name.startswith("$I") or name.startswith("$R")) and _is_inside_recycle_bin(path)


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------


def _read_manifest(path):
    """One path per line; blank lines and ``#`` comments are ignored."""
    paths = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            paths.append(os.path.abspath(line))
    return paths


def cmd_delete(args):
    targets = []

    if args.manifest:
        targets.extend(_read_manifest(args.manifest))
    for item in args.paths:
        targets.append(os.path.abspath(item))

    if not targets:
        print("error: nothing to delete", file=sys.stderr)
        return 1

    missing = [t for t in targets if not os.path.exists(t)]
    targets = [t for t in targets if os.path.exists(t)]

    to_recycle = args.mode == "recycle"

    print("mode:        %s" % args.mode)
    print("targets:     %d" % len(targets))
    if missing:
        print("missing:     %d (skipped)" % len(missing))
        for item in missing[:5]:
            print("  - %s" % item)
    for item in targets:
        print("  - %s" % item)

    if not args.execute:
        print()
        print("DRY RUN - nothing was deleted.  Re-run with --execute to proceed.")
        return 0

    print()
    print("deleting in batches of %d..." % args.batch)
    failures = []
    for start in range(0, len(targets), args.batch):
        batch = targets[start : start + args.batch]
        code, aborted = sh_delete(batch, to_recycle=to_recycle)
        if aborted:
            failures.append("batch %d reported aborted" % (start // args.batch + 1))

        # The return code is unreliable (0, 120 and 124 can all mean success),
        # so the only trustworthy check is whether the path is still there.
        for item in batch:
            if os.path.exists(item):
                failures.append("still present after delete: %s" % item)
        print("  batch %d/%d: %d items"
              % (start // args.batch + 1, (len(targets) + args.batch - 1) // args.batch, len(batch)),
              flush=True)

    print()
    if failures:
        print("FAILED (%d issues)" % len(failures))
        for item in failures:
            print("  - %s" % item)
        return 1

    print("deleted %d item(s)" % len(targets))
    if to_recycle:
        print()
        print("NOTE: directories may land in the recycle bin even when the caller")
        print("      did not ask for it.  Run the scan subcommand to check:")
        print("        python %s scan --prefix \"%s\"" % (os.path.basename(__file__), targets[0]))
    return 0


def cmd_scan(args):
    entries = scan_recycle_bin(args.prefix)
    if not entries:
        print("no matching recycle bin entries")
        return 0

    total = sum(entry["size"] for entry in entries)
    print("%d matching entries, %.1f MB total" % (len(entries), total / 1024 / 1024))
    for entry in entries[: args.limit]:
        print("  %s  %s  %.2f MB" % (entry["id"], entry["deletedAt"], entry["size"] / 1024 / 1024))
        print("      %s" % entry["originalPath"])
    if len(entries) > args.limit:
        print("  ... and %d more" % (len(entries) - args.limit))
    return 0


def cmd_purge(args):
    entries = scan_recycle_bin(args.prefix)
    if not entries:
        print("no matching recycle bin entries")
        return 0

    total = sum(entry["size"] for entry in entries)
    print("would reclaim: %.1f MB across %d entries" % (total / 1024 / 1024, len(entries)))

    payloads = [entry["payload"] for entry in entries if entry["payload"]]
    metadata = [entry["metadata"] for entry in entries]

    # Safety net: refuse to proceed if anything is not a genuine bin entry.
    unsafe = [p for p in payloads + metadata if not _is_bin_entry(p)]
    if unsafe:
        print("REFUSING TO PROCEED - these are not recycle bin entries:", file=sys.stderr)
        for item in unsafe[:10]:
            print("  %s" % item, file=sys.stderr)
        return 1

    if not args.execute:
        print()
        print("DRY RUN - nothing was purged.  Re-run with --execute to proceed.")
        return 0

    # Delete metadata first so a failure leaves recoverable data rather than
    # orphaned payloads nobody can identify.
    failures = []
    for start in range(0, len(metadata), args.batch):
        batch = metadata[start : start + args.batch]
        sh_delete(batch, to_recycle=False)
        for item in batch:
            if os.path.exists(item):
                failures.append("still present: %s" % item)

    for start in range(0, len(payloads), args.batch):
        batch = payloads[start : start + args.batch]
        sh_delete(batch, to_recycle=False)
        for item in batch:
            if os.path.exists(item):
                failures.append("still present: %s" % item)
        print("  purged %d/%d" % (min(start + args.batch, len(payloads)), len(payloads)), flush=True)

    print()
    if failures:
        print("FAILED (%d issues)" % len(failures))
        for item in failures[:20]:
            print("  - %s" % item)
        return 1

    print("purged %d entries, ~%.1f MB reclaimed" % (len(entries), total / 1024 / 1024))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Delete files and directories on Windows, with recycle bin support."
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--batch", type=int, default=50, help="items per operation (default 50)")
    shared.add_argument("--execute", action="store_true", help="actually do it; default is a dry run")

    p = sub.add_parser("delete", parents=[shared], help="delete files or directories")
    p.add_argument("paths", nargs="*", help="paths to delete")
    p.add_argument("--manifest", help="file listing paths, one per line")
    p.add_argument("--mode", choices=("recycle", "permanent"), default="recycle")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("scan", help="list recycle bin entries matching a prefix")
    p.add_argument("--prefix", required=True, help="original path prefix to match")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("purge", parents=[shared], help="permanently remove matching bin entries")
    p.add_argument("--prefix", required=True, help="original path prefix to match")
    p.set_defaults(func=cmd_purge)

    return parser


def main(argv=None):
    if os.name != "nt":
        print("error: this script is Windows-only", file=sys.stderr)
        return 1

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
