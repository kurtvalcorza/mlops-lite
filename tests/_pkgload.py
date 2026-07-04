"""Load a `gateway/app/*.py` file as a member of a synthetic package (offline harness helper).

018 FR-176 retired the standalone `sys.path` import fallbacks that let these modules be loaded with
no parent package (`quality._eval`'s `from app import evaluation`, `shadow`'s `import quality`,
`batch._load_rows`'s `from app.validation import parse_rows`). They are now only ever loaded
package-relative (the gateway package in production; the trainer reaches the cores through
`platformlib.gateway_bridge`). So the offline tests load them the same way: as a submodule of a
synthetic package rooted at `gateway/app`, so every `from . import sibling` resolves to the real file.

Each `fresh_package()` mints a UNIQUE package name so independently-loaded module instances (e.g. the
several configured `quality` modules the shadow tests build) never clobber each other's siblings in
`sys.modules`.
"""
import importlib.util
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "gateway", "app")
_SEQ = [0]


def fresh_package() -> str:
    """A unique synthetic package rooted at gateway/app; returns its name."""
    _SEQ[0] += 1
    name = f"_gwapp{_SEQ[0]}"
    pkg = types.ModuleType(name)
    pkg.__path__ = [APP]
    sys.modules[name] = pkg
    return name


def load_in_package(pkgname: str, name: str):
    """Load gateway/app/<name>.py as `<pkgname>.<name>` and exec it (its relative imports resolve via
    the package's __path__)."""
    fq = f"{pkgname}.{name}"
    spec = importlib.util.spec_from_file_location(fq, os.path.join(APP, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fq] = mod
    spec.loader.exec_module(mod)
    return mod


def register_sibling(pkgname: str, name: str, mod) -> None:
    """Register an already-configured module as `<pkgname>.<name>` so a sibling loaded afterwards binds
    its `from . import <name>` to it (used to inject the configured `quality` before loading `shadow`)."""
    sys.modules[f"{pkgname}.{name}"] = mod
