"""Running the Claude Code CLI: processes, cancellation, and streamed output.

Everything here is about the child process, not about consults: how to start
it with the prompt on stdin, how to kill it and everything it spawned, how to
stop it when the MCP client gives up, and how to read `stream-json` output so
that a CLI that has gone silent can be told apart from one that is still
working.
"""

import contextvars
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

from .errors import AdvisorError


def kill_tree(proc: "subprocess.Popen") -> None:
    """Kill the process AND its descendants.

    Plain kill() only takes out the direct child; with `cmd /c claude.cmd` the
    grandchild survives, keeps the stdout pipe open, and wedges communicate()
    forever -- which is how a "timeout" can still hang on Windows.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, stdin=subprocess.DEVNULL)
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()


class Cancellation:
    """Lets the async tool wrapper stop a consult running in a worker thread.

    `asyncio.to_thread` cannot interrupt its thread, so a client that gave up
    on a consult would otherwise leave `claude` running -- and spending quota --
    until it finished or timed out. The wrapper puts one of these in a context
    variable, which `to_thread` copies into the worker; the runners register
    each process they start, and `cancel` kills it. A streaming runner also
    hangs its `StreamState` here, so the heartbeat can say what Claude is doing.
    """

    def __init__(self):
        self.cancelled = False
        self.proc = None
        # A CLI's StreamState, or an HTTP backend's -- which can also `close`.
        self.stream = None
        # Per-advisor holders of a multi_advisor call, cancelled with this one.
        self.children: dict = {}
        self._lock = threading.Lock()

    def attach(self, proc) -> bool:
        """Register `proc`; False if the consult was already cancelled."""
        with self._lock:
            self.proc = proc
            return not self.cancelled

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            proc = self.proc
            stream = self.stream
            children = list(self.children.values())
        if proc is not None and proc.poll() is None:
            kill_tree(proc)
        close = getattr(stream, "close", None)
        if close is not None:
            close()  # an HTTP stream: unblocks the read in progress
        for child in children:
            child.cancel()

    def child(self, name: str) -> "Cancellation":
        """A holder for one of several consults running under this one."""
        holder = Cancellation()
        with self._lock:
            holder.cancelled = self.cancelled
            self.children[name] = holder
        return holder

    @property
    def note(self) -> str:
        if self.children:
            return "; ".join(f"{name}: {child.note}" for name, child
                             in list(self.children.items()) if child.note)
        return self.stream.note if self.stream is not None else ""


CANCELLATION: "contextvars.ContextVar[Optional[Cancellation]]" = (
    contextvars.ContextVar("wisdomtooth_cancellation", default=None))

CANCELLED = ("The consult was cancelled by the client, and the claude process "
             "was stopped.")


def _cmd_quote(arg: str) -> str:
    """One argument for a cmd.exe command line, always in double quotes.

    Inside quotes cmd.exe takes `&`, `|`, `<`, `>`, `^` and spaces literally,
    and backslashes follow the rules the CLI's own argv parser applies.
    cmd.exe still expands `%VAR%`, and an embedded `"` ends its quoting, so
    text the caller controls must not reach here unchecked -- the server
    validates the model name, and every other argument is its own.
    """
    out, backslashes = ['"'], 0
    for ch in arg:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            out.append("\\" * (backslashes * 2 + 1) + '"')
        else:
            out.append("\\" * backslashes + ch)
        backslashes = 0
    out.append("\\" * (backslashes * 2) + '"')
    return "".join(out)


def wrap_for_windows(cmd: list):
    """npm-installed Claude Code resolves to claude.cmd/.bat, which
    CreateProcess cannot exec directly, so those run through cmd.exe.

    The command line is built by hand: with `/s`, cmd.exe removes exactly the
    outer pair of quotes and keeps the rest, so every argument can be quoted.
    A list would go through `list2cmdline`, which leaves `&` unquoted and whose
    quoting cmd.exe mangles once the shim's path contains a space.
    """
    if os.name == "nt" and cmd and cmd[0].lower().endswith((".cmd", ".bat")):
        shell = os.environ.get("COMSPEC") or "cmd.exe"
        line = " ".join(_cmd_quote(str(arg)) for arg in cmd)
        return f'"{shell}" /d /s /c "{line}"'
    return cmd


def _popen(cmd, env, workdir) -> "subprocess.Popen":
    kwargs = dict(
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        env=env if env is not None else os.environ.copy(),
        cwd=workdir or os.path.expanduser("~"),
    )
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True  # own process group for killpg
    proc = subprocess.Popen(wrap_for_windows(list(cmd)), **kwargs)
    holder = CANCELLATION.get()
    if holder is not None and not holder.attach(proc):
        kill_tree(proc)
    return proc


def run(cmd, env=None, workdir=None, timeout_s=60, stdin_text=""):
    """Run the CLI to completion with the prompt on stdin and a hard timeout."""
    proc = _popen(cmd, env, workdir)
    holder = CANCELLATION.get()
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout_s, output=out, stderr=err)
    if holder is not None and holder.cancelled:
        raise AdvisorError(CANCELLED)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


class IdleTimeout(subprocess.TimeoutExpired):
    """The CLI sent nothing for `idle_s` seconds and was killed."""

    def __init__(self, cmd, idle_s: float, elapsed: float, output="", stderr=""):
        super().__init__(cmd, idle_s, output=output, stderr=stderr)
        self.idle_s = idle_s
        self.elapsed = elapsed


# Characters per word, including the space, for the "~N words so far" note.
_CHARS_PER_WORD = 6


class StreamState:
    """What a `stream-json` run has said so far.

    The CLI prints one JSON object per line: a system init, the raw API stream
    events (`--include-partial-messages`), the finished assistant message, and
    last a `result` object shaped exactly like `--output-format json` output.
    `stdout()` hands that object on, so the caller parses both formats the same
    way.
    """

    def __init__(self):
        self.result: Optional[dict] = None
        self.raw: list = []
        self.phase = ""
        self.chars = 0
        # The plan's own meter: Claude Code 2.1.x streams a `rate_limit_event`
        # with the 5-hour and 7-day utilization of the subscription.
        self.rate_limit: Optional[dict] = None

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except ValueError:
            self.raw.append(line)  # a CLI that ignored the format flag
            return
        if not isinstance(obj, dict):
            return
        kind = obj.get("type")
        if kind == "result":
            self.result = obj
        elif kind == "rate_limit_event":
            info = obj.get("rate_limit_info")
            if isinstance(info, dict):
                self.rate_limit = info
        elif kind == "stream_event":
            event = obj.get("event") or {}
            if event.get("type") == "content_block_start":
                block = (event.get("content_block") or {}).get("type")
                if block == "thinking":
                    self.phase = "thinking"
                elif block == "text":
                    self.phase = "writing"
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    self.phase = "writing"
                    self.chars += len(delta.get("text") or "")

    @property
    def note(self) -> str:
        if self.phase == "writing" and self.chars:
            return f"writing, ~{max(1, self.chars // _CHARS_PER_WORD):,} words so far"
        return self.phase

    def stdout(self) -> str:
        if self.result is not None:
            result = self.result
            if self.rate_limit is not None:
                result = dict(result, rate_limit_info=self.rate_limit)
            return json.dumps(result)
        return "\n".join(self.raw)


def run_streaming(cmd, env=None, workdir=None, timeout_s=60, stdin_text="",
                  idle_s=0, state=None):
    """Run a streaming CLI, killing it on silence as well as on the clock.

    Each output line restarts the idle clock, so an answer that takes most of
    an hour but keeps streaming runs to completion, while a CLI that has
    stopped talking is killed after `idle_s` seconds (0 = no idle limit).

    `state` reads the lines: by default a `StreamState` for Claude Code's
    `stream-json`, whose `stdout()` -- the final `result` object -- becomes
    the CompletedProcess's stdout. Any object with `feed`, `note` and
    `stdout` works, which is how other vendors' CLIs share this runner.
    """
    proc = _popen(cmd, env, workdir)
    holder = CANCELLATION.get()
    state = state if state is not None else StreamState()
    if holder is not None:
        holder.stream = state
    lines: "queue.Queue[Optional[str]]" = queue.Queue()
    errors: list = []

    def pump_stdout():
        try:
            for line in proc.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    def pump_stderr():
        try:
            errors.append(proc.stderr.read())
        except (OSError, ValueError):
            pass

    def feed_stdin():
        # A thread of its own: a large prompt fills the pipe buffer, and the
        # CLI may write before it has read all of it.
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    threads = [threading.Thread(target=fn, daemon=True)
               for fn in (pump_stdout, pump_stderr, feed_stdin)]
    for thread in threads:
        thread.start()

    def stderr_text() -> str:
        threads[1].join(timeout=5)
        return "".join(errors)

    started = last = time.monotonic()
    while True:
        now = time.monotonic()
        left = timeout_s - (now - started)
        if idle_s:
            left = min(left, idle_s - (now - last))
        if left <= 0:
            kill_tree(proc)
            if idle_s and now - last >= idle_s:
                raise IdleTimeout(cmd, idle_s, round(now - started),
                                  output=state.stdout(), stderr=stderr_text())
            raise subprocess.TimeoutExpired(cmd, timeout_s, output=state.stdout(),
                                            stderr=stderr_text())
        try:
            line = lines.get(timeout=min(left, 1.0))
        except queue.Empty:
            continue
        if line is None:
            break  # the CLI closed its output
        last = time.monotonic()
        state.feed(line)

    try:
        proc.wait(timeout=max(5.0, timeout_s - (time.monotonic() - started)))
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.wait(timeout=5)
    if holder is not None and holder.cancelled:
        raise AdvisorError(CANCELLED)
    return subprocess.CompletedProcess(cmd, proc.returncode, state.stdout(),
                                       stderr_text())


def parse_output(stdout: str):
    """Return (answer_text, parsed_json_or_None).

    Falls back to raw stdout so a CLI whose JSON shape has changed still yields
    an answer rather than an error.
    """
    text = stdout.strip()
    if not text.startswith("{"):
        return text, None
    try:
        payload = json.loads(text)
    except ValueError:
        return text, None
    if not isinstance(payload, dict):
        return text, None
    return str(payload.get("result", "")), payload


def spawn_login_console(cmd: list) -> str:
    """Open `cmd` in a console window the user can see and interact with.

    The CLI's OAuth flow needs a real console it can own -- piping its stdio
    would strand the user halfway through a browser handshake with nothing to
    type into. Returns a short description of what was opened. Raises if the
    machine has no desktop to put a window on (headless server, container,
    plain SSH).
    """
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen(wrap_for_windows(list(cmd)), creationflags=flags,
                         close_fds=True)
        return "a new console window"

    if sys.platform == "darwin":
        script = " ".join(f"'{part}'" for part in cmd)
        subprocess.Popen(
            ["osascript", "-e",
             f'tell application "Terminal" to do script "{script}"',
             "-e", 'tell application "Terminal" to activate'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "a new Terminal window"

    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("no graphical session")
    for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"),
                       ("konsole", "-e"), ("xfce4-terminal", "-x"),
                       ("xterm", "-e")):
        if shutil.which(term):
            subprocess.Popen([term, flag] + list(cmd), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"a new {term} window"
    raise RuntimeError("no terminal emulator found")
