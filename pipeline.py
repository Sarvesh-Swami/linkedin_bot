#!/usr/bin/env python3
"""
pipeline.py
============

Flask web app that orchestrates the LinkedIn scraping pipeline:

    main.py  →  extract_company_details.py  →  json_parser.py  →  people_profile_scraper.py

Runs each step as a subprocess, streams stdout/stderr to the browser
via Server-Sent Events (SSE), and exposes a "send input" endpoint so
the user can resume manual-login pauses from the UI.

Run:
    python pipeline.py
    # then open http://localhost:5050
"""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

# ---------------------------------------------------------------------------
# Pipeline definition
# ---------------------------------------------------------------------------

STEPS = [
    {
        "id": "job_scraper",
        "name": "Job Scraper",
        "description": "Search LinkedIn jobs and extract company names",
        "script": "main.py",
        "output_file": "companies.txt",
        "needs_login": True,
    },
    {
        "id": "company_details",
        "name": "Company Details Extractor",
        "description": "Visit each company's About page and extract details",
        "script": "extract_company_details.py",
        "output_file": "results.json",
        "needs_login": True,
    },
    {
        "id": "json_parser",
        "name": "JSON Parser",
        "description": "Clean raw scrape data into structured records",
        "script": "json_parser.py",
        "output_file": "clean_data.json",
        "needs_login": False,
    },
    {
        "id": "people_scraper",
        "name": "People Profile Scraper",
        "description": "Scrape profile URLs from each company's People page",
        "script": "people_profile_scraper.py",
        "output_file": "people.json",
        "needs_login": True,
    },
]


# ---------------------------------------------------------------------------
# Pipeline state  (single-run, single-user — fine for a local tool)
# ---------------------------------------------------------------------------

