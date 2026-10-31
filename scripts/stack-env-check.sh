#!/usr/bin/env bash
#
# Run before any `docker stack deploy` of gostyle-consumer. It loads
# stack.env exactly the way the deploy does (a shell, `set -a`) and compares
# every value with what the live services have now. A line it prints is a
# setting the deploy would change. Read only; prints names, never values.
#
# Runs ON the Swarm node, as root, from the repo checkout:
#
#   cd /root/gostyle-customer && bash scripts/stack-env-check.sh
#
# Deploy only when it prints nothing but "check done", or every line left is
# one you mean to change. A value with a space or any of $ ` " ' \ & ; | < > ( ) !
# must be in single quotes in stack.env, or the shell changes it
# (docs/DEPLOYMENT.md). "true" and "True" count as the same.
#
set -o pipefail

cd "${STACK_DIR:-/root/gostyle-customer}" || exit 1

envof() {
  docker service inspect "gostyle-consumer_$1" \
    --format '{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}'
}

echo "== names in stack.env more than once"
grep -v '^#' stack.env | cut -s -d= -f1 | sort | uniq -d

echo "== stack.env, loaded as a stack deploy loads it, vs live"
(
  set -a
  . ./stack.env
  set +a
  for s in api grpc worker; do
    envof "$s" | while IFS= read -r kv; do
      [ -z "$kv" ] && continue
      n=${kv%%=*}
      v=${kv#*=}
      if [ -z "${!n+x}" ]; then
        echo "$s $n: not in stack.env"
      elif [ "${!n}" != "$v" ]; then
        case "$(printf '%s' "${!n}" | tr A-Z a-z)/$(printf '%s' "$v" | tr A-Z a-z)" in
          true/true | false/false) ;;
          *) echo "$s $n: DIFFERENT" ;;
        esac
      fi
    done
  done
)
echo "check done"
