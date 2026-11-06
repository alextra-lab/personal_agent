"""Checks of a live dev-agent container against the spec (ADR-0159 AC-3; FRE-1682).

Every function is pure. It takes data that a caller read from Docker or from the probe, and
returns a list of violations. An empty list means the data matches the spec. A missing or
malformed field is a violation and never an exception, because a check that crashes on bad
input is a check that can be read as "no finding".
"""

from __future__ import annotations

from collections.abc import Mapping

from personal_agent.dev_agent.container_spec import ContainerProfile, tmpfs_option

#: Targets that the probe must report as unreachable. A probe report that omits one of them is
#: not evidence, so :func:`probe_report_violations` flags the omission.
REQUIRED_DENIED_LABELS: frozenset[str] = frozenset(
    {
        "postgres",
        "neo4j",
        "elasticsearch",
        "redis",
        "caddy-8600",
        "caddy-8601",
        "gateway",
        "metadata-169.254.169.254",
        "github.com",
        "github.com-via-proxy",
        "host-gateway",
        "ip-literal",
        "ip-literal-via-proxy",
        "ipv6-literal",
        "ipv6-literal-via-proxy",
        "npm-port-80-direct",
        "npm-port-80-via-proxy",
    }
)

#: Positive controls. The probe must reach these, or its "unreachable" results prove nothing.
REQUIRED_ALLOWED_LABELS: frozenset[str] = frozenset({"npm-https-via-proxy"})

#: Docker adds this variable to every container. It is not a declared one.
DOCKER_ADDED_ENV: frozenset[str] = frozenset({"HOSTNAME"})


def declared_env_names(profile: ContainerProfile) -> frozenset[str]:
    """Return the environment variable names that the spec allows in a container.

    Args:
        profile: The container profile.

    Returns:
        The fixed names, the launcher names, and the one name that Docker adds.
    """
    return frozenset(profile.env_fixed) | frozenset(profile.env_from_launcher) | DOCKER_ADDED_ENV


def _dig(obj: object, *path: str) -> object:
    """Walk nested mappings. Returns ``None`` when any step is missing."""
    node = obj
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def inspect_violations(
    profile: ContainerProfile,
    inspect: Mapping[str, object],
    *,
    network_name: str,
    volumes: Mapping[str, str],
) -> list[str]:
    """Compare a ``docker inspect`` object with a profile.

    Args:
        profile: The container profile that the container must match.
        inspect: One element of the ``docker inspect`` JSON array.
        network_name: Real name of the ``dev-agent`` network.
        volumes: The named volume of each mount target.

    Returns:
        One text per difference. An empty list means every flag, limit, mount and
        environment name matches, and no socket or host path is mounted.
    """
    found: list[str] = []

    def expect(field: str, actual: object, wanted: object) -> None:
        if actual != wanted:
            found.append(f"{field}: {actual!r}, spec says {wanted!r}")

    memory = profile.memory_mb * 1024 * 1024
    expect("Config.User", _dig(inspect, "Config", "User"), profile.user)
    expect("HostConfig.CapDrop", _dig(inspect, "HostConfig", "CapDrop"), list(profile.cap_drop))
    cap_add = _dig(inspect, "HostConfig", "CapAdd")
    if cap_add:
        found.append(f"HostConfig.CapAdd: {cap_add!r}, spec says none")
    security_opt = _dig(inspect, "HostConfig", "SecurityOpt")
    has_nnp = isinstance(security_opt, list) and "no-new-privileges" in security_opt
    expect("no-new-privileges", has_nnp, profile.no_new_privileges)
    expect("HostConfig.Privileged", _dig(inspect, "HostConfig", "Privileged"), False)
    expect("HostConfig.Memory", _dig(inspect, "HostConfig", "Memory"), memory)
    expect("HostConfig.MemorySwap", _dig(inspect, "HostConfig", "MemorySwap"), memory)
    expect(
        "HostConfig.NanoCpus",
        _dig(inspect, "HostConfig", "NanoCpus"),
        int(profile.cpus * 1_000_000_000),
    )
    expect("HostConfig.PidsLimit", _dig(inspect, "HostConfig", "PidsLimit"), profile.pids_limit)
    expect(
        "HostConfig.ReadonlyRootfs",
        _dig(inspect, "HostConfig", "ReadonlyRootfs"),
        profile.read_only_root,
    )
    expect("HostConfig.Init", bool(_dig(inspect, "HostConfig", "Init")), profile.init)
    expect("HostConfig.PidMode", _dig(inspect, "HostConfig", "PidMode"), "")
    expect("HostConfig.IpcMode", _dig(inspect, "HostConfig", "IpcMode"), "private")
    expect("HostConfig.UsernsMode", _dig(inspect, "HostConfig", "UsernsMode"), "")
    expect("HostConfig.Devices", _dig(inspect, "HostConfig", "Devices"), [])
    expect("HostConfig.PortBindings", _dig(inspect, "HostConfig", "PortBindings"), {})

    wanted_network = network_name if profile.network == "dev-agent" else "none"
    expect("HostConfig.NetworkMode", _dig(inspect, "HostConfig", "NetworkMode"), wanted_network)
    networks = _dig(inspect, "NetworkSettings", "Networks")
    attached = sorted(networks) if isinstance(networks, Mapping) else None
    expect("NetworkSettings.Networks", attached, [wanted_network])

    wanted_tmpfs = {t.path: tmpfs_option(t, profile.user).split(":", 1)[1] for t in profile.tmpfs}
    expect("HostConfig.Tmpfs", _dig(inspect, "HostConfig", "Tmpfs"), wanted_tmpfs)

    found += _mount_violations(profile, inspect, volumes)
    found += _env_violations(profile, inspect)
    return found


