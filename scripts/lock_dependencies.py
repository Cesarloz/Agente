"""Record the verified direct dependency set, keeping platform-specific transitives portable."""
from importlib.metadata import version
from pathlib import Path
import tomllib

from packaging.requirements import Requirement

project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
lines = ["# Direct dependencies resolved and tested on 2026-09-05 with Python 3.12.", "# Refresh deliberately: install pyproject.toml, run tests, then run this script."]
for entry in project["dependencies"]:
    requirement = Requirement(entry)
    extras = "[" + ",".join(sorted(requirement.extras)) + "]" if requirement.extras else ""
    lines.append(f"{requirement.name}{extras}=={version(requirement.name)}")
Path("requirements.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("requirements.lock updated (direct dependencies).")
