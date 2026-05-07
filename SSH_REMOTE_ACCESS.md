# SSH from your phone/iPad to Adamserver

Goal: tap an icon on your iPhone or iPad, land in a terminal on Adamserver, run
`claude` on the futures-app repo, walk away, come back later, and pick up
exactly where you left off.

You said Adamserver is already reachable from the internet, so this guide
assumes a public hostname / IP and a forwarded SSH port already work. If that
isn't true, jump to the **Tailscale alternative** at the bottom — it's safer
and easier than poking holes in your router.

---

## 1. On Adamserver: confirm SSH is up

From your Mac (where `ssh adamserver` already works per CLAUDE.md):

```bash
ssh adamserver 'sudo systemctl status sshd | head -5'
ssh adamserver 'ss -tlnp | grep ssh'        # which port is it on?
ssh adamserver 'cat ~/.ssh/authorized_keys' # existing keys (your Mac's should be here)
```

Write down:

- Public hostname or IP (e.g. `home.example.com` or `73.x.x.x`)
- SSH port (default `22`; if your router forwards e.g. `2222 → 22`, use `2222`)
- The Linux username on Adamserver (per CLAUDE.md, this is `joe`)

You'll plug those into the phone in step 3.

---

## 2. On Adamserver: harden SSH (do this BEFORE step 3)

Exposing SSH to the public internet means bots will hammer it. Lock it down:

```bash
ssh adamserver
sudo nano /etc/ssh/sshd_config
```

Set (or confirm) these lines:

```
PasswordAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
KbdInteractiveAuthentication no
```

Then:

```bash
sudo sshd -t                    # syntax-check
sudo systemctl reload sshd
sudo pacman -S --needed fail2ban   # Arch package name
sudo systemctl enable --now fail2ban
```

Quick test from your **Mac** (don't disconnect the existing SSH session yet —
keep it open as a safety net):

```bash
ssh -o PreferredAuthentications=password adamserver
# Should be rejected. If it lets you type a password, key-only mode isn't on yet.
```

---

## 3. On your phone/iPad: install an SSH client

Recommended: **Blink Shell** (App Store, ~$20 one-time). Supports SSH, mosh,
tmux, key management, custom keyboard, and survives network drops.

Alternatives:

- **Termius** — free tier works, $$ for sync
- **a-Shell** — free, less polished but fine for basic SSH

The rest of this guide uses Blink Shell commands. Other apps have a UI for the
same things.

---

## 4. Generate an SSH key on the phone

In Blink Shell, tap into the prompt and run:

```bash
config
```

This opens Blink's config UI. Go to **Keys** → **+** → **Create new** →
type **ED25519** → name it `phone` → no passphrase (or one if you want; Blink
will prompt every connect).

Then back at the Blink prompt:

```bash
ssh-copy-id -i ~/.ssh/phone joe@<adamserver-host>
```

If `ssh-copy-id` isn't available in your client, do it manually:

1. In Blink config → Keys → tap your `phone` key → **Copy public key**
2. From your **Mac** (which already has working SSH):

   ```bash
   ssh adamserver
   nano ~/.ssh/authorized_keys
   # paste the public key from your phone on a new line, save, exit
   chmod 600 ~/.ssh/authorized_keys
   ```

---

## 5. Add a host shortcut on the phone

Blink Shell → **config** → **Hosts** → **+**:

```
Alias:    adamserver
HostName: <adamserver-host>
Port:     <ssh-port>             (22 unless you changed it)
User:     joe
Key:      phone
```

Save. Now from the Blink prompt:

```bash
ssh adamserver
```

You should land at `joe@adamserver` with no password prompt.

---

## 6. Persistent sessions with tmux

Without tmux, every time your phone sleeps or your network blinks, your
`claude` session dies and you lose context. With tmux you reattach and
everything is exactly as you left it.

On Adamserver (one-time):

```bash
sudo pacman -S --needed tmux
```

Make a tiny launcher. On Adamserver:

```bash
nano ~/bin/claude-futures
```

Paste:

```bash
#!/usr/bin/env bash
# Attach to a long-lived tmux session running claude in ~/futures-app
set -euo pipefail
SESSION="futures"
cd "$HOME/futures-app"
if tmux has-session -t "$SESSION" 2>/dev/null; then
  exec tmux attach -t "$SESSION"
else
  exec tmux new-session -s "$SESSION" -c "$HOME/futures-app" "claude"
fi
```

Then:

```bash
mkdir -p ~/bin
chmod +x ~/bin/claude-futures
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
```

From Blink Shell on your phone:

```bash
ssh adamserver -t claude-futures
```

The `-t` forces a TTY (claude needs one). Detach any time with `Ctrl-b` then
`d` — the session keeps running on Adamserver. Reconnect later with the same
command and you're right back in claude.

Pro tip: in Blink's host config, set **Startup command** to
`claude-futures` and connecting becomes a single tap.

---

## 7. Survive flaky cell networks (optional)

SSH disconnects when your phone changes networks (LTE → wifi). Two options:

- **Mosh**: install on Adamserver (`sudo pacman -S mosh`), then in Blink use
  `mosh adamserver -- claude-futures`. Reconnects instantly when the network
  changes.
