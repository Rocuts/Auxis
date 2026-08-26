"""Render every ```mermaid block in the given Markdown files, and fail on any
that Mermaid cannot parse.

Why this exists: GitHub renders Mermaid client-side with its own bundled
build, and it does NOT bundle the C4 plugin — `C4Context` / `C4Component`
render as raw text there while rendering perfectly in the Mermaid live editor
and in this CLI (GitHub community discussion #197898, closed unanswered,
2026-06-03). That asymmetry is a trap: "it renders locally" proves nothing
about GitHub. So the diagrams in this repo are plain `flowchart`s applying C4
semantics through subgraph boundaries and typed labels, and this script
brackets the version risk by parsing them under two Mermaid majors — if a
diagram parses under both 10 and 11, GitHub's bundle renders it.

    uv run python scripts/check_diagrams.py README.md
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: Bracket GitHub's unknown bundled version rather than guess it.
MERMAID_VERSIONS = ("10", "11")
FENCE = re.compile(r"^```mermaid\n(.*?)^```", re.MULTILINE | re.DOTALL)


def blocks(path: Path) -> list[str]:
    return FENCE.findall(path.read_text())


def render(source: str, version: str, out_dir: Path, name: str) -> tuple[bool, str]:
    src = out_dir / f"{name}.mmd"
    src.write_text(source)
    proc = subprocess.run(
        [
            "npx",
            "--yes",
            f"@mermaid-js/mermaid-cli@{version}",
            "-i",
            str(src),
            "-o",
            str(out_dir / f"{name}.svg"),
        ],
        capture_output=True,
        text=True,
    )
    ok = proc.returncode == 0 and (out_dir / f"{name}.svg").exists()
    return ok, (proc.stderr or proc.stdout).strip()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_diagrams.py <markdown> [<markdown> ...]", file=sys.stderr)
        return 2
    if shutil.which("npx") is None:
        print("npx not found: install Node to run the diagram check", file=sys.stderr)
        return 2

    failures = 0
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        for name in argv:
            path = Path(name)
            found = blocks(path)
            if not found:
                print(f"{path}: no mermaid blocks")
                continue
            for index, source in enumerate(found, 1):
                total += 1
                if source.lstrip().startswith(("C4Context", "C4Container", "C4Component")):
                    print(f"{path} #{index}: FAIL - C4 syntax does not render on GitHub")
                    failures += 1
                    continue
                for version in MERMAID_VERSIONS:
                    ok, detail = render(source, version, out_dir, f"{path.stem}_{index}_{version}")
                    status = "ok" if ok else "FAIL"
                    print(f"{path} #{index} mermaid@{version}: {status}")
                    if not ok:
                        failures += 1
                        print(detail, file=sys.stderr)
    print(f"\n{total} diagram(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
