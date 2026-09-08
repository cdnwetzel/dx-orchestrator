import os
import subprocess
import sys
from pathlib import Path

from .config_loader import get_psoperator_config, get_psoperator_repo

# Short bounds for the auxiliary commands. These are status and control calls,
# not work — and an emergency stop that can hang is not an emergency stop.
CLI_TIMEOUT_S = 15
STOP_TIMEOUT_S = 5


def run_agent_script() -> Path:
    """Absolute path to psoperator's examples/run_agent.py.

    Resolved per call rather than at import time so PSOPERATOR_REPO and the
    manifest are honoured by anything that sets them after import.
    """
    return get_psoperator_repo() / "examples" / "run_agent.py"


class PSOperatorClient:
    """Thin wrapper around the PSOperator CLI and run_agent example.

    Assumes PSOperator is installed (pip install -e ~/ai/psoperator) so that
    `psoperator` is on PATH and the package imports. run_agent is invoked by
    absolute path, not `python -m examples.run_agent` — psoperator's examples/
    directory is not a package, so the module form does not resolve.
    """

    def __init__(self) -> None:
        try:
            cfg = get_psoperator_config()
        except FileNotFoundError:
            cfg = {}
        self.observer_port = str(
            os.environ.get("PSOPERATOR_OBSERVER_PORT")
            or cfg.get("observer_port", 8764)
        )
        self.gatekeeper_port = str(
            os.environ.get("PSOPERATOR_GATEKEEPER_PORT")
            or cfg.get("gatekeeper_port", 8765)
        )
        self.executor_port = str(
            os.environ.get("PSOPERATOR_EXECUTOR_PORT")
            or cfg.get("executor_port", 8766)
        )
        self.model_endpoint = str(
            os.environ.get("PSOPERATOR_MODEL_ENDPOINT")
            or cfg.get("model_endpoint", "http://localhost:8000/v1")
        )
        self.model_name = str(
            os.environ.get("PSOPERATOR_MODEL_NAME")
            or cfg.get("model_name", "ui-tars-1.5-7b")
        )
        self.audit_log_path = str(
            os.environ.get("PSOPERATOR_AUDIT_LOG_PATH")
            or cfg.get("audit_log_path", "psoperator_audit.jsonl")
        )

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PSOPERATOR_OBSERVER_PORT": self.observer_port,
                "PSOPERATOR_GATEKEEPER_PORT": self.gatekeeper_port,
                "PSOPERATOR_EXECUTOR_PORT": self.executor_port,
                "PSOPERATOR_MODEL_ENDPOINT": self.model_endpoint,
                "PSOPERATOR_MODEL_NAME": self.model_name,
                "PSOPERATOR_AUDIT_LOG_PATH": self.audit_log_path,
            }
        )
        return env

    def launch_gui_task(
        self, task_description: str, real_input: bool = False, timeout: int = 300
    ) -> bool:
        script = run_agent_script()
        if not script.exists():
            print(f"⚠️  run_agent.py not found at {script}.")
            print("     Set PSOPERATOR_REPO or clone psoperator to ~/ai/psoperator.")
            return False

        # Use sys.executable so we invoke the venv Python that has psoperator installed
        cmd = [sys.executable, str(script), "--task", task_description]
        if real_input:
            cmd.append("--real-input")
        try:
            result = subprocess.run(cmd, env=self._env(), timeout=timeout)
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            print("⚠️  PSOperator task timed out.")
            return False

    def verify_audit_log(self, path: str | None = None) -> bool:
        target = path or self.audit_log_path
        try:
            result = subprocess.run(
                ["psoperator", "audit-verify", target],
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_S,
            )
            if result.returncode != 0:
                print(f"⚠️  audit-verify failed: {result.stderr.strip()}")
            return result.returncode == 0
        except FileNotFoundError:
            print("⚠️  psoperator CLI not on PATH.")
            return False
        except subprocess.TimeoutExpired:
            print(f"⚠️  audit-verify timed out after {CLI_TIMEOUT_S}s.")
            return False

    def emergency_stop(self) -> None:
        try:
            subprocess.run(["psoperator", "kill"], check=False, timeout=STOP_TIMEOUT_S)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def observer_health(self) -> bool:
        try:
            result = subprocess.run(
                ["psoperator", "observer-health"],
                capture_output=True,
                timeout=CLI_TIMEOUT_S,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
