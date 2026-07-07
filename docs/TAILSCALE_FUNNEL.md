# Tailscale Funnel Setup for Library Site

Tailscale Funnel exposes your Library Site to the internet via a `*.ts.net` URL. This is useful when your main domain is blocked (e.g. at work) but Tailscale URLs are accessible.

## Prerequisites

- Tailscale account (Personal, Personal Plus, Premium, or Enterprise)
- Pi (pihole) with Library Site running via Docker

## One-Time Setup

### 1. Install and connect Tailscale on the Pi

```bash
# SSH to your Pi
ssh pihole@pihole

# Install Tailscale (if not already)
curl -fsSL https://tailscale.com/install.sh | sh

# Connect (authenticate at the URL it prints)
sudo tailscale up
```

### 2. Enable Funnel in Tailscale Admin

1. Go to [Tailscale Admin Console](https://login.tailscale.com/admin/acls)
2. Open **Access controls** → **Funnel** section
3. Click **Add Funnel to policy** (adds the `funnel` node attribute)
4. Save

### 3. Run the funnel setup script

On the Pi, from the Library Site directory:

```bash
cd "/opt/stacks/Library Site"
chmod +x tailscale-funnel-setup.sh
./tailscale-funnel-setup.sh
```

Or run the funnel command directly:

```bash
tailscale funnel --bg --yes localhost:8085
```

### 4. Get your funnel URL

```bash
tailscale funnel status
```

The URL will look like: `https://pihole.your-tailnet.ts.net`

## Usage

- **Access from anywhere**: Open the funnel URL in a browser (no Tailscale client needed on the client)
- **Survives reboots**: Funnel with `--bg` resumes automatically when Tailscale restarts
- **Stop funnel**: `tailscale funnel localhost:8085 off`
- **Restart funnel**: `tailscale funnel --bg --yes localhost:8085`

## Notes

- Funnel uses port 443 (HTTPS) on Tailscale's side; your app stays on 8085 locally
- TLS certificates are provisioned automatically by Tailscale
- The funnel URL is public—anyone with the link can access. Use your app's login for auth.
