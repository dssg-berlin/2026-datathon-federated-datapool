"""Live test dashboard for pytest (standard library only).

Starts a tiny local web server when pytest starts, opens the page, and streams
test progress, expected results and actual results to it. When the run ends a
standalone snapshot is written to reports/dashboard.html.

Disable with --no-dashboard (or env NO_DASHBOARD=1; automatically off when CI is set).
Expected results come from the table rows in TESTS.md and, for evals, the case in
cases.json. Actual results come from the assertions pytest evaluates (needs
enable_assertion_pass_hook in pytest.ini) and from structured eval records.
"""

import ast
import json
import os
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MAX_ASSERTIONS = 25
ANSI = re.compile(r"\x1b\[[0-9;]*m")

LAYERS = {
    "unit": "1 · Unit",
    "protocol": "2 · MCP protocol",
    "api": "3 · SPARQL API",
    "llm": "4 · Connection",
    "evals": "5 · LLM tool-calling evals",
}

STATE = None  # set in pytest_configure when the dashboard is enabled


# ---- helpers ---------------------------------------------------------------

def note(text: str) -> None:
    """Show a progress note (e.g. 'run 2/3') on the currently running test."""
    if STATE is not None:
        STATE.set_note(text)


def _humanize(name: str) -> str:
    name = re.sub(r"^test_", "", name).replace("_", " ")
    return name[:1].upper() + name[1:]


def _expected_from_testmd() -> dict:
    """Map test function name -> description, from the tables in TESTS.md."""
    path = ROOT / "TESTS.md"
    result = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        match = re.match(r"^\|\s*`(test_\w+)`[^|]*\|\s*(.+?)\s*\|\s*$", line)
        if match:
            result[match.group(1)] = re.sub(r"[`*]", "", match.group(2))
    return result


def _split_comparison(orig: str, expl: str):
    """For `left == right` assertions return (left_src, right_src, left_val, right_val)."""
    try:
        node = ast.parse(orig.strip(), mode="eval").body
    except SyntaxError:
        return None
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)):
        return None
    first_line = expl.split("\n", 1)[0]
    parts = first_line.split(" == ")
    if len(parts) != 2:
        return None
    return (
        ast.get_source_segment(orig.strip(), node.left),
        ast.get_source_segment(orig.strip(), node.comparators[0]),
        parts[0].strip().lstrip("("),
        parts[1].strip().rstrip(")"),
    )


def _clip(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


# ---- shared state ----------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.tests: dict[str, dict] = {}
        self.order: list[str] = []
        self.status = "collecting"
        self.exitstatus = None
        self.started = time.time()
        self.finished_at = None
        self.current = None
        self.rev = 0
        self.last_poll = 0.0
        self.final_served = threading.Event()

    def add(self, nodeid: str, data: dict) -> None:
        with self.lock:
            self.tests[nodeid] = data
            self.order.append(nodeid)
            self.rev += 1

    def update(self, nodeid: str, **fields) -> None:
        with self.lock:
            if nodeid in self.tests:
                self.tests[nodeid].update(fields)
                self.rev += 1

    def add_assertion(self, nodeid: str, entry: dict) -> None:
        with self.lock:
            test = self.tests.get(nodeid)
            if test is None:
                return
            if len(test["assertions"]) < MAX_ASSERTIONS:
                test["assertions"].append(entry)
            else:
                test["assertions_more"] += 1
            self.rev += 1

    def set_note(self, text: str) -> None:
        with self.lock:
            if self.current in self.tests:
                self.tests[self.current]["note"] = text
                self.rev += 1

    def set_status(self, status: str, exitstatus=None) -> None:
        with self.lock:
            self.status = status
            if status == "finished":
                self.finished_at = time.time()
                self.exitstatus = exitstatus
            self.rev += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "rev": self.rev,
                "status": self.status,
                "exitstatus": self.exitstatus,
                "started": self.started,
                "finished_at": self.finished_at,
                "now": time.time(),
                "current": self.current,
                "tests": [self.tests[n] for n in self.order],
            }


# ---- HTTP server -----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_text().replace("<!--STATE-->", "")
            self._send(html.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/state"):
            STATE.last_poll = time.time()
            snap = STATE.snapshot()
            if snap["status"] == "finished":
                STATE.final_served.set()
            self._send(json.dumps(snap, default=str).encode(), "application/json")
        else:
            self._send(b"not found", "text/plain", 404)


def _start_server(port: int):
    for candidate in (port, 0):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---- pytest hooks ----------------------------------------------------------

def pytest_addoption(parser):
    group = parser.getgroup("dashboard")
    group.addoption("--no-dashboard", action="store_true", help="Do not start the live test dashboard.")
    group.addoption("--dashboard-no-open", action="store_true", help="Start the dashboard but do not open a browser.")
    group.addoption("--dashboard-hold", action="store_true", help="Keep the dashboard server alive after the run (Ctrl+C to quit).")
    group.addoption("--dashboard-port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8765")))


def _enabled(config) -> bool:
    return not (
        config.getoption("--no-dashboard")
        or os.environ.get("NO_DASHBOARD")
        or os.environ.get("CI")
        or config.getoption("--collect-only")
        or hasattr(config, "workerinput")
    )


