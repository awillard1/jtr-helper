#!/usr/bin/env python3
"""
jtr-helper.py — wrapper for John with master wordlist building via potpy.

Behavior:
 - With -b / --build: build the master list FIRST and exit.
 - With -s / --script: build the master list FIRST, then run cracking, then
   IF any watched potfiles changed during cracking, build AGAIN at the end.
 - Without -s/-b: no master build is performed by this script.

Other details:
 - John is launched inheriting your TTY (hotkeys like 's' work).
 - Wildcard hash specs (e.g., "hashes/*") are expanded inside Python.
 - Build uses potpy.py (parallel+cache) synchronously, with clear timing.
"""
from __future__ import annotations
import os
import re
import argparse
import subprocess
import signal
import potpy
import random
import string
import time
from datetime import datetime
import sys
import shutil
import tempfile
import glob

######## BEGIN CONFIGURATION ########
johnConf = "/home/willard/src/john/run/john.conf"
johnLocalConf = "/home/willard/src/john/run/john-local.conf"
jtrLocation = "/home/willard/src/john/run/john"
######## END CONFIGURATION   ########

# ---- potpy parallel-merge config ----
potpy_script   = "/home/willard/scripts/potpy.py"        # path to potpy.py (fast merge CLI)
fast_tmpdir    = "/tmp/potpy"                            # set to disk-backed path if /tmp is tmpfs (e.g., /mnt/nvme/potpy_tmp)
fast_mem       = "25%"                                   # GNU sort memory (-S)
fast_parallel  = max(1, (os.cpu_count() or 1) // 2)      # --parallel for sort
final_master   = "/home/willard/wordlists/master.lst"    # final master path
cache_dir      = os.path.expanduser("~/.cache/potpy/decoded")
gc_cache_days  = 30                                      # days to keep decoded cache; 0 disables GC

# John/OpenMP tuning (forked children can be memory hungry)
omp_threads_per_fork = 1                                 # reduce if children were dying
# ------------------------------------

# Globals (some set later from args)
isWordlists = False
isChained = False
isRunning = False
ruleList = []
jtrsession = ""
johnFork = "1"
wordlist = ""
wordlistDir = ""
hashFormats = ""
hashFile = ""
minlength = '8'
maxlength = '24'

# -------- potfile change detection --------
def _pot_watch_list() -> list[str]:
    """Prefer potpy.potfiles; otherwise, watch common defaults."""
    try:
        files = list(getattr(potpy, "potfiles", []))
    except Exception:
        files = []
    # Add some likely defaults if missing
    defaults = [
        os.path.expanduser("~/.john/john.pot"),
        "/home/willard/src/john/run/john.pot",
        "/mnt/c/PenTesting/hashcat.potfile",
        "/mnt/c/PenTesting/data/hashcat-6.2.6/hashcat.potfile",
    ]
    for p in defaults:
        if p not in files:
            files.append(p)
    # Keep only paths that exist (we’ll still snapshot non-existent as None)
    return files

def _snapshot(paths: list[str]) -> dict[str, tuple[int,int] | None]:
    """
    Return {path: (mtime_ns, size)} for existing files; None for missing.
    """
    snap = {}
    for p in paths:
        try:
            st = os.stat(p)
            snap[p] = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            snap[p] = None
        except Exception:
            snap[p] = None
    return snap

def _changed(before: dict, after: dict) -> list[str]:
    """
    Return list of paths whose (mtime,size) changed (including newly created).
    """
    dirty = []
    for p, b in before.items():
        a = after.get(p, None)
        if b is None and a is not None:
            dirty.append(p)
        elif b is not None and a is None:
            # deleted; not typical for pots, ignore unless you want to rebuild anyway
            continue
        elif b is not None and a is not None and b != a:
            dirty.append(p)
    return dirty
# -----------------------------------------

def setJohnFork():
    global johnFork
    johnFork = input("Enter the --fork value for John (1 to " + str(os.cpu_count()) + "): ")
    if (johnFork.isnumeric() == False):
        print("--fork was not numeric. Please set --fork to a value between 1 and " + str(os.cpu_count()))
        exit()
    elif (int(johnFork) < 1 or int(johnFork) > os.cpu_count()):
        print("Please set --fork to a value between 1 and " + str(os.cpu_count()))
        exit()

def readConf():
    i = 0
    if (os.path.exists(johnConf) == False):
        print("Unable to locate john.conf: " + johnConf + "\r\nExiting")
        exit()
    with open(johnConf, "r", encoding="utf-8", errors="ignore") as jtrconf:
        for line in jtrconf:
            match = re.match(r"^\[List.Rules.(.*)\].*$", line)
            if match:
                rule = match.group(1)
                print("["+str(i)+"] "+rule)
                ruleList.append(rule)
                i = i + 1
    if (os.path.exists(johnLocalConf)):
        with open(johnLocalConf,"r", encoding="utf-8", errors="ignore") as jtrlocalconf:
            for line in jtrlocalconf:
                match = re.match(r"^\[List.Rules.(.*)\].*$", line)
                if match:
                    rule = match.group(1)
                    print("["+str(i)+"] "+rule)
                    ruleList.append(rule)
                    i = i + 1
    # Add Korelogic
    print("["+str(i)+"] korelogic")
    ruleList.append("korelogic")

def setSessionIfNull():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))

