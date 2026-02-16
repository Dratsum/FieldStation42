# Network Context Report: Local HLS Streaming Server

## Goal
Deploy a Docker container on the existing server infrastructure that serves an HLS stream via HTTP, accessible only to local LAN machines.

---

## Infrastructure Summary

| Component | Details |
|-----------|---------|
| **Docker Host** | Starlite — 192.168.0.51 (Linux Mint, static IP) |
| **Gateway/Firewall** | Ubiquiti UDM Pro — 192.168.0.1 |
| **LAN Subnet** | 192.168.0.0/24 (Default network) |
| **DNS** | Pi-hole at 192.168.0.51 (port 53, host-networked) |
| **Reverse Proxy** | Nginx Proxy Manager on Starlite (ports 80, 81, 443) |
| **Docker Network** | `diane_net` (172.20.0.0/x) — external bridge used by most containers |

## Docker Host (Starlite) — Key Details

- **19 containers already running** — resource awareness matters
- **Docker compose** is the standard deployment method
- **Port binding convention**: Security-sensitive services bind to `127.0.0.1`. LAN-accessible services bind to `0.0.0.0` (which is appropriate here since local clients need access)
- **UFW firewall** is active — rules exist for Docker subnets (172.16.0.0/12) on ports 80, 81, 443, 8123, 32400. **A new UFW rule will be needed** if the HLS server uses a port not already allowed.
- **NPM (Nginx Proxy Manager)** handles `*.starlite.local` domains for LAN services — a proxy host can be created for the stream (e.g., `stream.starlite.local`)

## Ports Already In Use on Starlite

Avoid these when choosing a port for the HLS server:

| Port | Service |
|------|---------|
| 53 | Pi-hole DNS |
| 80, 81, 443 | Nginx Proxy Manager |
| 3000, 3001 | Frontend apps / Homepage |
| 4000 | Backend app |
| 4242 | (proxied to Pi on .121) |
| 5001 | Whisper API |
| 5173 | Dreamboard frontend |
| 5432, 5433 | PostgreSQL |
| 5678 | n8n |
| 6380 | Redis |
| 8000, 8001 | Dreamboard backend / Chroma |
| 8080 | (was Adminer — disabled but avoid) |
| 8089 | Pi-hole web UI |
| 8123 | Home Assistant |
| 9000 | Portainer |
| 9002, 9003 | MinIO |
| 11434 | Ollama |
| 18555 | go2rtc (Home Assistant) |
| 32400 | Plex |

**Suggested free ports:** 8088, 8443, 8888, 9080, or anything in the 7000-7999 range.

## Network Access — What Clients Need

| Client | IP | How it connects |
|--------|-----|-----------------|
| MECCA (Windows) | 192.168.0.31 | 10Gbit wired |
| Grey Mouser (Ubuntu) | 192.168.0.37 | Wired Ethernet |
| PlayStation 5 | 192.168.0.22 | Wired |
| MONOLITH (Windows 11) | 192.168.0.125 | Wired |
| WiFi devices | DHCP | Via "House of Mysteries" (5 GHz) |

All clients are on the same 192.168.0.0/24 subnet, so no VLAN routing needed. Direct HTTP access to `192.168.0.51:<port>` will work.

## What Needs to Happen

1. **Choose a port** not in the list above (e.g., `8888`)
2. **Create the Docker container** — bind to `0.0.0.0:<port>` since LAN access is intended
3. **Add to `diane_net`** if it needs to talk to other containers, otherwise a standalone bridge is fine
4. **UFW rule** — allow the chosen port:
   ```bash
   sudo ufw allow from 192.168.0.0/24 to any port 8888 proto tcp comment "HLS stream server"
   ```
   This restricts access to LAN only.
5. **(Optional) NPM proxy host** — create `stream.starlite.local` pointing to the container for a clean URL. Would require a Pi-hole local DNS entry for `stream.starlite.local → 192.168.0.51` (or it may already resolve via the existing wildcard if one is configured).

## Security Notes

- **No port forwarding** should be created on the UDM Pro — this is LAN-only
- **UPnP is disabled** globally, so no ports will auto-open
- The UFW rule above restricts to the local subnet only
- **No Cloudflare Tunnel** route should be added
- IDS/IPS is active on the UDM Pro in Protect mode — legitimate HLS traffic (HTTP) won't trigger alerts

## Sonos VLAN Note

There is a second VLAN (192.168.20.0/24, VLAN 20) for Sonos speakers. If any Sonos devices or VLAN 20 clients need to access the stream, a firewall rule on the UDM Pro would be needed to allow VLAN 20 → 192.168.0.51 on the HLS port. Otherwise, ignore this.
