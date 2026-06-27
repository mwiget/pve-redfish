#!/usr/bin/env python3
"""Unit tests for the per-NIC boot-order (AMI BootOptions/BootOrder) emulation.

These tests exercise the real helper functions in redfish-proxmox.py and replay
NICo's libredfish AMI client algorithm (set_boot_order_dpu_first /
is_boot_order_setup) against them, proving the wire contract closes without a
live Proxmox or NICo. Run: python3 tests/test_boot_order.py
"""
import importlib.util
import json
import os
import sys
import types

# --- stub proxmoxer so the single-file daemon imports without the dependency ---
os.environ["REDFISH_LOGGING_ENABLED"] = "false"
_prox = types.ModuleType("proxmoxer")
class _ProxmoxAPI:  # noqa: D401
    def __init__(self, *a, **k):
        pass
_prox.ProxmoxAPI = _ProxmoxAPI
_core = types.ModuleType("proxmoxer.core")
class _ResourceException(Exception):
    def __init__(self, status_code=500, *a, **k):
        self.status_code = status_code
        super().__init__(*a)
_core.ResourceException = _ResourceException
_prox.core = _core
sys.modules["proxmoxer"] = _prox
sys.modules["proxmoxer.core"] = _core

_path = os.path.join(os.path.dirname(__file__), "..", "redfish-proxmox.py")
_spec = importlib.util.spec_from_file_location("redfish_proxmox", _path)
rp = importlib.util.module_from_spec(_spec)
sys.modules["redfish_proxmox"] = rp
_spec.loader.exec_module(rp)


# --- minimal fake Proxmox API (records config.set calls) ----------------------
class _FakeConfig:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sets = []

    def get(self):
        return dict(self.cfg)

    def set(self, **kw):
        self.sets.append(kw)
        self.cfg.update(kw)
        return "UPID:fake:0"


class _FakeQemu:
    def __init__(self, cfg):
        self.config = _FakeConfig(cfg)


class _FakeNodes:
    def __init__(self, cfg):
        self._q = _FakeQemu(cfg)

    def qemu(self, _vmid):
        return self._q


class FakeProxmox:
    def __init__(self, cfg):
        self._n = _FakeNodes(cfg)

    def nodes(self, _node):
        return self._n


# --- replay of NICo's libredfish AMI client logic (ami.rs) --------------------
def _match_http_ipv4_mac(members, mac):
    mac = mac.upper()
    for o in members:
        d = o["DisplayName"].upper()
        if "HTTP" in d and "IPV4" in d and mac in d:
            return o
    return None


def nico_set_boot_order_dpu_first(members, boot_order, mac):
    """Mirror of ami.rs set_boot_order_dpu_first: returns the new BootOrder."""
    target = _match_http_ipv4_mac(members, mac)
    assert target is not None, f"no HTTP/IPv4 boot option for MAC {mac}"
    ref = target["BootOptionReference"]
    new = [r for r in boot_order if r != ref]
    new.insert(0, ref)
    return new


def nico_is_boot_order_setup(members, boot_order, mac):
    """Mirror of ami.rs is_boot_order_setup: expected == actual first option."""
    target = _match_http_ipv4_mac(members, mac)
    expected = target["DisplayName"] if target else None
    first_ref = boot_order[0] if boot_order else None
    actual = next((o["DisplayName"] for o in members
                   if o["BootOptionReference"] == first_ref), None)
    return expected is not None and expected == actual


# --- tests --------------------------------------------------------------------
DPU_MAC = "BC:24:11:AA:BB:CC"
OTHER_MAC = "BC:24:11:00:11:22"


def sample_config():
    return {
        "name": "dpf-host",
        "net0": f"virtio={DPU_MAC},bridge=vmbr0,firewall=1",
        "net1": f"e1000={OTHER_MAC},bridge=vmbr1",
        "scsi0": "local-zfs:vm-100-disk-0,size=64G",
        "ide2": "local:iso/ubuntu.iso,media=cdrom",
        "boot": "order=scsi0;net0",
    }


