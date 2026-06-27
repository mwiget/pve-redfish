# Prototype: per-NIC boot order for NVIDIA NICo

This branch (`feat/per-nic-boot-order`) teaches the pve-redfish daemon to satisfy
NVIDIA **NICo**'s host boot-order sequencing when a Proxmox VM is used as a
NICo-managed host (with a BlueField DPU passed through via PCI passthrough).

## Why this is needed

NICo provisions a host by PXE/HTTP-booting it off the **DPU NIC**. Its
`libredfish` client does this per-NIC, not with a generic
`BootSourceOverrideTarget=Pxe`:

```
set_boot_order_dpu_first(mac):
  GET  /redfish/v1/Systems/{id}                         -> Boot.BootOrder[] + Boot.BootOptions
  GET  /redfish/v1/Systems/{id}/BootOptions?$expand=... -> [{BootOptionReference, DisplayName}, ...]
  pick option whose DisplayName (upper) contains "HTTP" AND "IPV4" AND <MAC>
  move its BootOptionReference to the front of BootOrder
  PATCH /redfish/v1/Systems/{id}/SD   (If-Match: *)   {"Boot":{"BootOrder":[...]}}  -> 204

is_boot_order_setup(mac):
  re-read the two GETs; true iff the HTTP/IPv4/<MAC> option is first in BootOrder
```

Crucially, **the generic libredfish client returns `NotSupported`** for
`set_boot_order_dpu_first` / `is_boot_order_setup`. Only the vendor clients
implement them. So the daemon advertises **`"Vendor": "AMI"`** in the service
root, which makes NICo select libredfish's AMI client (the cleanest, most
standard-Redfish of the vendor implementations, and the BlueField BMC stack).

## What this branch implements

| Redfish surface | Handler | Maps to Proxmox |
| --- | --- | --- |
| `GET /redfish/v1` adds `"Vendor":"AMI"` | `do_GET` service root | — (vendor selection) |
| `GET /Systems/{id}` Boot gains `BootOrder[]` + `BootOptions` | `get_vm_status` | parses `boot: order=...` |
| `GET /Systems/{id}/BootOptions[?$expand]` | `get_boot_options_collection` | one option per `hostpciN`(DPU)/`netN`/disk/cdrom |
| `GET /Systems/{id}/BootOptions/{ref}` | `get_boot_option_detail` | single device |
| `PATCH /Systems/{id}/SD` `{"Boot":{"BootOrder":[...]}}` → `204` | `do_PATCH` (SD branch) | `qm set --boot order=...` |

Design choice: a BootOption's `BootOptionReference` **is** the Proxmox device key
(`hostpci0`, `net0`, `scsi0`, `ide2`…), so a `BootOrder` array maps straight back
to `boot: order=hostpci0;scsi0;...` with no translation table. Each boot NIC's
`DisplayName` embeds its MAC (`UEFI HTTP IPv4: hostpci0 (AA:BB:..)`) so NICo's
MAC match succeeds. `PATCH /SD` accepts `If-Match: *` (libredfish always sends
the wildcard — no real ETag needed) and returns `204 No Content`.

### DPU boot interface (PCI passthrough)

In the DPF lab the host boots off the **BlueField DPU**, which is a `hostpciN`
passthrough device — not a virtio `netN` — so its MAC is **not** in `qm config`.
Modern Proxmox accepts `boot: order=hostpci0` (a PVE staff member confirmed the
boot-order window lists hostpci devices), so the same `qm --boot order` lever
works; we just need the DPU's PF MAC out-of-band to label its BootOption.

Supply it via env (keyed by VM id; value may be a bare MAC, `{"device","mac"}`,
or `{hostpciN: mac}`):

```bash
export REDFISH_DPU_MAC_MAP='{"200": "AA:BB:CC:DD:EE:FF"}'
# or a file:
export REDFISH_DPU_MAC_MAP_FILE=/etc/redfish-proxmox/dpu-macs.json
```

Only mapped, present `hostpciN` devices become bootable network options;
unmapped passthrough devices (GPUs, etc.) are ignored. The passthrough device
also needs **`rombar=1`** so OVMF loads the DPU's UEFI NIC driver and exposes an
HTTP boot option at all.

## Tests

`tests/test_boot_order.py` stubs `proxmoxer`, loads the daemon by path, and
**replays NICo's AMI algorithm** against the real helpers: it asserts
`is_boot_order_setup` is false before, applies the daemon's `/SD` translation,
and asserts it is true after — and that targeting a different MAC selects the
other NIC. Run:

```
python3 tests/test_boot_order.py   # 13 unit tests (pure helpers + AMI algorithm replay)
python3 tests/smoke_http.py        # 17 live-HTTP checks over a real socket
```

