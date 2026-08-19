# Session capacity and terminal secret boundary

This note records two remediated reliability and security lessons in generalized
form. It contains no deployment-specific measurements or identifiers.

## Reclaimable file cache is not memory pressure

Container admission must distinguish memory that the kernel can reclaim from
memory that cannot be reclaimed without pressure. A working-set calculation
that subtracts only inactive file cache can count active, still-reclaimable
pages as if they were anonymous memory.

For illustration, a synthetic 8 GiB container might report 6 GiB of file cache
and 1.5 GiB of anonymous memory. Treating all active file cache as pressure can
refuse a new session even though the anonymous footprint is well below the
configured watermark. The admission controller now subtracts both active and
inactive file cache, keeps swap-backed shared memory counted, and falls back to
count limits when cgroup accounting is unavailable or malformed.

The controller also accounts for in-flight launches, so concurrent requests
cannot all pass the same snapshot and collectively exceed the reserve. Its
status and rejection responses report whether telemetry is available, rather
than presenting unavailable memory data as a healthy zero.

## Browser terminals are a lower-trust environment

The application process may need long-lived service credentials and short-lived
token-broker capability. A browser terminal must not inherit that process
environment wholesale: model-generated code runs in the terminal tier, which
has lower trust than the Flask process.

Terminal environments therefore use a reviewed allowlist for non-secret
runtime settings, reject credential-bearing URL values, and apply a
credential-shaped-name deny check as defense in depth. Workspace credentials,
client secrets, registry tokens, bootstrap secrets, and arbitrary future
credential variables are excluded. The loopback broker coordinate is the
narrow, explicitly reviewed exception needed by terminal-launched CLIs; it is
not itself a bearer token.

This is an environment-inheritance control, not an OS process sandbox. Strong
confidentiality from terminal-executed code still requires separate identities
or container/UID isolation.
