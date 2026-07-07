#!/bin/bash
# Tailscale Funnel setup for Library Site
# Run on the Pi (pihole) to expose the site via a Tailscale Funnel URL
# The funnel URL (e.g. https://pihole.your-tailnet.ts.net) works from anywhere,
# including networks that block your main domain.

set -e

LIBRARY_PORT=8085  # Host port where the app listens (docker-compose maps 8085:8080)

echo "=== Tailscale Funnel Setup for Library Site ==="
echo ""

# Check if Tailscale is installed
if ! command -v tailscale &>/dev/null; then
    echo "Tailscale is not installed. Installing..."
    curl -fsSL https://tailscale.com/install.sh | sh
    echo ""
fi

# Check if Tailscale is connected
if ! tailscale status &>/dev/null; then
    echo "Tailscale is not connected. Run: sudo tailscale up"
    echo "Authenticate at the URL it prints, then run this script again."
    exit 1
fi

# Funnel requires root on Linux
echo "Enabling Tailscale Funnel (may prompt for admin approval in browser)..."
sudo tailscale funnel --yes 2>/dev/null || true

# Start funnel - proxy to Library Site on port 8085
# --bg = run in background, survives reboots (resumes when Tailscale restarts)
echo "Starting funnel to proxy localhost:${LIBRARY_PORT}..."
sudo tailscale funnel --bg --yes localhost:${LIBRARY_PORT}

echo ""
echo "=== Funnel status ==="
sudo tailscale funnel status

echo ""
echo "Your Library Site funnel URL is shown above (e.g. https://pihole.your-tailnet.ts.net)"
echo ""
echo "To stop: sudo tailscale funnel localhost:${LIBRARY_PORT} off"
echo "To restart: sudo tailscale funnel --bg --yes localhost:${LIBRARY_PORT}"
