# libredfish AMI client — wire contract reference

Reference notes for the AMI host-bring-up surface this shim emulates. Everything
below was read from **`github.com/NVIDIA/libredfish` tag `v0.44.16`** (the version
NICo `infra-controller` pins in `Cargo.lock`). File:line citations are into that
checkout. This is the source of truth for `redfish-proxmox.py`'s AMI behaviour —
keep it in sync if you bump the libredfish version.

> Why this matters: the **generic/standard** libredfish client returns
> `NotSupported` for `set_boot_order_dpu_first`, `is_boot_order_setup`,
> `boot_once`, `set_boot_override`, and `is_bios_setup` (`src/standard.rs`). Only
> the **vendor** clients implement them. So the shim advertises `Vendor: AMI` in
> the service root to make NICo select the AMI client, then speaks its dialect.

## Vendor selection

- `ServiceRoot.vendor` (`"Vendor"` field) → `RedfishVendor` via
  `model/service_root.rs:96` (`"ami"` ⇒ `RedfishVendor::AMI`). Falls back to the
  single OEM key if `Vendor` is absent (`vendor_string()`, `service_root.rs:87`).
- `RedfishVendor::AMI` selects the AMI client impl in `src/ami.rs`.
- AMI is the cleanest, most standard-Redfish of the vendor clients (it is the
  BlueField BMC stack), which is why we emulate it rather than Dell/HPE/Lenovo.

## Auth — `patch_with_if_match`

Mutating AMI calls use `network.rs:459` `patch_with_if_match`, which sends a
literal **`If-Match: *`** (wildcard — no real ETag needed) and treats **`204 No
Content`** as success, anything else as an error. The shim therefore returns `204`
with no body for these, and may ignore the `If-Match` value.

## Boot order (the core path)

| Op (`ami.rs`) | HTTP | Body / decision |
| --- | --- | --- |
| `set_boot_order_dpu_first` (1035) | `GET /Systems/{id}` then `GET /Systems/{id}/BootOptions?$expand=.($levels=1)` then `PATCH /Systems/{id}/SD` (If-Match) | find BootOption whose `DisplayName.upper()` contains `HTTP` **and** `IPV4` **and** `<MAC>`; move its `BootOptionReference` to front of `Boot.BootOrder`; PATCH `{"Boot":{"BootOrder":[...]}}` → `204` |
| `is_boot_order_setup` (1077) | same two GETs | true iff the HTTP/IPv4/`<MAC>` option's reference is `BootOrder[0]` (`get_expected_and_actual_first_boot_option`, 1407) |
| `change_boot_order` (712) | `PATCH /Systems/{id}/SD` (If-Match) | `{"Boot":{"BootOrder":[refs…]}}` → `204` |

- MAC is resolved via `resolve_boot_interface_mac` and upper-cased.
- `get_system_and_boot_options` (1339) reads `Boot.BootOptions` (an `@odata.id`)
  then fetches the collection **expanded**, parsing each Member as a full
  `BootOption` (`model/system.rs:259`: `BootOptionReference`, `DisplayName`, `Id`).
- `Boot` model (`model/boot.rs`): `BootOrder` is `Vec<String>` of references;
  `BootOptions` is an `@odata.id`. PascalCase field names.

### Shim mapping

`BootOptionReference` = the Proxmox device key (`hostpci0`/`net0`/`scsi0`/…), so a
`BootOrder` array maps straight to `boot: order=hostpci0;scsi0;…`. The boot NIC's
`DisplayName` embeds its MAC: `UEFI HTTP IPv4: hostpci0 (AA:BB:..)`. The DPU is a
`hostpciN` passthrough (no MAC in `qm config`), so the MAC is supplied out-of-band
via `REDFISH_DPU_MAC_MAP`.

## One-shot override

| Op (`ami.rs`) | HTTP | Body |
| --- | --- | --- |
| `set_boot_override` (≈710) / `boot_once` (643) | `PATCH /Systems/{id}` (If-Match) | `{"Boot":{"BootSourceOverrideTarget":"<Pxe\|UefiHttp\|Hdd>","BootSourceOverrideEnabled":"<Once\|Continuous\|Disabled>","BootSourceOverrideMode":"UEFI"[,"HttpBootUri":..]}}` → `204` |

Note the comment in `ami.rs`: AMI patches `/Systems/{id}` (**not** `/SD`) for the
override; mode defaults to `UEFI`. The shim keys this off the `If-Match` header so
the existing sushy/ironic `202`+task path (no If-Match) is preserved.

## BIOS (attribute-based)

