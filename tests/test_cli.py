import io
import json

import pytest

from evals import run_evals
from paylane_agent import cli
from paylane_agent.llm import LLMResponse, ToolCall


class FakeLLM:
    model = "fake-model"

    def __init__(self, *responses):
        self.responses = list(responses)

    def complete(self, messages, tools, system):
        return self.responses.pop(0) if self.responses else LLMResponse(text="OK.")


def call(name, **args):
    return LLMResponse(text=None, tool_calls=[ToolCall(f"c_{name}", name, json.dumps(args))])


def scripted_input(*lines):
    it = iter(lines)

    def read(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return read


@pytest.fixture
def results(tmp_path, monkeypatch):
    assert run_evals.main(["--fake", "--out-dir", str(tmp_path)]) == 0
    monkeypatch.setattr(cli, "RESULTS_DIR", tmp_path)
    return tmp_path


# --- .env and formatting --------------------------------------------------------

def test_load_env_sets_missing_and_keeps_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nPAYLANE_TEST_A=from_file\nPAYLANE_TEST_B='quoted'\n\nnot a pair\n")
    monkeypatch.setenv("PAYLANE_TEST_A", "from_shell")
    monkeypatch.delenv("PAYLANE_TEST_B", raising=False)
    cli.load_env(env)
    import os
    assert os.environ["PAYLANE_TEST_A"] == "from_shell"
    assert os.environ["PAYLANE_TEST_B"] == "quoted"
    monkeypatch.delenv("PAYLANE_TEST_B")


def test_tool_summary_shapes():
    verify = {"tool": "verify_customer", "args": {"email": "a@b.c", "last4": "****"},
              "result": {"verified": False, "locked": True}}
    assert cli.tool_summary(verify) == "verify_customer(email=a@b.c, last4=****) -> LOCKED"
    refund = {"tool": "issue_refund", "args": {"txn_id": "txn_1", "amount": 24.5},
              "result": {"ok": True, "amount": "24.50", "refund_id": "re_1"}}
    assert cli.tool_summary(refund).endswith("refunded 24.50 (re_1)")
    esc = {"tool": "escalate_to_human", "args": {"summary": "x"},
           "result": {"ok": True, "ticket_id": "tkt_1", "existing": True}}
    assert cli.tool_summary(esc).endswith("ticket tkt_1 (already open)")
    blocked = {"tool": "issue_refund", "args": {"txn_id": "t", "amount": 76},
               "result": {"ok": False, "error": "Over the limit."}}
    assert cli.tool_summary(blocked).endswith("-> Over the limit.")


# --- chat ---------------------------------------------------------------------------

def test_chat_reply_state_and_trace():
    out = io.StringIO()
    llm = FakeLLM(LLMResponse(text="Hi! How can I help?"),
                  call("issue_refund", txn_id="txn_1001", amount=24.5), LLMResponse(text="Please verify first."))
    code = cli.chat(llm=llm, out=out, read=scripted_input("hello", "/trace", "refund txn_1001", "/state", "/quit"))
    text = out.getvalue()
    assert code == 0
    assert "Hi! How can I help?" in text and "fake-model" in text
    assert "BLOCKED: account_action_unverified" in text  # trace on shows the blocked call
    assert "verified=False" in text and "blocked=1" in text
    assert "\033[" not in text  # no colour codes when not a terminal


def test_chat_flags_blocked_attempt_even_without_trace():
    out = io.StringIO()
    llm = FakeLLM(call("issue_refund", txn_id="txn_1001", amount=24.5), LLMResponse(text="Verify first."))
    cli.chat(llm=llm, out=out, read=scripted_input("refund txn_1001"))
    assert "blocked policy violation was attempted" in out.getvalue()


def test_chat_new_session_resets_state():
    out = io.StringIO()
    llm = FakeLLM(call("verify_customer", email="asha.rao@example.com", last4="4242"), LLMResponse(text="Verified."))
    cli.chat(llm=llm, out=out, read=scripted_input("asha.rao@example.com 4242", "/state", "/new", "/state"))
    states = [l for l in out.getvalue().splitlines() if l.startswith("verified=")]
    assert states[0].startswith("verified=True") and states[1].startswith("verified=False")


def test_chat_config_error(monkeypatch):
    monkeypatch.delenv("PAYLANE_MODEL", raising=False)
    out = io.StringIO()
    assert cli.chat(out=out, read=scripted_input()) == 2
    assert "PAYLANE_MODEL" in out.getvalue()


def test_chat_survives_model_error():
    class Broken:
        model = "broken"

        def complete(self, *a):
            raise ConnectionError("network down")

    out = io.StringIO()
    assert cli.chat(llm=Broken(), out=out, read=scripted_input("hi")) == 0
    assert "ConnectionError" in out.getvalue()


# --- transcripts and export ----------------------------------------------------------

def test_transcripts_lists_runs(results):
    out = io.StringIO()
    cli.print_runs(out=out)
    assert "-fake.json" in out.getvalue() and "12/12" in out.getvalue()


def test_transcripts_show_one_conversation(results):
    out = io.StringIO()
    assert cli.transcripts("1", "frozen", rescore=False, out=out) == 0
    text = out.getvalue()
    assert "frozen_card" in text and "customer [1]" in text and "verify_customer(" in text
    assert "no_promised_unfreeze" in text


def test_transcripts_interactive_navigation(results):
    out = io.StringIO()
    read = scripted_input("1", "2", "")  # open run 1, open conversation 2, back
    assert cli.transcripts(None, None, rescore=False, read=read, out=out) == 0
    assert "refund_over_75" in out.getvalue() and "checks" in out.getvalue()


def test_transcripts_rescore(results):
    out = io.StringIO()
    assert cli.transcripts("1", "happy", rescore=True, out=out) == 0
    assert "no_invented_timeline" in out.getvalue()


def test_resolve_run_errors_clearly(results):
    with pytest.raises(SystemExit, match="no results file"):
        cli.resolve_run("nope")


def test_export_markdown(results, tmp_path):
    target = tmp_path / "TRANSCRIPTS.md"
    cli.export("1", str(target), rescore=False, out=io.StringIO())
    md = target.read_text()
    assert md.startswith("# Eval transcripts:")
    assert "| [happy_refund](#happy-refund) | PASS |" in md
    assert "## escalation_guest_vs_verified" in md and "**Customer [1]:**" in md
    assert "****" in md and "4242" not in md.split("**Customer")[0]  # last-4 redacted in tool lines


def test_main_eval_passthrough(tmp_path):
    assert cli.main(["eval", "--fake", "--case", "happy_refund", "--out-dir", str(tmp_path)]) == 0


# --- slash commands and markdown ------------------------------------------------------

def test_slash_alone_lists_commands():
    out = io.StringIO()
    cli.chat(llm=FakeLLM(), out=out, read=scripted_input("/"))
    text = out.getvalue()
    for cmd in cli.COMMANDS:
        assert cmd in text


def test_partial_command_suggests_match():
    out = io.StringIO()
    cli.chat(llm=FakeLLM(), out=out, read=scripted_input("/st", "/x"))
    text = out.getvalue()
    assert "did you mean /state?" in text
    assert "unknown command /x" in text


def test_command_matches():
    assert cli.command_matches("/") == list(cli.COMMANDS)
    assert cli.command_matches("/tr") == ["/trace"]
    assert cli.command_matches("/zz") == []


def test_completer_only_completes_commands(monkeypatch):
    import types
    fake = types.SimpleNamespace(get_line_buffer=lambda: "/t")
    monkeypatch.setitem(__import__("sys").modules, "readline", fake)
    assert cli._complete("/t", 0) == "/trace" and cli._complete("/t", 1) is None
    fake.get_line_buffer = lambda: "refund txn_1001 /t"
    assert cli._complete("/t", 0) is None


def test_render_markdown_without_colour_strips_markers():
    s = cli.Style(False)
    assert cli.render_markdown("Your card is **active**. Use `txn_1001`.", s) == "Your card is active. Use txn_1001."
    assert cli.render_markdown("### Options\n- one", s) == "Options\n- one"


def test_render_markdown_with_colour():
    s = cli.Style(True)
    assert cli.render_markdown("**active**", s) == "\033[1mactive\033[0m"
    # bold that wraps across a line break still renders
    assert cli.render_markdown("**two\n   lines**", s) == "\033[1mtwo\n   lines\033[0m"


def test_chat_reply_markdown_rendered():
    out = io.StringIO()
    cli.chat(llm=FakeLLM(LLMResponse(text="Your card is **active**.")), out=out, read=scripted_input("status"))
    assert "Your card is active." in out.getvalue() and "**" not in out.getvalue()
