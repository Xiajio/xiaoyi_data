"""Maintenance orchestration with fake Docker/mount commands; no real service changes."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
if os.name == "nt":
    candidate = Path(shutil.which("git") or "").resolve().parent.parent / "bin" / "bash.exe"
    BASH = str(candidate) if candidate.exists() else None
STUBS = r"""
if command -v cygpath >/dev/null; then
  export XIAOYI_MAINTENANCE_LOCK="$(cygpath -u "$TEST_LOCK")"
else
  export XIAOYI_MAINTENANCE_LOCK="$(realpath -m "$TEST_LOCK")"
fi
flock() { [[ "${TEST_FAIL:-}" != locked ]]; }
mountpoint() { [[ "${TEST_FAIL:-}" != mount ]]; }
findmnt() {
  case "$3" in
    SOURCE) if [[ "${TEST_FAIL:-}" == source ]]; then printf 'wrong-source\n'; else printf '%s\n' "$XIAOYI_OFFSITE_EXPECTED_SOURCE"; fi ;;
    TARGET) realpath -e "$XIAOYI_OFFSITE_MOUNT" ;;
    MAJ:MIN) if [[ "${*: -1}" == */backups || "${TEST_FAIL:-}" == device ]]; then printf '0:1\n'; else printf '0:2\n'; fi ;;
    *) return 1 ;;
  esac
}
sync() { return 0; }
docker() {
  printf '%s\n' "$*" >> "$TEST_CALLS"
  if [[ "$1" == ps ]]; then cat "$TEST_STATE"; return; fi
  if [[ " $* " == *" config "* ]]; then return 0; fi
  if [[ " $* " == *" stop "* ]]; then printf 'database\n' > "$TEST_STATE"; return 0; fi
  if [[ " $* " == *" start "* ]]; then if [[ "${TEST_FAIL:-}" == restore ]]; then return 9; fi; return 0; fi
  if [[ " $* " == *" pg_dump --version "* ]]; then printf 'pg_dump (PostgreSQL) 16.0\n'; return 0; fi
  local arg previous="" target=""
  for arg; do
    if [[ "$previous" == --destination || "$previous" == --backup ]]; then target="$arg"; fi
    previous="$arg"
  done
  if [[ " $* " == *" xiaoyi_catalog.operations_backup backup "* ]]; then
    [[ "${TEST_FAIL:-}" != backup ]] || return 9
    mkdir "$XIAOYI_PROJECT_DIR/backups/${target#/backups/}"
    printf 'verified-fixture\n' > "$XIAOYI_PROJECT_DIR/backups/${target#/backups/}/payload"
    return 0
  fi
  if [[ " $* " == *" xiaoyi_catalog.operations_backup verify "* ]]; then
    if [[ "$target" == /offsite/* ]]; then
      [[ "${TEST_FAIL:-}" != offsite ]] || return 9
      [[ -f "$XIAOYI_OFFSITE_DIR/${target#/offsite/}/payload" ]]
    else
      [[ "${TEST_FAIL:-}" != verify ]] || return 9
      [[ -f "$XIAOYI_PROJECT_DIR/backups/${target#/backups/}/payload" ]]
    fi
    return
  fi
  printf 'Unexpected fake Docker call\n' >&2
  return 99
}
"""

def run_script(tmp_path, failure="", extra_service=""):
    if not BASH:
        pytest.skip("Bash required")
    project, offsite = tmp_path / "project", tmp_path / "offsite"
    (project / "backups").mkdir(parents=True)
    offsite.mkdir()
    state, calls, stubs = tmp_path / "state", tmp_path / "calls", tmp_path / "stubs.sh"
    state.write_text("database\nweb\napi\nworker-interactive\nworker-cpu\n" + extra_service, encoding="utf-8", newline="\n")
    calls.touch()
    stubs.write_text(STUBS, encoding="utf-8", newline="\n")
    env = {**os.environ, "BASH_ENV": str(stubs), "XIAOYI_PROJECT_DIR": str(project),
           "COMPOSE_PROJECT_NAME": "fixture", "XIAOYI_OFFSITE_DIR": str(offsite),
           "XIAOYI_OFFSITE_MOUNT": str(offsite), "XIAOYI_OFFSITE_EXPECTED_SOURCE": "backup:/fixture",
           "XIAOYI_MAINTENANCE_ACK": "exclusive-compose-writers", "TEST_LOCK": str(tmp_path / "lock"),
           "TEST_CALLS": str(calls), "TEST_STATE": str(state), "TEST_FAIL": failure}
    result = subprocess.run([BASH, "deploy/backup-maintenance.sh"], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    return result, calls.read_text(encoding="utf-8"), project, offsite

def test_backup_stops_verifies_both_copies_and_restores_only_prior_services(tmp_path):
    result, calls, project, offsite = run_script(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.index(" stop ") < calls.index("operations_backup backup") < calls.index("operations_backup verify")
    assert "/offsite/" in calls
    restore = next(line for line in calls.splitlines() if " start " in line)
    assert restore.endswith("web api worker-interactive worker-cpu")
    assert "worker-io" not in restore
    assert len(list((project / "backups").glob("daily-*/payload"))) == 1
    assert len(list(offsite.glob("daily-*/payload"))) == 1

@pytest.mark.parametrize("failure", ["backup", "verify", "offsite"])
def test_failure_restores_services_and_retains_logs(tmp_path, failure):
    result, calls, project, _ = run_script(tmp_path, failure)
    assert result.returncode != 0
    assert " stop " in calls and " start " in calls
    assert list((project / "backups").glob("*.log"))
    if failure != "backup":
        assert list((project / "backups").glob("daily-*/payload"))

@pytest.mark.parametrize("failure", ["locked", "mount", "source", "device"])
def test_invalid_mount_or_busy_lock_never_stops_services(tmp_path, failure):
    result, calls, _, _ = run_script(tmp_path, failure)
    assert result.returncode != 0
    assert " stop " not in calls and "operations_backup backup" not in calls

def test_active_manual_ops_prevents_maintenance(tmp_path):
    result, calls, _, _ = run_script(tmp_path, extra_service="ops\n")
    assert result.returncode != 0 and " stop " not in calls

def test_failed_restore_does_not_report_success(tmp_path):
    result, calls, _, _ = run_script(tmp_path, "restore")
    assert result.returncode != 0 and " start " in calls
    assert "SERVICE RESTORE FAILED" in result.stdout + result.stderr