| Op | HTTP | Notes |
| --- | --- | --- |
| `is_bios_setup` (461) → `diff_bios_bmc_attr` (1467) | `GET /Systems/{id}/Bios` | empty diff ⇒ true. Diff = serial-console check **+** machine-setup attrs |
| `bios()` (standard.rs:277) / `bios_attributes` (1499) | `GET /Systems/{id}/Bios` | reads the `Attributes` object |
| `set_bios` (833) | `PATCH /Systems/{id}/Bios/SD` (If-Match) | `{"Attributes":{…}}` → `204` |
| `change_uefi_password` (≈995) → `change_bios_password` (standard.rs:1662) | `POST /Systems/{id}/Bios/Actions/Bios.ChangePassword` | `{"PasswordName":"SETUP001","OldPassword":..,"NewPassword":..}` (AMI `UEFI_PASSWORD_NAME = "SETUP001"`, `ami.rs:58`) |
| `factory_reset_bios` (standard.rs:1510) | `POST /Systems/{id}/Bios/Actions/Bios.ResetBios` | — |

### Expected attribute values (non-Lenovo AMI)

These are what `diff_bios_bmc_attr` compares against; seed `/Systems/{id}/Bios`
`Attributes` with them so the first diff is empty.

`serial_console_attrs` (`ami.rs:96`, must equal the "enabled" value):

```
TER001=Enabled  TER010=Enabled  TER06B=COM1   TER0021=115200
TER0020=115200  TER012=VT100Plus TER011=VT-UTF8 TER05D=None
```

`machine_setup_attrs` (`ami.rs:1433`):

```
VMXEN=Enable  PCIS007=Enabled  LEM0001=3      NWSK000=Enabled
NWSK001=Disabled NWSK006=Enabled NWSK002=Disabled NWSK007=Disabled
FBO001=UEFI   EndlessBoot=Enabled
```

`set_bios`/`change_boot_order`/etc. use `/Bios/SD` for *pending* settings; the shim
collapses pending into current (applies the PATCH immediately).

## Manager / BMC

| Op (`ami.rs` / `standard.rs`) | HTTP | Body / fields |
| --- | --- | --- |
| `is_ipmi_over_lan_enabled` (1113) → `get_manager_network_protocol` (standard.rs:1687) | `GET /Managers/{id}/NetworkProtocol` | reads `IPMI.ProtocolEnabled` (`model/manager_network_protocol.rs`) |
| `enable_ipmi_over_lan` (1120) | `PATCH /Managers/{id}/NetworkProtocol` (If-Match) | `{"IPMI":{"ProtocolEnabled":<bool>}}` → `204` |
| `lockdown_status` (544) | `GET /Systems/{id}/Bios` (`KCSACP`, `USB000`) + `GET /Managers/{id}/HostInterfaces/Self` (`InterfaceEnabled`) | `is_locked = KCSACP=="Deny All" && USB000=="Disabled" && !InterfaceEnabled`; `is_unlocked` = the inverse |
| `lockdown_bmc` (1101) | `PATCH /Managers/{id}/HostInterfaces/Self` (If-Match) | `{"InterfaceEnabled":<bool>}` → `204` |
| `lockdown` (492, full) | BIOS (`KCSACP`/`USB000` via `set_bios` → `/Bios/SD`) + HostInterface | sets all three lock inputs |
| `bmc_reset` (338) / `Manager.Reset` (standard.rs:1725) | `POST /Managers/{id}/Actions/Manager.Reset` | — |
| `get_managers` (standard.rs) | `GET /Managers` | first collection member id = `manager_id` |

`manager_id` is taken from the first `/Managers` collection member; the shim
serves a single `BMC` manager. `HostInterfaces/Self` is hardcoded in libredfish.

### Lockdown convergence caveat

`lockdown_status` reports *locked* only when BIOS (`KCSACP`/`USB000`) **and**
HostInterface agree. The full `lockdown()` sets both (BIOS via `/Bios/SD`, which
the shim merges) and converges; `lockdown_bmc()` alone touches only the
HostInterface and cannot. The shim multiplexes all VMs under one `/Managers`, so
if NICo's `WaitingForLockdown` uses `lockdown_bmc`, disable host-lockdown policy
in NICo or run one shim per VM. See PROTOTYPE.md.

## Ops the host path never reaches (on a generic/AMI host)

Dell iDRAC OEM (`set_idrac_lockdown`, `get_boss_controller`,
`decommission_storage_controller`), SecureBoot, and `UpdateService` firmware are
DPU-side or Dell-only and are not exercised on the AMI host path — the shim does
not implement them. (The DPU itself is managed over its own real Redfish.)
