#!/usr/bin/env python3
"""
jtr-helper.py  —  interactive John-the-Ripper helper

Features
- HOME-based config (works across users)
- Interactive prompt to choose John [List.Rules.*] sections
  • Accepts indices or names; re-prompts until valid
  • Supports --chained: first selection is --rules, rest via --rules-stack:
  • Enumerates rules via `john --list=rules` (authoritative); file-scan fallback
- Hash glob support (e.g., -hash "hashes/*")
- Keeps john interactive (status keys like 's' still work)
- Integrates with potpy.py to (re)build master wordlist (-s or -b)
  • Prints start/end times and elapsed duration
- Safe signal handling to skip/exit cleanly
"""

import os
import re
import sys
import time
import glob
import shlex
import signal
import random
import string
import argparse
import subprocess
from pathlib import Path
from typing import List

# ============= BEGIN CONFIG (HOME-based) =============
HOME = Path.home()

# John paths
johnConf       = str(HOME / "src/john/run/john.conf")
johnLocalConf  = str(HOME / "src/john/run/john-local.conf")
jtrLocation    = str(HOME / "src/john/run/john")           # john binary (default; can override with --john-bin)

# potpy integration (for -s / -b master build)
potpy_script   = str(HOME / "scripts/potpy.py")
final_master   = str(HOME / "wordlists/master.lst")
# ============= END CONFIG ============================

# Globals (kept minimal)
ruleList: List[str] = []
isRunning = False

# ---------- Regex for fallback file scanning ----------
RULE_SECTION_RE = re.compile(r'^\s*\[List\.Rules[.:]([^\]]+)\]\s*(?:[#;].*)?$')


# ------------------ Utilities ------------------
def setSessionIfNull():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))


def _resolve_john_binary(john_bin_cfg: str) -> str:
    """Choose which 'john' to run."""
    # explicit override
    if john_bin_cfg and os.path.isfile(john_bin_cfg) and os.access(john_bin_cfg, os.X_OK):
        return john_bin_cfg
    # configured default
    if os.path.isfile(jtrLocation) and os.access(jtrLocation, os.X_OK):
        return jtrLocation
    # PATH search
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(p, "john")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    # last resort
    return john_bin_cfg or jtrLocation


def _list_rules_authoritatively(john_bin: str) -> List[str]:
    """Use `john --list=rules` to enumerate available rule sections."""
    cmd = [john_bin, "--list=rules"]
    print(f"[i] Listing rules via: {' '.join(shlex.quote(x) for x in cmd)}")
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    lines = out.decode("utf-8", "replace").splitlines()
    rules = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return rules


def _scan_file_for_rules(pth: str, found: List[str]) -> None:
    """Fallback: scan a config-like file for [List.Rules.*] headers."""
    try:
        with open(pth, "r", encoding="latin-1", errors="replace") as f:
            for line in f:
                m = RULE_SECTION_RE.match(line)
                if not m:
                    continue
                name = m.group(1).strip()
                if name and name not in found:
                    found.append(name)
    except OSError as e:
        print(f"[!] Failed to read {pth}: {e}")


def _fallback_scan_for_rules() -> List[str]:
    """Try known files/dirs if `--list=rules` is unavailable."""
    found: List[str] = []
    candidates = [
        johnConf,
        johnLocalConf,
        str(Path(johnConf).parent / "rules"),  # ~/src/john/run/rules
        str(HOME / ".john"),
        "/usr/share/john",
        "/etc/john",
    ]
    for c in candidates:
        p = Path(c)
        if p.is_file():
            _scan_file_for_rules(str(p), found)
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in (".conf", ".rules", ".ini", ".txt", ""):
                    _scan_file_for_rules(str(f), found)
    return found


def readConf(john_bin_cfg: str):
    """Populate global ruleList with every available ruleset."""
    global ruleList
    ruleList.clear()
    john_bin = _resolve_john_binary(john_bin_cfg)

    if not john_bin or not os.path.exists(john_bin):
        print(f"[!] john binary not found: {john_bin}\n"
              f"    Pass --john-bin or fix jtrLocation in the script.")
        sys.exit(1)

    # Authoritative first
    try:
        rules = _list_rules_authoritatively(john_bin)
        if not rules:
            raise RuntimeError("no rules returned")
        ruleList.extend(rules)
    except Exception as e:
        print(f"[!] `john --list=rules` failed or returned no rules ({e}). Falling back to file scan...")
        rules = _fallback_scan_for_rules()
        if not rules:
            print("[!] Could not discover any rules. Check john config/paths.")
            sys.exit(1)
        ruleList.extend(rules)

    # Print discovered list with indices
    for i, name in enumerate(ruleList):
        print(f"[{i}] {name}")
    print(f"[=] Total rules discovered: {len(ruleList)}")