class PipelineState:
    """Mutable singleton holding the entire pipeline's runtime state."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.status = "idle"            # idle | running | paused | completed | failed | stopped
        self.current_step = -1
        self.started_at = None
        self.finished_at = None
        self.config = {}
        self.steps = [
            {
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "script": s["script"],
                "output_file": s["output_file"],
                "needs_login": s["needs_login"],
                "status": "pending",    # pending | running | paused | completed | failed | skipped
                "started_at": None,
                "finished_at": None,
                "log_lines": [],
            }
            for s in STEPS
        ]
        self.process: subprocess.Popen | None = None
        self.log_queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()

    def to_dict(self):
        with self._lock:
            return {
                "status": self.status,
                "current_step": self.current_step,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "config": self.config,
                "steps": [
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "description": s["description"],
                        "output_file": s["output_file"],
                        "needs_login": s["needs_login"],
                        "status": s["status"],
                        "started_at": s["started_at"],
                        "finished_at": s["finished_at"],
                        "log_count": len(s["log_lines"]),
                    }
                    for s in self.steps
                ],
            }


STATE = PipelineState()


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _build_command(step_index: int, config: dict) -> list[str]:
    """Build the CLI command for a given pipeline step."""
    python = sys.executable
    step = STEPS[step_index]
    script = str(BASE_DIR / step["script"])
    session_file = str(BASE_DIR / "session.json")

    if step_index == 0:
        # main.py
        cmd = [python, script]
        keywords = config.get("keywords", "").strip()
        url = config.get("url", "").strip()
        if url:
            cmd += ["--url", url]
        elif keywords:
            cmd += ["--keywords", keywords]
        max_pages = config.get("max_pages")
        if max_pages:
            cmd += ["--max-pages", str(max_pages)]
        max_scroll = config.get("max_scroll_rounds")
        if max_scroll:
            cmd += ["--max-scroll-rounds", str(max_scroll)]
        cmd += ["--output-file", str(BASE_DIR / "companies.txt")]
        cmd += ["--session-file", session_file]
        return cmd

    elif step_index == 1:
        # extract_company_details.py  <companies_file>
        cmd = [python, script, str(BASE_DIR / "companies.txt")]
        cmd += ["--output", str(BASE_DIR / "results.json")]
        cmd += ["--session-file", session_file]
        return cmd

    elif step_index == 2:
        # json_parser.py  <input> <output> <failed>
        cmd = [
            python, script,
            str(BASE_DIR / "results.json"),
            str(BASE_DIR / "clean_data.json"),
            str(BASE_DIR / "results_failed.json"),
        ]
        return cmd

    elif step_index == 3:
        # people_profile_scraper.py  <input_file>
        cmd = [
            python, script,
            str(BASE_DIR / "clean_data.json"),
            "--output-file", str(BASE_DIR / "people.json"),
            "--session-file", session_file,
        ]
        return cmd

    return [python, script]



def _emit(event_type: str, data: dict):
    """Push an SSE message onto the queue."""
    STATE.log_queue.put({"event": event_type, "data": data})


def _read_stream(stream, step_index: int, stream_name: str):
    """Read a subprocess stream line-by-line and emit SSE events."""
    try:
        for raw_line in iter(stream.readline, ""):
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue

            timestamp = datetime.now().strftime("%H:%M:%S")
            entry = {"time": timestamp, "text": line, "stream": stream_name}

            with STATE._lock:
                STATE.steps[step_index]["log_lines"].append(entry)

            _emit("log", {
                "step": step_index,
                "step_id": STEPS[step_index]["id"],
                "time": timestamp,
                "text": line,
                "stream": stream_name,
            })

            # Detect pause prompts
            if "[PAUSED]" in line or "Press Enter" in line:
                with STATE._lock:
                    STATE.status = "paused"
                    STATE.steps[step_index]["status"] = "paused"
                _emit("step_update", {
                    "step": step_index,
                    "status": "paused",
                    "message": line,
                })
    except (ValueError, OSError):
        pass  # stream closed


def _run_step(step_index: int, config: dict) -> bool:
    """Run a single pipeline step. Returns True on success, False on failure."""
    step = STATE.steps[step_index]

    with STATE._lock:
        STATE.current_step = step_index
        STATE.status = "running"
        step["status"] = "running"
        step["started_at"] = datetime.now().isoformat(timespec="seconds")

    _emit("step_update", {"step": step_index, "status": "running"})

    cmd = _build_command(step_index, config)
    _emit("log", {
        "step": step_index,
        "step_id": STEPS[step_index]["id"],
        "time": datetime.now().strftime("%H:%M:%S"),
        "text": f"$ {' '.join(cmd)}",
        "stream": "system",
    })

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            cwd=str(BASE_DIR),
            bufsize=1,                 # line-buffered
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
    except Exception as e:
        with STATE._lock:
            step["status"] = "failed"
            step["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _emit("step_update", {"step": step_index, "status": "failed", "error": str(e)})
        return False

    with STATE._lock:
        STATE.process = proc

    # Read stdout and stderr in separate threads
    t_out = threading.Thread(target=_read_stream, args=(proc.stdout, step_index, "stdout"), daemon=True)
    t_err = threading.Thread(target=_read_stream, args=(proc.stderr, step_index, "stderr"), daemon=True)
    t_out.start()
    t_err.start()

    proc.wait()
    t_out.join(timeout=3)
    t_err.join(timeout=3)

    with STATE._lock:
        STATE.process = None

    exit_code = proc.returncode
    success = exit_code == 0

    with STATE._lock:
        step["status"] = "completed" if success else "failed"
        step["finished_at"] = datetime.now().isoformat(timespec="seconds")

    _emit("step_update", {
        "step": step_index,
        "status": step["status"],
        "exit_code": exit_code,
    })

    return success


def _run_pipeline(config: dict):
    """Run all pipeline steps in sequence."""
    with STATE._lock:
        STATE.status = "running"
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.config = config

    _emit("pipeline_update", {"status": "running"})

    for i in range(len(STEPS)):
        # Check if pipeline was stopped
        with STATE._lock:
            if STATE.status == "stopped":
                for j in range(i, len(STEPS)):
                    STATE.steps[j]["status"] = "skipped"
                _emit("pipeline_update", {"status": "stopped"})
                return

        success = _run_step(i, config)

        if not success:
            with STATE._lock:
                if STATE.status == "stopped":
                    for j in range(i + 1, len(STEPS)):
                        STATE.steps[j]["status"] = "skipped"
                    _emit("pipeline_update", {"status": "stopped"})
                    return
                STATE.status = "failed"
                STATE.finished_at = datetime.now().isoformat(timespec="seconds")
                # Mark remaining steps as skipped
                for j in range(i + 1, len(STEPS)):
                    STATE.steps[j]["status"] = "skipped"
            _emit("pipeline_update", {"status": "failed", "failed_step": i})
            return

    with STATE._lock:
        STATE.status = "completed"
        STATE.finished_at = datetime.now().isoformat(timespec="seconds")

    _emit("pipeline_update", {"status": "completed"})


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    return jsonify(STATE.to_dict())


@app.route("/api/start", methods=["POST"])
def api_start():
    with STATE._lock:
        if STATE.status == "running" or STATE.status == "paused":
            return jsonify({"error": "Pipeline is already running"}), 409

    config = request.json or {}
    STATE.reset()

    thread = threading.Thread(target=_run_pipeline, args=(config,), daemon=True)
    thread.start()

    return jsonify({"ok": True, "message": "Pipeline started"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with STATE._lock:
        STATE.status = "stopped"
        proc = STATE.process

    if proc and proc.poll() is None:
        try:
            if sys.platform == "win32":
                proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    _emit("pipeline_update", {"status": "stopped"})
    return jsonify({"ok": True, "message": "Pipeline stop requested"})


@app.route("/api/send-input", methods=["POST"])
def api_send_input():
    """Send text to the running subprocess's stdin (for login pauses)."""
    text = (request.json or {}).get("text", "\n")
    with STATE._lock:
        proc = STATE.process
        current = STATE.current_step

    if not proc or proc.poll() is not None:
        return jsonify({"error": "No running process to send input to"}), 400

    try:
        proc.stdin.write(text if text.endswith("\n") else text + "\n")
        proc.stdin.flush()
    except Exception as e:
        return jsonify({"error": f"Failed to send input: {e}"}), 500

    # Resume status
    with STATE._lock:
        if STATE.status == "paused":
            STATE.status = "running"
        if 0 <= current < len(STATE.steps) and STATE.steps[current]["status"] == "paused":
            STATE.steps[current]["status"] = "running"

    _emit("step_update", {"step": current, "status": "running"})
    _emit("pipeline_update", {"status": "running"})

    return jsonify({"ok": True})


