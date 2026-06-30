#!/usr/bin/env python3
"""
crucible boxpaths — the box resolver.

Turns a box folder (boxes/<nick>/) into concrete connection / build / lock / wake
commands, reading the contract trio:

    connection.json   how to reach the box   {host,user,ssh_key_path,ssh_opts,remote_root,remote_engine_dir}
    hardware.json     what the box is        gpu{present,arch,...}, power_control{wol_mac,wol_iface,wol_helper}
    campaign.json     the current window     (not read here; the relauncher owns it)

The point: NO prompt or script ever hardcodes a host, build dir, lock path, or wake
method. Everything routes through here, so the whole apparatus is box-agnostic — point
it at a different box folder and every command re-resolves.

Usage (box folder is argv[1]):
    boxpaths.py boxes/<nick> --ssh                 # SSH invocation PREFIX (append a remote cmd)
    boxpaths.py boxes/<nick> --build               # full remote path to the build bin dir (GPU if present)
    boxpaths.py boxes/<nick> --build --cpu         # force the CPU build bin dir
    boxpaths.py boxes/<nick> --lock-path           # target-side box-lock path
    boxpaths.py boxes/<nick> --wake                # EXECUTE an OS-appropriate wake (WoL)
    boxpaths.py boxes/<nick> --wake --dry-run      # print the wake command without running it

Example shapes (all values come from the box's contracts, never from this file):
    --ssh        -> ssh -i <ssh_key_path> <ssh_opts> <user>@<host>
    --build      -> <remote_engine_dir>/build-cuda<NN>/bin   (NN from hardware.json gpu.arch sm_NN)
    --build --cpu-> <remote_engine_dir>/build/bin
    --lock-path  -> <remote_root>/work/.box.lock
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys


def _die(msg: str, code: int = 2) -> "None":
    print(f"boxpaths: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _load_json(box: str, name: str, required: bool = True) -> dict:
    path = os.path.join(box, name)
    if not os.path.isfile(path):
        if required:
            _die(f"missing {name} in {box} (the contract trio: connection/hardware/campaign)")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _die(f"cannot read {path}: {e}")
    return {}


def _ssh_prefix(conn: dict) -> str:
    """The SSH invocation prefix; callers append the remote command."""
    key = conn.get("ssh_key_path")
    user = conn.get("user")
    host = conn.get("host")
    if not (user and host):
        _die("connection.json needs at least {user, host}")
    parts = ["ssh"]
    if key:
        parts += ["-i", key]
    opts = conn.get("ssh_opts", "")
    if opts:
        parts.append(opts)               # e.g. "-o IdentitiesOnly=yes" (kept verbatim)
    parts.append(f"{user}@{host}")
    return " ".join(parts)


def _cuda_tag(gpu: dict) -> str | None:
    """Derive the build-cuda<NN> suffix from gpu.arch (e.g. 'sm_50' -> 'cuda50')."""
    arch = str(gpu.get("arch", ""))
    m = re.search(r"sm_(\d+)", arch)
    if m:
        return f"cuda{m.group(1)}"
    # fall back to a bare 'compute capability X.Y' phrasing
    m = re.search(r"compute capability\s*(\d+)\.(\d+)", arch)
    if m:
        return f"cuda{m.group(1)}{m.group(2)}"
    return None


def _build_dir(conn: dict, hw: dict, cpu: bool) -> str:
    engine = conn.get("remote_engine_dir")
    if not engine:
        _die("connection.json needs remote_engine_dir")
    gpu = hw.get("gpu", {}) or {}
    if not cpu and gpu.get("present"):
        tag = _cuda_tag(gpu)
        if tag:
            return f"{engine}/build-{tag}/bin"
        # GPU present but no usable arch tag -> warn, fall through to CPU build
        print("boxpaths: gpu.present but no sm_NN arch tag; using CPU build", file=sys.stderr)
    return f"{engine}/build/bin"


def _lock_path(conn: dict) -> str:
    root = conn.get("remote_root")
    if not root:
        _die("connection.json needs remote_root")
    return f"{root}/work/.box.lock"


def _wake(box: str, hw: dict, dry_run: bool) -> int:
    """Execute an OS-appropriate wake. PowerShell helper on a Windows-reachable host,
    else wakeonlan/etherwake from power_control. Degrade clearly if no method exists."""
    pc = hw.get("power_control", {}) or {}
    helper = pc.get("wol_helper")
    mac = pc.get("wol_mac")
    iface = pc.get("wol_iface")

    # 1) A recorded .ps1 helper, runnable via powershell.exe (Windows host or WSL with interop).
    if helper and str(helper).endswith(".ps1"):
        helper_path = helper if os.path.isabs(helper) else os.path.join(box, helper)
        pwsh = shutil.which("powershell.exe") or shutil.which("pwsh") or shutil.which("powershell")
        if pwsh and os.path.isfile(helper_path):
            cmd = [pwsh, "-ExecutionPolicy", "Bypass", "-File", helper_path]
            print("wake: " + " ".join(cmd))
            if dry_run:
                return 0
            return subprocess.run(cmd).returncode
        if not os.path.isfile(helper_path):
            print(f"boxpaths: wol_helper recorded ({helper}) but not found at {helper_path}", file=sys.stderr)
        elif not pwsh:
            print("boxpaths: wol_helper is a .ps1 but no powershell found; trying wakeonlan", file=sys.stderr)

    # 2) wakeonlan / etherwake using the MAC (and iface for etherwake).
    if mac:
        won = shutil.which("wakeonlan")
        if won:
            cmd = [won, mac]
            print("wake: " + " ".join(cmd))
            if dry_run:
                return 0
            return subprocess.run(cmd).returncode
        ew = shutil.which("etherwake")
        if ew:
            cmd = [ew] + (["-i", iface] if iface else []) + [mac]
            print("wake: " + " ".join(cmd))
            if dry_run:
                return 0
            return subprocess.run(cmd).returncode
        print(f"boxpaths: have wol_mac {mac} but no wakeonlan/etherwake on this host", file=sys.stderr)

    print("boxpaths: no usable wake method (need a .ps1 helper + powershell, or "
          "wol_mac + wakeonlan/etherwake). Wake the box manually.", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="crucible box resolver")
    ap.add_argument("box", help="path to the box folder (boxes/<nick>)")
    ap.add_argument("--ssh", action="store_true", help="print the SSH invocation prefix")
    ap.add_argument("--build", action="store_true", help="print the remote build bin dir")
    ap.add_argument("--cpu", action="store_true", help="with --build: force the CPU build")
    ap.add_argument("--lock-path", action="store_true", dest="lock_path", help="print the target box-lock path")
    ap.add_argument("--wake", action="store_true", help="execute an OS-appropriate wake")
    ap.add_argument("--dry-run", action="store_true", help="with --wake: print the command, do not run it")
    a = ap.parse_args(argv)

    box = a.box.rstrip("/")
    if not os.path.isdir(box):
        _die(f"box folder not found: {box}")

    actions = sum(bool(x) for x in (a.ssh, a.build, a.lock_path, a.wake))
    if actions != 1:
        _die("pick exactly one of --ssh / --build / --lock-path / --wake")

    if a.ssh:
        print(_ssh_prefix(_load_json(box, "connection.json")))
    elif a.build:
        print(_build_dir(_load_json(box, "connection.json"),
                         _load_json(box, "hardware.json"), a.cpu))
    elif a.lock_path:
        print(_lock_path(_load_json(box, "connection.json")))
    elif a.wake:
        return _wake(box, _load_json(box, "hardware.json"), a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