def loopCrack(rule, crule):
    wordlistdir = wordlistDir.replace("*","")
    for root, dirs, files in os.walk(wordlistdir):
        for file in files:
            if (root == wordlistdir):
                w = root + file
            else:
                w = root +"/"+ file
            print("Loading: " + w)
            crackpwds(rule, w, crule)

def createRuleList():
    # NOTE: o3 and i3 excluded due to very long runtimes. Remove from list to include.
    # o1,i1,o2,i3 removed because they are covered by the oi rule set.
    extrarules = ["best64","d3ad0ne","dive","InsidePro","T0XlC","rockyou-30000","specific","o","i","i1","i2","o1","o2","o3","i3"]
    readConf()
    print("\r\nIf you want to run all the rules listed, enter * and press enter")
    print("If you want to run some rules, comma separate the numbers and press enter\r\n")
    if (isChained):
        val = input("Enter the numbers of the rules separeted by a comma. The first rule will be set for --rules and the rest will be assigned to --rules-stacked: ")
    else:
        val = input("Enter the number(s) of the rule to run: ")

    if (isChained):
        try:
            listNumberRule = val.split(",")
            rules = []
            for ruleNumber in listNumberRule:
                if (ruleNumber.isnumeric() and int(ruleNumber) >= 0 and int(ruleNumber) <= len(ruleList)):
                    rules.append(ruleList[int(ruleNumber)])
            crule = rules[0]
            rules.pop(0)
            rule = ','.join(rules)
            print("\r\nRule: " + crule + " and " + rule + " (as stacked rules)")
            if (isWordlists):
                loopCrack(rule, crule)
            else:
                crackpwds(rule, wordlist, crule)
        except:
            print("unable to split and run jtr")
            exit()
    elif ("," in val):
        try:
            listNumberRule = val.split(",")
            for ruleNumber in listNumberRule:
                if (ruleNumber.isnumeric() and int(ruleNumber) >= 0 and int(ruleNumber) <= len(ruleList)):
                    rule = ruleList[int(ruleNumber)]
                    print("\r\n" + rule + " ruleset will be used")
                    if (isWordlists):
                        loopCrack(rule, None)
                    else:
                        crackpwds(rule, wordlist, None)
        except:
            print("unable to split and run jtr")
            exit()
    elif (val == "*"):
        for r in ruleList:
            if (r not in extrarules):
                print("Rule: " + r)
                if (isWordlists):
                    loopCrack(r, None)
                else:
                    crackpwds(r,wordlist, None)
    elif (val.isnumeric() and int(val)>=0 and int(val) <= len(ruleList)):
        rule = ruleList[int(val)]
        print("\r\n" + rule + " ruleset will be used")
        if (isWordlists):
            loopCrack(rule, None)
        else:
            crackpwds(rule, wordlist, None)
    else:
        exit()

def _format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if hrs: parts.append(f"{hrs}h")
    if mins: parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return ' '.join(parts)

