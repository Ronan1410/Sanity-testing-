#!/usr/bin/env python3
"""
generate_report.py
-------------------
Converts the CSV results + logcat captures produced by adb_sanity_test.sh
into a single Markdown report. Optionally converts that Markdown to .docx
if pandoc is installed on the machine running this script.

Usage:
    python3 generate_report.py <results_dir>
    python3 generate_report.py <results_dir> --docx

Expects <results_dir>/results.csv and <results_dir>/logs/*.txt
(exactly what adb_sanity_test.sh produces).
"""

import csv
import sys
import shutil
import subprocess
import argparse
from pathlib import Path
from datetime import datetime

STATUS_ICON = {
    "PASS": "✅ PASS",
    "FAIL": "❌ FAIL",
    "MANUAL": "🟡 MANUAL",
    "MANUAL_CONFIRM": "🟡 NEEDS CONFIRM",
}


def read_results(results_dir: Path):
    csv_path = results_dir / "results.csv"
    if not csv_path.exists():
        sys.exit(f"Error: {csv_path} not found")
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        for row in reader:
            if not row:
                continue
            rows.append(dict(zip(header, row)))
    return rows


def load_log_excerpt(log_path_str: str, max_lines: int = 25) -> str:
    if not log_path_str:
        return ""
    p = Path(log_path_str)
    if not p.exists():
        return "(log file not found)"
    lines = p.read_text(errors="replace").splitlines()
    if len(lines) > max_lines:
        head = lines[:max_lines]
        return "\n".join(head) + f"\n... ({len(lines) - max_lines} more lines truncated)"
    return "\n".join(lines)


def build_markdown(rows, results_dir: Path) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(rows)
    counts = {"PASS": 0, "FAIL": 0, "MANUAL": 0, "MANUAL_CONFIRM": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    lines = []
    lines.append("# IVI Sanity Test Report")
    lines.append("")
    lines.append(f"**Generated:** {now}  ")
    lines.append(f"**Results source:** `{results_dir}`  ")
    lines.append(f"**Total test steps:** {total}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|---|---|")
    for key, label in [("PASS", "Pass"), ("FAIL", "Fail"),
                        ("MANUAL", "Manual (not adb-verifiable)"),
                        ("MANUAL_CONFIRM", "Needs tester confirmation")]:
        lines.append(f"| {label} | {counts.get(key, 0)} |")
    lines.append("")

    lines.append("## Results Table")
    lines.append("")
    lines.append("| Test ID | Description | Status | Detail |")
    lines.append("|---|---|---|---|")
    for r in rows:
        icon = STATUS_ICON.get(r["status"], r["status"])
        desc = r["description"].replace("|", "\\|")
        detail = r["detail"].replace("|", "\\|")
        lines.append(f"| {r['test_id']} | {desc} | {icon} | {detail} |")
    lines.append("")

    lines.append("## Detailed Logs")
    lines.append("")
    for r in rows:
        lines.append(f"### {r['test_id']} - {r['description']}")
        lines.append(f"**Status:** {STATUS_ICON.get(r['status'], r['status'])}  ")
        lines.append(f"**Detail:** {r['detail']}")
        lines.append("")
        excerpt = load_log_excerpt(r.get("logfile", ""))
        if excerpt:
            lines.append("```")
            lines.append(excerpt)
            lines.append("```")
        lines.append("")

    fail_count = counts.get("FAIL", 0)
    manual_count = counts.get("MANUAL", 0) + counts.get("MANUAL_CONFIRM", 0)
    lines.append("## Overall Verdict")
    lines.append("")
    if fail_count == 0 and manual_count == 0:
        lines.append("**All automated checks passed. No manual confirmation items remain.**")
    elif fail_count == 0:
        lines.append(
            f"**No automated failures.** {manual_count} item(s) require tester "
            "confirmation before sign-off (see MANUAL/NEEDS CONFIRM rows above)."
        )
    else:
        lines.append(
            f"**{fail_count} automated failure(s) detected.** Review the Detailed "
            "Logs section above before proceeding."
        )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir", help="Directory produced by adb_sanity_test.sh")
    parser.add_argument("--docx", action="store_true", help="Also produce a .docx via pandoc")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    rows = read_results(results_dir)
    md = build_markdown(rows, results_dir)

    out_md = results_dir / "sanity_report.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"Markdown report written to: {out_md}")

    if args.docx:
        if shutil.which("pandoc") is None:
            print("pandoc not found on this system - skipping .docx conversion.")
            print("Install pandoc, then run:")
            print(f"  pandoc \"{out_md}\" -o \"{results_dir / 'sanity_report.docx'}\"")
        else:
            out_docx = results_dir / "sanity_report.docx"
            subprocess.run(["pandoc", str(out_md), "-o", str(out_docx)], check=True)
            print(f"Word document written to: {out_docx}")


if __name__ == "__main__":
    main()
