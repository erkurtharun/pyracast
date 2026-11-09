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
import os
import platform
from pathlib import Path

if platform.system() == "Windows":
    handle = os.add_dll_directory(str(Path(os.environ["HDF5_DIR"]) / "bin"))
import h5py
import pandas as pd
import pvlib

assert h5py.version.hdf5_version == "2.2.0", h5py.version.hdf5_version
times = pd.DatetimeIndex(["2026-06-21T12:00:00Z"])
result = pvlib.solarposition.get_solarposition(times, 40, 29)
zenith = float(result["zenith"].iloc[0])
azimuth = float(result["azimuth"].iloc[0])
assert 29.0 < zenith < 29.4, zenith
assert 243.8 < azimuth < 244.3, azimuth
print(f"h5py {h5py.__version__}, HDF5 {h5py.version.hdf5_version}, pvlib {pvlib.__version__}")
print(f"solar position: zenith {zenith:.6f}, azimuth {azimuth:.6f}")
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
            [uv, "sync", "--locked", "--no-dev", "--extra", "scientific", "--no-cache"],
            env=env,
            check=True,
        )
        python = root / "venv" / ("Scripts/python.exe" if windows else "bin/python")
        subprocess.run([str(python), "-c", SMOKE], env=env, check=True)  # noqa: S603


if __name__ == "__main__":
    main()
