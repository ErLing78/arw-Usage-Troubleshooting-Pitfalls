"""Verify the examples and claims in awesome-restricted-windows/README.md.

Same purpose as stdpng's readme_examples_test.py: a README that claims things
should be checked against the code, not trusted.

Windows-only for the parts that touch Win32; the pngtool checks run anywhere.

    python tests/readme_examples_test.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print("%s  %s%s" % (status, label, ("  -- " + detail) if detail and not condition else ""))
    if not condition:
        failures.append(label if not detail else "%s: %s" % (label, detail))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args):
    return subprocess.run(
        [sys.executable] + args, capture_output=True, text=True
    )


def main():
    # --- every script the README lists must exist -------------------------
    print("=== README: listed files exist ===")
    listed = [
        "scripts/diagnose.ps1",
        "scripts/diagnose.py",
        "scripts/sh_delete.py",
        "scripts/pngtool.py",
        "docs/01-environment.md",
        "docs/02-loading-assemblies.md",
        "docs/03-image-processing.md",
        "docs/04-delete-and-recycle-bin.md",
        "docs/05-ocr-without-vision.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
    ]
    for rel in listed:
        check("exists: %s" % rel, os.path.exists(os.path.join(ROOT, rel)))

    # --- the scripts are self-contained: stdlib only ----------------------
    print()
    print("=== Scripts use only the standard library ===")
    # sys.stdlib_module_names only exists from 3.10, so on older interpreters the
    # list is derived from the running installation itself. That is more
    # reliable than a hand-maintained fallback set, which would drift.
    stdlib_names = set(getattr(sys, "stdlib_module_names", ()))
    if not stdlib_names:
        import sysconfig

        stdlib_names = set(sys.builtin_module_names)
        stdlib_dir = sysconfig.get_paths().get("stdlib")
        if stdlib_dir and os.path.isdir(stdlib_dir):
            for entry in os.listdir(stdlib_dir):
                if entry.endswith(".py"):
                    stdlib_names.add(entry[:-3])
                elif os.path.isdir(os.path.join(stdlib_dir, entry)) and not entry.startswith("."):
                    stdlib_names.add(entry)
    print("     (stdlib module list: %d names, from %s)"
          % (len(stdlib_names),
             "sys.stdlib_module_names" if hasattr(sys, "stdlib_module_names") else "installation scan"))
    import ast

    for name in ("diagnose.py", "sh_delete.py", "pngtool.py"):
        path = os.path.join(SCRIPTS, name)
        tree = ast.parse(open(path, encoding="utf-8").read())
        third_party = set()
        for node in ast.walk(tree):
            roots = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = [node.module.split(".")[0]]
            for root in roots:
                if root.startswith("_"):
                    continue
                if root in stdlib_names:
                    continue
                third_party.add(root)
        check("%s imports only stdlib" % name, not third_party,
              "found: %s" % ", ".join(sorted(third_party)))

    # --- pngtool.py: the four documented invocations ----------------------
    print()
    print("=== README example 3: pngtool.py ===")
    pngtool = load_module("pngtool_check", os.path.join(SCRIPTS, "pngtool.py"))

    with tempfile.TemporaryDirectory() as work:
        # The README's crop example uses coordinates like 120,340,480,60, which
        # assume a screenshot-sized image, so the fixture is 800x600 rather than
        # something tiny.
        src = os.path.join(work, "in.png")
        w, h = 800, 600
        pixels = bytearray(w * h * 4)
        for y in range(h):
            for x in range(w):
                o = (y * w + x) * 4
                pixels[o] = (x * 3) % 256
                pixels[o + 1] = (y * 5) % 256
                pixels[o + 2] = 128
                pixels[o + 3] = 255
        pngtool.write_png(src, w, h, pixels)

        # --info
        proc = run([os.path.join(SCRIPTS, "pngtool.py"), src, "--info"])
        check("pngtool --info exits 0", proc.returncode == 0, proc.stderr.strip())
        check("pngtool --info reports 800x600", "800x600" in proc.stdout, proc.stdout.strip())

        # --crop 120,340,480,60 --scale 4 --out
        out = os.path.join(work, "region.png")
        proc = run([os.path.join(SCRIPTS, "pngtool.py"), src,
                    "--crop", "120,340,480,60", "--scale", "4", "--out", out])
        check("pngtool crop+scale exits 0", proc.returncode == 0, proc.stderr.strip())
        if os.path.exists(out):
            cw, ch, _ = pngtool.read_png(out)
            check("crop 480x60 scaled 4x -> 1920x240", (cw, ch) == (1920, 240),
                  "got %dx%d" % (cw, ch))

        # --rotate 90 --out
        rot = os.path.join(work, "upright.png")
        proc = run([os.path.join(SCRIPTS, "pngtool.py"), src, "--rotate", "90", "--out", rot])
        check("pngtool --rotate 90 exits 0", proc.returncode == 0, proc.stderr.strip())
        if os.path.exists(rot):
            rw, rh, _ = pngtool.read_png(rot)
            check("rotate 90 swaps dimensions (600x800)", (rw, rh) == (600, 800),
                  "got %dx%d" % (rw, rh))

        # --ascii with both documented modes
        for mode in ("mean", "min"):
            proc = run([os.path.join(SCRIPTS, "pngtool.py"), src,
                        "--ascii", "120", "--mode", mode])
            check("pngtool --ascii --mode %s exits 0" % mode, proc.returncode == 0,
                  proc.stderr.strip()[:150])
            lines = [ln for ln in proc.stdout.split("\n") if ln]
            check("ascii (%s) output is 120 columns wide" % mode,
                  bool(lines) and all(len(ln) == 120 for ln in lines),
                  "first line len=%d" % (len(lines[0]) if lines else -1))

        # The README documents --mode as the switch, not a --mean flag.
        proc = run([os.path.join(SCRIPTS, "pngtool.py"), src, "--ascii", "40", "--mean"])
        check("pngtool rejects the undocumented --mean flag", proc.returncode != 0,
              "it succeeded, so docs and code disagree again")

        # The README says four rotations return to the original.
        cw, ch, cur = w, h, bytearray(pixels)
        for _ in range(4):
            cw, ch, cur = pngtool.rotate(cw, ch, cur, 90)
        check("four 90-degree rotations return to the original",
              bytes(cur) == bytes(pixels) and (cw, ch) == (w, h))

    # --- sh_delete.py: documented subcommands ----------------------------
    print()
    print("=== README example 4/5/6: sh_delete.py ===")
    if os.name != "nt":
        print("SKIP  sh_delete checks (not Windows)")
    else:
        sh = load_module("sh_delete_check", os.path.join(SCRIPTS, "sh_delete.py"))

        # README example 6: importable and callable
        check("sh_delete module exposes sh_delete", callable(getattr(sh, "sh_delete", None)))
        check("sh_delete module exposes scan_recycle_bin",
              callable(getattr(sh, "scan_recycle_bin", None)))

        # --help must work for each subcommand named in the README
        for sub in ("delete", "scan", "purge"):
            proc = run([os.path.join(SCRIPTS, "sh_delete.py"), sub, "--help"])
            check("sh_delete %s --help exits 0" % sub, proc.returncode == 0,
                  proc.stderr.strip()[:150])

        with tempfile.TemporaryDirectory() as work:
            target = os.path.join(work, "victim")
            os.makedirs(os.path.join(target, "sub"))
            for rel, body in (("a.txt", "hello"), ("sub/b.txt", "world")):
                with open(os.path.join(target, rel.replace("/", os.sep)), "w") as fh:
                    fh.write(body)

            # dry run must not delete
            proc = run([os.path.join(SCRIPTS, "sh_delete.py"), "delete", target,
                        "--mode", "recycle"])
            check("delete without --execute exits 0", proc.returncode == 0, proc.stderr.strip())
            check("dry run says DRY RUN", "DRY RUN" in proc.stdout, proc.stdout.strip())
            check("dry run did NOT delete anything", os.path.isdir(target))

            # --manifest path (README example 5)
            manifest = os.path.join(work, "cleanup.txt")
            with open(manifest, "w", encoding="utf-8") as fh:
                fh.write("# a comment\n%s\n" % target)
            proc = run([os.path.join(SCRIPTS, "sh_delete.py"), "delete",
                        "--manifest", manifest, "--mode", "recycle"])
            check("delete --manifest dry run exits 0", proc.returncode == 0, proc.stderr.strip())
            check("manifest mode found the target", target in proc.stdout or "victim" in proc.stdout,
                  proc.stdout.strip()[:200])

            # real delete, then reclaim
            proc = run([os.path.join(SCRIPTS, "sh_delete.py"), "delete", target,
                        "--mode", "recycle", "--execute"])
            check("delete --execute exits 0", proc.returncode == 0, proc.stderr.strip())
            check("target is gone", not os.path.exists(target))

            entries = sh.scan_recycle_bin(target)
            check("deleting a directory produces recycle bin entries", len(entries) >= 1,
                  "found %d" % len(entries))
            # Compare canonical forms: tempfile hands out a short name, while the
            # metadata holds the long one, so a raw startswith would be a false
            # alarm here.
            canonical_target = sh._canonical(target)
            check("entries carry the original path",
                  all(sh._canonical(e["originalPath"]).startswith(canonical_target)
                      for e in entries),
                  "paths: %s" % [e["originalPath"] for e in entries][:3])

            # Regression guard for the 8.3 short-name trap.
            #
            # tempfile.gettempdir() can return a SHORT name (something like
            # LONGPATH~1.USR), but Windows stores the LONG form in the $I
            # metadata. A naive string prefix comparison therefore finds
            # nothing, scan silently reports zero entries, and purge becomes a
            # no-op - leaving the disk space occupied with no error anywhere.
            # That bug shipped once; this check makes sure it cannot come back.
            if "~" in target:
                short_entries = sh.scan_recycle_bin(target)
                check("short-name prefix (with ~) still matches entries",
                      len(short_entries) >= 1,
                      "target %r matched %d entries" % (target, len(short_entries)))
                # And the long form of the same path must match identically.
                long_target = os.path.realpath(target)
                long_entries = sh.scan_recycle_bin(long_target)
                check("long-name prefix matches the same entries",
                      len(long_entries) == len(short_entries),
                      "short gave %d, long gave %d" % (len(short_entries), len(long_entries)))
            else:
                print("SKIP  short-name regression check (temp dir has no 8.3 name)")

            proc = run([os.path.join(SCRIPTS, "sh_delete.py"), "purge",
                        "--prefix", target, "--execute"])
            check("purge --execute exits 0", proc.returncode == 0, proc.stderr.strip())
            check("purge left nothing behind", not sh.scan_recycle_bin(target))
            # A short-name prefix must also purge completely, not partially.
            check("purge cleared entries found via the short prefix",
                  not sh.scan_recycle_bin(os.path.realpath(target)),
                  "long-name scan still finds entries")

    # --- diagnose.py: capability keys the README documents ---------------
    print()
    print("=== README capabilities block ===")
    documented = [
        "fullStdlib", "pillowAvailable", "numpyAvailable",
        "win32DeleteApi", "recycleBinAccess", "anyMirrorReachable",
    ]
    with tempfile.TemporaryDirectory() as work:
        report_path = os.path.join(work, "r.json")
        proc = run([os.path.join(SCRIPTS, "diagnose.py"), "--no-network",
                    "--output", report_path])
        check("diagnose.py --no-network exits 0", proc.returncode == 0, proc.stderr.strip())
        import json
        if os.path.exists(report_path):
            with open(report_path, encoding="utf-8") as fh:
                report = json.load(fh)
            caps = report.get("capabilities", {})
            for key in documented:
                check("capabilities.%s present" % key, key in caps)
            # The README prints these six lines; they must all be there.
            for key in documented:
                check("stdout lists %s" % key, key in proc.stdout, proc.stdout.strip()[:200])

    print()
    if failures:
        print("README VERIFICATION FAILED (%d)" % len(failures))
        for item in failures:
            print("  - " + item)
        return 1
    print("README VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
