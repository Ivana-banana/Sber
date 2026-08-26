# Worker readiness beacon

Write `/opt/runstate/ready.signal`. Its complete content must be exactly `Salut` with no trailing newline. The coordinator reads bytes, not console output.