def _prepare_tmpdir(path: str, verbose: bool=True) -> str:
    """Ensure tmpdir exists & is writable; fallback to system tmp if not."""
    try:
        if path:
            os.makedirs(path, exist_ok=True)
            testfile = os.path.join(path, ".jtr_helper_write_test")
            with open(testfile, "w") as f:
                f.write("x")
            os.remove(testfile)
            if verbose:
                print(f"[+] Using tmpdir: {path}")
            return path
    except Exception as e:
        if verbose:
            print(f"[!] Requested tmpdir '{path}' not usable ({e}); falling back to system temp.")

    sys_tmp = tempfile.gettempdir()
    try:
        testfile = os.path.join(sys_tmp, ".jtr_helper_write_test")
        with open(testfile, "w") as f:
            f.write("x")
        os.remove(testfile)
        if verbose:
            print(f"[+] Using system tmpdir: {sys_tmp}")
        return sys_tmp
    except Exception as e:
        if verbose:
            print(f"[!] System temp '{sys_tmp}' not usable ({e}); proceeding without -T.")
        return ""  # no usable temp; we won't pass --tmpdir

def updateShell():
    """
    Build the master wordlist via potpy (synchronously).
    Prints start/end/elapsed. Detaches stdin from the child.
    """
    py = shutil.which("python3") or sys.executable
    mem = fast_mem if fast_mem else "25%"
    par = fast_parallel if fast_parallel and int(fast_parallel) > 0 else max(1, (os.cpu_count() or 1) // 2)
    eff_tmpdir = _prepare_tmpdir(fast_tmpdir, verbose=True)

    cmd = [
        py, potpy_script,
        "--merge-parallel",
        "--final", final_master,
        "--mem", str(mem),
        "--parallel", str(par),
        "-v",
        "--gc-cache-days", str(gc_cache_days)
    ]
    if eff_tmpdir:
        cmd.extend(["--tmpdir", eff_tmpdir])
    if cache_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            cmd.extend(["--cache-dir", cache_dir])
        except Exception:
            pass

    start_dt = datetime.now().astimezone()
    print("\r\nBuilding Master Wordlist (potpy parallel merge + cache)")
    print(f"Start time: {start_dt:%Y-%m-%d %H:%M:%S %Z}")
    print("Command: " + " ".join(cmd))

    start = time.time()

    if os.path.exists(potpy_script):
        try:
            subprocess.check_call(cmd, stdin=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            elapsed = time.time() - start
            end_dt = datetime.now().astimezone()
            print(f"[!] Update failed after {_format_duration(elapsed)}")
            print(f"End time:   {end_dt:%Y-%m-%d %H:%M:%S %Z}\r\n")
            print(f"Error: {e}\r\n")
            return False
    else:
        try:
            result = potpy.process_potfile()
            if result:
                print(f"[+] potpy.process_potfile() returned: {result}")
        except Exception as e:
            elapsed = time.time() - start
            end_dt = datetime.now().astimezone()
            print(f"[!] Fallback potpy.process_potfile() failed after {_format_duration(elapsed)}")
            print(f"End time:   {end_dt:%Y-%m-%d %H:%M:%S %Z}\r\n")
            print(f"Error: {e}\r\n")
            return False

    elapsed = time.time() - start
    end_dt = datetime.now().astimezone()
    print(f"Output:    {final_master}")
    print(f"End time:  {end_dt:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Elapsed:   {_format_duration(elapsed)}\r\n")
    return True

def _expand_hash_inputs(raw_hash_spec: str):
    """
    Expand a hash file spec into a list of concrete files:
      - wildcard patterns (e.g., hashes/*)
      - a directory (all non-hidden files within)
      - a single file
      - comma-separated list
    """
    files = []
    if not raw_hash_spec:
        return files
    try:
        if any(ch in raw_hash_spec for ch in ["*", "?", "["]):
            matches = sorted(glob.glob(raw_hash_spec))
            if matches:
                return matches
        if os.path.isdir(raw_hash_spec):
            files = sorted(
                os.path.join(raw_hash_spec, f)
                for f in os.listdir(raw_hash_spec)
                if not f.startswith('.') and os.path.isfile(os.path.join(raw_hash_spec, f))
            )
            return files
        if os.path.exists(raw_hash_spec):
            return [raw_hash_spec]
        if "," in raw_hash_spec:
            parts = [p.strip() for p in raw_hash_spec.split(",") if p.strip()]
            for p in parts:
                if os.path.exists(p):
                    files.append(p)
            return files
    except Exception as e:
        print(f"[!] Error resolving hash files: {e}")
        return []
    return files

def _run_john_with_retry(cmd, env):
    """Run John once; if it fails and --fork:N present, retry once with half forks."""
    rc = subprocess.run(cmd, check=False, env=env).returncode
    if rc == 0:
        return 0
    forks = [tok for tok in cmd if tok.startswith("--fork:")]
    if forks:
        try:
            n = int(forks[0].split(":", 1)[1])
        except Exception:
            n = 0
        if n > 1:
            n2 = max(1, n // 2)
            cmd2 = [t for t in cmd if not t.startswith("--fork:")]
            cmd2.append(f"--fork:{n2}")
            env2 = dict(env)
            env2["OMP_NUM_THREADS"] = env.get("OMP_NUM_THREADS", "1")
            print(f"[!] John exited with code {rc}. Retrying once with --fork:{n2} and OMP_NUM_THREADS={env2['OMP_NUM_THREADS']} ...")
            return subprocess.run(cmd2, check=False, env=env2).returncode
    return rc

def crackpwds(rule, wordlist, crule):
    """
    Launch John in a TTY-friendly way so hotkeys (s, etc.) work.
    This function DOES NOT build the master list; builds happen before/after.
    """
    global isRunning
    isRunning = True

    if crule is None:
        stackedRule = ""
        r = rule
    else:
        stackedRule = f"--rules-stack:{rule}"
        r = crule

    env = os.environ.copy()
    env.setdefault("TERM", os.environ.get("TERM", "xterm-256color"))
    env.setdefault("LC_ALL", "C")
    env.setdefault("LANG", "C")
    if int(johnFork) > 1:
        env["OMP_NUM_THREADS"] = str(omp_threads_per_fork)

    hash_inputs = _expand_hash_inputs(hashFile)
    if not hash_inputs:
        print(f"[!] No valid hash files found from: {hashFile}")
        isRunning = False
        return

    for hashFormat in hashFormats.split(","):
        cmd = [jtrLocation] + hash_inputs + [
            f"--min-length:{minlength}",
            f"--max-length:{maxlength}",
            f"--wordlist:{wordlist}",
            f"--format:{hashFormat}",
            f"--rules:{r}",
            "--force-tty",
            # remove '--no-log' while diagnosing fork failures, then re-add if you prefer
            f"--session={jtrsession}"
        ]
        if int(johnFork) > 1:
            cmd.append(f"--fork:{johnFork}")
        if stackedRule:
            cmd.append(stackedRule)

        print("\r\nRunning:\r\n" + " ".join(cmd) + "\r\n")
        try:
            rc = _run_john_with_retry(cmd, env)
            if rc != 0:
                print(f"[!] John exited with code {rc}. Check ~/.john/john.log for details.")
        except KeyboardInterrupt:
            print("\r\nInterrupted John run by user (KeyboardInterrupt).")
        except Exception as e:
            print(f"[!] Error running John: {e}")

    isRunning = False

def displayConfig():
    print("Session Name: " + jtrsession)
    print("Min-Length: " + minlength)
    print("Max-Length: " + maxlength + "\r\n")

def main(build_first: bool, rebuild_on_crack: bool):
    displayConfig()

    # If requested, build master FIRST
    if build_first:
        ok = updateShell()
        if not ok:
            print("[!] Master build reported an error. Continuing to cracking anyway...")

    # Snapshot pots BEFORE cracking
    watch = _pot_watch_list() if rebuild_on_crack else []
    before = _snapshot(watch) if watch else {}

    setJohnFork()
    verifyPaths()
    createRuleList()

    # After cracking, if any potfile changed, rebuild master again
    if rebuild_on_crack and watch:
        after = _snapshot(watch)
        dirty = _changed(before, after)
        if dirty:
            print("\n[+] Detected updated potfiles during cracking:")
            for p in dirty:
                print("    -", p)
            print("[+] Rebuilding master wordlist to include newly cracked credentials...")
            updateShell()
        else:
            print("\n[+] No potfile changes detected; skipping post-crack rebuild.")

def verifyPaths():
    global wordlistDir
    if (isWordlists):
        wordlistDir = wordlist.replace("*","")
        if (os.path.exists(wordlistDir) == False):
            print("The wordlist directory could not be found:" + wordlistDir + "\r\nExiting")
            exit()
    else:
        if (os.path.exists(wordlist) == False):
            print("The wordlist could not be found:" + wordlist + "\r\nExiting")
            exit()

    # For hashes: allow patterns/dirs; only hard-fail if it's a literal missing path
    if (os.path.exists(hashFile.replace("*","")) == False) and (not any(ch in hashFile for ch in ["*", "?", "["])) and (not os.path.isdir(hashFile)):
        print("Hash file(s) could not be found:" + hashFile + "\r\nExiting")
        exit()

def handler(signal_received, frame):
    """Ctrl-C: skip/abort current run if running, else exit."""
    try:
        if (isRunning):
            print("\r\n\r\nAborting current john wordlist/rule\r\nIf another wordlist is available, cracking will continue.\r\n")
        else:
            exit(0)
    except:
        exit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, handler)

    print("___________________________________________________________")
    print("       _ __             __         __               ")
    print("      (_) /______      / /_  ___  / /___  ___  _____")
    print(r"     / / __/ ___/_____/ __ \/ _ \/ / __ \/ _ \/ ___/")
    print("    / / /_/ /  /_____/ / / /  __/ / /_/ /  __/ /    ")
    print(r" __/ /\__/_/        /_/ /_/\___/_/ .___/\___/_/     ")
    print("/___/                           /_/                 ")
    print("\r\njtr-helper 1.35")
    print("Ensure Configurations are set for jtr-helper.py")
    print("    set values for: johnConf, johnLocalConf, jtrLocation\r\n")
    print("                 __             ")
    print("    ____  ____  / /_____  __  __")
    print(r"   / __ \/ __ \/ __/ __ \/ / / /")
    print("  / /_/ / /_/ / /_/ /_/ / /_/ / ")
    print(r" / .___/\____/\__/ .___/\__, /  ")
    print("/_/             /_/    /____/   ")
    print("\r\npotpy (parallel+cache) integration + potfile-change rebuild")
    print("___________________________________________________________\n\n")

    parser = argparse.ArgumentParser()
    parser.add_argument("-b", "--build", help="build the master wordlist first and exit", action='store_const', const=True)
    parser.add_argument("-s", "--script", help="build the master wordlist first, then run cracking", action='store_const', const=True)

    parser.add_argument("-f", "--format", help="specify the jtr hash format")
    parser.add_argument("-w", "--wordlist", help="specify the file with wordlist")
    parser.add_argument("-r", "--recursive", help="used with wordlists if a directory is defined: -w /wordlistDIR/*", action='store_const', const=True)
    parser.add_argument("-hash", "--hashes", help="specify the file with hashes")
    parser.add_argument("-min", "--minlength", help="specify the min-length")
    parser.add_argument("-max", "--maxlength", help="specify the max-length")
    parser.add_argument("-session", "--session", help="specify the session")
    parser.add_argument("--no-post-rebuild", action="store_true", help="do not rebuild after cracking even if potfiles changed")

    args = parser.parse_args()

    # Build-only: build first then exit
    if args.build:
        ok = updateShell()
        sys.exit(0 if ok else 1)

    # Validate cracking args for normal/script mode
    if args.format and args.wordlist and args.hashes:
        hashFormats = args.format
        wordlist = args.wordlist
        hashFile = args.hashes
        isChained = False  # chained mode omitted here; can be re-enabled if needed
        if (args.recursive is None and "/*" in args.wordlist):
            print("You must specify a wordlist file. \r\n* can not be used without the -r option for wordlist.\r\nPlease correct: " + args.wordlist)
            exit()
        elif (args.recursive and "/*" in args.wordlist):
            isWordlists = True
        else:
            isWordlists = False
        ruleList = []
    else:
        parser.print_help()
        exit()

    if (args.minlength is None):
        minlength='8'
    else:
        minlength=args.minlength

    if (args.maxlength is None):
        maxlength='24'
    else:
        maxlength=args.maxlength

    if (minlength.isnumeric() == False):
        print("Please enter a number for minlength")
        exit()
    if (maxlength.isnumeric() == False):
        print("Please enter a number for maxlength")
        exit()

    if (args.session is None):
        jtrsession = setSessionIfNull()
    else:
        jtrsession=args.session

    # If -s/--script is present: build first, then run cracking.
    # Also rebuild at the end if potfiles changed, unless --no-post-rebuild is set.
    build_first = True if args.script else False
    rebuild_on_crack = False if args.no_post_rebuild else True

    main(build_first, rebuild_on_crack)
