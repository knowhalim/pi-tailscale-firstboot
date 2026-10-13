#!/usr/bin/env python3
"""pi-tailscale-firstboot

Prepare a Raspberry Pi OS SD card (flashed with Raspberry Pi Imager) so the Pi
installs Tailscale and joins your tailnet on first boot - no screen, keyboard
or local network discovery needed.

  python3 firstboot.py setup      prepare the SD card
  python3 firstboot.py log        read the Pi's progress log from the SD card

Run `python3 firstboot.py setup --help` for options.
"""
import argparse
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAGMENT = HERE / "cloud-init" / "firstboot.yaml"
ORIG_BACKUP = "user-data.before-ptf"   # pristine Imager user-data, kept on the card
KEY_FILE = "tailscale-authkey"
LOG_FILE = "ptf-log.txt"
REPORT_FILE = "ptf-report.txt"
ERROR_FILE = "ptf-errors.txt"
TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*):(.*)$")


def die(msg):
    print(f"\nError: {msg}", file=sys.stderr)
    sys.exit(1)


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or (default or "")


def confirm(prompt, default=False):
    ans = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
    return default if not ans else ans.startswith("y")


# --------------------------------------------------------------------------- #
# Finding the boot partition
# --------------------------------------------------------------------------- #
def looks_like_boot(p: Path) -> bool:
    return (p / "config.txt").is_file() and (p / "cmdline.txt").exists()


