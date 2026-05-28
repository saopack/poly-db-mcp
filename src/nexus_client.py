"""Nexus repository client for downloading DB packages."""

import os
import re
import logging
import tempfile
from typing import Optional, Dict, Any, List
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = int(os.environ.get("MCP_NEXUS_DOWNLOAD_TIMEOUT", "600"))


class NexusClient:
    """Queries and downloads artifacts from a Nexus 2.x repository."""

    def __init__(self, domain: str, username: str, password: str):
        self.domain = domain.rstrip('/')
        self.auth = HTTPBasicAuth(username, password)
        self._session = requests.Session()
        self._session.auth = self.auth

    # ---- version parsing ---------------------------------------------------

    @staticmethod
    def _parse_version(version: str) -> Dict[str, str]:
        """Parse version into components. Returns dict with keys:
        type ('basic'|'psu'|'build'), major, minor, build,
        and optionally psu / build_number.
        """
        parts = version.split('.')
        result = {
            'raw': version,
            'major': parts[0] if len(parts) > 0 else '0',
            'minor': parts[1] if len(parts) > 1 else '0',
            'build': parts[2] if len(parts) > 2 else '0',
            'type': 'basic',
        }
        if len(parts) == 4:
            last = parts[3].lower()
            if last.startswith('psu'):
                result['type'] = 'psu'
                result['psu'] = last.upper()
            elif last.replace('.', '').replace('-', '').isdigit():
                result['type'] = 'build'
                result['build_number'] = parts[3]
        return result

    @staticmethod
    def _render_template(template: str, parsed: Dict[str, str]) -> str:
        """Replace {key} placeholders with values from parsed version info.

        Available keys: major, minor, build, psu, build_number, version_code.
        version_code is derived as major+minor+build (e.g. '308').
        """
        version_code = f"{parsed['major']}{parsed['minor']}{parsed['build']}"
        result = template
        result = result.replace('{major}', parsed['major'])
        result = result.replace('{minor}', parsed['minor'])
        result = result.replace('{build}', parsed['build'])
        result = result.replace('{version_code}', version_code)
        result = result.replace('{psu}', parsed.get('psu', ''))
        result = result.replace('{build_num}', parsed.get('build_number', ''))
        return result

    # ---- search API --------------------------------------------------------

    def _rest_search(self, query: str, repository: str = "") -> List[Dict[str, Any]]:
        """Search using Nexus 3.x REST API. Returns list of asset dicts."""
        params = {}
        if repository:
            params["repository"] = repository
        params["q"] = query

        url = f"{self.domain}/service/rest/v1/search"
        try:
            resp = self._session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Nexus REST search failed for q={query}: {e}")
            return []

        results = []
        for item in data.get("items", []):
            for asset in item.get("assets", []):
                results.append({
                    "path": asset.get("path", ""),
                    "downloadUrl": asset.get("downloadUrl", ""),
                    "repository": item.get("repository", repository),
                })
        return results

    def _search_by_filters(self, filters: List[Dict[str, str]],
                           repository: str = "releases") -> Optional[Dict[str, Any]]:
        """Search Nexus with custom filter list (Nexus 2.x ExtDirect API).
        Returns first match or None."""
        search_url = f"{self.domain}/service/extdirect"
        payload = {
            "action": "coreui_Search",
            "method": "read",
            "data": [{
                "page": 1,
                "start": 0,
                "limit": 25,
                "sort": [{"property": "name", "direction": "ASC"}],
                "filter": filters,
            }],
            "type": "rpc",
            "tid": 1,
        }
        try:
            resp = self._session.post(search_url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Nexus search failed: {e}")
            return None

        results = data.get("result", {}).get("data", [])
        if not results:
            logger.warning(f"No Nexus artifact found for filters={filters}")
            return None
        return results[0]

    def search_artifact(self, version: str, repository: str = "releases") -> Optional[Dict[str, Any]]:
        """Search for a DB package matching the given version string."""
        return self._search_by_filters([
            {"property": "repository", "value": repository},
            {"property": "version", "value": version},
        ], repository=repository)

    def _search_by_keyword(self, keyword: str,
                           repository: str = "",
                           expected_keyword: str = "") -> Optional[Dict[str, Any]]:
        """Search by keyword. Uses Nexus 3.x REST API with ExtDirect fallback.

        If *expected_keyword* is provided, only returns a result whose
        filename starts with *expected_keyword* followed by a timestamp
        (digits) and ``.tar.gz`` / ``.tar.bz2``.
        """
        results = self._rest_search(keyword, repository=repository)
        if results:
            if expected_keyword:
                for r in results:
                    path = r.get("path", "")
                    basename = path.rsplit("/", 1)[-1]
                    if (basename.startswith(expected_keyword)
                            and basename.endswith((".tar.gz", ".tar.bz2"))):
                        suffix = basename[len(expected_keyword):]
                        # suffix must start with digits (timestamp)
                        suffix_no_ext = suffix.rsplit(".", 2)[0] if suffix.endswith(".gz") else suffix.rsplit(".", 2)[0]
                        if suffix_no_ext and suffix_no_ext.isdigit():
                            return r
                logger.warning(
                    f"No result filename matched expected_keyword={expected_keyword!r} "
                    f"among {len(results)} search results for keyword={keyword!r}, "
                    f"falling back to ExtDirect"
                )
            else:
                return results[0]

        logger.info("REST search returned no results, trying ExtDirect fallback")
        return self._search_by_filters([
            {"property": "keyword", "value": keyword},
        ])

    # ---- directory listing -------------------------------------------------

    def _list_directory(self, repo_path: str, repository: str = "") -> List[str]:
        """List file names in a Nexus repository directory via search API.

        Uses the Nexus 3.x REST search endpoint with the directory path as
        the query, then filters results to only include files located directly
        in the target directory (excluding subdirectory contents).

        Falls back to HTML index-page parsing for older Nexus 2.x instances.
        """
        # Normalize the repo_path: strip trailing slash for reliable comparison
        repo_path = repo_path.rstrip('/')
        encoded = quote(repo_path, safe='/:')
        search_url = (
            f"{self.domain}/service/rest/v1/search"
            f"?repository={repository or 'releases'}"
            f"&q={encoded}"
        )
        try:
            resp = self._session.get(search_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Nexus search failed for {repo_path}: {e}")
            return []

        files = []
        path_prefix = repo_path + "/"
        for item in data.get("items", []):
            for asset in item.get("assets", []):
                asset_path = asset.get("path", "")
                if not asset_path.startswith(path_prefix):
                    continue
                # Only include files directly in this directory (not subdirs)
                relative = asset_path[len(path_prefix):]
                if "/" not in relative:
                    files.append(relative)

        if not files:
            logger.warning(
                f"No files found in {repo_path} via search API, "
                f"trying HTML fallback..."
            )
            return self._list_directory_html(repo_path)

        return files

    def _list_directory_html(self, repo_path: str) -> List[str]:
        """Fallback: parse HTML index page for Nexus instances that serve it."""
        encoded = quote(repo_path, safe='/:')
        url = f"{self.domain}{encoded}"
        try:
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to list directory {repo_path}: {e}")
            return []

        files = []
        for match in re.finditer(r'<a\s+href="([^"]+)"', resp.text):
            href = match.group(1)
            if href == '../' or href.endswith('/'):
                continue
            files.append(href)
        return files

    # ---- download ----------------------------------------------------------

    @staticmethod
    def _build_download_url(domain: str, artifact: Dict[str, Any]) -> str:
        """Build a download URL from artifact metadata."""
        download_url = artifact.get("downloadUrl")
        if download_url:
            return download_url
        repo = artifact.get("repository", "releases")
        group_id = artifact.get("groupId", "").replace(".", "/")
        artifact_id = artifact.get("artifactId", "")
        version = artifact.get("version", "")
        packaging = artifact.get("packaging", "tar.gz")
        filename = f"{artifact_id}-{version}.{packaging}"
        return f"{domain}/repository/{repo}/{group_id}/{artifact_id}/{version}/{filename}"

    def download_artifact(self, download_url: str, dest_dir: str) -> str:
        """Download an artifact to *dest_dir*, return the local file path."""
        filename = download_url.rsplit('/', 1)[-1]
        dest_path = os.path.join(dest_dir, filename)
        logger.info(f"Downloading {download_url} -> {dest_path}")
        with self._session.get(download_url, stream=True, timeout=_DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
        size = os.path.getsize(dest_path)
        logger.info(f"Download complete: {dest_path} ({size} bytes)")
        return dest_path

    # ---- package fetching (routes by version format) -----------------------

    def fetch_package(self, version: str, dest_dir: str = None,
                      nexus_config: Dict[str, Any] = None) -> str:
        """Search for the artifact by version, download it, return local path.

        ``nexus_config`` must contain:
        - ``repository``: Nexus repository name (e.g. "vastbase-software")
        - ``package.psu``: dict with ``path_template`` and ``file_pattern``
        - ``package.build``: dict with ``keyword_template``

        Supports three version formats:
        - ``3.0.8``       basic  — exact version match in releases repo
        - ``3.0.8.psu0``  PSU    — directory listing under configured repo
        - ``3.0.8.24875`` build  — global keyword search by file name prefix
        """
        if dest_dir is None:
            dest_dir = tempfile.mkdtemp(prefix=f"nexus-pkg-{version}-")

        parsed = self._parse_version(version)

        if parsed['type'] == 'psu':
            return self._fetch_psu_package(parsed, dest_dir, nexus_config)
        elif parsed['type'] == 'build':
            return self._fetch_build_package(parsed, dest_dir, nexus_config)
        else:
            return self._fetch_basic_package(version, dest_dir)

    def _fetch_basic_package(self, version: str, dest_dir: str) -> str:
        """Basic version match (e.g. ``3.0.8``). Existing behaviour."""
        artifact = self.search_artifact(version)
        if not artifact:
            raise RuntimeError(
                f"No package found in Nexus for version {version}"
            )
        download_url = self._build_download_url(self.domain, artifact)
        return self.download_artifact(download_url, dest_dir)

    def _fetch_psu_package(self, parsed: Dict[str, str], dest_dir: str,
                           nexus_config: Dict[str, Any]) -> str:
        """PSU patch version (e.g. ``3.0.8.psu0``).

        Uses ``psu.path_template`` and ``psu.file_pattern`` from nexus config.
        """
        psu_config = nexus_config['package']['psu']
        repository = nexus_config['repository']

        dir_path = self._render_template(psu_config['path_template'], parsed)

        file_pattern = re.compile(
            self._render_template(psu_config['file_pattern'], parsed)
        )

        files = self._list_directory(dir_path, repository=repository)
        matches = [f for f in files if file_pattern.match(f)]

        if not matches:
            raise RuntimeError(
                f"No PSU package found for version {parsed['raw']} "
                f"in /repository/{repository}/{dir_path}"
            )

        download_url = (
            f"{self.domain}/repository/{repository}/{dir_path}/{matches[0]}"
        )
        return self.download_artifact(download_url, dest_dir)

    def _fetch_build_package(self, parsed: Dict[str, str], dest_dir: str,
                             nexus_config: Dict[str, Any]) -> str:
        """Build-number version (e.g. ``3.0.8.24875``).

        Uses ``build.keyword_template`` from nexus config.
        """
        build_config = nexus_config['package']['build']
        repository = nexus_config.get('repository', '')

        keyword = self._render_template(
            build_config['keyword_template'], parsed
        )

        artifact = self._search_by_keyword(keyword, repository=repository,
                                             expected_keyword=keyword)
        if not artifact:
            raise RuntimeError(
                f"No package found for build version {parsed['raw']} "
                f"(keyword: {keyword})"
            )

        download_url = artifact.get("downloadUrl") or self._build_download_url(
            self.domain, artifact
        )
        return self.download_artifact(download_url, dest_dir)