def _mount_violations(
    profile: ContainerProfile, inspect: Mapping[str, object], volumes: Mapping[str, str]
) -> list[str]:
    found: list[str] = []
    mounts = _as_mapping_list(_dig(inspect, "Mounts"))
    by_target = {str(m.get("Destination")): m for m in mounts}
    for mount in mounts:
        destination = str(mount.get("Destination"))
        source = str(mount.get("Source"))
        if "docker.sock" in destination or "docker.sock" in source:
            found.append(f"Mounts: the Docker socket is mounted at {destination}")
        if mount.get("Type") != "volume":
            found.append(
                f"Mounts: {destination} is a {mount.get('Type')!r} mount, only volumes are allowed"
            )
    for spec_mount in profile.mounts:
        actual = by_target.get(spec_mount.target)
        if actual is None:
            found.append(f"Mounts: {spec_mount.target} is not mounted")
            continue
        if actual.get("RW") != (not spec_mount.read_only):
            mode = "read-only" if spec_mount.read_only else "writable"
            found.append(f"Mounts: {spec_mount.target} must be {mode}")
        if actual.get("Name") != volumes.get(spec_mount.target):
            found.append(f"Mounts: {spec_mount.target} has the wrong volume {actual.get('Name')!r}")
    allowed_targets = {m.target for m in profile.mounts}
    for destination in sorted(set(by_target) - allowed_targets):
        found.append(f"Mounts: {destination} is not in the spec")
    binds = _dig(inspect, "HostConfig", "Binds")
    if isinstance(binds, list):
        for bind in binds:
            if str(bind).startswith("/") or "docker.sock" in str(bind):
                found.append(f"HostConfig.Binds: {bind!r} is a host path")
    return found


def _env_violations(profile: ContainerProfile, inspect: Mapping[str, object]) -> list[str]:
    found: list[str] = []
    raw = _dig(inspect, "Config", "Env")
    entries = [str(e) for e in raw] if isinstance(raw, list) else []
    actual = {e.partition("=")[0]: e.partition("=")[2] for e in entries}
    declared = set(profile.env_fixed) | set(profile.env_from_launcher)
    for name in sorted(set(actual) - declared):
        found.append(f"Config.Env: {name} is not declared in the spec")
    for name in sorted(declared - set(actual)):
        found.append(f"Config.Env: {name} is declared but not set")
    for name, value in profile.env_fixed.items():
        if name in actual and actual[name] != value:
            found.append(f"Config.Env: {name} has a value that differs from the spec")
    return found


def parse_csp(header: str) -> dict[str, frozenset[str]]:
    """Parse a CSP header into ``{directive: {tokens}}``.

    Args:
        header: The ``Content-Security-Policy`` header value.

    Returns:
        The directives. Token order is not significant.
    """
    parsed: dict[str, frozenset[str]] = {}
    for chunk in header.split(";"):
        parts = chunk.split()
        if parts:
            parsed[parts[0]] = frozenset(parts[1:])
    return parsed


