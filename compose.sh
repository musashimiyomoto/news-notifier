#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./compose.sh <cpu|gpu> <docker compose arguments...>

Examples:
  ./compose.sh cpu up -d --build
  ./compose.sh gpu up -d --build
  ./compose.sh gpu logs -f llm worker
  ./compose.sh cpu down
EOF
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" || ${1:-} == "help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 2 ]]; then
  usage >&2
  exit 2
fi

mode=$1
shift
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
compose_files=(-f "$script_dir/docker-compose.yml")

case "$mode" in
  cpu)
    ;;
  gpu)
    compose_files+=(-f "$script_dir/docker-compose.gpu.yml")
    ;;
  *)
    echo "Unknown LLM mode: $mode" >&2
    usage >&2
    exit 2
    ;;
esac

exec docker compose --project-directory "$script_dir" "${compose_files[@]}" "$@"
