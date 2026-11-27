"""Build the reviewable eisy-onewire plugin archive."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "eisy-onewire.iox_plugin.json"
PACKAGE_FILES = (
    "plugin.py",
    "diagnostic.py",
    "eisy-onewire.iox_plugin.json",
    "ds9490.conf",
    "install.sh",
    "requirements.txt",
    "POLYGLOT_CONFIG.md",
    "default-sensors.json",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "src/eisy_onewire/__init__.py",
    "src/eisy_onewire/plugin_wrapper.py",
    "src/eisy_onewire/adapter_exclusion.py",
    "src/eisy_onewire/inventory_evidence.py",
    "src/eisy_onewire/iox_profile.py",
    "src/eisy_onewire/controller_node.py",
    "src/eisy_onewire/ds18b20_node.py",
    "src/eisy_onewire/branch_temperature.py",
    "src/eisy_onewire/node_identity.py",
    "src/eisy_onewire/node_registry.py",
    "src/eisy_onewire/refresh_configuration.py",
    "src/eisy_onewire/automatic_refresh.py",
    "src/eisy_onewire/discovery_inventory.py",
    "src/eisy_onewire/manual_discovery.py",
    "src/eisy_onewire/ds2409_transport.py",
    "src/eisy_onewire/ds2409_status.py",
    "src/eisy_onewire/ds2438.py",
    "src/eisy_onewire/ds2438_transport.py",
    "src/eisy_onewire/ds2438_diagnostic.py",
    "src/eisy_onewire/ds2438_humidity.py",
    "src/eisy_onewire/ds2438_analog.py",
    "src/eisy_onewire/ds2438_service.py",
    "src/eisy_onewire/ds2438_node.py",
    "src/eisy_onewire/ds2438_analog_node.py",
    "src/eisy_onewire/topology_discovery.py",
    "src/eisy_onewire/sensor_config.py",
    "src/eisy_onewire/ds18b20.py",
    "src/eisy_onewire/ds18b20_transport.py",
    "src/eisy_onewire/ds2490_transport.py",
    "src/eisy_onewire/onewire_discovery.py",
    "src/eisy_onewire/search_diagnostic.py",
    "src/eisy_onewire/status_diagnostic.py",
    "src/eisy_onewire/temperature_diagnostic.py",
    "src/eisy_onewire/temperature_service.py",
    "src/eisy_onewire/temperature_presentation.py",
    "src/eisy_onewire/presentation_worker.py",
    "src/eisy_onewire/usb_detector.py",
    "src/eisy_onewire/version.py",
)
EXECUTABLES = {"plugin.py", "diagnostic.py", "install.sh"}


def build(output_dir: Path) -> Path:
    """Create a deterministic archive from the explicit plugin allowlist."""
    metadata = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = metadata["plugin"]["version"]
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"eisy-onewire-{version}.tar.gz"

    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                for relative_name in PACKAGE_FILES:
                    path = ROOT / relative_name
                    data = (
                        path.read_text(encoding="utf-8")
                        .replace("\r\n", "\n")
                        .encode("utf-8")
                    )
                    info = tarfile.TarInfo(relative_name)
                    info.size = len(data)
                    info.mode = 0o755 if relative_name in EXECUTABLES else 0o644
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "wheel"
                    tar.addfile(info, io.BytesIO(data))
    return archive


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(build(args.output_dir))


if __name__ == "__main__":
    main()
