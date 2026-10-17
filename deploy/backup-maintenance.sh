#!/usr/bin/env bash
# Stop-writer daily backups. Install only after the documented recovery drill.
set -Eeuo pipefail
umask 077

fail() { printf 'Backup refused: %s\n' "$*" >&2; exit 1; }
for program in docker flock findmnt mountpoint realpath cp date tee sync id; do
  command -v "$program" >/dev/null || fail "Required command missing: $program"
done
: "${XIAOYI_PROJECT_DIR:?Set XIAOYI_PROJECT_DIR}"
: "${COMPOSE_PROJECT_NAME:?Set the existing COMPOSE_PROJECT_NAME}"
: "${XIAOYI_OFFSITE_DIR:?Set an existing off-host backup directory}"
: "${XIAOYI_OFFSITE_MOUNT:?Set its dedicated mount point}"
: "${XIAOYI_OFFSITE_EXPECTED_SOURCE:?Set the exact findmnt SOURCE}"
: "${XIAOYI_MAINTENANCE_ACK:?Read the exclusive-writers agreement}"
[[ "$XIAOYI_MAINTENANCE_ACK" == "exclusive-compose-writers" ]] || fail "Maintenance agreement not acknowledged"
[[ "$COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail "Invalid Compose project name"
project=$(realpath -e -- "$XIAOYI_PROJECT_DIR")
backup_root=$(realpath -e -- "$project/backups")
offsite=$(realpath -e -- "$XIAOYI_OFFSITE_DIR")
offsite_mount=$(realpath -e -- "$XIAOYI_OFFSITE_MOUNT")
[[ -d "$backup_root" && -d "$offsite" ]] || fail "Backup directories must already exist"
[[ "$offsite_mount" != "/" ]] || fail "The root filesystem is not offsite storage"
[[ "$offsite" == "$offsite_mount" || "$offsite" == "$offsite_mount/"* ]] || fail "Offsite directory is outside the configured mount"
[[ "$offsite" != "$backup_root" && "$offsite" != "$backup_root/"* && "$backup_root" != "$offsite/"* ]] || fail "Backup paths overlap"
lock=${XIAOYI_MAINTENANCE_LOCK:-/run/lock/xiaoyi-catalog-maintenance.lock}
[[ "$lock" == /* ]] || fail "Maintenance lock must be an absolute host path"
exec 9>"$lock"
flock -n 9 || { printf 'Another maintenance command holds %s\n' "$lock" >&2; exit 75; }

check_offsite() {
  mountpoint -q -- "$offsite_mount" || fail "Offsite mount is absent"
  local source device local_device target
  source=$(findmnt -n -o SOURCE --target "$offsite")
  target=$(findmnt -n -o TARGET --target "$offsite")
  device=$(findmnt -n -o MAJ:MIN --target "$offsite")
  local_device=$(findmnt -n -o MAJ:MIN --target "$backup_root")
  [[ "$source" == "$XIAOYI_OFFSITE_EXPECTED_SOURCE" ]] || fail "Offsite mount source changed"
  [[ "$target" == "$offsite_mount" ]] || fail "Unexpected nested/offsite mount"
  [[ -n "$device" && "$device" != "$local_device" ]] || fail "Offsite and local backup paths share a filesystem"
}
check_offsite
cd -- "$project"
compose=(docker compose --project-name "$COMPOSE_PROJECT_NAME" --project-directory "$project")
"${compose[@]}" config --quiet
run_id="daily-$(date -u +%Y%m%dT%H%M%SZ)-$$"
log="$backup_root/$run_id.log"
exec > >(tee -a "$log") 2>&1
printf 'Maintenance started: %s\n' "$run_id"
managed=(web api worker-interactive worker-io worker-cpu)
resume=()
restart_needed=0

running_services() {
  docker ps --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --format '{{.Label "com.docker.compose.service"}}'
}
assert_no_other_writers() {
  local service database_seen=0 current
  current=$(running_services)
  while IFS= read -r service; do
    case "$service" in
      database) database_seen=1 ;;
      web|api|worker-interactive|worker-io|worker-cpu)
        [[ "$restart_needed" == 0 ]] || fail "A managed writer is still running: $service" ;;
      "") ;;
      *) fail "Another project container is active ($service); finish migrations/imports/ops first" ;;
    esac
  done <<< "$current"
  [[ "$database_seen" == 1 ]] || fail "The configured project's database must already be running"
}
cleanup() {
  local result=$?
  trap - EXIT INT TERM
  set +e
  if [[ "$restart_needed" == 1 && ${#resume[@]} -gt 0 ]]; then
    printf 'Restoring previously running services: %s\n' "${resume[*]}"
    if ! "${compose[@]}" start --wait --wait-timeout 180 "${resume[@]}"; then
      printf 'SERVICE RESTORE FAILED: operator action is required. Original error code: %s\n' "$result" >&2
      result=1
    fi
  fi
  if [[ "$result" != 0 ]]; then
    printf 'Maintenance failed (%s); retained all backup files and log: %s\n' "$result" "$log" >&2
  else
    printf 'Maintenance complete; retained backup and log: %s\n' "$log"
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
assert_no_other_writers
# Refuse missing images/client tools before any downtime; never build/pull here.
"${compose[@]}" run --rm --no-deps --pull never --no-build ops pg_dump --version
current=$(running_services)
while IFS= read -r service; do
  case "$service" in web|api|worker-interactive|worker-io|worker-cpu) resume+=("$service") ;; esac
done <<< "$current"
# Mark restoration necessary BEFORE stop; a partially failed stop is also recovered.
restart_needed=1
"${compose[@]}" stop --timeout 300 "${managed[@]}"
assert_no_other_writers
"${compose[@]}" run --rm --no-deps --pull never --no-build --volume "$backup_root:/backups" ops \
  python -m xiaoyi_catalog.operations_backup backup --destination "/backups/$run_id" --quiesced
"${compose[@]}" run --rm --no-deps --pull never --no-build --volume "$backup_root:/backups:ro" ops \
  python -m xiaoyi_catalog.operations_backup verify --backup "/backups/$run_id"
check_offsite
offsite_backup="$offsite/$run_id"
mkdir -- "$offsite_backup"
cp -a --no-preserve=ownership -- "$backup_root/$run_id/." "$offsite_backup/"
sync -f "$offsite_backup"
check_offsite
"${compose[@]}" run --rm --no-deps --pull never --no-build --user "$(id -u):$(id -g)" --volume "$offsite:/offsite:ro" ops \
  python -m xiaoyi_catalog.operations_backup verify --backup "/offsite/$run_id"
check_offsite
printf 'Both local and offsite copies verified: %s\n' "$run_id"
# EXIT trap restores only services that were online before this maintenance.
