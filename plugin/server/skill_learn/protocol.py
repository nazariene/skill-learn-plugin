import contextlib
import json
import sys

from .core import Core

VERSION = 1


def run(home, incoming=None, outgoing=None):
    incoming, outgoing = incoming or sys.stdin, outgoing or sys.stdout
    core = None
    startup_error = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            core = Core(home)
    except Exception as error:
        startup_error = str(error)
    try:
        for line in incoming:
            identity, operation = None, None
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Protocol frame must be an object")
                identity, operation = request.get("id"), request.get("op")
                if request.get("version") != VERSION or not isinstance(identity, str) or not identity or not isinstance(operation, str) or not isinstance(request.get("payload", {}), dict):
                    raise ValueError("Unsupported version or invalid request identity/operation/payload")
                if startup_error:
                    raise ValueError("Learning configuration/startup failed: " + startup_error)
                with contextlib.redirect_stdout(sys.stderr):
                    response = core.request(identity, operation, request.get("payload", {}))
            except Exception as error:
                response = {"ok": False, "error": {"code": "protocol_error" if not core else "request_error", "message": str(error)}}
            outgoing.write(json.dumps({"version": VERSION, "id": identity, "op": operation, **response}, ensure_ascii=False) + "\n")
            outgoing.flush()
    finally:
        if core:
            core.close()
