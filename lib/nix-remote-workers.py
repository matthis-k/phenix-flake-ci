#!/usr/bin/env python3
"""Ephemeral GitHub Actions jobs as Nix ssh-ng remote builders over Tailscale.

This is transport/provisioning only. Nix remains the single derivation scheduler.
Worker names are isolated by workflow run, attempt and pool size.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SYSTEMS = {"x86_64-linux", "aarch64-linux"}
COMPLETION = "/tmp/phenix-flake-ci-finished"
STARTED = "/tmp/phenix-flake-ci-started"
# GitHub's non-interactive SSH shells do not source /etc/profile.d/nix.sh.
# Use the absolute executable installed by cachix/install-nix-action.
REMOTE_DAEMON = "/nix/var/nix/profiles/default/bin/nix-daemon"
LOCAL_NIX = "/nix/var/nix/profiles/default/bin/nix"


def store_uri(ip):
    return f"ssh-ng://runner@{ip}?remote-program={REMOTE_DAEMON}"



def positive(value, minimum=1, maximum=16):
    n = int(value)
    if not minimum <= n <= maximum:
        raise ValueError(f"integer must be between {minimum} and {maximum}")
    return n


def worker_name(run_id, attempt, count, index):
    for value in (run_id, attempt, count, index):
        if type(value) is not int or value < 1:
            raise ValueError("run, attempt, count and worker index must be positive")
    if index > count or count > 16:
        raise ValueError("invalid worker index or worker count")
    name = f"fc-{run_id}-{attempt}-n{count}-w{index}"
    if len(name) > 63:
        raise ValueError("generated Tailscale hostname is too long")
    return name


def invoke(*command, timeout=15, check=True):
    return subprocess.run(list(command), text=True, capture_output=True,
                          timeout=timeout, check=check)


def matrix(args):
    count = positive(args.count)
    workers = json.dumps(list(range(1, count + 1)), separators=(",", ":"))
    print(f"workers={workers}")


def hostname(args):
    print(f"hostname={worker_name(args.run_id, args.attempt, args.count, args.index)}")


def peers(status):
    result = {}
    # Tailscale can report "Peer": null immediately after a successful
    # tailscale up, before its first peer snapshot has arrived. This is a
    # normal not-yet-ready state, not a failed authentication attempt.
    table = status.get("Peer") if isinstance(status, dict) else None
    if not isinstance(table, dict):
        return result
    for peer in table.values():
        if not isinstance(peer, dict):
            continue
        name = peer.get("HostName")
        ips = peer.get("TailscaleIPs")
        if isinstance(name, str) and isinstance(ips, list):
            ipv4 = next((ip for ip in ips if re.fullmatch(r"100\.\d{1,3}\.\d{1,3}\.\d{1,3}", ip)), None)
            if ipv4:
                result[name] = ipv4
    return result


def ssh_config(ips):
    hosts = " ".join(ips)
    config = (f"Host {hosts}\n"
              "    BatchMode yes\n"
              "    StrictHostKeyChecking accept-new\n"
              "    ConnectTimeout 5\n"
              "    UserKnownHostsFile /root/.ssh/known_hosts\n")
    invoke("sudo", "install", "-d", "-m", "0700", "/root/.ssh")
    subprocess.run(["sudo", "tee", "/etc/ssh/ssh_config.d/phenix-flake-ci.conf"],
                   input=config, text=True, stdout=subprocess.DEVNULL, check=True)


def discover(args):
    count = positive(args.count)
    if args.system not in SYSTEMS:
        raise ValueError("unsupported remote-build system")
    names = [worker_name(args.run_id, args.attempt, count, i)
             for i in range(1, count + 1)]
    end = time.monotonic() + positive(args.timeout, 1, 900)
    ready = {}
    discovered = {}
    last_errors = {}
    while time.monotonic() < end and len(ready) < count:
        status = json.loads(invoke("tailscale", "status", "--json").stdout)
        present = peers(status)
        candidates = {name: present[name] for name in names if name in present}
        discovered.update(candidates)
        if discovered:
            # Persist partial discovery so cleanup can release workers even
            # when one missing or misconfigured peer prevents acceptance.
            Path(args.hosts).write_text(json.dumps(discovered, sort_keys=True) + "\n")
            ssh_config(list(discovered.values()))
        for name, ip in candidates.items():
            if name in ready:
                continue
            try:
                # Prove the ssh-ng store protocol works before accepting a worker.
                invoke("sudo", LOCAL_NIX, "store", "ping", "--store",
                       store_uri(ip), timeout=12)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                if isinstance(exc, subprocess.TimeoutExpired):
                    reason = f"timeout after {exc.timeout}s"
                else:
                    reason = (exc.stderr or exc.stdout or
                              f"exit status {exc.returncode}").strip()[:1200]
                if last_errors.get(name) != reason:
                    print(f"Nix ssh-ng probe failed for {name}: {reason}", flush=True)
                    last_errors[name] = reason
                continue
            # Mark the worker as claimed only after proving both the Nix
            # remote-store protocol and its authenticated SSH control path.
            # Unclaimed workers self-retire after the bounded startup grace.
            try:
                invoke("sudo", "ssh", f"runner@{ip}", "touch", STARTED,
                       timeout=12)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                print(f"Worker startup lease failed for {name}: {exc}", flush=True)
                continue
            ready[name] = ip
            print(f"Builder ready: {name}", flush=True)
        if len(ready) != count:
            time.sleep(4)
    if len(ready) != count:
        raise RuntimeError(f"only {len(ready)} of {count} authenticated Nix builders became ready")
    machine_file = Path(args.machines).resolve()
    machine_file.parent.mkdir(parents=True, exist_ok=True)
    slots = positive(args.slots, 1, 32)
    # Format: URI system SSH-key maxJobs speedFactor supportedFeatures mandatoryFeatures
    machine_file.write_text("".join(
        f"{store_uri(ready[name])} {args.system} - {slots} 1 - -\n"
        for name in names
    ))
    Path(args.hosts).write_text(json.dumps(ready, sort_keys=True) + "\n")
    print(f"Configured {count} Nix remote builders ({slots} slots each)")


def build(args):
    machines = Path(args.machines).resolve()
    if not machines.is_file():
        raise ValueError("missing verified Nix machine list")
    env = os.environ.copy()
    env["NIX_CONFIG"] = (env.get("NIX_CONFIG", "") +
                         f"\nbuilders = @{machines}\nmax-jobs = 0\n"
                         "builders-use-substitutes = true\n")
    started = time.monotonic()
    try:
        command = ["python3", args.dag, "build", "--plan", args.plan,
                   "--graph", args.graph, "--output", args.evidence]
        if args.test_packages:
            command += ["--test-packages", args.test_packages,
                        "--test-root", args.test_root]
        subprocess.run(command, env=env, check=True)
    finally:
        output = Path(args.timing)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"wallSeconds": round(time.monotonic() - started, 2),
                                      "workerCount": len(machines.read_text().splitlines()),
                                      "mode": "remote-nix-ssh-ng"},
                                     sort_keys=True) + "\n")


def stop(args):
    path = Path(args.hosts)
    hosts = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(hosts, dict):
        raise ValueError("invalid remote worker host inventory")

    count = getattr(args, "count", None)
    expected = (set(worker_name(args.run_id, args.attempt, count, i)
                    for i in range(1, count + 1)) if count else set())
    # If discovery aborted before persisting a complete host list, the
    # already-running workers still need releasing. Resolve their exact
    # run/attempt identities through the current authenticated tailnet.
    # This is bounded; never issue SSH to unrelated peers.
    if expected and not expected.issubset(hosts):
        deadline = time.monotonic() + positive(args.timeout, 1, 180)
        while time.monotonic() < deadline and not expected.issubset(hosts):
            try:
                visible = peers(json.loads(invoke("tailscale", "status",
                                                 "--json").stdout))
                hosts.update({name: visible[name] for name in expected
                              if name in visible})
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                    json.JSONDecodeError):
                pass  # transient daemon/bootstrap state; retry below
            if not expected.issubset(hosts):
                time.sleep(3)

    errors = []
    targets = {name: ip for name, ip in hosts.items()
               if not expected or name in expected}
    for ip in sorted(set(targets.values())):
        try:
            invoke("sudo", "ssh", f"runner@{ip}", "touch", COMPLETION, timeout=15)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
    missing = sorted(expected - targets.keys())
    if missing or errors:
        raise RuntimeError(f"could not release workers: missing={missing}, "
                           f"ssh_failures={len(errors)}")


def hold(args):
    end = time.monotonic() + positive(args.timeout, 1, 3600)
    startup_end = time.monotonic() + positive(args.startup_timeout, 1, 900)
    claimed = False
    while time.monotonic() < end:
        if Path(COMPLETION).is_file():
            print("Remote worker released by coordinator")
            return
        claimed = claimed or Path(STARTED).is_file()
        if not claimed and time.monotonic() >= startup_end:
            raise RuntimeError("unclaimed remote worker startup lease expired")
        time.sleep(4)
    raise RuntimeError("coordinator did not release worker before deadline")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("matrix")
    m.add_argument("--count", type=int, required=True)
    m.set_defaults(func=matrix)
    h = sub.add_parser("hostname")
    for q in (h,):
        q.add_argument("--run-id", type=int, required=True)
        q.add_argument("--attempt", type=int, required=True)
        q.add_argument("--count", type=int, required=True)
        q.add_argument("--index", type=int, required=True)
    h.set_defaults(func=hostname)
    d = sub.add_parser("discover")
    d.add_argument("--run-id", type=int, required=True)
    d.add_argument("--attempt", type=int, required=True)
    d.add_argument("--count", type=int, required=True)
    d.add_argument("--slots", type=int, default=2)
    d.add_argument("--system", default="x86_64-linux")
    d.add_argument("--timeout", type=int, default=300)
    d.add_argument("--machines", required=True)
    d.add_argument("--hosts", required=True)
    d.set_defaults(func=discover)
    b = sub.add_parser("build")
    b.add_argument("--machines", required=True)
    b.add_argument("--dag", required=True)
    b.add_argument("--plan", required=True)
    b.add_argument("--graph", required=True)
    b.add_argument("--evidence", required=True)
    b.add_argument("--timing", required=True)
    b.add_argument("--test-packages")
    b.add_argument("--test-root", default="ci-result/test-package-roots")
    b.set_defaults(func=build)
    s = sub.add_parser("stop")
    s.add_argument("--hosts", required=True)
    s.add_argument("--run-id", type=int)
    s.add_argument("--attempt", type=int)
    s.add_argument("--count", type=int)
    s.add_argument("--timeout", type=int, default=45)
    s.set_defaults(func=stop)
    w = sub.add_parser("hold")
    w.add_argument("--timeout", type=int, default=1800)
    w.add_argument("--startup-timeout", type=int, default=360)
    w.set_defaults(func=hold)
    args = p.parse_args()
    try:
        args.func(args)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"nix-remote-workers: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
