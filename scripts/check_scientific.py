import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HDF5_RELEASE = "https://github.com/HDFGroup/hdf5/releases/download/2.2.0/"
WINDOWS_ARCHIVE = "hdf5-2.2.0-win-vs2026_cl.zip"
WINDOWS_SHA256 = "0564e8d75cbc7cd86dec2a629afa8498f9ad22cdcb06d9078ebaa4e834390ac4"
LINUX_ARCHIVE = "hdf5-2.2.0-ubuntu-2404_gcc.tar.gz"
LINUX_SHA256 = "efa520a59202830a0d48ac7da1312314507ec1fcc40c744cf7b1c37a5edfaaeb"

SMOKE = """
import json
import os
import platform
import sys
from math import sqrt
from pathlib import Path

if platform.system() == "Windows":
    handle = os.add_dll_directory(str(Path(os.environ["HDF5_DIR"]) / "bin"))
import h5py
import numpy as np
import pandas as pd
import pvlib

from pyracast.plants.tracking import tracking_gain

assert h5py.version.hdf5_version == "2.2.0", h5py.version.hdf5_version
times = pd.DatetimeIndex(["2026-06-21T12:00:00Z"])
result = pvlib.solarposition.get_solarposition(times, 40, 29)
zenith = float(result["zenith"].iloc[0])
azimuth = float(result["azimuth"].iloc[0])
assert 29.0 < zenith < 29.4, zenith
assert 243.8 < azimuth < 244.3, azimuth
print(f"h5py {h5py.__version__}, HDF5 {h5py.version.hdf5_version}, pvlib {pvlib.__version__}")
print(f"solar position: zenith {zenith:.6f}, azimuth {azimuth:.6f}")

root = Path(sys.argv[1])


def scalar(values: object) -> float:
    return float(np.ravel(values)[0])


def chain_gain(
    reference: float, true_zenith: float, apparent_zenith: float, azimuth: float, day_of_year: int
) -> float:
    # GHI from p = 0.9964 g - 0.1484 g^2 (g = GHI / 1000), the documented model input.
    ghi = 1000 * (sqrt(0.9964**2 - 4 * 0.1484 * reference) - 0.9964) / (2 * -0.1484)
    parts = pvlib.irradiance.erbs(ghi, true_zenith, day_of_year)
    tracker = pvlib.tracking.singleaxis(
        apparent_zenith,
        azimuth,
        axis_tilt=0,
        axis_azimuth=180,
        max_angle=55,
        backtrack=True,
        gcr=0.4,
    )
    plane = scalar(
        pvlib.irradiance.get_total_irradiance(
            scalar(tracker["surface_tilt"]),
            scalar(tracker["surface_azimuth"]),
            apparent_zenith,
            azimuth,
            scalar(parts["dni"]),
            ghi,
            scalar(parts["dhi"]),
            dni_extra=pvlib.irradiance.get_extra_radiation(day_of_year),
            model="haydavies",
            albedo=0.2,
        )["poa_global"]
    )

    def cell(irradiance: float) -> float:
        return pvlib.temperature.pvsyst_cell(
            irradiance, 0.0, u_c=29.0, u_v=0.0, module_efficiency=0.0, alpha_absorption=1.0
        )

    return plane / ghi * (1 - 0.0035 * (cell(plane) - cell(ghi)))


fixtures = root / "tests" / "fixtures"
# v1 gives both steps one zenith; v2 gives Erbs the true and the geometry the apparent zenith.
versions = {
    "v1": (fixtures / "tracking_gain_reference.json", "zenith", "zenith"),
    "v2": (fixtures / "tracking_gain_reference_v2.json", "true_zenith", "apparent_zenith"),
}
for version, (table, true_key, apparent_key) in versions.items():
    cases = json.loads(table.read_text("utf-8"))
    assert cases, f"Tracker gain {version} reference table must contain cases."
    for case in cases:
        inputs = (
            case["reference_fraction"],
            case[true_key],
            case[apparent_key],
            case["azimuth"],
            case["day_of_year"],
        )
        expected = chain_gain(*inputs)
        model = tracking_gain(*inputs)
        assert abs(expected - case["gain"]) <= 0.0001, (version, case, expected)
        assert abs(model - expected) <= 0.012, (version, case, model, expected)
        print(f"tracker gain {version} {inputs}: pvlib {expected:.4f}, model {model:.4f}")
"""


def main() -> None:
    system = platform.system()
    if system not in {"Windows", "Linux"}:
        raise RuntimeError("Scientific check requires Windows or Linux")
    windows = system == "Windows"
    name, digest = (WINDOWS_ARCHIVE, WINDOWS_SHA256) if windows else (LINUX_ARCHIVE, LINUX_SHA256)
    temp_parent = Path(os.environ.get("RUNNER_TEMP", str(Path.home())))
    with tempfile.TemporaryDirectory(prefix="ps", dir=temp_parent) as directory:
        root = Path(directory)
        archive = root / name
        urllib.request.urlretrieve(HDF5_RELEASE + name, archive)  # noqa: S310 -- fixed HTTPS URL.
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise RuntimeError("HDF5 archive checksum mismatch")
        if windows:
            with zipfile.ZipFile(archive) as source:
                source.extractall(root)  # noqa: S202 -- archive is pinned by SHA256.
            with zipfile.ZipFile(root / "hdf5" / "HDF5-2.2.0-win64.zip") as source:
                source.extractall(root)  # noqa: S202 -- nested in the verified archive.
            hdf5 = root / "HDF5-2.2.0-win64"
        else:
            with tarfile.open(archive, "r:gz") as source:
                source.extractall(root, filter="data")
            with tarfile.open(root / "hdf5" / "HDF5-2.2.0-Linux.tar.gz", "r:gz") as source:
                source.extractall(root, filter="data")
            hdf5 = root / "HDF5-2.2.0-Linux" / "HDF_Group" / "HDF5" / "2.2.0"
        env = os.environ.copy()
        env["HDF5_DIR"] = str(hdf5)
        env["UV_PROJECT_ENVIRONMENT"] = str(root / "venv")
        env["TEMP"] = env["TMP"] = str(root)
        library = hdf5 / ("bin" if windows else "lib")
        variable = "PATH" if windows else "LD_LIBRARY_PATH"
        env[variable] = str(library) + os.pathsep + env.get(variable, "")
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv is required")
        subprocess.run(  # noqa: S603 -- fixed uv arguments.
            [
                uv,
                "sync",
                "--locked",
                "--no-build",
                "--no-dev",
                "--extra",
                "scientific",
            ],
            env=env,
            check=True,
        )
        python = root / "venv" / ("Scripts/python.exe" if windows else "bin/python")
        root_directory = str(Path(__file__).resolve().parents[1])
        subprocess.run([str(python), "-c", SMOKE, root_directory], env=env, check=True)  # noqa: S603


if __name__ == "__main__":
    main()
