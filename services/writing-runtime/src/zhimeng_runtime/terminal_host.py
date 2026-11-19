"""ConPTY worker inside the runtime's suspended-then-assigned Windows Job.

Only framed UTF-8 JSON crosses the parent pipes. Terminal text is masked and
persisted by the parent, using the same journal as ordinary command sessions.
The worker's descendants inherit the Job before any user command can start.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time


def main() -> int:
    from winpty import Backend, PtyProcess

    output_lock = threading.Lock()
    def send(value):
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        with output_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def requests():
        buffer = b""
        while chunk := os.read(0, 65536):
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield json.loads(line)

    incoming = requests()
    initial = next(incoming)
    process = None
    try:
        process = PtyProcess.spawn([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", initial["script"]],
                                   cwd=os.getcwd(), dimensions=(initial["rows"], initial["columns"]), backend=Backend.ConPTY)
        send({"resize": [initial["rows"], initial["columns"]]})
        send({"ack": initial["id"]})

        def control():
            try:
                for request in incoming:
                    try:
                        if "input" in request:
                            process.write(request["input"])
                        elif "rows" in request:
                            # Record geometry before native output from the new
                            # size. Reattaching must replay the same dimensions.
                            send({"resize": [request["rows"], request["columns"]]})
                            process.setwinsize(request["rows"], request["columns"])
                        send({"ack": request["id"]})
                    except Exception as exc:
                        send({"ack": request["id"], "error": str(exc)})
                # Parent input disappears on shutdown; this worker never adopts
                # commands from another runtime or leaves them running detached.
                process.terminate(force=True)
            except (OSError, ValueError, KeyError):
                process.terminate(force=True)

        thread = threading.Thread(target=control, daemon=True)
        thread.start()
        while True:
            try:
                text = process.read(8192)
            except EOFError:
                break
            if text:
                send({"output": text})
        while process.isalive():
            time.sleep(0.025)
        return process.exitstatus
    except Exception as exc:
        send({"ack": initial["id"], "error": str(exc)})
        return 1
    finally:
        if process is not None:
            process.close(force=True)


if __name__ == "__main__":
    raise SystemExit(main())
