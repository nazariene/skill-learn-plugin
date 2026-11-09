import argparse
import json
import sys
import uuid

from .core import Core
from .errors import SkillServiceError
from .settings import DEFAULT_HOME


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skill-learn")
    parser.add_argument("--home", default=DEFAULT_HOME)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("pending", "child", "status"):
        sub.add_parser(command)
    report = sub.add_parser("report")
    period = report.add_mutually_exclusive_group()
    period.add_argument("--days", type=int, default=argparse.SUPPRESS, help="Report the past N days (default: 7)")
    period.add_argument("--all", dest="days", action="store_const", const=None, default=argparse.SUPPRESS, help="Report all history")
    for command in ("show", "approve", "reject", "adopt", "pin", "unpin"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("id")
        if command in {"adopt", "pin", "unpin"}:
            command_parser.add_argument("--project", dest="projectDirectory", help="Project containing .opencode/skills")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("reviewID")
    reconcile.add_argument("--state", choices=("abandoned", "missing", "ambiguous", "active", "finished"), required=True)
    reconcile.add_argument("--result", help="File with the finished native review's final text")
    delete = sub.add_parser("delete-session")
    delete.add_argument("harness")
    delete.add_argument("sessionID")
    probe = sub.add_parser("cache-probe")
    probe.add_argument("parentID")
    probe.add_argument("--mode", choices=("fork", "digest"), default="fork")
    args = parser.parse_args(argv)
    if args.command == "child":
        from .protocol import run
        run(args.home)
        return 0
    core = None
    try:
        if args.command == "status":
            from .diagnostics import status
            print(json.dumps(status(args.home), ensure_ascii=False, indent=2, default=str))
            return 0
        core = Core(args.home)
        payload = {key: value for key, value in vars(args).items() if key not in {"command", "home", "result"}}
        if payload.get("projectDirectory"):
            from pathlib import Path
            payload["projectDirectory"] = str(Path(payload["projectDirectory"]).expanduser().resolve())
        if args.command == "reconcile" and args.state == "finished":
            if not args.result:
                raise SkillServiceError("Finished reconciliation requires --result")
            from pathlib import Path
            payload["text"] = Path(args.result).read_text(encoding="utf-8")
        operation = "request-probe" if args.command == "cache-probe" else args.command
        response = core.request("cli_" + uuid.uuid4().hex, operation, payload)
        if not response["ok"]:
            raise SkillServiceError(response["error"]["message"])
        answer = response["result"]
        print(json.dumps(answer, ensure_ascii=False, indent=2))
        return 1 if isinstance(answer, dict) and answer.get("ok") is False else 0
    except (SkillServiceError, OSError, ValueError) as error:
        print(f"skill-learn: {error}", file=sys.stderr)
        return 1
    finally:
        if core:
            core.close()
