"""Terminal interface: chat with the agent, browse eval transcripts, export them, run evals.

    paylane                        chat (default)
    paylane chat [--trace]         chat; --trace shows every tool call
    paylane transcripts            list saved eval runs, then pick one to read
    paylane transcripts RUN [ID]   show a run's conversations, or one conversation
    paylane export RUN [-o FILE]   write a run as readable Markdown
    paylane eval [args...]         run the eval suite (same flags as evals/run_evals.py)

RUN is a results filename (or its number from `paylane transcripts`). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "evals" / "results"


# --- presentation -----------------------------------------------------------------

class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t: str) -> str: return self._wrap("1", t)
    def dim(self, t: str) -> str: return self._wrap("2", t)
    def green(self, t: str) -> str: return self._wrap("32", t)
    def red(self, t: str) -> str: return self._wrap("31", t)
    def yellow(self, t: str) -> str: return self._wrap("33", t)
    def cyan(self, t: str) -> str: return self._wrap("36", t)


def _style(stream=sys.stdout) -> Style:
    return Style(stream.isatty() and "NO_COLOR" not in os.environ)


def _width() -> int:
    return max(60, min(100, shutil.get_terminal_size((88, 24)).columns))


def _wrapped(text: str, indent: str) -> str:
    width = _width() - len(indent)
    lines = []
    for para in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(para, width) or [""])
    return "\n".join(indent + line for line in lines)


def tool_summary(entry: dict) -> str:
    """One short line describing a logged tool call. Never prints last-4 (already redacted)."""
    tool, args, r = entry["tool"], entry["args"], entry["result"]
    shown = ", ".join(f"{k}={v}" for k, v in args.items() if k not in ("summary",))
    if tool == "escalate_to_human":
        shown = f'"{args.get("summary", "")[:60]}"'
    if tool == "verify_customer":
        outcome = "verified" if r.get("verified") else ("LOCKED" if r.get("locked") else "not verified")
    elif tool == "issue_refund" and r.get("ok"):
        outcome = f"refunded {r.get('amount')} ({r.get('refund_id')})"
    elif tool == "get_transaction" and r.get("ok"):
        outcome = f"{r['amount']} {r['merchant']} {r['status']}, {r['age_days']}d old"
    elif tool == "escalate_to_human" and r.get("ok"):
        outcome = f"ticket {r['ticket_id']}" + (" (already open)" if r.get("existing") else "")
    else:
        outcome = r.get("error") or r.get("message") or ("ok" if r.get("ok") else "failed")
    return f"{tool}({shown}) -> {outcome}"


def _print_tool(entry: dict, s: Style, out) -> None:
    line = "    " + tool_summary(entry)
    if entry.get("violation"):
        print(s.red(f"{line}   BLOCKED: {entry['violation']}"), file=out)
    else:
        print(s.dim(line), file=out)


# --- .env -------------------------------------------------------------------------

def load_env(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# --- chat -------------------------------------------------------------------------

CHAT_HELP = """commands:
  /trace   toggle showing tool calls
  /state   show session state (verified, locked, attempts, tickets)
  /new     start a new chat session (a fresh session, as a new chat would get)
  /help    this help
  /quit    exit (or Ctrl-D)"""


def chat(trace: bool = False, llm=None, read=input, out=sys.stdout) -> int:
    from .agent import run_turn
    from .session import Session

    s = _style(out)
    if llm is None:
        from .llm import LLM
        try:
            llm = LLM()
        except RuntimeError as exc:
            print(s.red(f"config error: {exc}"), file=out)
            print("Copy .env.example to .env and fill in OPENAI_API_KEY, OPENAI_BASE_URL, PAYLANE_MODEL.", file=out)
            return 2
    model = getattr(llm, "model", "model")
    session = Session()

    print(s.bold("Paylane support") + s.dim(f"  ·  {model}  ·  trace {'on' if trace else 'off'}"), file=out)
    print(s.dim("Type a message. /help for commands, /quit to exit.\n"), file=out)
    while True:
        try:
            user = read(s.cyan("you › ")).strip()
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            return 0
        if not user:
            continue
        if user.startswith("/"):
            cmd = user.split()[0].lower()
            if cmd in ("/quit", "/exit", "/q"):
                return 0
            if cmd == "/help":
                print(s.dim(CHAT_HELP), file=out)
            elif cmd == "/trace":
                trace = not trace
                print(s.dim(f"trace {'on' if trace else 'off'}"), file=out)
            elif cmd == "/state":
                print(s.dim(f"verified={session.verified}  locked={session.locked}  "
                            f"failed_attempts={session.failed_attempts}  tickets={session.tickets or '-'}  "
                            f"refunded={sorted(session.refunded_txn_ids) or '-'}  "
                            f"blocked={len(session.policy_violations)}"), file=out)
            elif cmd == "/new":
                session = Session()
                print(s.dim("new chat session started"), file=out)
            else:
                print(s.dim(f"unknown command {cmd}; /help for commands"), file=out)
            continue

        seen = len(session.tool_call_log)
        try:
            reply = run_turn(session, user, llm=llm)
        except Exception as exc:  # network/auth/rate-limit problems with the LLM endpoint
            print(s.red(f"[error talking to model: {type(exc).__name__}: {exc}]"), file=out)
            continue
        new_calls = session.tool_call_log[seen:]
        if trace:
            for entry in new_calls:
                _print_tool(entry, s, out)
        elif any(e.get("violation") for e in new_calls):
            print(s.red("    (a blocked policy violation was attempted; /trace to see tool calls)"), file=out)
        print(s.green("paylane › ") + _wrapped(reply, " " * 10).lstrip(), file=out)
        print(file=out)


# --- transcripts --------------------------------------------------------------------

def list_runs(results_dir: Path | None = None) -> list[Path]:
    return sorted((results_dir or RESULTS_DIR).glob("*.json"), reverse=True)


def resolve_run(ref: str, results_dir: Path | None = None) -> Path:
    results_dir = results_dir or RESULTS_DIR
    runs = list_runs(results_dir)
    if ref.isdigit() and 1 <= int(ref) <= len(runs):
        return runs[int(ref) - 1]
    for candidate in (Path(ref), results_dir / ref, results_dir / f"{ref}.json"):
        if candidate.is_file():
            return candidate
    raise SystemExit(f"no results file matches {ref!r} (see `paylane transcripts`)")


def load_run(path: Path, rescore: bool = False) -> dict:
    data = json.loads(path.read_text())
    if rescore:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        from evals.checks import run_checks
        from evals.conversations import CONVERSATIONS
        from evals.run_evals import from_record
        cases = {c["id"]: c for c in CONVERSATIONS}
        kept = []
        for rec in data["conversations"]:
            if rec["id"] in cases:
                results = run_checks(from_record(rec, cases[rec["id"]]))
                kept.append({**rec, "checks": results, "passed": all(r["passed"] for r in results)})
        data["conversations"] = kept
    return data


def _score(data: dict) -> str:
    convs = data["conversations"]
    return f"{sum(c['passed'] for c in convs)}/{len(convs)}"


def print_runs(results_dir: Path | None = None, out=sys.stdout) -> list[Path]:
    s = _style(out)
    runs = list_runs(results_dir)
    if not runs:
        print(f"no results in {results_dir or RESULTS_DIR}; run `paylane eval` first", file=out)
        return runs
    print(s.bold(f"{'#':>3}  {'run':<32} {'model':<24} {'passed':>6}  note"), file=out)
    for i, path in enumerate(runs, 1):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            print(f"{i:>3}  {path.name:<32} {s.red('unreadable')}", file=out)
            continue
        note = "incomplete" if data.get("incomplete") else ("offline fake" if data.get("model") == "fake" else "")
        print(f"{i:>3}  {path.name:<32} {data.get('model', '?')[:24]:<24} {_score(data):>6}  {s.dim(note)}", file=out)
    return runs


def print_run(data: dict, name: str, out=sys.stdout) -> None:
    s = _style(out)
    print(s.bold(f"{name}") + s.dim(f"  ·  {data.get('model')}  ·  {_score(data)} passed"), file=out)
    for i, c in enumerate(data["conversations"], 1):
        passed = sum(r["passed"] for r in c["checks"])
        status = s.green("PASS") if c["passed"] else s.red("FAIL")
        blocked = f"  {len(c['violations'])} blocked" if c["violations"] else ""
        print(f"{i:>3}  {status}  {c['id']:<30} {passed}/{len(c['checks'])} checks{s.yellow(blocked)}", file=out)


def print_conversation(c: dict, out=sys.stdout) -> None:
    s = _style(out)
    print(s.bold(c["id"]) + "  " + (s.green("PASS") if c["passed"] else s.red("FAIL")), file=out)
    print(s.dim(_wrapped(c["tests"], "")), file=out)
    print(file=out)
    for n, t in enumerate(c["turns"], 1):
        print(s.cyan(f"customer [{n}]"), file=out)
        print(_wrapped(t["user"], "  "), file=out)
        for entry in t["tool_calls"]:
            _print_tool(entry, s, out)
        print(s.green("agent"), file=out)
        print(_wrapped(t["assistant"], "  "), file=out)
        print(file=out)
    if c.get("error"):
        print(s.red(f"error: {c['error']}"), file=out)
    print(s.bold("checks"), file=out)
    for r in c["checks"]:
        mark = s.green("  ok  ") if r["passed"] else s.red("  FAIL")
        print(f"{mark} {r['check']:<28} {s.dim(r['reason'])}", file=out)
    for v in c["violations"]:
        print(s.yellow(f"  blocked attempt: {v['tool']}: {v['violation']}"), file=out)


def transcripts(ref: str | None, case: str | None, rescore: bool, read=input, out=sys.stdout) -> int:
    # Prompt for a choice only when someone can answer (a terminal, or a test passing `read`).
    can_prompt = read is not input or sys.stdin.isatty()
    if ref is None:
        runs = print_runs(out=out)
        if not runs or not can_prompt:
            return 0
        ref = _pick(read, "open run # (enter to quit): ", len(runs))
        if ref is None:
            return 0
    path = resolve_run(ref)
    data = load_run(path, rescore)
    if case is None:
        while True:
            print(file=out)
            print_run(data, path.name, out)
            if not can_prompt:
                return 0
            pick = _pick(read, "open conversation # (enter to go back): ", len(data["conversations"]))
            if pick is None:
                return 0
            print(file=out)
            print_conversation(data["conversations"][int(pick) - 1], out)
    match = [c for c in data["conversations"] if c["id"] == case or c["id"].startswith(case)]
    if not match:
        raise SystemExit(f"no conversation {case!r} in {path.name}")
    print_conversation(match[0], out)
    return 0


def _pick(read, prompt: str, n: int) -> str | None:
    while True:
        try:
            choice = read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not choice or choice.lower() in ("q", "quit"):
            return None
        if choice.isdigit() and 1 <= int(choice) <= n:
            return choice
        print(f"pick 1-{n}, or press enter to go back")


# --- export -------------------------------------------------------------------------

def to_markdown(data: dict, name: str) -> str:
    convs = data["conversations"]
    lines = [f"# Eval transcripts: {name}", "",
             f"Model: `{data.get('model')}`. Score: **{_score(data)}** conversations passed"
             + (" (incomplete run)" if data.get("incomplete") else "") + ".", "",
             "| Conversation | Result | Checks | Blocked attempts |", "|---|---|---|---|"]
    for c in convs:
        passed = sum(r["passed"] for r in c["checks"])
        lines.append(f"| [{c['id']}](#{c['id'].replace('_', '-')}) | {'PASS' if c['passed'] else '**FAIL**'} "
                     f"| {passed}/{len(c['checks'])} | {len(c['violations'])} |")
    for c in convs:
        lines += ["", f"## {c['id']}", "", f"*{c['tests']}* **{'PASS' if c['passed'] else 'FAIL'}**", ""]
        for n, t in enumerate(c["turns"], 1):
            lines.append(f"**Customer [{n}]:** {t['user']}")
            lines.append("")
            if t["tool_calls"]:
                lines.append("```")
                for entry in t["tool_calls"]:
                    blocked = f"   BLOCKED: {entry['violation']}" if entry.get("violation") else ""
                    lines.append(tool_summary(entry) + blocked)
                lines.append("```")
                lines.append("")
            lines.append("**Agent:** " + t["assistant"].replace("\n", "  \n"))
            lines.append("")
        if c.get("error"):
            lines += [f"Error: `{c['error']}`", ""]
        lines.append("| Check | Result | Detail |")
        lines.append("|---|---|---|")
        for r in c["checks"]:
            lines.append(f"| `{r['check']}` | {'ok' if r['passed'] else '**FAIL**'} | {r['reason'].replace('|', '/')} |")
        for v in c["violations"]:
            lines.append(f"\nBlocked attempt: `{v['tool']}: {v['violation']}`")
    return "\n".join(lines) + "\n"


def export(ref: str, output: str | None, rescore: bool, out=sys.stdout) -> int:
    path = resolve_run(ref)
    md = to_markdown(load_run(path, rescore), path.name)
    if output:
        Path(output).write_text(md)
        print(f"wrote {output}", file=out)
    else:
        out.write(md)
    return 0


# --- entry point --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    load_env(Path.cwd() / ".env")
    load_env(ROOT / ".env")

    if argv and argv[0] == "eval":
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        from evals.run_evals import main as run_evals_main
        return run_evals_main(argv[1:])

    parser = argparse.ArgumentParser(prog="paylane", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    p_chat = sub.add_parser("chat", help="chat with the agent")
    p_chat.add_argument("--trace", action="store_true", help="show every tool call")
    p_tr = sub.add_parser("transcripts", help="browse saved eval runs")
    p_tr.add_argument("run", nargs="?", help="results file or its number")
    p_tr.add_argument("case", nargs="?", help="conversation id (prefix is enough)")
    p_tr.add_argument("--rescore", action="store_true", help="apply the current checks instead of the saved ones")
    p_ex = sub.add_parser("export", help="write a run as Markdown")
    p_ex.add_argument("run", help="results file or its number")
    p_ex.add_argument("-o", "--output", help="output file (default: stdout)")
    p_ex.add_argument("--rescore", action="store_true", help="apply the current checks instead of the saved ones")
    sub.add_parser("eval", help="run the eval suite (passes flags to evals/run_evals.py)")
    args = parser.parse_args(argv)

    if args.command == "transcripts":
        return transcripts(args.run, args.case, args.rescore)
    if args.command == "export":
        return export(args.run, args.output, args.rescore)
    return chat(trace=getattr(args, "trace", False) or os.environ.get("PAYLANE_TRACE") == "1")
