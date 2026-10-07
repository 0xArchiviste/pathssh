"""SSH path selection using real SSH banner probes; no third-party runtime libraries."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import sys
import time


def config_host(filename, name):
    config = json.loads(Path(filename).expanduser().read_text())
    host = config["hosts"][name]
    for field in ("ssh_alias", "host_key_alias"):
        value = host.get(field, host.get("ssh_alias", name))
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value.startswith("-"):
            raise ValueError(f"Invalid {field}")
        host[field] = value
    paths = host["paths"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 16:
        raise ValueError("Configure between 1 and 16 paths")
    names = set()
    for path in paths:
        if not isinstance(path.get("name"), str) or path["name"] in names:
            raise ValueError("Paths need unique string names")
        names.add(path["name"])
        if not isinstance(path.get("address"), str) or not re.fullmatch(r"[A-Za-z0-9_.:%-]+", path["address"]) or path["address"].startswith("-"):
            raise ValueError("Invalid path address")
        port = path.setdefault("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid port")
    command = host.get("command", [])
    if not isinstance(command, list) or any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise ValueError("command must be an array of strings")
    return host


async def banner_probe(path, timeout):
    writer = None
    started = time.monotonic()
    try:
        async def exchange():
            nonlocal writer
            reader, writer = await asyncio.open_connection(path["address"], path["port"], limit=8192)
            for _ in range(20):
                line = await reader.readline()
                if line.startswith(b"SSH-2.0-") or line.startswith(b"SSH-1.99-"):
                    return time.monotonic() - started
                if not line:
                    break
            raise ValueError("No SSH banner")
        return await asyncio.wait_for(exchange(), timeout), None
    except (OSError, ValueError, asyncio.TimeoutError) as exc:
        return None, type(exc).__name__
    finally:
        if writer is not None:
            writer.close()


async def measure(paths, samples, timeout):
    async def one(path):
        values, errors = [], []
        for _ in range(samples):
            latency, error = await banner_probe(path, timeout)
            if latency is None:
                errors.append(error)
            else:
                values.append(latency)
        median = statistics.median(values) if values else None
        jitter = max(values) - min(values) if values else None
        # Failed samples cost a full timeout. Lower score is better.
        score = median + jitter + timeout * len(errors) / samples if values else None
        return {**path, "successful_samples": len(values), "samples": samples,
                "median_seconds": median, "jitter_seconds": jitter,
                "score": score, "errors": errors}
    return await asyncio.gather(*(one(path) for path in paths))


def record(args, event, **details):
    directory = Path(args.state_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = {"timestamp": time.time(), "pid": os.getpid(), "host": args.host,
             "event": event, **details}
    with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
        os.chmod(directory / "events.jsonl", 0o600)
        stream.write(json.dumps(entry) + "\n")


def ssh_command(host, path):
    # Disable master reuse so an older connection cannot override path selection.
    argv = ["ssh", "-t", "-o", f"HostName={path['address']}",
            "-p", str(path["port"]), "-o", f"HostKeyAlias={host['host_key_alias']}",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
            "-o", "StrictHostKeyChecking=ask",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=15", host["ssh_alias"]]
    if host.get("command"):
        argv.append(shlex.join(host["command"]))
    return argv


def run(args):
    if args.action == "status":
        filename = Path(args.state_dir).expanduser() / "events.jsonl"
        if filename.exists():
            # Keep memory bounded even after many runs.
            from collections import deque
            with filename.open() as stream:
                for line in deque(stream, maxlen=20):
                    print(line.rstrip())
        else:
            print("No recorded activity")
        return 0
    host = config_host(args.config, args.host)
    if args.reconnect and not host.get("command"):
        raise ValueError("Reconnection requires an explicit persistent-session attach command in config")
    attempts = args.max_attempts if args.reconnect else 1
    excluded = set()
    last_code = 1
    for attempt in range(1, attempts + 1):
        paths = [p for p in host["paths"] if p["name"] not in excluded]
        if not paths:
            excluded.clear()
            paths = host["paths"]
        results = asyncio.run(measure(paths, args.samples, args.timeout))
        record(args, "probe", attempt=attempt, results=results)
        available = sorted((r for r in results if r["score"] is not None), key=lambda r: r["score"])
        if args.action == "probe":
            print(json.dumps({"paths": results, "selected": available[0]["name"] if available else None}, indent=2))
            return 0 if available else 1
        if available:
            path = available[0]
            print(f"pathssh: using {path['name']} (SSH banner {path['median_seconds'] * 1000:.0f} ms), attempt {attempt}/{attempts}", file=sys.stderr)
            record(args, "connect", attempt=attempt, path=path["name"])
            child = subprocess.Popen(ssh_command(host, path))
            try:
                last_code = child.wait()
            except KeyboardInterrupt:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                raise
            record(args, "exit", path=path["name"], code=last_code)
            # 255 is OpenSSH's transport/authentication failure code.
            # Clean exits and remote command errors must not rerun commands.
            if last_code != 255:
                return last_code if last_code >= 0 else 128 - last_code
            excluded.add(path["name"])
        else:
            print("pathssh: no reachable SSH endpoint", file=sys.stderr)
            record(args, "unreachable", attempt=attempt)
            last_code = 1
        if attempt < attempts:
            time.sleep(args.retry_delay)
    record(args, "exhausted", attempts=attempts)
    return last_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["probe", "connect", "status"])
    parser.add_argument("host", nargs="?", default="desktop")
    parser.add_argument("--config", default="~/.config/pathssh/config.json")
    parser.add_argument("--state-dir", default="~/.local/state/pathssh")
    parser.add_argument("--samples", type=int, choices=range(1, 11), default=3, metavar="1..10")
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--reconnect", action="store_true", help="Retry failed transport; only use with a safe attach command")
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--retry-delay", type=float, default=2)
    args = parser.parse_args()
    if not 0.1 <= args.timeout <= 60 or not 1 <= args.max_attempts <= 100 or not 0 <= args.retry_delay <= 60:
        parser.error("timeout must be 0.1..60, max-attempts 1..100, retry-delay 0..60")
    try:
        return run(args)
    except KeyboardInterrupt:
        record(args, "cancelled")
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"pathssh: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