def csp_header_violations(
    header: str,
    *,
    origin: str,
    expected: Mapping[str, frozenset[str]],
    artifacts_origin: str,
) -> list[str]:
    """Compare a served CSP header with the expected artifact policy.

    Args:
        header: The header that the test server sent.
        origin: The localhost origin of the test server.
        expected: ``expected_csp_directives()``.
        artifacts_origin: The artifacts origin token inside ``expected``. It is replaced by
            ``origin`` before the comparison.

    Returns:
        One text per directive that is missing, extra or different.
    """
    served = parse_csp(header)
    wanted = {
        directive: frozenset(origin if token == artifacts_origin else token for token in tokens)
        for directive, tokens in expected.items()
    }
    found: list[str] = []
    for directive in sorted(set(wanted) - set(served)):
        found.append(f"CSP: directive {directive} is missing")
    for directive in sorted(set(served) - set(wanted)):
        found.append(f"CSP: directive {directive} is not in the expected policy")
    for directive in sorted(set(wanted) & set(served)):
        if served[directive] != wanted[directive]:
            found.append(
                f"CSP: {directive} is {sorted(served[directive])}, "
                f"expected {sorted(wanted[directive])}"
            )
    return found


#: Required targets that the probe must attempt by address. A name that only fails to resolve
#: proves nothing about the network, so these need a TCP or proxy stage.
ADDRESS_STAGE_LABELS: frozenset[str] = frozenset(
    {
        "postgres",
        "neo4j",
        "elasticsearch",
        "redis",
        "caddy-8600",
        "caddy-8601",
        "gateway",
        "metadata-169.254.169.254",
        "host-gateway",
        "github.com-via-proxy",
        "ip-literal",
        "ip-literal-via-proxy",
        "ipv6-literal",
        "ipv6-literal-via-proxy",
        "npm-port-80-via-proxy",
        "npm-https-via-proxy",
    }
)

_VALID_RESULTS: frozenset[str] = frozenset({"connected", "failed"})


def probe_report_violations(
    report: Mapping[str, object], *, allowed_env: frozenset[str]
) -> list[str]:
    """Judge a probe report independently of the probe's own verdict.

    The checker sets the polarity of each required target. A probe cannot turn a forbidden
    target into an allowed one by labelling it so.

    Args:
        report: The JSON report that the probe printed.
        allowed_env: The environment names that may exist in the container.

    Returns:
        One text per problem: a required target that is missing or reported twice, a forbidden
        target that answered, an allowed target that did not, a target that was not attempted,
        a result that is not ``connected`` or ``failed``, a required address target that failed
        only at name resolution, or an undeclared environment name.
    """
    targets = _as_mapping_list(report.get("targets"))
    if not targets:
        return ["probe report has no targets"]
    found: list[str] = []
    by_label: dict[str, list[Mapping[str, object]]] = {}
    for target in targets:
        by_label.setdefault(str(target.get("label")), []).append(target)

    required = {label: "deny" for label in REQUIRED_DENIED_LABELS}
    required |= {label: "allow" for label in REQUIRED_ALLOWED_LABELS}
    for label in sorted(required):
        entries = by_label.get(label, [])
        if not entries:
            found.append(f"probe did not report the required target {label}")
        elif len(entries) > 1:
            found.append(f"probe reported the target {label} {len(entries)} times")
    for label, entries in sorted(by_label.items()):
        for entry in entries:
            found += _target_violations(label, entry, required.get(label))
    env = report.get("env")
    names = [str(n) for n in env] if isinstance(env, list) else []
    for name in sorted(set(names) - allowed_env):
        found.append(f"environment name {name} is not declared in the spec")
    return found


def _target_violations(
    label: str, entry: Mapping[str, object], required_polarity: str | None
) -> list[str]:
    """Check one probe entry. ``required_polarity`` overrides the entry's own ``expect``."""
    found: list[str] = []
    result = entry.get("result")
    polarity = required_polarity or str(entry.get("expect"))
    if entry.get("attempted") is not True:
        found.append(f"target {label} was not attempted")
    if result not in _VALID_RESULTS:
        found.append(f"target {label} has the result {result!r}")
        return found
    if polarity == "deny" and result != "failed":
        found.append(f"forbidden target {label} answered ({result!r})")
    if polarity == "allow" and result != "connected":
        found.append(f"allowed target {label} did not answer ({result!r})")
    if label in ADDRESS_STAGE_LABELS and entry.get("stage") not in {"tcp", "proxy"}:
        found.append(f"target {label} failed at stage {entry.get('stage')!r}, not at the network")
    return found
