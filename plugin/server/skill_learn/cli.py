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
    for command in ("pending", "report", "child"):
        sub.add_parser(command)
    for command in ("show", "approve", "reject", "adopt", "pin", "unpin"):
        sub.add_parser(command).add_argument("id")
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
        core = Core(args.home)
        payload = {key: value for key, value in vars(args).items() if key not in {"command", "home", "result"}}
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
