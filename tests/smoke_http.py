#!/usr/bin/env python3
"""Live HTTP smoke test: starts the real Redfish handler with a stubbed Proxmox
backend and exercises the AMI surface (service root, Bios, Managers, and the 204
boot-order / settings PATCHes) over a real socket. Run: python3 tests/smoke_http.py
"""
import importlib.util
import json
import os
import socketserver
import sys
import threading
import types
import urllib.request

os.environ["REDFISH_LOGGING_ENABLED"] = "false"
os.environ["REDFISH_DPU_MAC_MAP"] = json.dumps({"100": "AA:BB:CC:DD:EE:FF"})

# --- stub proxmoxer with a chainable fake backed by one VM ---------------------
VM_CONFIG = {
    "100": {
        "name": "dpf-host",
        "bios": "ovmf",
        "hostpci0": "0000:21:00,pcie=1,rombar=1",
        "net0": "virtio=BC:24:11:00:11:22,bridge=vmbr0",
        "scsi0": "local-zfs:vm-100-disk-0,size=64G",
        "boot": "order=scsi0",
    }
}


class _Config:
    def __init__(self, vmid):
        self.vmid = str(vmid)

    def get(self):
        return dict(VM_CONFIG[self.vmid])

    def set(self, **kw):
        VM_CONFIG[self.vmid].update(kw)
        return "UPID:fake:0"


class _Status:
    class current:
        @staticmethod
        def get():
            return {"status": "stopped"}


class _Qemu:
    def __init__(self, vmid=None):
        self.vmid = vmid
        self.config = _Config(vmid) if vmid is not None else None
        self.status = _Status()

    def __call__(self, vmid):
        return _Qemu(vmid)

    def get(self):  # list VMs
        return [{"vmid": int(v)} for v in VM_CONFIG]


class _Nodes:
    def qemu(self, vmid=None):
        return _Qemu(vmid)

    @property
    def qemu_attr(self):
        return _Qemu()


class _NodesCallable:
    def __call__(self, _node):
        n = _Nodes()
        # support both proxmox.nodes(N).qemu.get() and .qemu(id)
        n.qemu = _Qemu()
        return n


class _FakeProxmoxAPI:
    def __init__(self, *a, **k):
        pass

    def nodes(self, _node):
        n = _Nodes()
        n.qemu = _Qemu()
        return n


_prox = types.ModuleType("proxmoxer")
_prox.ProxmoxAPI = _FakeProxmoxAPI
_core = types.ModuleType("proxmoxer.core")
class _RE(Exception):
    def __init__(self, status_code=500, *a, **k):
        self.status_code = status_code
        super().__init__(*a)
_core.ResourceException = _RE
_prox.core = _core
sys.modules["proxmoxer"] = _prox
sys.modules["proxmoxer.core"] = _core

_path = os.path.join(os.path.dirname(__file__), "..", "redfish-proxmox.py")
_spec = importlib.util.spec_from_file_location("redfish_proxmox", _path)
rp = importlib.util.module_from_spec(_spec)
sys.modules["redfish_proxmox"] = rp
_spec.loader.exec_module(rp)

rp.AUTH = None
rp.SECURE = None
rp.PROXMOX_NODE = "node"


def req(method, path, body=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json", "If-Match": "*"})
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))


