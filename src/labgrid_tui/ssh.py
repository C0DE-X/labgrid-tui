"""Build the ssh command for logging into an exporter host."""

from __future__ import annotations

import shlex

from .config import Profile

DEFAULT_SSH_COMMAND = "ssh {jump} {target}"


def ssh_target_host(name: str, fqdn: str) -> str:
    """Host to connect to: the FQDN the exporter reports, else its name.

    The exporter name defaults to the machine's hostname, so it is a reasonable
    fallback when the FQDN is unknown (exporter offline and never seen online).
    """
    return fqdn or name


def ssh_command(profile: Profile, name: str, fqdn: str = "") -> list[str]:
    """Expand the profile's ssh command template.

    Placeholders: {target} (user@host or host), {host}, {user}, {name} (exporter
    name), {fqdn}, and {jump}, which becomes ``-J <jump host>`` or disappears.
    The jump host defaults to the profile's labgrid proxy (-P), because an
    exporter that needs a proxy for labgrid usually needs it for ssh too.
    Set ``ssh_jump = "none"`` to disable that.
    """
    host = ssh_target_host(name, fqdn)
    user = profile.ssh_user
    jump = profile.ssh_jump or profile.proxy
    if jump.lower() == "none":
        jump = ""
    values = {
        "target": f"{user}@{host}" if user else host,
        "host": host,
        "user": user,
        "name": name,
        "fqdn": fqdn or host,
    }
    cmd: list[str] = []
    for token in shlex.split(profile.ssh_command or DEFAULT_SSH_COMMAND):
        if token == "{jump}":
            cmd += ["-J", jump] if jump else []
            continue
        value = token.format(**values)
        if value:
            cmd.append(value)
    return cmd
