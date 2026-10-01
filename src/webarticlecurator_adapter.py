"""
WebArticleCurator Adapter module for CorpusAI.

Provides an isolated interoperability wrapper around the official ELTE-DH/WebArticleCurator package.
Invokes supported CLI / API commands safely without shell=True or arbitrary command execution.
"""

import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional


class WebArticleCuratorAdapter:
    """
    Safe wrapper for interacting with ELTE-DH/WebArticleCurator.
    """

    def __init__(self, python_executable: Optional[str] = None):
        self.python_exe = python_executable or sys.executable

    def is_available(self) -> bool:
        """
        Check if webarticlecurator is installed in the environment.
        """
        try:
            import webarticlecurator  # noqa: F401
            return True
        except ImportError:
            return False

    def get_version(self) -> Optional[str]:
        """
        Retrieve installed version of webarticlecurator.
        """
        try:
            import webarticlecurator
            return getattr(webarticlecurator, "__version__", "1.13.0")
        except ImportError:
            return None

    def _run_cli(self, args: List[str], timeout: int = 60) -> Dict[str, Any]:
        """
        Execute a webarticlecurator CLI command safely via subprocess.run (shell=False).

        Args:
            args: Argument list following 'python -m webarticlecurator'.
            timeout: Command timeout in seconds.

        Returns:
            Dict containing success (bool), returncode (int), stdout (str), stderr (str).
        """
        cmd = [self.python_exe, "-m", "webarticlecurator"] + args
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            return {
                "success": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "command": cmd,
            }
        except subprocess.TimeoutExpired as e:
            return {
                "success": False,
                "returncode": -1,
                "stdout": (e.stdout or "").strip() if isinstance(e.stdout, str) else "",
                "stderr": f"Command timed out after {timeout} seconds",
                "command": cmd,
            }
        except Exception as e:
            return {
                "success": False,
                "returncode": -1,
                "stdout": "",
                "stderr": f"Subprocess invocation error: {str(e)}",
                "command": cmd,
            }

    def list_urls(self, warc_path: str) -> Dict[str, Any]:
        """
        List URLs recorded in a WARC file using 'webarticlecurator listurls'.
        """
        if not os.path.exists(warc_path):
            return {"success": False, "urls": [], "error": f"WARC file not found: {warc_path}"}

        res = self._run_cli(["listurls", warc_path])
        if not res["success"]:
            return {"success": False, "urls": [], "error": res["stderr"]}

        urls = [line.strip() for line in res["stdout"].splitlines() if line.strip()]
        return {"success": True, "urls": urls, "error": None}

    def validate_warc(self, warc_path: str) -> Dict[str, Any]:
        """
        Validate a WARC file using 'webarticlecurator validate'.
        """
        if not os.path.exists(warc_path):
            return {"success": False, "valid": False, "error": f"WARC file not found: {warc_path}"}

        res = self._run_cli(["validate", warc_path])
        return {
            "success": res["success"],
            "valid": res["success"],
            "output": res["stdout"],
            "error": res["stderr"] if not res["success"] else None,
        }

    def archive_single_url(
        self, url: str, target_warc: str, timeout: int = 30
    ) -> Dict[str, Any]:
        """
        Download a single URL to a WARC file using 'webarticlecurator download'.
        """
        os.makedirs(os.path.dirname(os.path.abspath(target_warc)), exist_ok=True)
        res = self._run_cli(["download", url, target_warc], timeout=timeout)

        if not res["success"]:
            return {
                "success": False,
                "url": url,
                "warc_path": target_warc,
                "error": res["stderr"] or res["stdout"],
            }

        return {
            "success": True,
            "url": url,
            "warc_path": target_warc,
            "error": None,
        }