@app.route("/api/stream")
def api_stream():
    """SSE endpoint — streams real-time events to the browser."""
    def generate():
        # Send initial state
        yield f"event: init\ndata: {json.dumps(STATE.to_dict())}\n\n"

        while True:
            try:
                msg = STATE.log_queue.get(timeout=30)
                event = msg["event"]
                data = json.dumps(msg["data"])
                yield f"event: {event}\ndata: {data}\n\n"
            except queue.Empty:
                # Send keepalive
                yield ": keepalive\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/results")
def api_results():
    """Return a summary of results files."""
    summary = {}

    companies_path = BASE_DIR / "companies.txt"
    if companies_path.exists():
        lines = [l.strip() for l in companies_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        summary["companies"] = {"count": len(lines), "sample": lines[:10]}

    clean_path = BASE_DIR / "clean_data.json"
    if clean_path.exists():
        try:
            data = json.loads(clean_path.read_text(encoding="utf-8"))
            summary["clean_data"] = {"count": len(data)}
        except Exception:
            pass

    failed_path = BASE_DIR / "results_failed.json"
    if failed_path.exists():
        try:
            data = json.loads(failed_path.read_text(encoding="utf-8"))
            summary["failed"] = {"count": len(data)}
        except Exception:
            pass

    people_path = BASE_DIR / "people.json"
    if people_path.exists():
        try:
            data = json.loads(people_path.read_text(encoding="utf-8"))
            total_profiles = sum(len(c.get("profiles", [])) for c in data)
            summary["people"] = {"companies": len(data), "total_profiles": total_profiles}
        except Exception:
            pass

    return jsonify(summary)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  LinkedIn Bot Pipeline UI")
    print("  Open http://localhost:5050 in your browser")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
