# Running the shim for a NICo / DPF Proxmox host

This walks through standing up the pve-redfish daemon as the **emulated BMC** for
a Proxmox VM that NVIDIA **NICo** provisions as a DPF host (BlueField DPU passed
through via PCI passthrough). It covers the VM prerequisites, the daemon install,
the DPU MAC map, pointing NICo at it, and the on-host checks that close out the
open questions in [PROTOTYPE.md](PROTOTYPE.md).

> The boot-order + AMI BIOS/Manager emulation is a **prototype**. Validate on a
> throwaway host first. See the caveats at the end.
>
> Tracking PR: **[mwiget/pve-redfish#1](https://github.com/mwiget/pve-redfish/pull/1)**

---

## 0. Topology

```
NICo (site controller)  --HTTPS/Redfish-->  pve-redfish daemon  --PVE API-->  Proxmox node
                                            (emulated AMI BMC)                 └─ VM (DPF host)
                                                                                   └─ hostpci0 = BlueField DPU
host boots off the DPU  <--UEFI HTTP boot (DHCP opt 67)--  DPU (DHCP/HTTP server, real Redfish)
```

NICo talks Redfish to the daemon for the **host** (power, boot order, BIOS). The
**DPU** has its own real BMC/rshim and is managed over real Redfish — the daemon
is not involved there.

---

## 1. Prepare the VM (the DPF host)

The VM must be OVMF/UEFI with the DPU passed through so OVMF can HTTP-boot off it:

```bash
# q35 + OVMF + an EFI disk (UEFI boot order lives here)
qm set <vmid> --machine q35
qm set <vmid> --bios ovmf
qm set <vmid> --efidisk0 <storage>:1,efitype=4m,pre-enrolled-keys=0

# BlueField DPU PF as hostpci0 -- rombar=1 is REQUIRED so OVMF loads the DPU's
# UEFI NIC driver and exposes an HTTP boot option.
qm set <vmid> --hostpci0 <DPU_PCI_ADDR>,pcie=1,rombar=1
```

Confirm:

```bash
qm config <vmid> | egrep 'bios|machine|efidisk|hostpci0'
#   bios: ovmf
#   machine: q35
#   hostpci0: 0000:21:00,pcie=1,rombar=1     <- rombar=1 present
pveversion                                    # PVE 8.x supports boot: order=hostpci
```

Note the DPU PF **MAC address** (from the card/label or NICo's inventory) — you
need it for the MAC map in step 3.

---

## 2. Install the daemon (on the Proxmox node)

```bash
sudo mkdir -p /opt/redfish_daemon
sudo cp redfish-proxmox.py /opt/redfish_daemon/
sudo python3 -m venv /opt/redfish_daemon/venv
sudo /opt/redfish_daemon/venv/bin/pip install proxmoxer requests

# TLS cert for HTTPS/Redfish (self-signed is fine for a lab; NICo expects https)
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout /opt/redfish_daemon/key.pem -out /opt/redfish_daemon/cert.pem \
  -subj "/CN=pve-redfish"
```

Create a Proxmox API user the daemon authenticates *to PVE* with (or reuse
`root@pam`):

```bash
pveum user add redfish@pve --password '<pw>'
pveum aclmod / --user redfish@pve --role PVEVMAdmin   # power + config.set on VMs
```

---

## 3. Configure (systemd)

The shipped `redfish-proxmox.service` runs `ExecStart=… -A None` (no Redfish auth;
the daemon uses the env credentials to reach PVE) with TLS on **:443**. Add the
DPU MAC map and your PVE credentials via a drop-in:

```bash
sudo cp redfish-proxmox.service /etc/systemd/system/
sudo systemctl edit redfish-proxmox    # creates an override.conf
```

Put in the override (one entry per VM you manage; key = vmid):

```ini
[Service]
Environment="PROXMOX_HOST=127.0.0.1"
Environment="PROXMOX_USER=redfish@pve"
Environment="PROXMOX_PASSWORD=<pw>"
Environment="PROXMOX_NODE=<pve-node-name>"
Environment="VERIFY_SSL=false"
Environment=REDFISH_DPU_MAC_MAP={"200":"AA:BB:CC:DD:EE:FF"}
```

The map value may be a bare MAC (first `hostpciN`), `{"device":"hostpci1","mac":..}`,
or `{"hostpci0":..}`. For many VMs, use a file instead:

```ini
Environment="REDFISH_DPU_MAC_MAP_FILE=/etc/redfish_daemon/dpu-macs.json"
```
```json
{ "200": "AA:BB:CC:DD:EE:FF", "201": "AA:BB:CC:DD:EE:01" }
```

Start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now redfish-proxmox
journalctl -u redfish-proxmox -f      # set REDFISH_LOGGING_ENABLED=true for verbose
```

> **One instance vs. one-per-VM.** A single daemon serves every VM on the node
> (Redfish system id = vmid). That is fine for the provisioning (unlocked) path.
> If you enable host **lockdown** in NICo, run **one daemon per VM** (separate
> port/IP) so the BMC `/Managers` state is per-host — see the caveat at the end.

---

## 4. Smoke-test from another host

```bash
# service root must report the AMI vendor (so NICo picks the AMI client)
curl -sk https://<node>:443/redfish/v1 | python3 -m json.tool | egrep 'Vendor|Managers'
#   "Vendor": "AMI",

# the DPU must appear as an HTTP/IPv4 boot option carrying its MAC
curl -sk https://<node>:443/redfish/v1/Systems/<vmid>/BootOptions \
  | python3 -c 'import sys,json;[print(m["Id"],m["DisplayName"]) for m in json.load(sys.stdin)["Members"]]'
#   hostpci0  UEFI HTTP IPv4: hostpci0 (AA:BB:CC:DD:EE:FF)
```

If the daemon needs Redfish auth instead of `-A None`, start it with `-A Session`
or `-A Basic`; NICo then authenticates with credentials that are valid **Proxmox**
users (the daemon proxies them to the PVE API).

---

## 5. Point NICo at it

Register the host's BMC in NICo with:

- **Redfish endpoint**: `https://<node>:443` (or the per-VM port)
- **System id**: the Proxmox `<vmid>` (NICo addresses `/redfish/v1/Systems/<vmid>`)
- **Credentials**: per the `-A` mode chosen above
- Vendor is auto-detected as **AMI** from the service root.

NICo's normal DPF wiring still applies: the **DPU** serves DHCP/PXE/HTTP boot to
the host, so the host's UEFI HTTP-boots off `hostpci0` once it is first in the
boot order (which NICo sets via this shim).

Then start the machine lifecycle in NICo and watch it traverse
`Discovered → … → SetBootOrder → … → Ready`.

---

## 6. Validate the two host-only unknowns

These are environment facts the shim can't settle (see PROTOTYPE.md):

1. **Boot order actually applies.** After NICo's `SetBootOrder` + power-cycle,
   from inside the guest:
   ```bash
   efibootmgr        # the HTTP IPv4 / hostpci0 entry should be BootOrder[0]
   ```
   `qm config <vmid> | grep boot` on the node should show `order=hostpci0;…`.
2. **It persists across the cycle.** If OVMF's efivars override the host-set order
   after the first boot, switch the `/SD` handler to a one-shot `BootNext` via
   `virt-fw-vars` against the efidisk (DPF-faithful: the DPU drives the URI via
   DHCP option 67). Tracked in PROTOTYPE.md.

---

## Caveats

- **Prototype.** Emulates the libredfish AMI host bring-up surface (boot order
  incl. hostpci DPU, BIOS attributes, IPMI, lockdown, manager, `boot_once`).
  BIOS/Manager state is in-memory per daemon process (emulation, not real VM
  state) and resets on restart.
- **Lockdown ENABLE.** The seed leaves the host unlocked/provisioning-ready. The
  final `WaitingForLockdown` only converges if NICo uses the full `lockdown()`
  (sets BIOS + HostInterface, which we merge). If it uses `lockdown_bmc()` alone,
  **disable host-lockdown policy in the NICo site config** or run **one daemon per
  VM**.
- **rombar=1** on the DPU `hostpciN` is mandatory, else OVMF exposes no HTTP boot
  option and there is nothing to order first.
- Run against a **throwaway host** until the two validations above pass.

## Tests (no Proxmox/NICo needed)

```bash
python3 tests/test_boot_order.py    # unit: pure helpers + AMI algorithm replay
python3 tests/smoke_http.py         # live-HTTP: full AMI surface over a socket
```