def find_boot_partition(explicit=None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not looks_like_boot(p):
            die(f"{p} does not look like a Raspberry Pi boot partition (no config.txt/cmdline.txt).")
        return p
    candidates = []
    system = platform.system()
    if system == "Darwin":
        candidates = sorted(Path("/Volumes").glob("*"))
    elif system == "Windows":
        candidates = [Path(f"{d}:/") for d in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    else:
        user = os.environ.get("USER", "")
        for base in (f"/media/{user}", f"/run/media/{user}", "/media", "/mnt"):
            candidates += sorted(Path(base).glob("*")) if Path(base).is_dir() else []
    found = [c for c in candidates if c.is_dir() and looks_like_boot(c)]
    if not found:
        die("No Raspberry Pi boot partition found. Insert the SD card, or pass --boot <path>.")
    if len(found) > 1:
        die("Several boot partitions found: " + ", ".join(map(str, found)) + ". Pass --boot <path>.")
    return found[0]


# --------------------------------------------------------------------------- #
# cmdline.txt
# --------------------------------------------------------------------------- #
def check_cmdline(boot: Path, assume_yes: bool) -> str:
    """Return a valid single-line cmdline, recovering it from FSCK*.REC if it was emptied."""
    path = boot / "cmdline.txt"
    text = path.read_text(errors="replace").strip()
    if "root=" in text:
        return text.splitlines()[0].strip()

    print("\n!! cmdline.txt is empty or damaged - the Pi cannot boot like this.")
    print("   (Usually caused by pulling the card or power without ejecting/shutting down.)")
    for rec in sorted(boot.glob("FSCK*.REC")):
        recovered = rec.read_bytes().split(b"\x00")[0].decode(errors="replace").strip()
        if "root=" in recovered:
            line = recovered.splitlines()[0].strip()
            print(f"   Found the original in {rec.name}:\n     {line}")
            if assume_yes or confirm("   Restore cmdline.txt from it?", default=True):
                return line
    die("cmdline.txt is damaged and no recovery copy was found. Re-flash the card with Raspberry Pi Imager.")


# --------------------------------------------------------------------------- #
# Minimal, order-preserving cloud-config merge (no YAML library needed)
# --------------------------------------------------------------------------- #
def split_top_level(text):
    """[(key or None, [lines])] - None holds the header (e.g. '#cloud-config')."""
    blocks, key, lines = [], None, []
    for line in text.splitlines():
        m = TOP_KEY.match(line)
        if m:
            blocks.append((key, lines))
            key, lines = m.group(1), [line]
        else:
            lines.append(line)
    blocks.append((key, lines))
    return blocks


def list_items(block_lines):
    """Body of a list block with its '- ' items de-indented to column 0."""
    body = block_lines[1:]
    while body and not body[-1].strip():
        body.pop()
    indents = [len(l) - len(l.lstrip()) for l in body if l.lstrip().startswith("- ")]
    cut = min(indents) if indents else 0
    return [l[cut:] if l.strip() else "" for l in body]


def merge_cloud_config(base: str, fragment: str) -> str:
    blocks = split_top_level(base)
    keys = [k for k, _ in blocks]
    for fkey, flines in split_top_level(fragment):
        if fkey is None:
            continue
        items = list_items(flines)
        if fkey in keys:
            idx = keys.index(fkey)
            _, olines = blocks[idx]
            inline = TOP_KEY.match(olines[0]).group(2).strip()
            if inline not in ("", "[]"):
                die(f"user-data has an inline '{fkey}: {inline}' value I can't merge into. Edit it to block style first.")
            body = olines[1:]
            trailing = []
            while body and not body[-1].strip():
                trailing.insert(0, body.pop())
            indent = next((len(l) - len(l.lstrip()) for l in body if l.lstrip().startswith("- ")), 2)
            new_body = body + [(" " * indent + l) if l else "" for l in items]
            blocks[idx] = (fkey, [f"{fkey}:"] + new_body + trailing)
        else:
            blocks.append((fkey, [f"{fkey}:"] + [("  " + l) if l else "" for l in items]))
            keys.append(fkey)
    out = []
    for _, lines in blocks:
        out.extend(lines)
    return "\n".join(out).rstrip("\n") + "\n"


def set_scalar(text: str, key: str, value: str) -> str:
    line = f"{key}: {json.dumps(value)}"
    if re.search(rf"^{key}:.*$", text, flags=re.M):
        return re.sub(rf"^{key}:.*$", line, text, count=1, flags=re.M)
    return text.rstrip("\n") + f"\n{line}\n"


def network_config(ssid, password, country) -> str:
    lines = [
        "network:",
        "  version: 2",
        "  ethernets:",
        "    eth0:",
        "      dhcp4: true",
        "      dhcp6: true",
        "      optional: true",
    ]
    if ssid:
        lines += [
            "  wifis:",
            "    wlan0:",
            "      dhcp4: true",
            f"      regulatory-domain: {json.dumps(country)}",
            "      access-points:",
        ]
        if password:
            lines += [f"        {json.dumps(ssid)}:", f"          password: {json.dumps(password)}"]
        else:
            lines += [f"        {json.dumps(ssid)}: {{}}"]
        lines += ["      optional: true"]
    return "\n".join(lines) + "\n"


def current_wifi(boot: Path):
    nc = boot / "network-config"
    if not nc.exists():
        return [], None
    text = nc.read_text(errors="replace")
    ssids = re.findall(r'^\s{8}"?([^":\n]+)"?:\s*(?:\{\})?\s*$', text, flags=re.M)
    country = re.search(r'regulatory-domain:\s*"?([A-Za-z]{2})"?', text)
    return ssids, (country.group(1).upper() if country else None)


def validate_yaml(paths):
    try:
        import yaml  # type: ignore
        for p in paths:
            yaml.safe_load(p.read_text())
        return "ok (PyYAML)"
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        die(f"generated YAML is invalid: {e}")
    if shutil.which("ruby"):
        code = "ARGV.each{|f| YAML.load_file(f)}"
        r = subprocess.run(["ruby", "-ryaml", "-e", code, *map(str, paths)], capture_output=True, text=True)
        if r.returncode:
            die(f"generated YAML is invalid: {r.stderr.strip()}")
        return "ok (ruby)"
    return "skipped (install PyYAML to validate: pip install pyyaml)"


def sync_disk():
    if hasattr(os, "sync"):
        os.sync()


def eject(boot: Path):
    if platform.system() == "Darwin":
        subprocess.run(["diskutil", "eject", str(boot)])
    else:
        print("Now safely eject/unmount the SD card before removing it.")


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #
def cmd_setup(a):
    if not FRAGMENT.exists():
        die(f"missing {FRAGMENT} - run this from a full checkout of the repository.")
    boot = find_boot_partition(a.boot)
    print(f"Boot partition: {boot}")
    if not (boot / "user-data").exists() and not (boot / ORIG_BACKUP).exists():
        print("No user-data found. This tool needs a cloud-init based Raspberry Pi OS image\n"
              "(Raspberry Pi OS 'Trixie' or newer). Flashing with Raspberry Pi Imager and\n"
              "setting a username/password in OS customisation creates it.")
        if not (a.yes or confirm("Create a minimal user-data anyway?")):
            sys.exit(1)

    cmdline = check_cmdline(boot, a.yes)

    # --- inputs -------------------------------------------------------------
    key = os.environ.get("TS_AUTHKEY", "")
    if a.authkey_file:
        key = Path(a.authkey_file).expanduser().read_text().strip()
    if not key:
        print("\nCreate an auth key at https://login.tailscale.com/admin/settings/keys")
        print("(tip: one-off + pre-approved, optionally tagged). Input is hidden.")
        key = getpass.getpass("Tailscale auth key: ").strip()
    if not key:
        die("an auth key is required.")
    if not key.startswith("tskey-"):
        print("Warning: that does not look like a Tailscale auth key (expected 'tskey-...').")
        if not (a.yes or confirm("Use it anyway?")):
            sys.exit(1)

    hostname = a.hostname
    if hostname is None and not a.yes:
        hostname = ask("Pi hostname (blank = keep what Imager set)")
    if hostname:
        hostname = hostname.lower()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", hostname):
            die("hostname may only contain letters, digits and '-'.")

    existing, existing_country = current_wifi(boot)
    wifi_mode, ssid, wpass = "keep", None, None
    if a.no_wifi:
        wifi_mode = "none"
    elif a.wifi_ssid is not None:
        wifi_mode, ssid = "set", a.wifi_ssid
        wpass = os.environ.get("WIFI_PASSWORD") or (None if a.yes else getpass.getpass(f"Password for '{ssid}' (blank = open network): "))
    elif not a.yes:
        print(f"\nWi-Fi currently on the card: {', '.join(existing) if existing else 'none'}")
        choice = ask("Wi-Fi: [k]eep, [r]eplace, or [n]one (Ethernet only)", "k").lower()[:1]
        if choice == "r":
            wifi_mode = "set"
            print("SSID is case-sensitive. Raspberry Pi 3 / Zero W only see 2.4 GHz and do not support WPA3.")
            ssid = ask("Wi-Fi name (SSID)")
            wpass = getpass.getpass(f"Password for '{ssid}' (blank = open network): ")
            if not ssid:
                die("SSID cannot be empty.")
        elif choice == "n":
            wifi_mode = "none"
    country = (a.country or existing_country or "US").upper()

    # --- write --------------------------------------------------------------
    backup_dir = HERE / "backups" / time.strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in ("user-data", "meta-data", "cmdline.txt"):
        if (boot / name).exists():
            shutil.copy2(boot / name, backup_dir / name)

    if not (boot / ORIG_BACKUP).exists():
        src = boot / "user-data"
        (boot / ORIG_BACKUP).write_text(src.read_text() if src.exists() else "#cloud-config\n")
    if not hostname and (boot / "user-data").exists():
        # keep a hostname chosen on a previous run
        m = re.search(r'^hostname:\s*"?([^"\n]+)"?\s*$', (boot / "user-data").read_text(), flags=re.M)
        hostname = m.group(1).strip() if m else None
    base = (boot / ORIG_BACKUP).read_text()
    if not base.lstrip().startswith("#cloud-config"):
        base = "#cloud-config\n" + base

    flags = "--ssh" if a.ssh else ""
    fragment = FRAGMENT.read_text().replace("@TS_UP_FLAGS@", flags)
    user_data = merge_cloud_config(base, fragment)
    if hostname:
        user_data = set_scalar(user_data, "hostname", hostname)
    (boot / "user-data").write_text(user_data)

    if wifi_mode == "set":
        (boot / "network-config").write_text(network_config(ssid, wpass, country))
    elif wifi_mode == "none":
        (boot / "network-config").write_text(network_config(None, None, country))

    (boot / KEY_FILE).write_text(key + "\n")

    # A new instance-id makes cloud-init run the config again even if the Pi booted before.
    instance = f"ptf-{time.time_ns() // 1_000_000}"
    md = boot / "meta-data"
    md_text = md.read_text() if md.exists() else ""
    if re.search(r"^instance-id:", md_text, flags=re.M):
        md_text = re.sub(r"^instance-id:.*$", f"instance-id: {instance}", md_text, flags=re.M)
    else:
        md_text = f"instance-id: {instance}\n" + md_text
    md.write_text(md_text)
    cmdline = re.sub(r"(ds=nocloud[^ ]*?;i=)[^ ;]+", rf"\g<1>{instance}", cmdline)
    (boot / "cmdline.txt").write_text(cmdline + "\n")

    for f in (LOG_FILE, REPORT_FILE, ERROR_FILE):
        (boot / f).unlink(missing_ok=True)

    to_check = [boot / "user-data", boot / "meta-data"]
    if (boot / "network-config").exists():
        to_check.append(boot / "network-config")
    yaml_status = validate_yaml(to_check)
    sync_disk()

    print("\nSD card ready.")
    print(f"  user-data       merged ({yaml_status})")
    print(f"  hostname        {hostname or '(unchanged)'}")
    print(f"  wifi            {wifi_mode}{' -> ' + ssid if ssid else ''}")
    print(f"  tailscale ssh   {'on' if a.ssh else 'off'}")
    print(f"  instance-id     {instance}")
    print(f"  local backup    {backup_dir}")
    print("\nNext: eject the card, put it in the Pi, connect Ethernet or be in Wi-Fi range,")
    print("power on, and watch for the Pi at https://login.tailscale.com/admin/machines")
    print("(a Pi 3 can take 10-15 minutes). If it doesn't show up, power it off, put the")
    print("card back in this computer and run:  python3 firstboot.py log")
    if a.eject or (not a.yes and confirm("\nEject the card now?", default=True)):
        eject(boot)


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #
def cmd_log(a):
    boot = find_boot_partition(a.boot)
    log = boot / LOG_FILE
    if not log.exists():
        print("No progress log on the card yet.")
        if (boot / KEY_FILE).exists():
            print("The auth key is still there, so setup never ran. Either the Pi did not boot\n"
                  "into Linux (check the green LED flickers during boot, check the power supply,\n"
                  "re-run `setup` which also repairs an empty cmdline.txt), or it was unplugged\n"
                  "within the first 2-3 minutes.")
        return
    lines = log.read_text(errors="replace").splitlines()
    print(f"--- {LOG_FILE} (last {a.lines} lines) ---")
    print("\n".join(lines[-a.lines:]))
    for extra in (ERROR_FILE,):
        if (boot / extra).exists() and (boot / extra).stat().st_size:
            print(f"\n--- {extra} ---\n" + (boot / extra).read_text(errors="replace")[-3000:])
    if a.report and (boot / REPORT_FILE).exists():
        print(f"\n--- {REPORT_FILE} ---\n" + (boot / REPORT_FILE).read_text(errors="replace"))

    print("\n--- What it means ---")
    text = "\n".join(lines)
    if "SUCCESS" in text:
        print("The Pi joined your tailnet. Find it in the Tailscale admin console.")
        return
    beats = [l for l in lines if " up=" in l]
    if not beats:
        print("The Pi booted but no status lines yet - it was probably unplugged too early.")
        return
    last = beats[-1]
    field = lambda k: (re.search(rf"{k}=(\S*)", last) or [None, ""])[1]  # noqa: E731
    ip, web, https, power = field("ip"), field("web"), field("https"), field("power")
    if power not in ("", "0x0"):
        print(f"* Power problem reported (throttled={power}). Use a proper 5V/2.5A+ supply.")
    if ip in ("[]", ""):
        print("* No IP address. Wi-Fi did not connect and/or Ethernet gave no address.")
        print("  - Check wifi_seen=[...] lists your network (SSID is case-sensitive).")
        print("  - Pi 3 / Zero W: 2.4 GHz only and no WPA3 - set the hotspot/router to WPA2 + 2.4 GHz.")
        print("  - Office/campus Ethernet ports may need the Pi's MAC address registered.")
    elif web == "204" or https == "200":
        print("* The Pi has internet. Tailscale should install shortly - check the errors file /")
        print("  re-run with --report. If 'tailscale up failed' appears, the auth key is invalid,")
        print("  expired or already used.")
    elif web == "000" and https == "000":
        print("* The Pi has an address but all web traffic is blocked (firewall).")
        print("  Try another network (phone hotspot, home router).")
    else:
        print(f"* The Pi has an address but web={web} https={https}: probably a captive portal /")
        print("  login page or a network that needs device registration. Try another network.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="prepare the SD card")
    s.add_argument("--boot", help="path to the mounted boot partition (auto-detected if omitted)")
    s.add_argument("--authkey-file", help="read the Tailscale auth key from this file (or set TS_AUTHKEY)")
    s.add_argument("--hostname", help="hostname for the Pi and its Tailscale machine name")
    s.add_argument("--wifi-ssid", help="Wi-Fi network to join (password from prompt or WIFI_PASSWORD)")
    s.add_argument("--no-wifi", action="store_true", help="remove Wi-Fi from the card (Ethernet only)")
    s.add_argument("--country", help="2-letter Wi-Fi country code, e.g. US, GB, DE")
    s.add_argument("--no-ssh", dest="ssh", action="store_false", help="don't enable Tailscale SSH")
    s.add_argument("--eject", action="store_true", help="eject the card when done (macOS)")
    s.add_argument("-y", "--yes", action="store_true", help="non-interactive; keep current Wi-Fi unless told otherwise")
    s.set_defaults(func=cmd_setup)

    l = sub.add_parser("log", help="read the Pi's progress log from the SD card")
    l.add_argument("--boot", help="path to the mounted boot partition")
    l.add_argument("-n", "--lines", type=int, default=15)
    l.add_argument("--report", action="store_true", help="also print the full network report")
    l.set_defaults(func=cmd_log)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
