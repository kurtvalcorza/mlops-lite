#!/usr/bin/env python3
"""Spec-artifact consistency + retired-port guard (023 T495/T507, FR-280/FR-296).

The `specs` CI gate (contracts/delivery-gates.md §Specification consistency). Stdlib-only, exit 0
on pass / 1 with a per-finding report on failure. Checks:

  1. **Artifact sets** — every `specs/NNN-*` feature folder carries `spec.md`/`plan.md`/`tasks.md`;
     folders newer than the SpecKit full-artifact convention also carry `research.md`,
     `data-model.md`, `quickstart.md`. Historical folders that predate the convention are
     explicitly allow-listed below — a NEW incomplete folder is a failure, never silently ignored.
  2. **Relative links** — `](./...)`/`](../...)` markdown links inside a feature folder resolve to
     real files (catches a renamed contract the spec still points at).
  3. **Placeholders** — no unresolved `[NEEDS CLARIFICATION: ...]` markers or `{{template}}`
     placeholders remain anywhere under `specs/`.
  4. **ID discipline** — within each folder, `FR-`/`SC-` definitions in spec.md and `T` definitions
     in tasks.md are unique and strictly increasing (an inserted duplicate or out-of-order ID is a
     copy-paste error waiting to mislead an implementer).
  5. **Story coverage** — for folders using the story convention, every `[USn]` task tag maps to a
     `### User Story n` heading in spec.md, every story has at least one task, and every story
     declares an **Independent Test**.
  6. **Retired-port guard (FR-280)** — executable sources must not form request URLs against the
     six per-daemon inference ports (`:8090`–`:8095`) retired at 018 T364 (everything serves from
     the one host agent, `platformlib.topology.AGENT_PORT`). Documentation, historical comments,
     and `specs/`/`docs/` prose are exempt; a deliberate negative fixture opts out with an inline
     `retired-port-ok` marker. (The supervisor status port :8099 is still live — not in the set.)

The delivery-gates rule that implementation tasks stay unchecked in a *spec-only PR* needs PR
context a repository checker doesn't have; it stays a review-time rule (the PR template/reviewer),
not a false-positive source here.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPECS = os.path.join(REPO, "specs")

#: Feature folders that predate the full six-artifact SpecKit convention (015+). Frozen: never add
#: a new folder here — a new increment missing artifacts must fail the gate (delivery-gates.md).
HISTORICAL_PARTIAL = frozenset({
    "002-hardening", "003-frontend", "004-hardening", "005-hardening", "006-inference-tracing",
    "007-stack-refresh", "008-gpu-lease", "009-inference-modalities", "010-multimodal-finetune",
    "011-evaluation-gates", "012-hyperparameter-optimization", "013-quality-monitoring",
    "014-batch-and-validation", "019-review-remediation-018",
})

ALWAYS_REQUIRED = ("spec.md", "plan.md", "tasks.md")
FULL_SET = ALWAYS_REQUIRED + ("research.md", "data-model.md", "quickstart.md")

# -- retired-port guard (FR-280) --------------------------------------------------------------------

RETIRED_PORTS = tuple(range(8090, 8096))  # the six per-daemon inference ports, retired at 018 T364
#: URL-forming usage only (`:<port>` in a host:port position) — a bare number in a hash/id is noise.
_PORT_RE = re.compile(r":(?:%s)\b" % "|".join(str(p) for p in RETIRED_PORTS))
EXEC_EXTS = {".py", ".sh", ".ps1", ".yml", ".yaml", ".ts", ".tsx", ".mjs", ".env"}
SKIP_DIRS = {"specs", "docs", "node_modules", ".next", ".git", "__pycache__", ".specify",
             "notebooks", ".claude"}
_COMMENT = re.compile(r"^\s*(#|//|--|<!--|\*|REM\b)")


def _iter_exec_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if os.path.splitext(f)[1] in EXEC_EXTS or f.startswith(".env"):
                yield os.path.join(root, f)


def check_retired_ports(problems: list) -> None:
    for path in _iter_exec_files():
        try:
            lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
        except OSError:
            continue
        is_py = path.endswith(".py")
        in_docstring = False
        for i, line in enumerate(lines, 1):
            # Track triple-quoted blocks in Python: a docstring is documentation, not routing —
            # exempt like comments (FR-280). An odd count of `\"\"\"` on a line toggles the state.
            if is_py:
                was_doc = in_docstring
                if line.count('"""') % 2 == 1:
                    in_docstring = not in_docstring
                if was_doc or line.lstrip().startswith('"""'):
                    continue  # inside a docstring block, or a one-line/opening docstring line
            if not _PORT_RE.search(line):
                continue
            if "retired-port-ok" in line or _COMMENT.match(line):
                continue  # historical comment / explicit negative fixture — exempt (FR-280)
            rel = os.path.relpath(path, REPO)
            problems.append(f"{rel}:{i}: retired daemon port in executable source — route via "
                            f"platformlib.topology.agent_url() (FR-280): {line.strip()[:100]}")


