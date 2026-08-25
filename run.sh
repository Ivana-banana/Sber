#!/bin/sh
# Entrypoint fixed by the competition contract: ./run.sh "<task instruction>"
#
# Deliberately /bin/sh and not bash: the target image is unverified and this file is
# the one thing that must never fail to start. A missing interpreter here is a zero
# on every task at once.
#
# The working directory is NOT changed, but not for the reason it might look like.
# Harbor execs us with cwd set to the agent's own install directory
# (/opt/harbor/local-agent), NOT the task's directory -- verified against Harbor
# 0.16.1 and the competition wrapper. So cwd is our source tree, and treating it as
# the workspace would point every relative lookup at ourselves. The agent resolves the
# real workspace itself; see agent.main.resolve_workspace. cwd is preserved only so
# that resolution still has the runner's own choice available as a last resort.
#
# The archive is reached through PYTHONPATH rather than cd for the same reason.
#
# "$@" rather than "$1" so an instruction containing quotes, newlines, backslashes or
# leading dashes arrives intact.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec python3 -m agent.main "$@"
