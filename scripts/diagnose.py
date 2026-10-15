#!/usr/bin/env python3
"""Report what Python can actually do on this machine.

The companion to ``diagnose.ps1``.  Between them they cover both sides of a
locked-down Windows box: PowerShell's assembly and policy situation, and
Python's module and network situation.

Runs on stock CPython with no dependencies, and never raises: a diagnostic that
crashes is worse than no diagnostic.

    python scripts/diagnose.py
    python scripts/diagnose.py --output C:\\report.json --no-network
"""

import argparse
import importlib
import json
import os
import platform
import sys
import sysconfig
import time

# Modules the standard library guarantees, plus ones worth confirming on a
# stripped-down or embedded interpreter.
STDLIB_MODULES = [
    "zlib", "struct", "ctypes", "sqlite3", "hashlib", "json", "argparse",
    "unittest", "tempfile", "shutil", "subprocess", "urllib.request",
    "xml.etree.ElementTree", "csv", "configparser", "logging", "pathlib",
    "concurrent.futures", "multiprocessing", "email", "http.server",
]

THIRD_PARTY_MODULES = [
    "PIL", "numpy", "cv2", "pytesseract", "requests", "docx", "openpyxl",
    "pptx", "pandas", "matplotlib", "win32com", "win32api", "psutil", "yaml",
]

PIP_MIRRORS = [
    "https://pypi.org/simple/",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/",
]


def probe_module(name):
    """Import a module and report the outcome without letting it escape."""
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        return {"ok": False, "error": "ImportError: %s" % exc}
    except Exception as exc:  # noqa: BLE001 - a broken module must not abort the sweep
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    version = getattr(module, "__version__", None)
    path = getattr(module, "__file__", None)
    return {"ok": True, "version": version, "path": path}


def probe_network(url, timeout=4.0):
    """HEAD the index URL.  Distinguishes unreachable from rejected."""
    import urllib.error
    import urllib.request

    started = time.time()
    request = urllib.request.Request(url, method="HEAD")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: D102
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "seconds": round(time.time() - started, 2),
        }
    return {
        "ok": 200 <= status < 400,
        "status": status,
        "seconds": round(time.time() - started, 2),
    }


def probe_ctypes_win32():
    """Confirm the Win32 pieces the tooling depends on actually resolve."""
    if os.name != "nt":
        return {"ok": False, "error": "not Windows"}

    import ctypes
    from ctypes import wintypes

    results = {}

    def check(label, fn):
        try:
            fn()
            results[label] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            results[label] = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

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

    def shell32_available():
        shell = ctypes.windll.shell32
        if not hasattr(shell, "SHFileOperationW"):
            raise RuntimeError("SHFileOperationW missing from shell32")
        # Building the struct catches any field-type mismatch up front.
        struct = SHFILEOPSTRUCTW()
        if struct.fFlags != 0:
            raise RuntimeError("struct did not zero-initialise")

    def kernel32_available():
        if not hasattr(ctypes.windll.kernel32, "GetLastError"):
            raise RuntimeError("kernel32 missing GetLastError")

    check("shell32.SHFileOperationW", shell32_available)
    check("kernel32", kernel32_available)
    return {"ok": True, "detail": results}