def t_parse_net_mac():
    assert rp.parse_net_mac(f"virtio={DPU_MAC},bridge=vmbr0") == DPU_MAC
    assert rp.parse_net_mac(f"e1000={OTHER_MAC},bridge=vmbr1,tag=10") == OTHER_MAC
    assert rp.parse_net_mac("virtio=,bridge=vmbr0") is None


def t_list_boot_devices_order():
    devs = rp.list_boot_devices(sample_config())
    keys = [d[0] for d in devs]
    # NICs first, then disks, then cd
    assert keys == ["net0", "net1", "scsi0", "ide2"], keys
    kinds = {d[0]: d[1] for d in devs}
    assert kinds["net0"] == "net" and kinds["scsi0"] == "disk" and kinds["ide2"] == "cd"


def t_display_name_matches_nico_predicate():
    name = rp.boot_option_display_name("net0", "net", DPU_MAC)
    up = name.upper()
    assert "HTTP" in up and "IPV4" in up and DPU_MAC in up, name


def t_current_boot_order_and_roundtrip():
    cfg = sample_config()
    assert rp.current_boot_order(cfg) == ["scsi0", "net0"]
    assert rp.boot_order_to_proxmox(["net0", "scsi0", "ide2"]) == "order=net0;scsi0;ide2"
    # dedup + drop empties
    assert rp.boot_order_to_proxmox(["net0", "net0", ""]) == "order=net0"
    # empty default falls back to device discovery order
    cfg2 = {k: v for k, v in cfg.items() if k != "boot"}
    assert rp.current_boot_order(cfg2) == ["net0", "net1", "scsi0", "ide2"]


def t_boot_options_collection_via_fake_proxmox():
    cfg = sample_config()
    coll = rp.get_boot_options_collection(FakeProxmox(cfg), 100)
    assert coll["Members@odata.count"] == 4
    net0 = next(m for m in coll["Members"] if m["Id"] == "net0")
    assert net0["BootOptionReference"] == "net0"
    assert DPU_MAC in net0["DisplayName"].upper()


def t_end_to_end_dpu_first():
    """Full replay: NICo sets DPU first, daemon applies it, NICo verifies true."""
    cfg = sample_config()
    fake = FakeProxmox(cfg)

    # 1. NICo reads BootOptions + BootOrder
    members = rp.get_boot_options_collection(fake, 100)["Members"]
    boot_order = rp.current_boot_order(cfg)

    # Pre-condition: DPU is NOT first yet -> is_boot_order_setup == False
    assert nico_is_boot_order_setup(members, boot_order, DPU_MAC) is False

    # 2. NICo computes DPU-first order and PATCHes /SD -> daemon translates it
    new_order = nico_set_boot_order_dpu_first(members, boot_order, DPU_MAC)
    proxmox_boot = rp.boot_order_to_proxmox(new_order)
    fake.nodes("n").qemu(100).config.set(boot=proxmox_boot)

    # The Proxmox boot string must now have the DPU NIC first
    assert proxmox_boot == "order=net0;scsi0", proxmox_boot
    assert cfg["boot"] == "order=net0;scsi0"

    # 3. NICo re-reads and verifies -> is_boot_order_setup == True
    members2 = rp.get_boot_options_collection(fake, 100)["Members"]
    boot_order2 = rp.current_boot_order(cfg)
    assert nico_is_boot_order_setup(members2, boot_order2, DPU_MAC) is True

    # Targeting the OTHER nic must select net1, not the DPU
    other_order = nico_set_boot_order_dpu_first(members2, boot_order2, OTHER_MAC)
    assert other_order[0] == "net1", other_order


# --- DPU passthrough (hostpci) boot interface -------------------------------
DPU_HOSTPCI_MAC = "AA:BB:CC:DD:EE:FF"