# -- spec folder checks (FR-296) --------------------------------------------------------------------

_LINK_RE = re.compile(r"\]\((\.{1,2}/[^)#]+)")
_PLACEHOLDER_RES = (re.compile(r"\[NEEDS CLARIFICATION:"),
                    re.compile(r"(?<!\$)\{\{[^}]*\}\}"))
_FR_DEF = re.compile(r"^- \*\*FR-(\d+)\*\*", re.M)
_SC_DEF = re.compile(r"^- \*\*SC-(\d+)\*\*", re.M)
_TASK_DEF = re.compile(r"^- \[[ xX]\] \*\*T(\d+)\*\*", re.M)
_STORY_HEAD = re.compile(r"^###+ User Story (\d+)", re.M)
_TASK_STORY = re.compile(r"\[US(\d+)\]")


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


def _check_ids(problems, folder, label, numbers):
    seen = set()
    prev = None
    for n in numbers:
        n = int(n)
        if n in seen:
            problems.append(f"{folder}: duplicate {label}-{n:03d} definition")
        seen.add(n)
        if prev is not None and n <= prev:
            problems.append(f"{folder}: {label}-{n:03d} defined out of order (after {label}-{prev:03d})")
        prev = n


def check_spec_folders(problems: list) -> None:
    for entry in sorted(os.listdir(SPECS)):
        folder = os.path.join(SPECS, entry)
        if not os.path.isdir(folder) or not re.match(r"\d{3}-", entry):
            continue
        required = ALWAYS_REQUIRED if entry in HISTORICAL_PARTIAL else FULL_SET
        for f in required:
            if not os.path.isfile(os.path.join(folder, f)):
                problems.append(f"specs/{entry}: missing required artifact {f}")

        md_files = []
        for root, dirs, files in os.walk(folder):
            md_files += [os.path.join(root, f) for f in files if f.endswith(".md")]

        for path in md_files:
            text = _read(path)
            rel = os.path.relpath(path, REPO)
            for pat in _PLACEHOLDER_RES:
                if pat.search(text):
                    problems.append(f"{rel}: unresolved placeholder/clarification marker "
                                    f"({pat.pattern[:30]}…)")
            for target in _LINK_RE.findall(text):
                resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
                if not os.path.exists(resolved):
                    problems.append(f"{rel}: broken relative link -> {target}")

        spec_md = os.path.join(folder, "spec.md")
        tasks_md = os.path.join(folder, "tasks.md")
        spec_text = _read(spec_md) if os.path.isfile(spec_md) else ""
        tasks_text = _read(tasks_md) if os.path.isfile(tasks_md) else ""
        _check_ids(problems, f"specs/{entry}", "FR", _FR_DEF.findall(spec_text))
        _check_ids(problems, f"specs/{entry}", "SC", _SC_DEF.findall(spec_text))
        _check_ids(problems, f"specs/{entry}", "T", _TASK_DEF.findall(tasks_text))

        # Story coverage — only for folders that use the [USn] convention in tasks.md.
        stories = set(_STORY_HEAD.findall(spec_text))
        tagged = set(_TASK_STORY.findall(tasks_text))
        if tagged:
            for us in sorted(tagged - stories):
                problems.append(f"specs/{entry}: tasks reference [US{us}] but spec.md has no "
                                f"'User Story {us}' heading")
            for us in sorted(stories - tagged):
                problems.append(f"specs/{entry}: User Story {us} has no [US{us}] task coverage")
            if stories and "**Independent Test**" not in spec_text \
                    and "**Independent test**" not in spec_text:
                problems.append(f"specs/{entry}: stories declare no Independent Test")


def main() -> int:
    problems: list = []
    check_spec_folders(problems)
    check_retired_ports(problems)
    if problems:
        print(f"check_specs: {len(problems)} problem(s)")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("check_specs: OK (artifacts, links, placeholders, IDs, stories, retired-port guard)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
