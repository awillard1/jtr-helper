#!/usr/bin/env python3
"""
potpy.py — fast potfile password extractor & master list builder

Features:
 - Extracts plain passwords from hashcat/john pot files (handles $HEX[...] decoding)
 - Single-file mode (extract to stdout or outfile)
 - Legacy-compatible shims: process_potfile(), main()
 - Merge modes:
    * --merge            : per-file decode -> merge (legacy path)
    * --merge-fast       : stream decoded lines to tuned `sort -u` (pipe-limited)
    * --merge-parallel   : recommended — parallel decode -> sort -u (with cache)
    * --use-jtr-unique   : dedup via John's `unique` (unsorted)
 - Persistent decode cache to skip unchanged potfiles
 - Automatic safe-retry for sort on OOM (SIGKILL) with progressive backoff
 - Copies final master to /mnt/c/PenTesting/data/wordlists/master.lst
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import queue
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List, Optional
import hashlib
import time

# -------- CONFIGURATION (edit as needed) --------
directory = "/mnt/c/PenTesting/data/potfiles/"
potfiles: List[str] = []

# Collect files in directory
if os.path.isdir(directory):
    for filename in os.listdir(directory):
        full_path = os.path.join(directory, filename)
        if os.path.isfile(full_path):
            potfiles.append(full_path)

# Additional known potfiles (edit for your environment)
potfiles.extend([
    "/mnt/c/PenTesting/data/hashcat-6.2.6/master.txt",
    "/mnt/c/PenTesting/hashcat.potfile",
    "/mnt/c/PenTesting/data/hashcat-6.2.6/hashcat.potfile",
    "/home/willard/src/john/run/john.pot",
])

wordlist_dir = "/home/willard/wordlists/"
finalFileName = "master.lst"
# ------------------------------------------------

# ---------------- Utility / decoding ----------------
def decode_hashcat_hexstring(hexstring: str) -> str:
    """Decode $HEX[....] payload to latin-1 string; return original on failure."""
    try:
        start = hexstring.find('[') + 1
        end = hexstring.rfind(']')
        if start <= 0 or end <= start:
            return hexstring
        hexdata = hexstring[start:end]
        return bytes.fromhex(hexdata).decode('latin-1', errors='replace')
    except Exception:
        return hexstring

def decode_hashcat_hex(data: str) -> str:
    """Repeatedly decode $HEX[...] occurrences until no longer present."""
    max_iters = 10
    i = 0
    while data.startswith('$HEX[') and ']' in data and i < max_iters:
        decoded = decode_hashcat_hexstring(data)
        if decoded == data:
            break
        data = decoded
        i += 1
    return data

def extract_password_from_line(line: str) -> str:
    """Split on FIRST colon; return password part (handles JtR & hashcat pot formats)."""
    raw = line.rstrip('\r\n')
    left, sep, right = raw.partition(':')
    return right if sep else left

# ---------- Robust tmpdir preparation ----------
def _prepare_tmpdir(tmpdir: Optional[str], verbose: bool=False) -> Optional[str]:
    """
    Ensure a tmpdir exists and is writable. Returns a usable path or None.
    Falls back to system temp if the requested path is not usable.
    """
    def usable(path: str) -> bool:
        try:
            os.makedirs(path, exist_ok=True)
            testfile = os.path.join(path, ".potpy_write_test")
            with open(testfile, "w") as f:
                f.write("x")
            os.remove(testfile)
            return True
        except Exception:
            return False

    if tmpdir and usable(tmpdir):
        if verbose:
            print(f"[+] Using tmpdir: {tmpdir}")
        return tmpdir

    sys_tmp = tempfile.gettempdir()
    if usable(sys_tmp):
        if verbose and tmpdir:
            print(f"[!] tmpdir '{tmpdir}' not usable; falling back to system tmp: {sys_tmp}")
        elif verbose:
            print(f"[+] Using system tmpdir: {sys_tmp}")
        return sys_tmp

    if verbose:
        print("[!] No usable tmpdir found; proceeding without -T.")
    return None

# --------------- Per-file processing ----------------
def process_potfile_to_out(potfile_path: str, outfile_path: str, verbose: bool=False) -> None:
    """Read potfile line-by-line, decode $HEX, and write password per line to outfile."""
    if verbose:
        print(f"[+] Processing: {potfile_path} -> {outfile_path}")
    os.makedirs(os.path.dirname(outfile_path), exist_ok=True)
    with open(potfile_path, 'r', encoding='latin-1', errors='replace') as fh_in, \
         open(outfile_path, 'w', encoding='latin-1', errors='replace') as fh_out:
        for raw_line in fh_in:
            if not raw_line:
                continue
            pw = extract_password_from_line(raw_line)
            if pw.startswith('$HEX['):
                pw = decode_hashcat_hex(pw)
            fh_out.write(pw + '\n')

# -------------- Iterators for streaming --------------
def _iter_passwords_from_file(potfile_path: str):
    """Yield decoded passwords from potfile one-by-one (latin-1)."""
    with open(potfile_path, 'r', encoding='latin-1', errors='replace') as fh:
        for raw_line in fh:
            if not raw_line:
                continue
            pw = extract_password_from_line(raw_line)
            if pw.startswith('$HEX['):
                pw = decode_hashcat_hex(pw)
            yield pw

# --------------- Merge helpers -----------------------
def merge_files_unique_sorted(file_paths: Iterable[str], dest_path: str, verbose: bool=False) -> None:
    """
    Merge several files into dest_path, producing unique, sorted lines.
    Prefers system sort -u; falls back to Python-set if not available.
    """
    file_list = [p for p in file_paths if p and os.path.isfile(p)]
    if not file_list:
        open(dest_path, 'w').close()
        return

    sort_exe = shutil.which('sort')
    if sort_exe:
        cmd = [sort_exe, '-u', '-o', dest_path] + file_list
        env = os.environ.copy()
        env['LC_ALL'] = 'C'
        try:
            if verbose:
                print(f"[+] Running: {' '.join(cmd)}")
            subprocess.check_call(cmd, env=env)
            return
        except subprocess.CalledProcessError as e:
            if verbose:
                print("[!] system sort failed, falling back to Python unique merge:", e)

    # Python fallback (may use a lot of memory)
    if verbose:
        print("[+] Falling back to Python-based unique/merge (memory-heavy).")
    unique = set()
    for fp in file_list:
        with open(fp, 'r', encoding='latin-1', errors='replace') as fh:
            for ln in fh:
                unique.add(ln.rstrip('\n'))
    with open(dest_path, 'w', encoding='latin-1', errors='replace') as out:
        for ln in sorted(unique):
            out.write(ln + '\n')

# ------------- Copy final to legacy dest -------------
def _copy_master_to_legacy(final_name: str, verbose: bool=False) -> None:
    """Copy the final master list to the legacy destination."""
    dest_copy = "/mnt/c/PenTesting/data/wordlists/master.lst"
    try:
        os.makedirs(os.path.dirname(dest_copy), exist_ok=True)
        shutil.copy2(final_name, dest_copy)
        if verbose:
            print(f"[+] Copied merged master to {dest_copy}")
    except Exception as e:
        if verbose:
            print(f"[!] Could not copy merged master to {dest_copy}: {e}")

# ------------- Fast streaming merge to sort -------------
def merge_streaming_with_sort(
    file_paths: Iterable[str],
    dest_path: str,
    tmpdir: Optional[str]=None,
    parallel: Optional[int]=None,
    mem: str='60%',
    verbose: bool=False
) -> str:
    """
    Stream decoded passwords from all files into a tuned `sort -u`.
    Returns the destination path.
    Note: can be limited by Python's single stdin writer; see --merge-parallel for faster path.
    """
    file_list = [p for p in file_paths if p and os.path.isfile(p)]
    if not file_list:
        open(dest_path, 'w').close()
        return dest_path

    sort_exe = shutil.which('sort')
    if not sort_exe:
        if verbose:
            print("[!] `sort` not found; falling back to merge_files_unique_sorted()")
        merge_files_unique_sorted(file_list, dest_path, verbose=verbose)
        return dest_path

    if parallel is None:
        parallel = max(1, multiprocessing.cpu_count())

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    cmd = [sort_exe, '-u', '-o', dest_path, '-S', str(mem), f'--parallel={parallel}']

    # Validate tmpdir; fallback to system tmp if unusable
    real_tmpdir = _prepare_tmpdir(tmpdir, verbose=verbose)
    if real_tmpdir:
        cmd.extend(['-T', real_tmpdir])

    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'

    if verbose:
        print(f"[+] Running: {' '.join(cmd)}")
        if real_tmpdir:
            print(f"[+] sort temp dir: {real_tmpdir}")

    # Launch the sort process with stdin as pipe (text mode)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=env, text=True, bufsize=1)

    q = queue.Queue(maxsize=20000)
    sentinel = object()

    def writer():
        try:
            while True:
                it = q.get()
                if it is sentinel:
                    break
                proc.stdin.write(it)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    wthr = threading.Thread(target=writer, daemon=True)
    wthr.start()

    def produce(path: str):
        count = 0
        for pw in _iter_passwords_from_file(path):
            q.put(pw + '\n')
            count += 1
        if verbose:
            print(f"[+] {os.path.basename(path)} -> {count:,} lines")

    with ThreadPoolExecutor(max_workers=min(len(file_list), parallel)) as ex:
        futures = [ex.submit(produce, p) for p in file_list]
        for f in futures:
            f.result()

    q.put(sentinel)
    wthr.join()

    ret = proc.wait()
    if ret != 0:
        raise RuntimeError(f"`sort` exited with code {ret}")

    if verbose:
        print(f"[+] Wrote: {dest_path}")
    return dest_path

# -------------- Parallel decode → sort -u (recommended) --------------
def _decode_file_to_tmp(pf_path: str, tmpdir: str, verbose: bool=False) -> str:
    """
    Decode a single potfile -> temp decoded file (latin-1).
    Returns path to the temp file.
    """
    base = os.path.basename(pf_path)
    fd, out_path = tempfile.mkstemp(prefix=f"potpy_{base}_", suffix=".decoded", dir=tmpdir, text=True)
    os.close(fd)
    count = 0
    with open(pf_path, 'r', encoding='latin-1', errors='replace') as fh_in, \
         open(out_path, 'w', encoding='latin-1', errors='replace') as out:
        for raw_line in fh_in:
            if not raw_line:
                continue
            pw = extract_password_from_line(raw_line)
            if pw.startswith('$HEX['):
                pw = decode_hashcat_hex(pw)
            out.write(pw + '\n')
            count += 1
    if verbose:
        print(f"[+] Decoded {base}: {count:,} lines -> {out_path}")
    return out_path

def _decode_file_to_tmp_worker(args):
    """Top-level worker for multiprocessing (picklable)."""
    pf_path, tmpdir, verbose = args
    return _decode_file_to_tmp(pf_path, tmpdir, verbose)

# --------- Cache helpers for parallel fast path ----------
def _default_cache_dir() -> str:
    """Return default cache dir like ~/.cache/potpy/decoded"""
    base = os.path.join(os.path.expanduser("~"), ".cache", "potpy", "decoded")
    os.makedirs(base, exist_ok=True)
    return base

def _file_signature(path: str) -> str:
    """
    Build a stable signature based on abs path, mtime_ns, size.
    Fast and sufficient to detect changes.
    """
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()

def _cached_decoded_path(cache_dir: str, signature: str) -> str:
    return os.path.join(cache_dir, f"{signature}.decoded")

def _has_valid_cache(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except Exception:
        return False

def _decode_file_to_path(pf_path: str, out_path: str, verbose: bool=False) -> str:
    """
    Decode pf_path directly into out_path (atomic write).
    """
    tmp = out_path + ".tmp-" + str(os.getpid()) + "-" + str(int(time.time()*1000))
    count = 0
    with open(pf_path, 'r', encoding='latin-1', errors='replace') as fh_in, \
         open(tmp, 'w', encoding='latin-1', errors='replace') as out:
        for raw_line in fh_in:
            if not raw_line:
                continue
            pw = extract_password_from_line(raw_line)
            if pw.startswith('$HEX['):
                pw = decode_hashcat_hex(pw)
            out.write(pw + '\n')
            count += 1
    os.replace(tmp, out_path)  # atomic on same fs
    if verbose:
        base = os.path.basename(pf_path)
        print(f"[+] Decoded {base}: {count:,} lines -> {out_path}")
    return out_path

def _decode_file_to_specific_worker(args):
    """Top-level worker so multiprocessing can pickle it."""
    pf_path, out_path, verbose = args
    # If another process created it while we queued: reuse
    if _has_valid_cache(out_path):
        return out_path
    return _decode_file_to_path(pf_path, out_path, verbose)

def _gc_cache(cache_dir: str, older_than_days: int, verbose: bool=False) -> int:
    """
    Remove cache files older than N days. Returns count removed.
    Only touches *.decoded files in cache_dir.
    """
    if older_than_days <= 0:
        return 0
    now = time.time()
    cutoff = now - older_than_days * 86400
    removed = 0
    for name in os.listdir(cache_dir):
        if not name.endswith(".decoded"):
            continue
        p = os.path.join(cache_dir, name)
        try:
            st = os.stat(p)
            if st.st_mtime < cutoff:
                os.remove(p)
                removed += 1
        except Exception:
            pass
    if verbose and removed:
        print(f"[+] GC: removed {removed} old cache files from {cache_dir}")
    return removed

# -------------- Sort-run helper with retries on SIGKILL (OOM) --------------
def _run_sort_with_retries(decoded_files: List[str],
                           final_name: str,
                           mem: str,
                           parallel: int,
                           tmpdir: Optional[str],
                           env: dict,
                           verbose: bool=False,
                           max_retries: int=4) -> None:
    """
    Run GNU sort -u with retries on SIGKILL (OOM).
    Each retry reduces memory and parallelism.
    """
    sort_exe = shutil.which('sort')
    if not sort_exe:
        raise RuntimeError("GNU sort not found in PATH.")

    # choose usable tmpdir
    use_tmp = tmpdir if tmpdir and os.path.isdir(tmpdir) and os.access(tmpdir, os.W_OK) else None

    cur_mem = str(mem)
    try:
        cur_par = max(1, int(parallel))
    except Exception:
        cur_par = max(1, multiprocessing.cpu_count())

    attempt = 0
    while True:
        attempt += 1
        cmd = [sort_exe, '-u', '-o', final_name, '-S', str(cur_mem), f'--parallel={cur_par}']
        if use_tmp:
            cmd.extend(['-T', use_tmp])
        cmd.extend(decoded_files)

        if verbose:
            print(f"[+] Attempt {attempt}: {' '.join(cmd)}")

        try:
            subprocess.check_call(cmd, env=env)
            if verbose:
                print(f"[+] sort succeeded with -S {cur_mem} --parallel={cur_par}")
            return
        except subprocess.CalledProcessError as e:
            rc = e.returncode
            # Negative rc => killed by signal (e.g., -9 == SIGKILL)
            if rc is not None and rc < 0:
                sig = -rc
                if verbose:
                    print(f"[!] sort died by signal {sig} (rc={rc}).")
                if sig == 9 and attempt <= max_retries:
                    # Back off: reduce mem and parallel
                    # If mem is percent, halve it (min 5%)
                    if isinstance(cur_mem, str) and cur_mem.endswith('%'):
                        try:
                            pct = max(5, int(cur_mem[:-1]) // 2)
                            cur_mem = f"{pct}%"
                        except Exception:
                            cur_mem = "1G"
                    else:
                        # If mem like "8G", halve; else fallback to 1G
                        s = str(cur_mem).lower()
                        try:
                            if s.endswith('g'):
                                val = max(1, int(float(s[:-1]) // 2) or 1)
                                cur_mem = f"{val}G"
                            else:
                                cur_mem = "1G"
                        except Exception:
                            cur_mem = "1G"
                    cur_par = max(1, cur_par // 2)
                    if verbose:
                        print(f"[!] Retrying sort with -S {cur_mem} --parallel={cur_par}")
                    continue
            # non-signal failure or out of retries: re-raise
            raise

def build_master_parallel(final_local: Optional[str]=None,
                          tmpdir: Optional[str]=None,
                          mem: str='60%',
                          parallel: Optional[int]=None,
                          verbose: bool=False,
                          cache_dir: Optional[str]=None,
                          no_cache: bool=False,
                          gc_cache_days: int=0) -> str:
    """
    Fast path with caching:
      - Map each potfile to a cache path by (path, mtime_ns, size) signature.
      - Decode only those not present in cache.
      - sort -u across cached decoded files (with safe-retry).
    """
    file_list = [p for p in potfiles if p and os.path.isfile(p)]
    os.makedirs(wordlist_dir, exist_ok=True)
    final_name = final_local if final_local else os.path.join(wordlist_dir, finalFileName)

    if not file_list:
        open(final_name, 'w').close()
        _copy_master_to_legacy(final_name, verbose=verbose)
        return final_name

    sort_exe = shutil.which('sort')
    if parallel is None:
        parallel = max(1, multiprocessing.cpu_count())

    # Prepare tmpdir for sort
    real_tmpdir = _prepare_tmpdir(tmpdir, verbose=verbose) or tempfile.gettempdir()
    os.makedirs(real_tmpdir, exist_ok=True)

    # Prepare cache dir
    if no_cache:
        cache_dir = None
    else:
        cache_dir = cache_dir or _default_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        if gc_cache_days > 0:
            _gc_cache(cache_dir, gc_cache_days, verbose=verbose)

    # Map inputs -> decoded outputs (cache-aware)
    tasks = []          # items needing decode
    decoded_files = []  # all decoded files (cached or newly produced)
    if cache_dir:
        for pf in file_list:
            sig = _file_signature(pf)
            out_path = _cached_decoded_path(cache_dir, sig)
            if _has_valid_cache(out_path):
                if verbose:
                    print(f"[=] Cache hit: {os.path.basename(pf)} -> {out_path}")
                decoded_files.append(out_path)
            else:
                tasks.append((pf, out_path, verbose))
        if verbose:
            print(f"[+] Cache stats: hits={len(decoded_files)} misses={len(tasks)}")
    else:
        # No cache: decode to temp dir each time
        for pf in file_list:
            out_path = os.path.join(real_tmpdir, f"nocache_{os.path.basename(pf)}_{os.getpid()}.decoded")
            tasks.append((pf, out_path, verbose))

    # Decode misses in parallel
    if tasks:
        ctx = multiprocessing.get_context("fork") if hasattr(multiprocessing, "get_context") else multiprocessing
        with ctx.Pool(processes=min(len(tasks), parallel)) as pool:
            for out_path in pool.imap_unordered(_decode_file_to_specific_worker, tasks, chunksize=1):
                decoded_files.append(out_path)

    # If sort is available, let it merge in parallel with safe retry logic
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'

    if sort_exe:
        # Use the retry wrapper which progressively reduces memory/parallel if sort gets SIGKILL'd
        _run_sort_with_retries(
            decoded_files=decoded_files,
            final_name=final_name,
            mem=mem,
            parallel=parallel,
            tmpdir=real_tmpdir,
            env=env,
            verbose=verbose,
            max_retries=4
        )
    else:
        if verbose:
            print("[!] `sort` not found; using Python merge.")
        merge_files_unique_sorted(decoded_files, final_name, verbose=verbose)

    # Cleanup temporary decoded files if we were in no-cache mode
    if cache_dir is None:
        for f in decoded_files:
            try: os.remove(f)
            except Exception: pass

    if verbose:
        print(f"[+] Wrote: {final_name}")
    _copy_master_to_legacy(final_name, verbose=verbose)
    return final_name

# --------------- John's unique fallback ----------------
def build_master_with_jtr_unique(final_local: Optional[str]=None, tmpdir: Optional[str]=None, verbose: bool=False) -> str:
    """
    Use John's 'unique' binary for fast deduplication (unsorted).
    Requires 'unique' in PATH.
    """
    unique_exe = shutil.which('unique')
    if not unique_exe:
        raise RuntimeError("John's 'unique' binary not found in PATH.")

    os.makedirs(wordlist_dir, exist_ok=True)
    final_name = final_local if final_local else os.path.join(wordlist_dir, finalFileName)

    real_tmpdir = _prepare_tmpdir(tmpdir, verbose=verbose) or tempfile.gettempdir()
    os.makedirs(real_tmpdir, exist_ok=True)
    tmp_in = os.path.join(real_tmpdir, f"potpy_decode_{os.getpid()}.lst")

    with open(tmp_in, 'w', encoding='latin-1', errors='replace') as out:
        for pf in potfiles:
            if not pf or not os.path.isfile(pf):
                if verbose:
                    print(f"[!] skipping missing potfile: {pf}")
                continue
            for pw in _iter_passwords_from_file(pf):
                out.write(pw + '\n')

    cmd = [unique_exe, final_name, tmp_in]
    if verbose:
        print(f"[+] Running: {' '.join(cmd)}")
    subprocess.check_call(cmd)
    try:
        os.remove(tmp_in)
    except Exception:
        pass

    if verbose:
        print(f"[+] Wrote: {final_name}")
    _copy_master_to_legacy(final_name, verbose=verbose)
    return final_name

# --------------- High-level build functions ----------------
def build_master_from_configured_potfiles(final_local: Optional[str]=None, verbose: bool=False) -> str:
    """
    Legacy-style: produce outputs for each configured potfile, then merge using sort/uniq.
    Preserves original behavior but may be slower due to intermediate files.
    """
    os.makedirs(wordlist_dir, exist_ok=True)
    processed_files = []
    for idx, pf in enumerate(potfiles):
        if not pf or not os.path.isfile(pf):
            if verbose:
                print(f"[!] skipping missing configured potfile: {pf}")
            continue
        basename = os.path.basename(pf)
        outname = os.path.join(wordlist_dir, f"{idx:03d}-{basename}-potpy.out")
        process_potfile_to_out(pf, outname, verbose=verbose)
        processed_files.append(outname)

    if not processed_files:
        raise RuntimeError("No potfiles were processed (check configuration).")

    final_name = final_local if final_local else os.path.join(wordlist_dir, finalFileName)
    merge_files_unique_sorted(processed_files, final_name, verbose=verbose)

    # cleanup
    for pf in processed_files:
        try:
            os.remove(pf)
        except Exception:
            pass

    if verbose:
        print(f"[+] Wrote: {final_name}")
    _copy_master_to_legacy(final_name, verbose=verbose)
    return final_name

def build_master_fast(final_local: Optional[str]=None, tmpdir: Optional[str]=None, mem: str='60%', parallel: Optional[int]=None, verbose: bool=False) -> str:
    """
    Streaming fast path: decode all configured potfiles into a single tuned `sort -u` via stdin.
    Often fast, but can be slower than --merge-parallel for very large inputs.
    """
    os.makedirs(wordlist_dir, exist_ok=True)
    final_name = final_local if final_local else os.path.join(wordlist_dir, finalFileName)
    out = merge_streaming_with_sort(
        potfiles,
        final_name,
        tmpdir=tmpdir,
        parallel=parallel,
        mem=mem,
        verbose=verbose
    )
    _copy_master_to_legacy(final_name, verbose=verbose)
    return out

# ---------------- Legacy API shims ----------------
def process_potfile():
    """
    Legacy entrypoint expected by older scripts (e.g., jtr-helper.py).
    Processes the configured potfiles list and writes the merged master file.
    Returns the path to the merged file.
    """
    return build_master_from_configured_potfiles(verbose=False)

def main():
    """Legacy main wrapper delegating to CLI."""
    return main_cli()

# ---------------- CLI ----------------
def parse_args():
    p = argparse.ArgumentParser(description="Extract plain passwords from hashcat/JTR pot files and build a master list.")
    p.add_argument('-f', '--filename', help="Single potfile to process")
    p.add_argument('-o', '--outfile', help="Write output to file (for single -f). If omitted, prints to stdout.")
    p.add_argument('--merge', action='store_true', help="Process configured potfiles and build master merged list (legacy path).")
    p.add_argument('--merge-fast', action='store_true', help="Stream to tuned sort -u (no per-file temps).")
    p.add_argument('--merge-parallel', action='store_true', help="Parallel decode to temp files, then sort -u them (usually fastest).")
    p.add_argument('--use-jtr-unique', action='store_true', help="Use John's 'unique' tool for fast dedup (unsorted).")
    p.add_argument('--final', help="Optional override path for merged final output (when using merge modes).")
    p.add_argument('--tmpdir', help="Temporary directory for sort/unique (fast NVMe recommended).")
    p.add_argument('--mem', default='60%', help="Sort buffer size (e.g., '8G' or '60%').")
    p.add_argument('--parallel', type=int, help="Override parallelism for sort/producers.")
    # Cache flags (for --merge-parallel)
    p.add_argument('--cache-dir', help="Directory to store/reuse decoded cache files (default: ~/.cache/potpy/decoded).")
    p.add_argument('--no-cache', action='store_true', help="Disable cache; always decode fresh.")
    p.add_argument('--gc-cache-days', type=int, default=0, help="Garbage-collect cache files older than N days.")
    p.add_argument('-v', '--verbose', action='store_true', help="Verbose progress output")
    return p.parse_args()

def process_single_file_cli(input_path: str, output_path: Optional[str], verbose: bool=False) -> str:
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input potfile not found: {input_path}")

    if output_path:
        process_potfile_to_out(input_path, output_path, verbose=verbose)
        return output_path
    else:
        with open(input_path, 'r', encoding='latin-1', errors='replace') as fh:
            for raw_line in fh:
                pw = extract_password_from_line(raw_line)
                if pw.startswith('$HEX['):
                    pw = decode_hashcat_hex(pw)
                print(pw)
        return ""

def main_cli():
    args = parse_args()

    # Preferred fast path (with cache)
    if args.merge_parallel:
        outpath = build_master_parallel(final_local=args.final,
                                        tmpdir=args.tmpdir,
                                        mem=args.mem,
                                        parallel=args.parallel,
                                        verbose=args.verbose,
                                        cache_dir=args.cache_dir,
                                        no_cache=args.no_cache,
                                        gc_cache_days=args.gc_cache_days)
        if args.verbose:
            print(f"[+] Merge complete -> {outpath}")
        return

    # Streaming fast path
    if args.merge_fast:
        outpath = build_master_fast(final_local=args.final,
                                    tmpdir=args.tmpdir,
                                    mem=args.mem,
                                    parallel=args.parallel,
                                    verbose=args.verbose)
        if args.verbose:
            print(f"[+] Merge complete -> {outpath}")
        return

    # John's unique path
    if args.use_jtr_unique:
        outpath = build_master_with_jtr_unique(final_local=args.final,
                                               tmpdir=args.tmpdir,
                                               verbose=args.verbose)
        if args.verbose:
            print(f"[+] Merge complete -> {outpath}")
        return

    # Legacy merge
    if args.merge:
        outpath = build_master_from_configured_potfiles(final_local=args.final, verbose=args.verbose)
        if args.verbose:
            print(f"[+] Merge complete -> {outpath}")
        return

    # single-file mode
    if not args.filename:
        print("Please specify -f /path/to/potfile or use --merge / --merge-fast / --merge-parallel.")
        sys.exit(2)

    try:
        result = process_single_file_cli(args.filename, args.outfile, verbose=args.verbose)
        if args.verbose and args.outfile:
            print(f"[+] Wrote output to {result}")
    except Exception as e:
        print("[!] Error:", e)
        sys.exit(1)

if __name__ == "__main__":
    main_cli()