- **Just rely on tmux**: even if SSH drops, the tmux session keeps your work.
  Reconnect and `claude-futures` reattaches.

Both is best.

---

## Tailscale alternative (more secure, easier)

Instead of port-forwarding SSH to the public internet:

1. Install Tailscale on Adamserver: `sudo pacman -S tailscale && sudo
   systemctl enable --now tailscaled && sudo tailscale up --ssh`
2. Install the Tailscale app on your phone, sign in with the same account
3. From Blink Shell: `ssh joe@adamserver` (Tailscale gives every machine a
   `*.ts.net` name and routes traffic over an encrypted overlay network)

Benefits: no public SSH exposure, no port-forward, no fail2ban needed,
survives ISP IP changes. Free for personal use up to 100 devices.

---

## Stop the PWA URL from changing on every restart

The reason your `web_controller.py` URL keeps changing is in
`web_controller.py:74-84`: if `WEB_CONTROLLER_TOKEN` / `WEB_VIEWER_TOKEN`
aren't set, it generates fresh random ones every boot and only prints them to
stdout. Pin them once and the URL is stable forever.

On Adamserver:

```bash
ssh adamserver
cd ~/futures-app
# Generate two strong tokens (one-time)
ADMIN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
VIEWER=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

# Append to .env if not already pinned
grep -q '^WEB_CONTROLLER_TOKEN=' .env && \
  sed -i "s|^WEB_CONTROLLER_TOKEN=.*|WEB_CONTROLLER_TOKEN=$ADMIN|" .env || \
  echo "WEB_CONTROLLER_TOKEN=$ADMIN" >> .env

grep -q '^WEB_VIEWER_TOKEN=' .env && \
  sed -i "s|^WEB_VIEWER_TOKEN=.*|WEB_VIEWER_TOKEN=$VIEWER|" .env || \
  echo "WEB_VIEWER_TOKEN=$VIEWER" >> .env

chmod 600 .env

# Restart the service so it picks up the new env
sudo systemctl restart futures-web 2>/dev/null \
  || systemctl --user restart futures-web 2>/dev/null \
  || echo "Service name may differ — check 'systemctl --user list-units | grep futures'"
```

After this, the URL is a fixed:

```
http://<adamserver-host>:5100/?token=<WEB_CONTROLLER_TOKEN>
```

Save it as a bookmark on your phone's home screen — done.

---

## A `show-url` helper for the rare case you do need to recover it

Even with tokens pinned, sometimes you'll forget which token is which or want
to confirm the service is actually up. On Adamserver:

```bash
nano ~/bin/show-url
```

```bash
#!/usr/bin/env bash
# Print the current PWA URL with token, plus quick health check.
set -euo pipefail
ENV_FILE="$HOME/futures-app/.env"
PORT="${PORT:-5100}"
HOST="$(hostname -f 2>/dev/null || hostname)"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "no .env at $ENV_FILE" >&2; exit 1
fi

ADMIN=$(grep -E '^WEB_CONTROLLER_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
VIEWER=$(grep -E '^WEB_VIEWER_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

if [[ -z "$ADMIN" ]]; then
  echo "WEB_CONTROLLER_TOKEN not pinned in .env — service is generating a fresh one each boot."
  echo "Run the pinning steps in SSH_REMOTE_ACCESS.md."
  exit 1
fi

echo "Admin:  http://$HOST:$PORT/?token=$ADMIN"
echo "Viewer: http://$HOST:$PORT/?token=$VIEWER"
echo
echo -n "Health: "
curl -sf -m 3 "http://localhost:$PORT/api/health?token=$ADMIN" \
  && echo " ✓ up" || echo " ✗ no response (is futures-web running?)"
```

```bash
chmod +x ~/bin/show-url
```

Now from Blink Shell on your phone:

```bash
ssh adamserver show-url
```

Three taps to get the working URL, even if you forgot it.

---

## What "code from my phone" looks like in practice

Once you're SSH'd in via Blink:

```bash
claude-futures        # opens claude inside ~/futures-app via tmux
```

Then you can type things like:

- *"Add a 'paused' indicator to the dashboard sidebar"*
- *"What's the largest losing trade in the last 7 days and which strategy?"*
- *"Show me the diff of strategy_engine.py since last week"*

I'll edit files, run scripts, commit, push — same as on the desktop. Detach
with `Ctrl-b d` to leave it running, reattach later with `claude-futures`.

For *very* light edits you can also just open files in `nano` directly:

```bash
nano ~/futures-app/futures_config.py
```

But the claude session is more useful — it understands the whole project.

---

## Troubleshooting

- **`Permission denied (publickey)`**: phone's public key isn't in
  `~/.ssh/authorized_keys` on Adamserver, or file perms are wrong (`chmod 700
  ~/.ssh && chmod 600 ~/.ssh/authorized_keys`).
- **`Connection refused`**: wrong port, or sshd not running, or firewall
  blocking. Try from Mac first: `ssh -p <port> -v joe@<host>`.
- **`Connection timed out`**: port-forward not set up on the router, or ISP
  blocks inbound. Use Tailscale.
- **`claude: command not found` after SSH**: `claude` not in PATH for
  non-login shells. Either use full path in `claude-futures`, or add the
  install location to `~/.bashrc`.