def setJohnFork():
    """Prompt for --fork unless stdin is non-interactive."""
    global johnFork
    if not sys.stdin.isatty():
        johnFork = "1"
        print("[i] Non-interactive stdin; defaulting --fork=1")
        return
    max_fork = os.cpu_count() or 1
    val = input(f"Enter the --fork value for John (1 to {max_fork}): ").strip()
    if not val.isnumeric():
        print(f"--fork was not numeric. Please set --fork to a value between 1 and {max_fork}")
        sys.exit(1)
    if int(val) < 1 or int(val) > max_fork:
        print(f"Please set --fork to a value between 1 and {max_fork}")
        sys.exit(1)
    johnFork = val


def displayConfig():
    print("Session Name:", jtrsession)
    print("Min-Length:", minlength)
    print("Max-Length:", maxlength, "\n")


def verifyPaths(wordlist, hashFile, isWordlists):
    """Be permissive with globs (hashes/*)."""
    if isWordlists:
        wordlistDir = wordlist.replace("*", "")
        if not os.path.exists(wordlistDir):
            print(f"The wordlist directory could not be found: {wordlistDir}\nExiting")
            sys.exit(1)
    else:
        if not os.path.exists(wordlist):
            print(f"The wordlist could not be found: {wordlist}\nExiting")
            sys.exit(1)

    hp = hashFile
    if any(ch in hp for ch in "*?[]"):
        matches = glob.glob(hp)
        if not matches:
            print(f"[!] No files matched hash glob: {hp}\nExiting")
            sys.exit(1)
    else:
        if not os.path.exists(hp):
            print(f"Hash file(s) could not be found: {hp}\nExiting")
            sys.exit(1)


import os, sys, time, shlex, subprocess

def _run_potpy_build():
    """Run potpy to (re)build master list; prints start/end and elapsed."""
    if not os.path.isfile(potpy_script):
        print(f"[!] potpy.py not found: {potpy_script}")
        return False

    # Tunables (env overrides allowed)
    tmpdir   = os.environ.get("POTPY_TMPDIR", "/tmp/potpy")
    threads  = int(os.environ.get("POTPY_THREADS", max(1, min((os.cpu_count() or 1), 16))))
    mem      = os.environ.get("POTPY_MEM", "50%")  # external sort/join memory (e.g., 50% or 8G)

    # Ensure tmpdir exists
    try:
        os.makedirs(tmpdir, exist_ok=True)
    except Exception as e:
        print(f"[!] Could not create tmpdir '{tmpdir}': {e}")
        return False

    # Build candidate commands (best → fallback)
    candidates = [
        # Fast, parallel external sort path
        [sys.executable, potpy_script, "--merge-parallel",
         "--final", final_master, "--tmpdir", tmpdir, "--mem", str(mem), "--parallel", str(threads)],
        # External sort path without Python multiprocessing (still quite fast)
        [sys.executable, potpy_script, "--merge-fast",
         "--final", final_master, "--tmpdir", tmpdir, "--mem", str(mem), "--parallel", str(threads)],
        # Pure-Python path (slowest, but most compatible)
        [sys.executable, potpy_script, "--merge",
         "--final", final_master],
    ]

    start = time.time()
    print("\nUpdating Master Wordlist")
    print(f"Start time: {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(start))}")

    ret = 1
    last_cmd = None
    for cmd in candidates:
        last_cmd = cmd
        print("[i] Running:", " ".join(shlex.quote(x) for x in cmd))
        try:
            ret = subprocess.call(cmd)
        except KeyboardInterrupt:
            print("\n[!] Update aborted by user.")
            return False
        if ret == 0:
            break
        else:
            print(f"[!] Mode failed (exit {ret}), trying next…")

    end = time.time()
    if ret == 0:
        print("[+] Update Completed")
    else:
        print(f"[!] Update failed (exit code {ret})")

    print(f"End time:   {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(end))}")
    print(f"Elapsed:    {end - start:.1f}s\n")
    return ret == 0