def run_checks():
    # service root advertises AMI + Managers
    st, body = req("GET", "/redfish/v1")
    check("serviceroot vendor=AMI", body.get("Vendor") == "AMI", body.get("Vendor"))
    check("serviceroot has Managers", "Managers" in body)

    # BootOptions: DPU hostpci0 present with HTTP/IPv4/MAC
    st, body = req("GET", "/redfish/v1/Systems/100/BootOptions")
    names = {m["Id"]: m["DisplayName"].upper() for m in body.get("Members", [])}
    check("hostpci0 boot option", "hostpci0" in names, list(names))
    check("hostpci0 HTTP/IPv4/MAC", "hostpci0" in names
          and all(t in names["hostpci0"] for t in ("HTTP", "IPV4", "AA:BB:CC:DD:EE:FF")))

    # Bios: seeded attrs + Settings link
    st, body = req("GET", "/redfish/v1/Systems/100/Bios")
    a = body.get("Attributes", {})
    check("bios FBO001=UEFI", a.get("FBO001") == "UEFI")
    check("bios NWSK006=Enabled", a.get("NWSK006") == "Enabled")
    check("bios Settings->/Bios/SD",
          body.get("@Redfish.Settings", {}).get("SettingsObject", {}).get("@odata.id", "").endswith("/Bios/SD"))

    # Manager NetworkProtocol + HostInterface
    st, body = req("GET", "/redfish/v1/Managers/BMC/NetworkProtocol")
    check("ipmi enabled", body.get("IPMI", {}).get("ProtocolEnabled") is True)
    st, body = req("GET", "/redfish/v1/Managers/BMC/HostInterfaces/Self")
    check("host iface enabled", body.get("InterfaceEnabled") is True)

    # PATCH /Bios/SD -> 204, and it merges
    st, _ = req("PATCH", "/redfish/v1/Systems/100/Bios/SD", {"Attributes": {"NWSK006": "Disabled"}})
    check("PATCH /Bios/SD -> 204", st == 204, st)
    st, body = req("GET", "/redfish/v1/Systems/100/Bios")
    check("Bios/SD merge took", body["Attributes"].get("NWSK006") == "Disabled")

    # PATCH /Managers NetworkProtocol + HostInterface -> 204
    st, _ = req("PATCH", "/redfish/v1/Managers/BMC/NetworkProtocol", {"IPMI": {"ProtocolEnabled": True}})
    check("PATCH NetworkProtocol -> 204", st == 204, st)
    st, _ = req("PATCH", "/redfish/v1/Managers/BMC/HostInterfaces/Self", {"InterfaceEnabled": False})
    check("PATCH HostInterface -> 204", st == 204, st)

    # PATCH /SD BootOrder (hostpci0 first) -> 204, reflected in Proxmox config
    st, _ = req("PATCH", "/redfish/v1/Systems/100/SD", {"Boot": {"BootOrder": ["hostpci0", "scsi0"]}})
    check("PATCH /SD BootOrder -> 204", st == 204, st)
    check("qm boot order applied", VM_CONFIG["100"]["boot"] == "order=hostpci0;scsi0",
          VM_CONFIG["100"]["boot"])

    # boot_once / set_boot_override (AMI): PATCH /Systems/{id} with If-Match -> 204
    VM_CONFIG["100"]["boot"] = "order=scsi0"
    st, _ = req("PATCH", "/redfish/v1/Systems/100",
                {"Boot": {"BootSourceOverrideTarget": "UefiHttp",
                          "BootSourceOverrideEnabled": "Once",
                          "BootSourceOverrideMode": "UEFI"}})
    check("boot_once -> 204", st == 204, st)
    check("boot_once put DPU first", VM_CONFIG["100"]["boot"] == "order=hostpci0;scsi0",
          VM_CONFIG["100"]["boot"])

    # POST Bios.ChangePassword + Manager.Reset -> 2xx
    st, _ = req("POST", "/redfish/v1/Systems/100/Bios/Actions/Bios.ChangePassword",
                {"PasswordName": "SETUP001", "OldPassword": "", "NewPassword": "x"})
    check("POST ChangePassword 2xx", 200 <= st < 300, st)
    st, _ = req("POST", "/redfish/v1/Managers/BMC/Actions/Manager.Reset", {})
    check("POST Manager.Reset 2xx", 200 <= st < 300, st)


PORT = 0


def main():
    global PORT
    httpd = socketserver.TCPServer(("127.0.0.1", 0), rp.RedfishRequestHandler)
    PORT = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        run_checks()
    finally:
        httpd.shutdown()

    failed = 0
    for name, ok, detail in CHECKS:
        if ok:
            print(f"  PASS {name}")
        else:
            failed += 1
            print(f"  FAIL {name}  ({detail})")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
