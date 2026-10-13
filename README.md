# pi-tailscale-firstboot

Get a headless Raspberry Pi onto your [Tailscale](https://tailscale.com) network on its **first boot**, with no monitor, no keyboard, and no need to find it on the local network.

You prepare the SD card on your computer. When the Pi boots, it waits for internet, installs Tailscale, joins your tailnet, and deletes the auth key. If anything goes wrong, it writes a plain-text progress log to the SD card's boot partition, which any computer can read, so you can see what failed without ever plugging in a screen.

```
python3 firstboot.py setup     # prepare the SD card
python3 firstboot.py log       # read what the Pi did (put the card back in your computer)
```

## Why

`ping raspberrypi.local` only works when your computer and the Pi are on the same local network and that network allows multicast. Office, campus and hotel networks often don't: they isolate clients, block mDNS, or need devices registered. Tailscale avoids all of that. Once the Pi is on your tailnet, you can reach it from anywhere as `ssh user@your-pi`.

## Requirements

- A **Raspberry Pi OS image that uses cloud-init** (Raspberry Pi OS "Trixie" or newer), flashed with **Raspberry Pi Imager**. In Imager's OS customisation, set a username and password and enable SSH.
- **Python 3.8+** on the computer you use to prepare the card. No extra packages are needed. If PyYAML or Ruby is installed, the generated config is also validated.
- A **Tailscale auth key**: [admin console → Settings → Keys](https://login.tailscale.com/admin/settings/keys). A one-off, pre-approved key is ideal.
- **Internet for the Pi**: Ethernet, or a Wi-Fi network you configure with this tool.

Works on macOS, Linux and Windows. The boot partition is detected automatically, or you can pass `--boot`.

## Quick start

1. Flash Raspberry Pi OS with Raspberry Pi Imager (with OS customisation, as above).
2. Re-insert the SD card so its `bootfs` partition mounts.
3. Run:
   ```
   git clone https://github.com/knowhalim/pi-tailscale-firstboot.git
   cd pi-tailscale-firstboot
   python3 firstboot.py setup
   ```
   You'll be asked for the auth key (input is hidden), an optional hostname, and whether to keep, replace or remove the Wi-Fi network stored on the card.
4. Eject the card, put it in the Pi, connect Ethernet (or be in range of the Wi-Fi), and power on.
5. Watch the [Machines page](https://login.tailscale.com/admin/machines). A Pi 4/5 usually appears within 3–5 minutes; a Pi 3 can take 10–15 minutes.
6. Connect: `ssh <user>@<hostname>`. If your tailnet requires a Tailscale SSH check, open the link it prints.

### Non-interactive

```
TS_AUTHKEY=tskey-auth-... WIFI_PASSWORD='...' \
  python3 firstboot.py setup --hostname mypi --wifi-ssid "My Hotspot" --country GB --eject -y
```

| Option | Meaning |
|---|---|
| `--boot PATH` | Boot partition path (auto-detected otherwise) |
| `--authkey-file FILE` | Read the auth key from a file instead of the prompt or `TS_AUTHKEY` |
| `--hostname NAME` | Pi hostname, also used as the Tailscale machine name |
| `--wifi-ssid NAME` | Replace the Wi-Fi on the card (password from prompt or `WIFI_PASSWORD`) |
| `--no-wifi` | Remove Wi-Fi from the card (Ethernet only) |
| `--country XX` | Wi-Fi country code (default: whatever is on the card, else `US`) |
| `--no-ssh` | Don't enable Tailscale SSH |
| `--eject` | Eject the card when done (macOS) |
| `-y` | Don't ask questions; keep the current Wi-Fi unless told otherwise |

You can run `setup` again as often as you like: it always rebuilds from the original Imager config, which it keeps in `user-data.before-ptf` on the card.

## It didn't show up: read the log

Power the Pi off, put the card in your computer, and run:

```
python3 firstboot.py log            # last lines + what they mean
python3 firstboot.py log --report   # full network report (addresses, routes, Wi-Fi scan, cloud-init, NetworkManager)
```

The Pi writes one line every 30 seconds:

```
2026-01-01 10:02:30 up=150s power=0x0 ip=[192.0.2.10] web=204 https=200 tailscale=[none] wifi_seen=[My Hotspot|Neighbour|]
```

| Field | What to look for |
|---|---|
| `power` | `0x0` is good. Anything else means under-voltage or throttling: use a proper 5V / 2.5A+ supply. |
| `ip` | Empty means no network at all: Wi-Fi didn't connect and Ethernet gave no address. |
| `web` | `204` means open internet. `302` or `200` usually means a captive portal or login page. `000` means blocked. |
| `https` | `200` means Tailscale's servers are reachable, so installation will go ahead. |
| `tailscale` | The Pi's Tailscale IP once it has joined. |
| `wifi_seen` | The networks the Pi can actually see. Is yours in the list? |

No log at all, with the auth key still on the card, means the Pi never got as far as running Linux. See the troubleshooting section below.

## Troubleshooting

These are real problems hit while building this tool. The fixes are built in where possible.

| Symptom | Cause | Fix |
|---|---|---|
| Pi never appears; `cmdline.txt` on the card is **0 bytes**; a `FSCK0000.REC` file exists | The card was removed or the power was cut mid-write, which corrupted the boot partition | `setup` detects this and restores `cmdline.txt` from the `.REC` recovery file. Always eject the card, and shut the Pi down before unplugging it. |
| Wi-Fi never connects; your network is missing from `wifi_seen` | **Pi 3 / Zero W are 2.4 GHz only** and have a weak antenna. Phone hotspots often default to 5 GHz. | Set the hotspot band to **2.4 GHz**, keep the phone close, and keep the hotspot screen open. |
| Wi-Fi never connects even though it's in `wifi_seen` | The SSID is **case-sensitive** (`My Hotspot` ≠ `my hotspot`), or the network uses **WPA3 / WPA2-WPA3 mixed** mode, which Pi 3 / Zero W don't support | Re-run `setup` with the exact name, and set the network to **WPA2**. On an iPhone, turn on "Maximize Compatibility". |
| `ip` present but `web`/`https` not 204/200 | Captive portal, or an office/campus network that needs devices registered | Try a phone hotspot or home router, or register the Pi's MAC address with your IT team. |
| `ping 1.1.1.1` fails but the internet works | Many corporate networks block ICMP | Nothing to do. This tool checks HTTPS, not ping. |
| Changed the card but the Pi ignores it | cloud-init runs once per instance-id | `setup` always writes a new instance-id, so re-running it is enough. |
| `tailscale up failed` in the log | Auth key expired, already used, or needs approval | Make a new key and re-run `setup`. |
| `ssh` prints "Tailscale SSH requires an additional check" | Your tailnet's SSH policy uses check mode | Open the printed link and approve. |
| `ping <pi>.local` fails | Different network, client isolation, or mDNS is blocked | Use the Tailscale name instead. That's the point of this tool. |

## After the Pi has joined

1. In the admin console, open **Machines → your Pi → Disable key expiry**. Otherwise the Pi drops off the tailnet after about 180 days.
2. Revoke the auth key if it was reusable. The Pi stays connected.
3. Remove the helper services and logs:
   ```
   ssh <user>@<hostname> sudo ptf-cleanup                       # services, scripts, logs
   ssh -t <user>@<hostname> sudo ptf-cleanup --disable-cloud-init   # also stop cloud-init re-running the card config
   ```
   Tailscale itself stays installed and starts on every boot.

## How it works

`firstboot.py setup` edits only the FAT boot partition:

| File | Change |
|---|---|
| `user-data` | Merges [`cloud-init/firstboot.yaml`](cloud-init/firstboot.yaml) into Imager's config: three small scripts and two systemd services |
| `network-config` | Optionally replaces or removes Wi-Fi (Ethernet DHCP is always on) |
| `meta-data`, `cmdline.txt` | New cloud-init instance-id, so the config is applied again |
| `cmdline.txt` | Restored from `FSCK*.REC` if it was emptied |
| `tailscale-authkey` | The auth key, deleted by the Pi after it joins |

On the Pi:

- **`ptf-setup.service`** waits until `https://pkgs.tailscale.com` is reachable, installs Tailscale with the official install script, runs `tailscale up --auth-key=… --hostname=… --ssh`, deletes the key, and disables itself. Until then it retries every 30 seconds, and again on every boot.
- **`ptf-heartbeat.service`** appends a status line to `/boot/firmware/ptf-log.txt` every 30 seconds and writes `ptf-report.txt` at about 2, 6 and 20 minutes. It stops once Tailscale is up, or after 2 hours, to spare the SD card.
- Both run as systemd services rather than from cloud-init's early boot hooks. That way they always start after the files are written, restart if they fail, and log errors to `ptf-errors.txt`.

## Security notes

- Until the Pi's first successful boot, the auth key is stored **in plain text on the SD card**. Use a **one-off** key with a short expiry, and consider a [tag](https://tailscale.com/kb/1068/acl-tags) with limited ACLs.
- Wi-Fi passwords in `network-config` are plain text. That's how Raspberry Pi Imager stores them too.
- The tool never prints the auth key or the Wi-Fi password. Local backups of the edited files go to `./backups/`, which is git-ignored.

## License

MIT