def updateShell(isUpdateMaster):
    if not isUpdateMaster:
        return
    _run_potpy_build()


def _quote_maybe(path: str) -> str:
    """Quote a path unless it has glob chars (we want shell expansion for hashes/*)."""
    if any(ch in path for ch in "*?[]"):
        return path  # leave unquoted for shell glob expansion
    return shlex.quote(path)


def crackpwds(rule, wordlist, crule, john_bin: str):
    """Invoke john with selected rules / stacking; keep interactive status keys working."""
    global isRunning
    isRunning = True

    if crule is None:
        stackedRule = ""    # no stack
        r = rule
    else:
        stackedRule = f" --rules-stack:{rule} " if rule else ""
        r = crule

    # Build john command for each requested format (comma-separated)
    for hashFormat in hashFormats.split(","):
        # Construct command string manually so we can allow shell globbing on hashFile
        parts = [
            _quote_maybe(john_bin),
            _quote_maybe(hashFile),
            f"--min-length:{minlength}",
            f"--max-length:{maxlength}",
            f"--wordlist={_quote_maybe(wordlist)}",
            f"--format:{hashFormat}",
            f"--rules:{r}",
            "--force-tty",
            "--no-log",
            f"--session={jtrsession}",
        ]
        if int(johnFork) > 1:
            parts.append(f"--fork:{johnFork}")
        if crule is not None and stackedRule:
            parts.insert(6, stackedRule.strip())  # place near --rules

        cmd_str = " ".join(parts)

        print("\nRunning:\n" + cmd_str + "\n")

        # Let john inherit the terminal so hotkeys (e.g., 's') work.
        try:
            ret = subprocess.call(cmd_str, shell=True)
        except KeyboardInterrupt:
            print("\n[!] Aborted current john run by user.")
            continue

    isRunning = False
    # Post-run: if john cracked more, optionally rebuild master if -s was set
    if rebuildAfterCrack:
        print("\n[i] Rebuilding master list because -s was set...")
        _run_potpy_build()


def createRuleList(isChained, isWordlists, wordlist, john_bin: str):
    """Prompt user to select rules; supports indices or names; always shows what’s loaded."""
    print("\nDiscovering rule sections...")
    readConf(john_bin)

    if not ruleList:
        print("[!] No rules discovered. Check john config/paths / binary.")
        sys.exit(1)

    print("\nEnter rules by index (e.g., 0,1,5) or by name (e.g., best64,d3ad0ne).")
    print("Use * to run all (minus some extra-heavy sets).")
    if isChained:
        prompt = ("Enter the numbers/names separated by commas. "
                  "The FIRST becomes --rules, the rest become --rules-stack: ")
    else:
        prompt = "Enter the number(s)/name(s) of the rule(s) to run: "

    # keep prompting until we get at least one valid selection
    while True:
        if not sys.stdin.isatty():
            default_rule = ruleList[0]
            print(f"[i] Non-interactive; defaulting to rule: {default_rule}")
            crackpwds(default_rule, wordlist, None, john_bin)
            return

        val = input(f"\n{prompt}").strip()
        # Skip a few notorious mega-sets when '*' is used (avoid huge load times)
        extrarules = ["o3", "i3"]  # you can expand if desired

        selected: List[str] = []

        if val == "*":
            selected = [r for r in ruleList if r not in extrarules]
        else:
            tokens = [x.strip() for x in val.split(",") if x.strip()]
            for t in tokens:
                if t.isnumeric():
                    idx = int(t)
                    if 0 <= idx < len(ruleList):
                        selected.append(ruleList[idx])
                    else:
                        print(f"[!] Index out of range: {idx} (0..{len(ruleList)-1})")
                else:
                    if t in ruleList:
                        selected.append(t)
                    else:
                        # case-insensitive exact match
                        matches = [r for r in ruleList if r.lower() == t.lower()]
                        if matches:
                            selected.extend(matches)
                        else:
                            print(f"[!] No such rule name: {t}")

        # dedupe, preserve order
        seen = set()
        selected = [r for r in selected if not (r in seen or seen.add(r))]

        if not selected:
            print("[!] No valid rules selected. Please try again.")
            continue

        if isChained:
            crule = selected[0]
            stacked = ",".join(selected[1:]) if len(selected) > 1 else ""
            print(f"\nUsing --rules:{crule}" + (f" and --rules-stack:{stacked}" if stacked else ""))
            crackpwds(stacked, wordlist, crule, john_bin)
        else:
            for rname in selected:
                print(f"\nUsing --rules:{rname}")
                crackpwds(rname, wordlist, None, john_bin)
        return