No Proxmox or NICo required (both stub `proxmoxer`).

## AMI BIOS + Manager stubs (implemented)

Advertising `Vendor: AMI` makes NICo use the AMI client for the *whole* host
path, so several attribute-based ops fire beyond boot order. These are now
stubbed with a small in-memory store (`_BIOS_STATE` per VM, `_MGR_STATE` for the
emulated BMC) **seeded with the exact values libredfish's AMI client expects**
(from `ami.rs`: `serial_console_attrs` + `machine_setup_attrs`), so set/verify
loops converge on the first read:

| AMI op | Endpoint | Behaviour |
| --- | --- | --- |
| `is_bios_setup` → `diff_bios_bmc_attr` | `GET /Systems/{id}/Bios` | `Attributes` seeded so the diff is empty ⇒ true |
| `machine_setup` / `set_bios` | `PATCH /Systems/{id}/Bios/SD` (If-Match) | merges Attributes → `204` |
| `change_uefi_password` | `POST /Systems/{id}/Bios/Actions/Bios.ChangePassword` | stubbed `200` |
| `factory_reset_bios` | `POST /Systems/{id}/Bios/Actions/Bios.ResetBios` | re-seed → `200` |
| `is_ipmi_over_lan_enabled` | `GET /Managers/{id}/NetworkProtocol` | `IPMI.ProtocolEnabled` (seeded true) |
| `enable_ipmi_over_lan` | `PATCH /Managers/{id}/NetworkProtocol` (If-Match) | toggles state → `204` |
| `lockdown_status` | `GET /Bios` (KCSACP/USB000) + `GET /Managers/{id}/HostInterfaces/Self` | seeded **unlocked** |
| `lockdown_bmc` | `PATCH /Managers/{id}/HostInterfaces/Self` (If-Match) | toggles `InterfaceEnabled` → `204` |
| `bmc_reset` | `POST /Managers/{id}/Actions/Manager.Reset` | stubbed `200` |
| `get_manager` / managers collection | `GET /Managers[/{id}]` | minimal Manager |

`tests/smoke_http.py` exercises all of the above over a real socket (17 checks).

### Caveat: lockdown-ENABLE convergence

`lockdown_status` computes *locked* from BIOS `KCSACP="Deny All"` + `USB000="Disabled"`
**and** HostInterface `InterfaceEnabled=false`. The full AMI `lockdown()` sets all
three (BIOS via `/Bios/SD`, which we merge), so it converges. But `lockdown_bmc()`
alone only toggles the HostInterface, so it cannot drive `lockdown_status` to
*locked*. If NICo's final `WaitingForLockdown` uses `lockdown_bmc` (not `lockdown`),
**disable host-lockdown policy in the NICo site config** (that state then skips to
`BomValidating`), or run **one shim instance per VM** so the manager state is truly
per-host. The provisioning (unlocked) path — what gets the host *to* Ready — is fully
coherent as-is.

## Still open (NOT in this branch)

- **`boot_once` / `set_boot_override` (AMI)** → `PATCH /Systems/{id}` (If-Match) expecting
  `204`. The existing `PATCH /Systems/{id}` returns `202`+task; add a `204` path when
  called with `If-Match` so the DPU/host one-shot HTTP boot works. (Used on the DPU
  install path, not strictly the host boot-order path.)

## hostpci boot — resolved, with two caveats to verify on the host

The boot interface NICo targets is the **DPU**, a `hostpciN` passthrough device.
Two findings resolved the open question:

- **Modern Proxmox accepts `boot: order=hostpci0`** (PVE staff confirmed the boot
  order window lists hostpci devices), so the `qm --boot order` lever is correct —
  this branch now enumerates `hostpciN` and orders it like any other device.
  Older PVE needs the `args: -device vfio-pci,...,bootindex=N` workaround instead.

Two things still to confirm on the actual host (commands, not code):

1. **`rombar` + PVE version.** `qm config <vmid>` — the DPU must be `hostpciN` with
   `rombar=1` (else OVMF exposes no HTTP boot option); `pveversion` — confirm the
   build supports hostpci in boot order vs. the `args bootindex` fallback.
2. **OVMF efivars persistence.** OVMF-on-QEMU usually re-applies the Proxmox/fw_cfg
   boot order each boot, but once OVMF writes its own `BootOrder` to efivars (in the
   efidisk) it can override the host-set order. Since NICo *sets boot order then
   power-cycles*, verify the `hostpci0`-first entry sticks: from the guest after a
   cycle, `efibootmgr` should show it as `BootOrder[0]`. If it doesn't, switch the
   `/SD` handler to set a one-shot `BootNext` via `virt-fw-vars` against the efidisk
   (the DPF-faithful path: the DPU then drives the boot URI via DHCP option 67).
