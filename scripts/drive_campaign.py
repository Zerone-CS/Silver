#!/usr/bin/env python3
"""Drive a campaign with a site worker. Each invocation receives status JSON on stdin."""
import argparse
from contextlib import ExitStack
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

CONTROL = Path(__file__).resolve().with_name("campaign.py")
EXPERIENCE = CONTROL.with_name("experience.py")


def status(directory):
    output = subprocess.check_output([sys.executable, "-B", str(CONTROL), "status", "--campaign",
                                      str(directory)], text=True)
    return json.loads(output)


def archive_rounds(directory, database, logdir):
    attempt = Path(tempfile.mkdtemp(prefix="archive-", dir=str(logdir)))
    with (attempt / "stdout.json").open("xb") as out, (attempt / "stderr").open("xb") as err:
        result = subprocess.run(
            [sys.executable, "-B", str(EXPERIENCE), "sync", "--campaign", str(directory), "--db", str(database)],
            stdout=out, stderr=err, check=False,
        )
    return {"directory": str(attempt), "exit_code": result.returncode}


def require_released(state):
    outstanding = state.get("device_budget", {}).get("pending", [])
    if outstanding:
        raise ValueError("reconcile and release outstanding device claims on the site: " + ", ".join(outstanding))


def drive(directory, lock, worker, timeout, resume_after=None, experience_db=None):
    directory = directory.resolve(strict=True)
    logdir = directory / "driver"
    logdir.mkdir(exist_ok=True)
    pending = logdir / "pending.json"
    archived_through, archive_result = 0, None
    with ExitStack() as stack:
        descriptors = []
        for path in dict.fromkeys([logdir / "campaign.lock", lock.resolve()]):
            lease = stack.enter_context(path.open("a"))
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("lock is held by another driver: " + str(path)) from None
            descriptors.append(lease.fileno())
        if pending.exists():
            previous = json.loads(pending.read_text(encoding="utf-8"))["attempt"]
            if resume_after != previous:
                raise ValueError("inspect the unresolved worker action and actual deployment: " +
                                 str(logdir / previous) + "; then use --resume-after " + previous)
            recovered = status(directory)
            require_released(recovered)
            receipt = logdir / previous / "reconciled.json"
            if not receipt.exists():
                temporary = receipt.with_suffix(".tmp")
                temporary.write_text(json.dumps({"at": time.time(), "deployment": recovered["deployment_last_verified"],
                                                 "device_budget": recovered.get("device_budget")}), encoding="utf-8")
                temporary.replace(receipt)
            pending.unlink()
        elif resume_after:
            raise ValueError("no pending attempt matches --resume-after")
        while True:
            before = status(directory)
            require_released(before)
            if experience_db is not None and before["round"] - 1 > archived_through:
                archive_result = archive_rounds(directory, experience_db, logdir)
                archived_through = before["round"] - 1
            action = before["next_action"]
            if action == "stopped":
                if archive_result is not None:
                    before["experience_archive"] = archive_result
                return before
            if action == "stop":
                command = [sys.executable, "-B", str(CONTROL), "stop", "--campaign", str(directory)]
                if before["stop_reason"]:
                    command.extend(("--reason", before["stop_reason"]))
                subprocess.run(command, stdout=subprocess.DEVNULL, check=True)
                continue
            attempt = Path(tempfile.mkdtemp(prefix="action-", dir=str(logdir)))
            action_id = f'{before["campaign_id"]}/{attempt.name}'
            request = dict(before, campaign=str(directory), action_id=action_id)
            request_file = attempt / "request.json"
            request_file.write_text(json.dumps(request, ensure_ascii=False) + "\n", encoding="utf-8")
            limit = timeout
            if before["deadline"] and action != "close_at_baseline":
                limit = min(limit, max(0.1, before["deadline"] - time.time()))
            marker = logdir / "pending.tmp"
            marker.write_text(json.dumps({"attempt": attempt.name, "action_id": action_id}), encoding="utf-8")
            marker.replace(pending)
            with request_file.open("rb") as src, (attempt / "stdout").open("xb") as out, (attempt / "stderr").open("xb") as err:
                with subprocess.Popen(worker, stdin=src, stdout=out, stderr=err, start_new_session=True,
                                          pass_fds=descriptors) as proc:
                    try:
                        code = proc.wait(timeout=limit)
                    except BaseException:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        code = proc.wait()
                        (attempt / "exit.json").write_text(json.dumps({"exit_code": code, "interrupted": True}))
                        print("interrupted worker; inspect local and remote state: " + str(attempt), file=sys.stderr)
                        raise
            (attempt / "exit.json").write_text(json.dumps({"exit_code": code}) + "\n", encoding="utf-8")
            if code:
                raise ValueError("worker stopped; inspect its receipts before resuming: " + str(attempt))
            after = status(directory)
            require_released(after)
            if {k: value for k, value in after.items() if k != "device_budget"} == {
                    k: value for k, value in before.items() if k != "device_budget"}:
                raise ValueError("worker returned without a state transition: " + str(attempt))
            pending.unlink()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign", required=True, type=Path)
    p.add_argument("--resource-lock", required=True, type=Path,
                   help="shared local lock path for all drivers using these devices")
    p.add_argument("--resume-after", help="reconcile the named pending attempt after verifying the live service")
    p.add_argument("--worker-timeout", default=3600, type=float, help="seconds for one worker invocation")
    p.add_argument("--experience-db", type=Path, help="archive verified closed rounds to this private database")
    p.add_argument("worker", nargs=argparse.REMAINDER, help="-- PROGRAM [ARGS...]")
    a = p.parse_args()
    argv = a.worker[1:] if a.worker[:1] == ["--"] else a.worker
    if not argv or not math.isfinite(a.worker_timeout) or a.worker_timeout <= 0:
        p.error("supply a worker argv and positive finite timeout")
    try:
        print(json.dumps(drive(a.campaign, a.resource_lock, argv, a.worker_timeout, a.resume_after,
                               experience_db=a.experience_db), ensure_ascii=False, indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