def handler(signal_received, frame):
    try:
        if isRunning:
            print("\n\nAborting current john wordlist/rule\n"
                  "If another wordlist is available, cracking will continue.\n")
        else:
            sys.exit(0)
    except Exception:
        sys.exit(0)


# ------------------ Main ------------------
def main():
    # ASCII header
    print("___________________________________________________________")
    print("       _ __             __         __               ")
    print("      (_) /______      / /_  ___  / /___  ___  _____")
    print(r"     / / __/ ___/_____/ __ \/ _ \/ / __ \/ _ \/ ___/")
    print("    / / /_/ /  /_____/ / / /  __/ / /_/ /  __/ /    ")
    print(r" __/ /\__/_/        /_/ /_/\___/_/ .___/\___/_/     ")
    print("/___/                           /_/                 ")
    print("\njtr-helper\n")
    print("Ensure Configurations are set:")
    print("  johnConf, johnLocalConf, jtrLocation")
    print("  potpy_script, final_master\n")
    print("___________________________________________________________\n")

    parser = argparse.ArgumentParser()
    parser.add_argument("-b", "--build", help="only build the master wordlist and exit",
                        action='store_const', const=True)
    parser.add_argument("-f", "--format", dest="format", help="specify the jtr hash format (comma-separated OK)")
    parser.add_argument("-w", "--wordlist", help="specify the file with wordlist")
    parser.add_argument("-r", "--recursive",
                        help="used with wordlists if a directory is defined: -w /wordlistDIR/*",
                        action='store_const', const=True)
    parser.add_argument("-hash", "--hashes", help="specify the file with hashes (globs like hashes/* allowed)")
    parser.add_argument("-min", "--minlength", help="specify the min-length")
    parser.add_argument("-max", "--maxlength", help="specify the max-length")
    parser.add_argument("-session", "--session", help="specify the session")
    parser.add_argument("-s", "--script",
                        help="also (re)build master wordlist before and after cracking",
                        action='store_const', const=True)
    parser.add_argument("-c", "--chained",
                        help="chain rules: first selection is --rules; remaining are --rules-stack",
                        action='store_const', const=True)
    parser.add_argument("--john-bin", help="Path to john binary (overrides jtrLocation)")

    args = parser.parse_args()

    # Build only?
    if args.build:
        updateShell(True)
        sys.exit(0)

    if not (args.format and args.wordlist and args.hashes):
        parser.print_help()
        sys.exit(1)

    # Globals set here
    global hashFormats, wordlist, hashFile, isWordlists, isChained
    global minlength, maxlength, jtrsession, rebuildAfterCrack

    hashFormats = args.format
    wordlist = args.wordlist
    hashFile = args.hashes

    isChained = True if args.chained is not None else False
    rebuildAfterCrack = True if args.script is not None else False

    # Wordlist directory recursion
    if args.recursive and "/*" in args.wordlist:
        isWordlists = True
    else:
        isWordlists = False
        if args.recursive and "/*" not in args.wordlist:
            print("[!] -r ignored because -w does not end with /*")

    # Lengths
    minlength = args.minlength if args.minlength else '8'
    maxlength = args.maxlength if args.maxlength else '24'
    if not minlength.isnumeric():
        print("Please enter a number for minlength")
        sys.exit(1)
    if not maxlength.isnumeric():
        print("Please enter a number for maxlength")
        sys.exit(1)

    # Session
    if args.session:
        jtrsession = args.session
    else:
        jtrsession = setSessionIfNull()

    # Resolve john bin
    john_bin = _resolve_john_binary(args.john_bin or "")

    # Show basic config
    displayConfig()

    # Pre-build if -s
    if rebuildAfterCrack:
        updateShell(True)

    # Verify paths (and allow hash globs)
    verifyPaths(wordlist, hashFile, isWordlists)

    # Get a fork value (interactive if TTY)
    setJohnFork()

    # PROMPT FOR RULES
    if isWordlists:
        root = wordlist.replace("*", "")
        createRuleList(isChained, isWordlists=True,  wordlist=root,     john_bin=john_bin)
    else:
        createRuleList(isChained, isWordlists=False, wordlist=wordlist, john_bin=john_bin)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, handler)
    main()
