# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""A no-account workbench tour backed by the real offline integration demo."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def prepare() -> tuple[Path, Path]:
    from euler.demo import run_offline_demo
    report = run_offline_demo(retain_workdir=True)
    if not report.ok:
        raise RuntimeError(report.format_text())
    root = Path(report.workdir)
    completed = root / "projects" / "DEMO"
    fresh = root / "projects" / "exploration"
    fresh.mkdir()
    # This starting project intentionally has no Facts, manuscript, or Lean files.
    (fresh / "project.json").write_text(json.dumps({
        "name": "New exploration (demo)", "workers": [],
        "goal": "Explore a new mathematical question; no result has been accepted."
    }), encoding="utf-8")
    catalog = root / "catalog.json"
    catalog.write_text(json.dumps({"projects": [
        {"id": "completed", "name": "Offline workflow (demo)",
         "description": "Deterministic mock models; tests the control path, not mathematical ability.",
         "project_dir": str(completed)},
        {"id": "exploration", "name": "New exploration (demo)",
         "description": "An open task with no paper, Lean code, or accepted Facts.",
         "project_dir": str(fresh)}
    ]}), encoding="utf-8")
    config = root / "dashboard.json"
    config.write_text(json.dumps({"name": "Offline workflow (demo)",
        "description": "Real scheduler, receipts and verification protocol; deterministic mock models. Not a research result.",
        "project_catalog": str(catalog)}), encoding="utf-8")
    (root / "demo-result.json").write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return completed, config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--prepare-only", action="store_true", help="print paths without starting a server")
    args = parser.parse_args()
    project, config = prepare()
    print(json.dumps({"project": str(project), "config": str(config),
                      "url": f"http://127.0.0.1:{args.port}/"}), flush=True)
    if not args.prepare_only:
        import uvicorn
        os.environ["EULER_DASHBOARD_PROJECT"] = str(project)
        os.environ["EULER_DASHBOARD_CONFIG"] = str(config)
        uvicorn.run("euler.observability.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