def probe_recycle_bin():
    """Locate and count this user's recycle bin entries.

    The SID is resolved from the process token rather than guessed, because the
    recycle bin directory is named after it.  Every Win32 function here needs an
    explicit ``restype``: ctypes defaults to ``c_int``, which silently truncates
    the 64-bit HANDLE returned by ``GetCurrentProcess`` and makes the whole call
    fail with no useful error.  That bug is exactly why this file spells the
    signatures out.
    """
    if os.name != "nt":
        return {"ok": False, "error": "not Windows"}

    import ctypes

    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32

    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.OpenProcessToken.restype = ctypes.c_int

    advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.GetTokenInformation.restype = ctypes.c_int

    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_int

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    TOKEN_QUERY = 0x0008
    TokenUser = 1

    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        return {"ok": False, "error": "OpenProcessToken failed"}

    sid = None
    try:
        # First call measures the buffer, second fills it.  This two-pass
        # pattern is standard for GetTokenInformation.
        needed = ctypes.c_uint32(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(needed))
        if not needed.value:
            return {"ok": False, "error": "GetTokenInformation size probe failed"}

        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, TokenUser, buffer, needed.value, ctypes.byref(needed)
        ):
            return {"ok": False, "error": "GetTokenInformation failed"}

        # TOKEN_USER starts with a SID_AND_ATTRIBUTES, whose first field is a
        # pointer to the SID.
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        string_sid = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(
            ctypes.c_void_p(sid_pointer), ctypes.byref(string_sid)
        ):
            return {"ok": False, "error": "ConvertSidToStringSidW failed"}
        try:
            sid = string_sid.value
        finally:
            kernel32.LocalFree(string_sid)
    finally:
        kernel32.CloseHandle(token)

    if not sid:
        return {"ok": False, "error": "empty SID"}

    path = os.path.join("C:\\$Recycle.Bin", sid)
    if not os.path.isdir(path):
        return {"ok": False, "error": "not found: %s" % path, "sid": sid}

    # os.listdir rather than a glob: on a bin holding tens of thousands of
    # entries this is dramatically faster.
    entries = os.listdir(path)
    metadata = [e for e in entries if e.startswith("$I")]
    return {
        "ok": True,
        "sid": sid,
        "path": path,
        "entries": len(entries),
        "metadataFiles": len(metadata),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="", help="where to write the JSON report")
    parser.add_argument(
        "--no-network", action="store_true", help="skip pip mirror reachability checks"
    )
    args = parser.parse_args()

    report = {
        "script": "awesome-restricted-windows / diagnose.py",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    report["interpreter"] = {
        "version": sys.version,
        "versionInfo": list(sys.version_info[:3]),
        "executable": sys.executable,
        "implementation": platform.python_implementation(),
        "is64Bit": sys.maxsize > 2 ** 32,
        "inVirtualEnv": sys.prefix != sys.base_prefix,
        "prefix": sys.prefix,
    }

    report["platform"] = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "osName": os.name,
        "cpuCount": os.cpu_count(),
    }

    report["stdlib"] = {name: probe_module(name) for name in STDLIB_MODULES}
    report["thirdParty"] = {name: probe_module(name) for name in THIRD_PARTY_MODULES}

    try:
        report["paths"] = {
            "purelib": sysconfig.get_paths().get("purelib"),
            "scripts": sysconfig.get_paths().get("scripts"),
        }
    except Exception as exc:  # noqa: BLE001
        report["paths"] = {"ok": False, "error": str(exc)}

    # pip configuration
    pip_ini = os.path.join(os.environ.get("APPDATA", ""), "pip", "pip.ini")
    if os.path.isfile(pip_ini):
        with open(pip_ini, "r", encoding="utf-8", errors="replace") as handle:
            report["pipConfig"] = {"path": pip_ini, "content": handle.read().strip()}
    else:
        report["pipConfig"] = {"path": pip_ini, "content": None}

    if args.no_network:
        report["network"] = {"skipped": True}
    else:
        report["network"] = {url: probe_network(url) for url in PIP_MIRRORS}

    report["win32"] = probe_ctypes_win32()
    report["recycleBin"] = probe_recycle_bin()

    # Derived conclusions, mirroring the PowerShell report.
    stdlib_ok = all(entry["ok"] for entry in report["stdlib"].values())
    report["capabilities"] = {
        "fullStdlib": stdlib_ok,
        "pillowAvailable": report["thirdParty"].get("PIL", {}).get("ok", False),
        "numpyAvailable": report["thirdParty"].get("numpy", {}).get("ok", False),
        "win32DeleteApi": bool(report["win32"].get("detail", {}).get(
            "shell32.SHFileOperationW", {}
        ).get("ok")),
        "recycleBinAccess": report["recycleBin"].get("ok", False),
        "anyMirrorReachable": any(
            entry.get("ok") for entry in report["network"].values()
            if isinstance(entry, dict)
        ),
    }

    output = args.output or os.path.join(os.getcwd(), "diagnose_python.json")
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)

    print("diagnostic written to %s" % output)
    print("python %s at %s" % (platform.python_version(), sys.executable))
    for name, value in report["capabilities"].items():
        print("  %-22s %s" % (name, "yes" if value else "no"))
    missing = [n for n, v in report["stdlib"].items() if not v["ok"]]
    if missing:
        print("missing standard-library modules: %s" % ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
