# pathssh

Choose a responsive SSH route to one machine through Tailscale, NetBird, or
other reachable endpoints. Optionally reconnect to a persistent remote session
after a transport failure.

**Status: initial implementation. Real dual-network end-to-end validation is
pending.** This is a connection selector, not a seamless connection migration
or bandwidth bonding system.

## How it works

1. Probe each configured endpoint concurrently, taking three SSH banner samples.
2. Rank reachable endpoints by median connection/banner latency, variation, and
   failed samples. Lower scores win.
3. Launch normal OpenSSH through that endpoint. Keep that path for the life of
   the connection.
4. With `--reconnect`, probe again after SSH exits with code 255. Avoid the
   previously failed path for the next attempt. Stop after the attempt limit.

SSH banner timing measures connection responsiveness. It does **not** measure
authenticated terminal round-trip latency or bulk bandwidth, and a responsive
banner does not prove login will succeed. Scores are observations, not a
guarantee that the selected connection will remain fastest.

## Requirements

- Python 3.9 or newer and OpenSSH on the client.
- SSH server and a persistent session tool (Herdr or tmux) on the destination.
- Both VPNs configured on the client and destination if using both paths.
- Working routes, DNS and firewall rules for each endpoint. Tailscale and
  NetBird coexistence must be checked for address/routing/DNS conflicts.

The program does not install or configure VPN software.

## Install

```sh
git clone https://github.com/0xArchiviste/pathssh.git
cd pathssh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/pathssh --help
```

You can also run `python3 pathssh.py` directly without installing a package.

## Configure

Copy `config.example.json` to `~/.config/pathssh/config.json`. Replace the
example addresses with the two addresses of the **same SSH server**. Set
`ssh_alias` to an existing entry in your SSH configuration, so its user and
identity file are reused.

```json
{
  "hosts": {
    "desktop": {
      "ssh_alias": "desktop",
      "host_key_alias": "desktop",
      "command": ["herdr"],
      "paths": [
        {"name": "tailscale", "address": "desktop.example.ts.net", "port": 22},
        {"name": "netbird", "address": "desktop.example.netbird.cloud", "port": 22}
      ]
    }
  }
}
```

Use an absolute remote executable path if it is not in the remote SSH shell's
PATH. A tmux alternative is `"command": ["tmux", "new-session", "-A", "-s", "work"]`.
The command is an argument array, shell-quoted before OpenSSH sends it to the
remote shell. It should only attach to a persistent session when reconnecting.

Both paths share one `host_key_alias`: verify the SSH server fingerprint through
a trusted channel before accepting it. Changed keys are rejected; the tool never
automatically trusts a key or disables host verification. If the two addresses
reach different SSH servers, configure separate hosts instead.

OpenSSH connection sharing is disabled for these connections, preventing an
existing master connection from overriding path selection. ProxyJump and
ProxyCommand are disabled so configured endpoints are reached directly. Other
SSH alias settings still apply, including identity, user, and compression.

## Use

```sh
# Observe actual endpoint measurements without logging in.
pathssh probe desktop

# Choose a path and attach once.
pathssh connect desktop

# Reattach after failures, with at most five total attempts.
pathssh connect desktop --reconnect --max-attempts 5

# Inspect the last 20 local events.
pathssh status
```

Ctrl-C cancels the connection/retry loop. A clean remote exit stops the program.
Non-255 remote errors also stop it. Code 255 can indicate authentication or host
verification failures as well as network failures, so those may also consume
the bounded retry budget. A remote command that deliberately exits 255 is
indistinguishable from an SSH failure: **only enable reconnection for safe
session attach commands**, never trading, payments, deployment, or other tasks
whose repetition could cause harm.

An active TCP/SSH connection cannot migrate to another endpoint. A drop causes
an interruption, a new SSH login and a new session attachment. Persistent work
survives only if the remote session manager keeps it running. Keepalives request
failure detection after roughly 30 seconds without replies; actual timing may
vary. A healthy connection is never switched just because another path becomes
faster.

## Local records

Events are appended to `~/.local/state/pathssh/events.jsonl`, including probe
results, selected paths, exit codes, retries and cancellation. `--state-dir`
changes the destination. The file is created with owner-only permissions.
Records include configured endpoint addresses; keep them private. Remote terminal
output, keys and passwords are not recorded. `status` shows past events, not a
live process health check. No background service is installed.

## Planned real end-to-end validation

Run these checks against a real desktop with both VPN endpoints available:

1. Confirm both endpoints reach the same server and match its trusted host key.
2. Run `probe`; record successful samples and selection for both paths.
3. Attach to Herdr and confirm existing workspaces and terminal interaction.
4. Interrupt only the selected VPN path while keeping the other reachable;
   confirm bounded reconnection chooses the other path and reattaches.
5. Confirm a normal session exit stops, and Ctrl-C cancels during retry.
6. Make both paths unavailable; confirm the attempt limit ends with failure.
7. Confirm a mismatched server key is rejected, without accepting the new key.

No live validation has been claimed yet. Initial checks cover source syntax and
CLI startup only; they do not establish network behavior.

## References

- [OpenSSH connection configuration](https://man.openbsd.org/ssh_config.5)
- [Tailscale coexistence with other VPNs](https://tailscale.com/docs/reference/faq/other-vpns)

MIT licensed.
