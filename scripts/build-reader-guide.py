from pathlib import Path
root = Path(__file__).resolve().parents[1]
for lang, suffix in [("en", ""), ("zh", ".zh")]:
    (root / "euler/observability/static" / f"help.{lang}.md").write_text(
        (root / "docs" / f"DASHBOARD-GUIDE{suffix}.md").read_text(encoding="utf-8"), encoding="utf-8")
