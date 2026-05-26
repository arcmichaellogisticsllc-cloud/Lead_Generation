"""
Install the daily pipeline as a macOS launchd job.

Runs at 7:00 AM on weekdays (Mon–Fri).
Safe to re-run — will unload the old job first if it already exists.

Usage:
    python scripts/install_scheduler.py          # install
    python scripts/install_scheduler.py --remove # uninstall
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT    = Path(__file__).parent.parent.resolve()
VENV_PY    = PROJECT / ".venv" / "bin" / "python"

JOBS = [
    {
        "label":    "com.ga-payment-leads.daily",
        "script":   PROJECT / "scripts" / "daily_pipeline.py",
        "args":     [],
        "log_out":  PROJECT / "data" / "launchd_stdout.log",
        "log_err":  PROJECT / "data" / "launchd_stderr.log",
        "schedule": "Mon–Fri 7:00 AM (full scan)",
        "hour": 7, "minute": 0,
    },
    {
        "label":    "com.ga-payment-leads.quick",
        "script":   PROJECT / "scripts" / "daily_pipeline.py",
        "args":     ["--mode", "quick"],
        "log_out":  PROJECT / "data" / "quick_stdout.log",
        "log_err":  PROJECT / "data" / "quick_stderr.log",
        "schedule": "Mon–Fri 1:00 PM (quick scan)",
        "hour": 13, "minute": 0,
    },
    {
        "label":    "com.ga-payment-leads.digest",
        "script":   PROJECT / "scripts" / "morning_digest.py",
        "args":     [],
        "log_out":  PROJECT / "data" / "digest_stdout.log",
        "log_err":  PROJECT / "data" / "digest_stderr.log",
        "schedule": "Mon–Fri 7:30 AM (digest)",
        "hour": 7, "minute": 30,
    },
    {
        "label":    "com.ga-payment-leads.calibrate",
        "script":   PROJECT / "scripts" / "scoring_feedback.py",
        "args":     ["--apply", "--days", "90"],
        "log_out":  PROJECT / "data" / "calibrate_stdout.log",
        "log_err":  PROJECT / "data" / "calibrate_stderr.log",
        "schedule": "Mon only 8:00 AM (weekly calibration)",
        "hour": 8, "minute": 0,
        "weekdays": [1],   # Monday only
    },
]

# Legacy single-job references (kept for --remove backward compat)
LABEL      = "com.ga-payment-leads.daily"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
        {extra_args_xml}
    </array>

    <key>WorkingDirectory</key>
    <string>{project}</string>

    <key>StartCalendarInterval</key>
    <array>
        {days_xml}
    </array>

    <key>StandardOutPath</key>
    <string>{log_out}</string>

    <key>StandardErrorPath</key>
    <string>{log_err}</string>

    <!-- Keep env vars launchd strips -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{home}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
        <key>DISCOVER_SCAN</key>
        <string>300</string>
    </dict>

    <!-- Don't auto-restart on crash -->
    <key>KeepAlive</key>
    <false/>

    <!-- Only run if logged in (needs browser) -->
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
</dict>
</plist>
"""


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _already_loaded(label: str) -> bool:
    r = _run(["launchctl", "list", label], check=False)
    return r.returncode == 0


def _make_plist(job: dict) -> str:
    weekdays = job.get("weekdays", list(range(1, 6)))  # Mon–Fri by default
    days_xml = "\n        ".join(
        f'<dict><key>Weekday</key><integer>{d}</integer>'
        f'<key>Hour</key><integer>{job["hour"]}</integer>'
        f'<key>Minute</key><integer>{job["minute"]}</integer></dict>'
        for d in weekdays
    )
    extra_args_xml = "\n        ".join(
        f"<string>{arg}</string>" for arg in job.get("args", [])
    )
    return PLIST_TEMPLATE.format(
        label=job["label"],
        python=str(VENV_PY),
        script=str(job["script"]),
        extra_args_xml=extra_args_xml,
        project=str(PROJECT),
        log_out=str(job["log_out"]),
        log_err=str(job["log_err"]),
        home=str(Path.home()),
        days_xml=days_xml,
    )


def install() -> None:
    print("GA Payment Leads — Scheduler Setup")
    print("=" * 45)
    for job in JOBS:
        print(f"  [{job['schedule']}]  {job['script'].name}")
    print()

    if not VENV_PY.exists():
        print(f"ERROR: {VENV_PY} not found. Activate your venv first.")
        sys.exit(1)
    for job in JOBS:
        if not job["script"].exists():
            print(f"ERROR: {job['script']} not found.")
            sys.exit(1)

    answer = input("Install both launchd jobs? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (PROJECT / "data").mkdir(parents=True, exist_ok=True)

    for job in JOBS:
        plist = _plist_path(job["label"])
        if _already_loaded(job["label"]):
            print(f"  Unloading existing: {job['label']}")
            _run(["launchctl", "unload", str(plist)], check=False)
        plist.write_text(_make_plist(job))
        result = _run(["launchctl", "load", str(plist)], check=False)
        if result.returncode != 0:
            print(f"  ERROR loading {job['label']}: {result.stderr.strip()}")
        else:
            print(f"  Loaded: {job['label']}  ({job['schedule']})")

    print()
    print("Verify:      launchctl list | grep ga-payment")
    print("Run full:    launchctl start com.ga-payment-leads.daily")
    print("Run quick:   launchctl start com.ga-payment-leads.quick")
    print("Calibrate:   launchctl start com.ga-payment-leads.calibrate")
    print("Log:         tail -f data/pipeline.log")


def remove() -> None:
    answer = input("Remove all GA Leads launchd jobs? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    for job in JOBS:
        label = job["label"]
        plist = _plist_path(label)
        if _already_loaded(label):
            _run(["launchctl", "unload", str(plist)], check=False)
            print(f"  Unloaded: {label}")
        if plist.exists():
            plist.unlink()
            print(f"  Removed:  {plist}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--remove", action="store_true", help="Uninstall the job")
    args = parser.parse_args()

    if args.remove:
        remove()
    else:
        install()
