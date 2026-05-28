"""Package manager: download DB packages from Nexus, extract binaries, cache locally."""

import os
import glob
import shutil
import tarfile
import tempfile
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "packages"
)

_PACKAGE_CACHE_DIR = os.environ.get("MCP_PACKAGE_CACHE_DIR", _DEFAULT_CACHE_DIR)


class PackageManager:
    """Downloads, extracts, and caches DB binary directories from Nexus packages.

    Extraction follows the same steps as the Dockerfile.j2 template:
      1. tar -xf outer.tar.gz -C tmp/
      2. tar -xf tmp/vastbase-installer/*.tar.gz -C tmp/
      3. tar -xf tmp/*.tar.bz2 -C <cache_path>/
    """

    def __init__(self, cache_dir: str = None):
        self._cache_dir = cache_dir or _PACKAGE_CACHE_DIR

    def prepare_binaries(self, db_type: str, version: str,
                         nexus_config: Dict[str, Any]) -> str:
        """Ensure binaries for *version* are extracted locally.

        Returns the host path to the binary directory (to be volume-mounted
        into the container at ``/home/vastbase/vastbase``).
        """
        cache_path = os.path.join(self._cache_dir, f"{db_type}-{version}")
        if os.path.isdir(cache_path) and os.listdir(cache_path):
            logger.info(
                f"PackageManager: using cached binaries at {cache_path}"
            )
            return cache_path

        logger.info(
            f"PackageManager: binaries not cached for {db_type} {version}, "
            f"downloading from Nexus ..."
        )

        from .nexus_client import NexusClient
        nexus = NexusClient(
            domain=nexus_config["domain"],
            username=nexus_config["username"],
            password=nexus_config["password"],
        )

        download_dir = tempfile.mkdtemp(prefix=f"pkg-dl-{db_type}-{version}-")
        extract_dir = tempfile.mkdtemp(prefix=f"pkg-extract-{db_type}-{version}-")
        try:
            pkg_path = nexus.fetch_package(version, download_dir,
                                           nexus_config=nexus_config)
            logger.info(f"PackageManager: downloaded {os.path.basename(pkg_path)}")

            os.makedirs(cache_path, exist_ok=True)

            # Step 1: extract outer .tar.gz
            self._extract_tar(pkg_path, extract_dir)
            logger.info("PackageManager: step 1/3 - outer package extracted")

            # Step 2: extract installer .tar.gz from vastbase-installer/
            installer_dir = os.path.join(extract_dir, "vastbase-installer")
            installer_tars = (
                glob.glob(os.path.join(installer_dir, "*.tar.gz")) +
                glob.glob(os.path.join(installer_dir, "*.tar"))
            )
            if not installer_tars:
                raise RuntimeError(
                    f"No installer tarball found in vastbase-installer/ "
                    f"under {extract_dir}"
                )
            self._extract_tar(installer_tars[0], extract_dir)
            logger.info("PackageManager: step 2/3 - installer extracted")

            # Step 3: extract final .tar.bz2 (or .tar.gz) to cache_path
            bz2_files = (
                glob.glob(os.path.join(extract_dir, "*.tar.bz2")) +
                glob.glob(os.path.join(extract_dir, "*.tar.gz")) +
                glob.glob(os.path.join(extract_dir, "*.tar"))
            )
            # Exclude files from installer_dir that may have been extracted flat
            bz2_files = [f for f in bz2_files
                         if "vastbase-installer" not in f]
            if not bz2_files:
                raise RuntimeError(
                    f"No final tarball (*.tar.bz2) found in {extract_dir}"
                )
            self._extract_tar(bz2_files[0], cache_path)
            logger.info(
                f"PackageManager: step 3/3 - binaries extracted to {cache_path}"
            )

        finally:
            shutil.rmtree(download_dir, ignore_errors=True)
            shutil.rmtree(extract_dir, ignore_errors=True)

        logger.info(f"PackageManager: binaries cached at {cache_path}")
        return cache_path

    @staticmethod
    def _extract_tar(tar_path: str, dest_dir: str) -> None:
        """Extract a tar archive (supports .tar, .tar.gz, .tar.bz2)."""
        os.makedirs(dest_dir, exist_ok=True)
        mode = "r"
        if tar_path.endswith(".gz") or tar_path.endswith(".tgz"):
            mode = "r:gz"
        elif tar_path.endswith(".bz2") or tar_path.endswith(".tbz2"):
            mode = "r:bz2"
        with tarfile.open(tar_path, mode) as tf:
            tf.extractall(path=dest_dir)
