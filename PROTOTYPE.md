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
| `GET /Systems/{id}/BootOptions[?$expand]` | `get_boot_options_collection` | one option per `netN`/disk/cdrom |
| `GET /Systems/{id}/BootOptions/{ref}` | `get_boot_option_detail` | single device |
| `PATCH /Systems/{id}/SD` `{"Boot":{"BootOrder":[...]}}` → `204` | `do_PATCH` (SD branch) | `qm set --boot order=...` |

Design choice: a BootOption's `BootOptionReference` **is** the Proxmox device key
(`net0`, `scsi0`, `ide2`…), so a `BootOrder` array maps straight back to
`boot: order=net0;scsi0;...` with no translation table. Each `netN` option's
`DisplayName` embeds its MAC (`UEFI HTTP IPv4: net0 (BC:24:11:..)`) so NICo's
MAC match succeeds. `PATCH /SD` accepts `If-Match: *` (libredfish always sends
the wildcard — no real ETag needed) and returns `204 No Content`.

## Tests

`tests/test_boot_order.py` stubs `proxmoxer`, loads the daemon by path, and
**replays NICo's AMI algorithm** against the real helpers: it asserts
`is_boot_order_setup` is false before, applies the daemon's `/SD` translation,
and asserts it is true after — and that targeting a different MAC selects the
other NIC. Run:

```
python3 tests/test_boot_order.py
```

No Proxmox or NICo required.

## Open items before a full host bring-up (NOT in this branch)

Advertising `Vendor: AMI` makes NICo use the AMI client for the *whole* host
path, so a few more AMI ops fire beyond boot order. Boot order is the hard,
novel piece and is done; these remain:

1. **`is_bios_setup` (AMI)** calls `diff_bios_bmc_attr()` — GETs `/Systems/{id}/Bios`
   and compares pending vs current attributes; empty diff ⇒ true. Needs a `/Bios`
   (+ `/Bios/Settings`) stub that reports no pending diff when no BIOS profile is
   configured for AMI in the NICo site config. (Confirm whether the site config
   has an AMI profile; if not, `machine_setup` is a no-op and the diff is empty.)
2. **`enable_ipmi_over_lan` / `is_ipmi_over_lan_enabled`** → `GET`/`PATCH
   /Managers/{id}/NetworkProtocol` (If-Match) — needs a `/Managers` + NetworkProtocol stub.
3. **`lockdown_bmc` (AMI)** → `PATCH /Managers/{id}/HostInterfaces/Self` — stub to `204`.
4. **`boot_once` / `set_boot_override` (AMI)** → `PATCH /Systems/{id}` (If-Match) expecting
   `204`. The existing `PATCH /Systems/{id}` returns `202`+task; add a `204` path when
   called with `If-Match` so the DPU/host one-shot HTTP boot works.

## Key validation question (architecture, not code)

The boot interface NICo targets is the **DPU NIC**. With PCI passthrough the DPU
is a `hostpciN` device, **not** a Proxmox `netN` virtio NIC, so it does not appear
in `qm`'s boot order the same way and its UEFI HTTP/PXE boot option is presented
by the DPU's own option ROM. Before relying on this shim, confirm how the VM's
UEFI enumerates the passed-through DPU as a boot option and whether Proxmox
`boot: order=` controls it. If not, the BootOption for the DPU must be synthesized
from the `hostpciN` device (and `/SD` may need to drive UEFI boot-next rather than
`qm --boot order`). The `netN` mechanism here is correct for virtio-NIC boot and is
the right scaffold to extend.
