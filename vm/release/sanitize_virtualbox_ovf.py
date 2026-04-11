#!/usr/bin/env python3
from __future__ import annotations

import io
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


OVF_NS = "http://schemas.dmtf.org/ovf/envelope/1"
RASD_NS = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
VSSD_NS = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
VBOX_NS = "http://www.virtualbox.org/ovf/machine"
NS = {"ovf": OVF_NS, "rasd": RASD_NS, "vssd": VSSD_NS, "vbox": VBOX_NS}


def qname(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def register_namespaces() -> None:
    ET.register_namespace("", OVF_NS)
    ET.register_namespace("ovf", OVF_NS)
    ET.register_namespace("rasd", RASD_NS)
    ET.register_namespace("vssd", VSSD_NS)
    ET.register_namespace("vbox", VBOX_NS)


def remove_standard_items(root: ET.Element) -> None:
    hardware = root.find(".//ovf:VirtualHardwareSection", NS)
    if hardware is None:
        return
    for item in list(hardware.findall("ovf:Item", NS)):
        resource_type = item.findtext("rasd:ResourceType", default="", namespaces=NS)
        # Remove empty IDE controllers and the appliance sound card. Both are
        # optional and make the exported OVA less portable across host OSes.
        if resource_type in {"5", "35"}:
            hardware.remove(item)


def remove_host_specific_vbox_data(root: ET.Element) -> None:
    machine = root.find(".//vbox:Machine", NS)
    if machine is None:
        return

    extra_data = machine.find(qname(OVF_NS, "ExtraData"))
    if extra_data is not None:
        machine.remove(extra_data)

    hardware = machine.find(qname(OVF_NS, "Hardware"))
    if hardware is None:
        return

    audio = hardware.find(qname(OVF_NS, "AudioAdapter"))
    if audio is not None:
        hardware.remove(audio)

    guest_props = hardware.find(qname(OVF_NS, "GuestProperties"))
    if guest_props is not None:
        hardware.remove(guest_props)

    network = hardware.find(qname(OVF_NS, "Network"))
    if network is not None:
        for adapter in list(network.findall(qname(OVF_NS, "Adapter"))):
            if adapter.attrib.get("slot") != "0":
                network.remove(adapter)
                continue
            for child_name in ("DisabledModes", "NAT"):
                child = adapter.find(qname(OVF_NS, child_name))
                if child is not None:
                    adapter.remove(child)

    clipboard = hardware.find(qname(OVF_NS, "Clipboard"))
    if clipboard is not None:
        hardware.remove(clipboard)

    storage = hardware.find(qname(OVF_NS, "StorageControllers"))
    if storage is not None:
        for controller in list(storage.findall(qname(OVF_NS, "StorageController"))):
            if controller.attrib.get("type") == "PIIX4":
                storage.remove(controller)


def normalize_ostype(root: ET.Element) -> None:
    os_section = root.find(".//ovf:OperatingSystemSection", NS)
    if os_section is not None:
        description = os_section.find("ovf:Description", NS)
        if description is not None:
            description.text = "Linux26_64"
        vbox_ostype = os_section.find("vbox:OSType", NS)
        if vbox_ostype is not None:
            vbox_ostype.text = "Linux26_64"

    machine = root.find(".//vbox:Machine", NS)
    if machine is not None:
        machine.attrib["OSType"] = "Linux26_64"


def normalize_vm_name(root: ET.Element, vm_name: str) -> None:
    normalized = vm_name.strip()
    if not normalized:
        return

    virtual_system = root.find(".//ovf:VirtualSystem", NS)
    if virtual_system is not None:
        virtual_system.attrib[qname(OVF_NS, "id")] = normalized

    system_identifier = root.find(".//vssd:VirtualSystemIdentifier", NS)
    if system_identifier is not None:
        system_identifier.text = normalized

    machine = root.find(".//vbox:Machine", NS)
    if machine is not None:
        machine.attrib["name"] = normalized


def sanitize_tree(root: ET.Element) -> ET.Element:
    remove_standard_items(root)
    remove_host_specific_vbox_data(root)
    normalize_ostype(root)
    normalize_vm_name(root, os.environ.get("ARIA_OVF_VM_NAME", ""))
    return root


def sanitize_ovf_bytes(raw_bytes: bytes) -> bytes:
    register_namespaces()
    root = ET.fromstring(raw_bytes)
    sanitize_tree(root)
    out = io.BytesIO()
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    return out.getvalue()


def sanitize_ovf_file(ovf_path: Path) -> None:
    ovf_path.write_bytes(sanitize_ovf_bytes(ovf_path.read_bytes()))


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: sanitize_virtualbox_ovf.py <ovf-path>", file=sys.stderr)
        return 1

    sanitize_ovf_file(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