def passthrough_config():
    """A DPF-style host: BlueField DPU passed through as hostpci0 (no MAC in
    qm config), plus a virtio mgmt NIC and a disk."""
    return {
        "name": "dpf-host",
        "hostpci0": "0000:21:00,pcie=1,rombar=1",      # BlueField DPU PF
        "net0": f"virtio={OTHER_MAC},bridge=vmbr0",     # mgmt nic
        "scsi0": "local-zfs:vm-200-disk-0,size=64G",
        "boot": "order=scsi0",
    }


def _with_dpu_map(vmid, value):
    os.environ["REDFISH_DPU_MAC_MAP"] = json.dumps({str(vmid): value})


def _clear_dpu_map():
    os.environ.pop("REDFISH_DPU_MAC_MAP", None)
    os.environ.pop("REDFISH_DPU_MAC_MAP_FILE", None)


def t_dpu_mac_map_forms():
    cfg = passthrough_config()
    try:
        _with_dpu_map(200, DPU_HOSTPCI_MAC)                       # bare string
        assert rp.dpu_hostpci_macs(200, cfg) == {"hostpci0": DPU_HOSTPCI_MAC}
        _with_dpu_map(200, {"device": "hostpci0", "mac": DPU_HOSTPCI_MAC.lower()})
        assert rp.dpu_hostpci_macs(200, cfg) == {"hostpci0": DPU_HOSTPCI_MAC}
        _with_dpu_map(200, {"hostpci0": DPU_HOSTPCI_MAC})         # dict of devices
        assert rp.dpu_hostpci_macs(200, cfg) == {"hostpci0": DPU_HOSTPCI_MAC}
        assert rp.dpu_hostpci_macs(999, cfg) == {}                # unknown vmid
        _with_dpu_map(200, {"hostpci5": DPU_HOSTPCI_MAC})         # device not present
        assert rp.dpu_hostpci_macs(200, cfg) == {}
    finally:
        _clear_dpu_map()


def t_hostpci_first_in_boot_devices():
    cfg = passthrough_config()
    macs = {"hostpci0": DPU_HOSTPCI_MAC}
    devs = rp.list_boot_devices(cfg, macs)
    assert devs[0] == ("hostpci0", "hostpci", DPU_HOSTPCI_MAC), devs
    name = rp.boot_option_display_name("hostpci0", "hostpci", DPU_HOSTPCI_MAC).upper()
    assert "HTTP" in name and "IPV4" in name and DPU_HOSTPCI_MAC in name
    # Without the map, hostpci0 is NOT a boot option (no MAC, can't HTTP-boot).
    assert all(d[0] != "hostpci0" for d in rp.list_boot_devices(cfg))


def t_end_to_end_dpu_first_hostpci():
    """The real DPF case: order the passed-through DPU (hostpci0) first."""
    cfg = passthrough_config()
    try:
        _with_dpu_map(200, DPU_HOSTPCI_MAC)
        fake = FakeProxmox(cfg)
        macs = rp.dpu_hostpci_macs(200, cfg)

        members = rp.get_boot_options_collection(fake, 200)["Members"]
        order = rp.current_boot_order(cfg, macs)
        assert nico_is_boot_order_setup(members, order, DPU_HOSTPCI_MAC) is False

        new_order = nico_set_boot_order_dpu_first(members, order, DPU_HOSTPCI_MAC)
        proxmox_boot = rp.boot_order_to_proxmox(new_order)
        assert proxmox_boot == "order=hostpci0;scsi0", proxmox_boot
        fake.nodes("n").qemu(200).config.set(boot=proxmox_boot)

        members2 = rp.get_boot_options_collection(fake, 200)["Members"]
        order2 = rp.current_boot_order(cfg, rp.dpu_hostpci_macs(200, cfg))
        assert nico_is_boot_order_setup(members2, order2, DPU_HOSTPCI_MAC) is True
    finally:
        _clear_dpu_map()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
