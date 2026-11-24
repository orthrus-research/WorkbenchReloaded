"""What this machine's ``[lab]`` says: the lab home, the trusted projects, the EULA and the driver's variable names.

wb-host passes the table it checked at start in the job's spec (``lab``); a job run without one reads the machine's
config folder. A project's configure hook or driver runs only when the project is in ``[lab] trusted``: Workbench
builds and plays any launch, but runs a project's own code only where the machine's owner said so.
"""

from __future__ import annotations

import os
from pathlib import Path

from wb_core.module import EnvironmentProblem, ParamError


def lab(ctx) -> dict:
    """The checked ``[lab]`` table for a job: the one its spec carries, else the machine config's, else defaults."""
    from wb_core.settings import SettingsError, check_lab
    given = (getattr(ctx, "spec", None) or {}).get("lab")
    if not isinstance(given, dict):
        try:
            from wb import config as wbconfig
            given = wbconfig.load().values.get("lab")
        except Exception:  # noqa: BLE001 - no machine config (a test, a bare checkout): the defaults
            given = None
    try:
        return check_lab(given)
    except SettingsError as error:
        raise EnvironmentProblem(str(error)) from None


def home(ctx) -> Path:
    """The lab home: ``[lab] home``, else the one wb-host names (``[runner] lab_home``)."""
    value = lab(ctx)["home"] or os.environ.get("WB_LAB_HOME", "")
    if not value:
        raise EnvironmentProblem("no lab home: set [lab] home, or run this on wb-host, which names its lab home")
    return Path(os.path.expanduser(value))


def require_trusted(ctx, project: str, what: str):
    """Refuses ``what`` (a configure hook, a driver) of ``project`` unless the machine trusts the project."""
    trusted = lab(ctx)["trusted"]
    if project not in trusted:
        raise ParamError(f"{what} is {project or 'an unnamed project'}'s code, and {project or 'it'} is not in [lab] "
                         f"trusted on this machine ({', '.join(trusted) or 'none trusted'}); add it there to let its "
                         "hooks and drivers run, or go without")