def pytest_configure(config):
    global STATE
    config._dashboard = None
    if not _enabled(config):
        return
    STATE = State()
    server = _start_server(config.getoption("--dashboard-port"))
    config._dashboard = {"server": server, "url": f"http://127.0.0.1:{server.server_address[1]}"}


def pytest_report_header(config):
    if config._dashboard:
        return f"dashboard: {config._dashboard['url']}"


def pytest_collection_finish(session):
    config = session.config
    if STATE is None:
        return
    expected = _expected_from_testmd()
    for item in session.items:
        base = getattr(item, "originalname", None) or re.sub(r"\[.*\]$", "", item.name)
        path = Path(str(item.path)).resolve()
        rel = path.relative_to(ROOT)
        layer_key = rel.parts[1] if len(rel.parts) > 2 else "other"
        func = getattr(item, "function", None)
        doc = (func.__doc__ or "").strip().split("\n")[0] if func else ""
        param = item.name[len(base):] if item.name.startswith(base) else ""
        case = (getattr(item, "callspec", None) and item.callspec.params.get("case")) or None
        expected_struct = None
        expected_text = expected.get(base, "")
        if isinstance(case, dict) and "expect_tool" in case:
            expected_struct = {
                "prompt": case.get("prompt"),
                "tool": case["expect_tool"],
                "args": case.get("expect_args", {}),
                "answer_contains": case.get("answer_contains", []),
            }
            doc = case.get("prompt", doc)
            param = f"[{item.callspec.params.get('model')}]"
            base = f"{base} · {case['id']}"
            expected_text = expected_text or "The model picks the right tool with the right arguments, and its final answer uses the tool result."
        STATE.add(item.nodeid, {
            "id": item.nodeid,
            "layer": LAYERS.get(layer_key, layer_key),
            "file": rel.name,
            "name": base,
            "param": param.strip("[]"),
            "title": doc or _humanize(base),
            "expected": expected_text,
            "expected_struct": expected_struct,
            "status": "pending",
            "duration": None,
            "note": "",
            "assertions": [],
            "assertions_more": 0,
            "failure": "",
            "failure_detail": "",
            "skip_reason": "",
            "eval_runs": None,
        })
    STATE.set_status("running")
    if session.items and not config.getoption("--dashboard-no-open"):
        try:
            webbrowser.open(config._dashboard["url"])
        except Exception:  # noqa: BLE001 - never break the test run for the browser
            pass


def pytest_runtest_logstart(nodeid, location):
    if STATE is None:
        return
    STATE.current = nodeid
    STATE.update(nodeid, status="running", started_at=time.time(), note="")


def pytest_assertion_pass(item, lineno, orig, expl):
    if STATE is None:
        return
    entry = {"line": lineno, "check": _clip(orig.strip(), 200), "evaluated": _clip(expl.split("\n", 1)[0], 300)}
    split = _split_comparison(orig, expl)
    if split:
        entry.update(left_src=split[0], right_src=split[1],
                     actual=_clip(split[2], 300), expected=_clip(split[3], 300))
    STATE.add_assertion(item.nodeid, entry)


def pytest_runtest_logreport(report):
    if STATE is None:
        return
    fields = {}
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        if hasattr(report, "wasxfail"):
            status = "xfailed" if report.outcome == "skipped" else "xpassed"
            fields["skip_reason"] = report.wasxfail
        elif report.skipped:
            status = "skipped"
            reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
            fields["skip_reason"] = reason.replace("Skipped: ", "")
        elif report.failed:
            status = "failed"
        else:
            status = "passed"
        fields.update(status=status, duration=round(report.duration, 3), note="")
        props = dict(report.user_properties or [])
        if "eval_runs" in props:
            fields["eval_runs"] = props["eval_runs"]
    elif report.when == "teardown" and report.failed:
        fields["status"] = "failed"
    if report.failed or fields.get("status") == "xfailed":
        crash = getattr(report.longrepr, "reprcrash", None)
        fields["failure"] = ANSI.sub("", crash.message if crash else "")[:2000]
        fields["failure_detail"] = ANSI.sub("", report.longreprtext)[:6000]
    if fields:
        STATE.update(report.nodeid, **fields)


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if STATE is None:
        return
    STATE.set_status("finished", int(exitstatus))
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    payload = json.dumps(STATE.snapshot(), default=str).replace("</", "<\\/")
    html = (HERE / "index.html").read_text().replace(
        "<!--STATE-->", f"<script>window.__STATE__ = {payload};</script>"
    )
    (out / "dashboard.html").write_text(html)

    server = config._dashboard["server"]
    if config.getoption("--dashboard-hold"):
        print(f"\ndashboard kept alive at {config._dashboard['url']} (Ctrl+C to quit)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    elif time.time() - STATE.last_poll < 5:  # a browser is watching: let it fetch the final state
        STATE.final_served.wait(timeout=5)
    server.shutdown()


def pytest_terminal_summary(terminalreporter):
    if STATE is not None:
        terminalreporter.write_line(f"dashboard snapshot: {ROOT / 'reports' / 'dashboard.html'}")
