#!/bin/sh
# Entrypoint fixed by the competition contract: ./run.sh "<task instruction>"
#
# Deliberately /bin/sh and not bash: the target image is unverified and this file is
# the one thing that must never fail to start. A missing interpreter here is a zero
# on every task at once.
#
# The working directory is NOT changed. The runner starts us inside the task
# workspace, and that path is the agent's whole world; cd-ing to the archive would
# quietly point every relative lookup at our own source tree instead. The archive is
# reached through PYTHONPATH instead.
#
# "$@" rather than "$1" so an instruction containing quotes, newlines, backslashes or
# leading dashes arrives intact.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec python3 -m agent.main "$@"
