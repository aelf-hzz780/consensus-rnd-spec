#!/usr/bin/env bash
set -euo pipefail

repo_root="${REPO_ROOT:-$(pwd)}"
host_env="$repo_root/.consensus-rnd-spec/host.env"
if [ -f "$host_env" ]; then
  # shellcheck disable=SC1090
  source "$host_env"
fi

command="${1:-plan}"
enabled="${NATIVE_FULL_LOOP_ENABLE:-false}"
skill_root="${NATIVE_CONSENSUS_SKILL_ROOT:-}"

if [ "$enabled" != "true" ]; then
  printf '{"backend":"native","status":"blocked","reason":"NATIVE_FULL_LOOP_ENABLE is false"}\n'
  exit 2
fi

if [ -z "$skill_root" ] || [ ! -f "$skill_root/SKILL.md" ]; then
  printf '{"backend":"native","status":"blocked","reason":"NATIVE_CONSENSUS_SKILL_ROOT is invalid"}\n'
  exit 2
fi

case "$command" in
  plan)
    if [ ! -x "$skill_root/scripts/consensus-rnd-cli" ]; then
      printf '{"backend":"native","status":"blocked","reason":"native consensus-rnd-cli not found or not executable"}\n'
      exit 2
    fi
    printf '{"backend":"native","status":"ready","skill_root":"%s","next":"delegate to codex-refactor-loop via consensus-rnd-cli spawn-codex"}\n' "$skill_root"
    ;;
  prompt)
    mkdir -p "$repo_root/.consensus-rnd-spec/prompts"
    prompt_file="$repo_root/.consensus-rnd-spec/prompts/native-loop.md"
    cat > "$prompt_file" <<EOF
Use the codex-refactor-loop skill from:
$skill_root

Run one unattended Consensus R&D native loop turn for:
$repo_root

Requirements:
- Treat this dispatch as explicitly operator-confirmed unattended execution.
- Do not ask for plan confirmation, approval, or additional Human input before acting.
- Source host configuration before acting.
- Preserve the native skill's GitHub, branch, PR, merge, and label rules.
- Keep synthetic Human: text separate from maintainer approval.
- Do not run git push unless the target remote and branch were explicitly confirmed in this session.
- Emit durable status in the native loop's normal state surface.
EOF
    printf '{"backend":"native","status":"prompt-ready","prompt_file":"%s"}\n' "$prompt_file"
    ;;
  run)
    prompt_json=$("$0" prompt)
    prompt_file=$(printf '%s' "$prompt_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["prompt_file"])')
    log_dir="$repo_root/.consensus-rnd-spec/logs"
    mkdir -p "$log_dir"
    exec "$skill_root/scripts/consensus-rnd-cli" spawn-codex \
      --cd "$repo_root" \
      --add-dir "$repo_root" \
      --prompt "$prompt_file" \
      --log "$log_dir/native-loop-$(date -u +%Y%m%dT%H%M%SZ).log" \
      --stall 3600
    ;;
  *)
    printf '{"backend":"native","status":"blocked","reason":"unsupported command"}\n'
    exit 2
    ;;
esac
