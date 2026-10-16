#!/usr/bin/env python3
"""chat.py — terminal client for the letsClaw core.

The core (server.py) holds the conversation; this renders it. Start the core
first — there is no in-process fallback:

    ./serve.sh          then, in another shell:
    ./terminalUI.sh

Two things here are less obvious than they look:

*   Input runs on a dedicated daemon thread, not `asyncio.to_thread`. With
    to_thread a Ctrl-C never surfaces and the process hangs unkillable, because
    the worker is parked in a blocking tty read that the executor then tries to
    join at shutdown. A daemon thread can simply be abandoned — hence the
    `os._exit` on the way out.
*   All output goes through Renderer. Another client's tokens can land while you
    are half-way through typing, so anything printed outside a turn has to erase
    the input line and put your text back afterwards.
"""

import argparse
import asyncio
import os
import sys
import threading
import time
import traceback
from urllib.parse import quote

try:
    import readline
except ImportError:
    readline = None

import aiohttp

from config import known_models, load_config
from core import PROTOCOL_VERSION
from ui import thinking_indicator

PROMPT = "👤 "

COMMANDS = {
    "/quit": "leave letsClaw (the core keeps running)",
    "/exit": "leave letsClaw (the core keeps running)",
    "/clear": "forget the conversation, keep the archives",
    "/rollover": "archive it, carry a handoff forward, start a fresh window",
    "/new": "wipe everything — conversation and this session's archives",
    "/model": "list models, or /model <name> to switch",
    "/info": "recent history and context usage",
    "/behavior": "print the loaded behavior files (base + model + session)",
    "/reasoning": "toggle live display of the model's thinking",
    "/stop": "interrupt the turn in progress",
    "/steering": "send a correction to the turn in progress",
    "/reload": "re-read config.yaml into the running core",
    "/restart": "restart the core process (/restart force cancels running turns)",
}

# Handled here, not by the core: they change this terminal, not the conversation.
LOCAL_COMMANDS = {"/quit", "/exit", "/reasoning"}

# What counts as an answer to the rollover question, and nothing else does —
# see the pending branch in the input loop.
YES = ("y", "yes")
NO = ("n", "no")
# Grace on top of the core's own timeout before this side stops treating a
# bare yes/no as an answer. Covers the round trip, so a genuine last-second
# answer is not thrown away here. The Discord client holds the same two
# figures for the same reason (ASK_SLACK_S, DEFAULT_ASK_TIMEOUT_S).
ASK_SLACK_S = 5
# Used when the core's rollover_ask carries no timeout_s. Anything finite
# beats the old behaviour, which was to wait for ever.
DEFAULT_ASK_TIMEOUT_S = 120


def _bind_tab_to_complete():
    """CPython leaves TAB inserting a literal tab. The two readline flavours want
    different syntax and do not ignore each other's — hand GNU readline the
    libedit form and it binds the letter 'b'."""
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


def install_completer(model_names):
    """Tab completion: /commands, and model names after '/model '."""
    if readline is None:
        return
    readline.set_completer_delims(" \t\n")
    _bind_tab_to_complete()

    def candidates(text):
        prior = readline.get_line_buffer()[:readline.get_begidx()].split()
        if not prior:
            return [c for c in COMMANDS if c.startswith(text)]
        if prior == ["/model"]:
            return [m for m in model_names if m.startswith(text)]
        return []

    matches = []

    def completer(text, state):
        if state == 0:
            try:
                # Trailing space: CPython zeroes readline's own append character.
                matches[:] = [c + " " for c in candidates(text)]
            except Exception:
                matches[:] = []
        return matches[state] if state < len(matches) else None

    def show_matches(substitution, shown, longest):
        sys.stdout.write("\n")
        for m in (c.rstrip() for c in shown):
            desc = COMMANDS.get(m)
            sys.stdout.write(f"  {m.ljust(longest + 2)}{desc}\n" if desc else f"  {m}\n")
        sys.stdout.write(PROMPT + readline.get_line_buffer())
        sys.stdout.flush()

    readline.set_completer(completer)
    readline.set_completion_display_matches_hook(show_matches)


class Renderer:
    """Turns core events into the terminal output letsClaw has always produced.

    Owns stdout. `line()` is the safe path for anything arriving while the user
    may be mid-keystroke; raw streaming inside a turn writes directly.
    """

    def __init__(self):
        self.show_reasoning = False
        self.in_turn = False
        self.opened = None      # None | "text" | "reasoning"
        self.ind = None
        self.thought = 0        # tokens thought this turn, from the core
        self.thought_base = 0   # the total when the current run of thinking began
        self.prompt_shown = False
        self.pending_ask = None   # an unanswered rollover question's request_id
        self.pending_until = 0.0  # ...and the monotonic deadline it dies at

    # ---- low-level -------------------------------------------------------

    def line(self, text=""):
        """Print a whole line without eating a half-typed input line."""
        self._stop_spinner()
        self._close_stream()
        sys.stdout.write("\r\x1b[K" + text + "\n")
        if self.prompt_shown and not self.in_turn:
            buf = readline.get_line_buffer() if readline else ""
            sys.stdout.write(PROMPT + buf)
        sys.stdout.flush()

    def _close_stream(self):
        if self.opened:
            sys.stdout.write("\n")
            self.opened = None

    def _stop_spinner(self):
        if self.ind:
            self.ind.stop()
            self.ind = None

    def _open(self, kind, marker):
        if self.opened != kind:
            self._close_stream()
            self._stop_spinner()
            sys.stdout.write(marker)
            self.opened = kind

    def spinner(self):
        self._stop_spinner()
        # Carries the turn's count into the new spinner: the thinking a tool
        # round already did did not stop counting while the tool ran.
        self.ind = thinking_indicator(tokens=self.thought - self.thought_base,
                                      total=self.thought)
        self.ind.start()

    def _end_think_block(self):
        """Words or a tool call: this run of thinking is over, the next starts at 0.

        The same boundary the WebUI stamps at — the terminal has no stamp to
        leave, so the block figure simply restarts and the turn total carries on.
        """
        self.thought_base = self.thought

    # ---- events ----------------------------------------------------------

    def handle(self, e):
        t = e.get("t")
        if t == "hello":
            self.hello(e)
        elif t == "turn_start":
            self.in_turn = True
            self.thought = self.thought_base = 0
            self.spinner()
        elif t == "reasoning_stat":
            # The count only has somewhere to go while the spinner is up, which
            # is precisely when the thinking is hidden — with it shown, the
            # thinking itself is the progress report.
            self.thought = e["tokens"]
            if self.ind:
                self.ind.tokens = self.thought - self.thought_base
                self.ind.total = self.thought
        elif t == "reasoning":
            if self.show_reasoning:
                self._open("reasoning", "\n🧠 ")
                sys.stdout.write("\x1b[2m" + e["delta"] + "\x1b[0m")
                sys.stdout.flush()
        elif t == "text":
            if self.opened != "text":
                self._end_think_block()
            self._open("text", "\n🐱 ")
            sys.stdout.write(e["delta"])
            sys.stdout.flush()
        elif t == "tool_call":
            self._end_think_block()
            self.line(f"⚙️ {e['name']} {e.get('arguments','')[:150]}")
            self.spinner()
        elif t == "tool_result":
            self._stop_spinner()
            self.line(f"   ↳ {e['size']} chars")
            self.spinner()
        elif t == "steering":
            # A correction landed mid-turn: the round was interrupted and the
            # model re-plans. line() closes the open stream; the spinner goes
            # back up until the next words or thinking arrive.
            self.line(f"\n📌 {e['text']}")
            self.spinner()
        elif t == "stats":
            self.stats(e)
        elif t == "turn_end":
            self._stop_spinner()
            self._close_stream()
            self.in_turn = False
        elif t == "rollover_start":
            self.line(f"\n🔄 Rolling over — {e['reason']}.")
        elif t == "rollover_done":
            if e.get("transcript"):
                self.line(f"   💾 Transcript  {e['transcript']}")
            if e.get("handoff"):
                self.line(f"   🧠 Handoff     {e['handoff']}")
            if e.get("handoff_text"):
                self.line("   What the new window carries:")
                for ln in e["handoff_text"].splitlines() or [""]:
                    self.line(f"     {ln}")
            self.line(f"   ✨ New session  {e['used']}/{e['budget']} tok")
        elif t == "session_state":
            if e["what"] == "cleared":
                self.line("\n🧹 History cleared (/rollover archives it instead).")
                if e.get("record"):
                    self.line(f"   📓 what was said is still in {e['record']}")
                self.fresh_context(e)
            elif e["what"] == "wiped":
                self.line("\n🔥 Everything wiped — conversation and archives.")
                if e.get("files"):
                    self.line(f"   🗑️  {e['files']} archive file(s), "
                              f"{e['bytes']:,} bytes deleted")
                self.fresh_context(e)
            elif e["what"] == "model":
                self.line(f"\n🤖 Switched to {e['model']} — context budget {e['budget']} tok.")
            elif e["what"] == "reloaded":
                # Broadcast to every attached session, including ones whose client
                # did not ask — so say which session moved, not just that it did.
                self.line(f"\n♻️  Config reloaded — {e['model']}, "
                          f"context budget {e['budget']} tok.")
        elif t == "busy":
            self.line(f"⏳ {e['reason']}")
        elif t == "notice":
            mark = {"warn": "⚠️ ", "info": ""}.get(e.get("level"), "")
            self.line(f"   {mark}{e['text']}")
        elif t == "error":
            self.line(f"\n❌ {e['msg']}")

    def fresh_context(self, e):
        """What the rebuilt window costs before a word is said.

        Worth a line because the number is not zero and people reasonably expect
        it to be: an emptied conversation still carries the three behavior MDs
        and the tool schemas. Saying so is also the only confirmation that the
        files went back in — refresh_behavior only speaks up when one changed.
        """
        if not e.get("budget"):
            return      # an older core, which sent no figure
        self.line(f"   ✨ fresh context ~{e.get('used', 0)}/{e['budget']} tok "
                  f"(behavior files + tool schemas)")

    def hello(self, e):
        if e.get("proto") != PROTOCOL_VERSION:
            self.line(f"⚠️  core speaks protocol {e.get('proto')}, this client speaks "
                      f"{PROTOCOL_VERSION} — update one of them.")
        self.line(f"\n🤖 Model: {e['model']}   session: {e['session']}   "
                  f"budget: {e['budget']} tok")
        n = len(e.get("messages", []))
        if n:
            self.line(f"📜 Rejoined a conversation already {n} messages long.")
        if e.get("gap"):
            self.line("   (some earlier output was missed while detached)")
        for missed in e.get("missed", []):
            self.handle(missed)
        if e.get("busy"):
            self.line("   ⏳ a turn is running in this session")

    def stats(self, e):
        rtok = f" + reasoning {e['reasoning_tokens']} tok" if e.get("reasoning_tokens") else ""
        ttft = f" ttft {e['ttft']:.1f}s ·" if e.get("ttft") is not None else ""
        n = e.get("tool_calls") or 0
        tparts = f" · {n} tool call{'s' if n != 1 else ''}" if n else ""
        approx = "" if e.get("measured") else "~"
        used, budget = e["used"], e["budget"]
        pct = (used * 100) // budget if budget else 0
        warn = "  ⚠️ over budget" if budget and used > budget else ""
        # Output is everything the model generated this turn — answer, thinking and
        # tool arguments across every round — which is more than the LLM text
        # above and is what a metered endpoint bills. Its own ~ : the prompt side
        # and the output side can be measured or estimated independently.
        out = ""
        if e.get("output_tokens") is not None:
            oapprox = "" if e.get("output_measured") else "~"
            out = f" · out {oapprox}{e['output_tokens']} tok"
            if e.get("output_total"):
                out += f" ({e['output_total']} this session)"
        self.line(f"⚡{ttft} user {e['user_tokens']} + LLM {e['llm_tokens']} "
                  f"tok{rtok}{tparts}{out} · context {approx}{used}/{budget} ({pct}%){warn}")

    def show_info(self, r):
        i = r["info"]
        self.line(f"\n🤖 Model: {i['model']} @ {i['base_url']}")
        # The id, not just the name: it is what the archive filenames and
        # logs/session_ID.log are keyed on, and unlike the name it never changes.
        sid = i.get("session_id")
        self.line(f"🆔 Session: {i['session']}"
                  + (f"  (id {sid} — archives are {sid}_*)" if sid else ""))
        self.line(f"💬 History: {i['messages']} messages")
        self.line("📜 Recent history:")
        for m in i["recent"]:
            emoji = {"system": "🤖", "user": "👤", "tool": "🔧"}.get(m["role"], "🐱")
            self.line(f"  {emoji} {m['role']}: {m['content']} ({m['tokens']} tok)")
        pct = (i["used"] * 100) // i["budget"] if i["budget"] else 0
        # Same ~ convention as the per-turn stats line above: it marks the
        # estimator's padded guess, and is absent once the server has reported
        # a real prompt_tokens for this window.
        approx = "~" if i.get("estimated", True) else ""
        how = "~ conservative estimate" if approx else "counted by the server"
        self.line(f"  ⚡ Context: {approx}{i['used']}/{i['budget']} tok ({pct}%)   "
                  f"({how}; {i['tools_tokens']} tok of tool schemas)")
        if i.get("output_total") is not None:
            self.line(f"  📤 Output: {i['output_total']} tok generated in this session"
                      f"  (an odometer — /clear does not rewind it)")
        ro = i["rollover"]
        n = ro.get("count") or 0
        count = f" · {n} rollover{'s' if n != 1 else ''}"
        self.line(f"  🔄 Rollover: {ro['mode']} at {ro['percent']}% ({ro['trip']} tok){count}"
                  if ro["percent"] else f"  🔄 Rollover: disabled (/rollover still works){count}")

    def show_reload(self, r):
        changed = r.get("changed") or []
        if not changed:
            self.line(f"\n♻️  {r.get('note') or 'nothing changed'}")
            return
        self.line("\n♻️  Config reloaded:")
        for line in changed:
            self.line(f"     {line}")
        n, d = r.get("updated", 0), r.get("deferred", 0)
        # A deferred session is mid-turn; it takes the change when that turn ends.
        self.line(f"     {n} session(s) updated"
                  + (f", {d} waiting for a turn to finish" if d else ""))


def start_reader(loop, queue):
    """Read lines on a daemon thread so the event loop keeps streaming.

    readline (history, tab completion) needs a blocking read and therefore its
    own thread; daemon=True is what lets us walk away from it at exit.
    """
    def run():
        while True:
            try:
                line = input(PROMPT)
            except (EOFError, KeyboardInterrupt):
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return
            loop.call_soon_threadsafe(queue.put_nowait, line)
    threading.Thread(target=run, daemon=True, name="stdin").start()


async def run_client(url, session_name, render, token=""):
    lines = asyncio.Queue()
    loop = asyncio.get_running_loop()
    query = f"session={quote(session_name)}"
    if token:
        query += f"&token={quote(token)}"

    async with aiohttp.ClientSession() as http:
        try:
            ws = await http.ws_connect(f"{url}/ws?{query}", heartbeat=20)
        except aiohttp.WSServerHandshakeError as e:
            if e.status == 401:
                print("❌ The core refused the connection: core.token is set and no\n"
                      "   matching one was offered. Pass it with\n"
                      "     ./terminalUI.sh --token <secret>\n"
                      "   or  CLAW_TOKEN=<secret> ./terminalUI.sh\n"
                      "   (a core on this machine: its token in config.yaml is\n"
                      "   picked up automatically)")
            else:
                print(f"❌ The core refused the handshake ({e.status}).")
            return 1
        except aiohttp.ClientError as e:
            print(f"❌ Cannot reach the core at {url} ({e}).")
            print("   Start it first:  ./serve.sh")
            return 1

        async def pump():
            """Core → terminal."""
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                # Parsed inside a try of its own. It used to sit above the
                # try meant to protect it, so a truncated or non-JSON frame
                # raised straight out of `async for` and killed the pump: the
                # terminal went on looking connected while nothing the core
                # said ever reached it again.
                try:
                    e = msg.json()
                except (ValueError, TypeError) as exc:
                    render.line(f"⚠️  ignored a malformed event from the core "
                                f"({type(exc).__name__})")
                    continue
                try:
                    _dispatch(e, render, ws)
                except Exception as exc:
                    # One unrenderable event must not silently kill the stream —
                    # which is exactly what a bare create_task() would do.
                    render.line(f"⚠️  could not render {e.get('t')!r}: "
                                f"{type(exc).__name__}: {exc}")
            # 1012 is SERVICE_RESTART, which the core sends only from its
            # /restart path. This client has no reconnect, so without keying on
            # it a restart someone triggered from the WebUI read here as an
            # unexplained drop — and the advice ("the core is down") was the
            # opposite of true, since the core is back a moment later.
            if ws.close_code == 1012:
                render.line("\n♻️  The core is restarting — it will be back in "
                            "a moment. Run ./terminalUI.sh again to reattach; "
                            "the conversation is kept.")
            else:
                render.line("\n⚠️  The core closed the connection.")
            await lines.put(None)

        def _dispatch(e, render, ws):
                if e.get("t") == "response":
                    if "info" in e:
                        render.show_info(e)
                    elif "behavior" in e:
                        if e.get("base"):
                            render.line(f"\n📄 Base behavior:\n{e['base']}")
                        if e.get("behavior"):
                            model = f" ({e['model']})" if e.get("model") else ""
                            render.line(f"\n📄 Model behavior{model}:\n{e['behavior']}")
                        if e.get("session_behavior"):
                            sess = f" ({e['session']})" if e.get("session") else ""
                            render.line(f"\n📄 Session behavior{sess}:\n"
                                        f"{e['session_behavior']}")
                        if not any(e.get(k) for k in
                                   ("base", "behavior", "session_behavior")):
                            render.line("\nNo behavior file loaded.")
                    elif "configured" in e:
                        render.line(f"\n🤖 Current: {e.get('current')}")
                        render.line(f"📋 Configured models: {', '.join(e['configured']) or '(none)'}")
                    elif "changed" in e:
                        render.show_reload(e)
                    elif not e.get("ok"):
                        render.line(f"\n❌ {e.get('error')}")
                elif e.get("t") == "rollover_ask":
                    render.line(f"\n⚠️  Context at {e['used']}/{e['budget']} ({e['percent']}%).")
                    render.line(f"   Roll over to a new session? [y/N]  "
                                f"(no answer in {e['timeout_s']}s keeps it)")
                    render.pending_ask = e["request_id"]
                    # The core stops waiting after timeout_s and keeps the
                    # session; this has to stop waiting too. Without the
                    # deadline the id stayed pending for ever, and the next
                    # thing typed — any time later, including /quit — was sent
                    # as a reply to a request_id the core had long forgotten,
                    # which server.py drops without a word. The message simply
                    # vanished.
                    try:
                        timeout_s = float(e.get("timeout_s") or 0)
                    except (TypeError, ValueError):
                        timeout_s = 0.0
                    render.pending_until = time.monotonic() + (
                        timeout_s + ASK_SLACK_S if timeout_s > 0
                        else DEFAULT_ASK_TIMEOUT_S)
                else:
                    render.handle(e)
                    if e.get("t") == "hello":
                        ready.set()

        ready = asyncio.Event()
        pumping = asyncio.create_task(pump())
        # Don't put a prompt on screen until the greeting has been drawn, or the
        # first redraw lands on a prompt readline has already printed.
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
        except asyncio.TimeoutError:
            render.line("⚠️  the core accepted the socket but sent no greeting")
        start_reader(loop, lines)
        render.prompt_shown = True

        while True:
            line = await lines.get()
            if line is None:
                break
            text = line.strip()
            if not text:
                continue

            pending = getattr(render, "pending_ask", None)
            if pending:                      # answering the rollover question
                answer = text.lower()
                live = time.monotonic() <= getattr(render, "pending_until", 0.0)
                # Only an actual yes or no, and only while the core is still
                # listening for one. Everything else is what the person meant
                # to say: this branch sits above the command dispatch, so an
                # unanswered question used to eat /quit, /stop and /info too —
                # and eat them into a dead request_id, so there was not even an
                # error to see.
                if live and answer in YES + NO:
                    render.pending_ask = None
                    await ws.send_json({"t": "rollover_reply", "request_id": pending,
                                        "yes": answer in YES})
                    continue
                render.pending_ask = None
                if not live:
                    render.line("   (the rollover question timed out — the "
                                "session was kept)")

            if text in ("/quit", "/exit"):
                break
            if text == "/reasoning":
                render.show_reasoning = not render.show_reasoning
                render.line(f"\n🧠 Reasoning display: "
                            f"{'ON' if render.show_reasoning else 'OFF'}")
                continue
            if text == "/stop":
                await ws.send_json({"t": "stop"})
                continue
            if text.startswith("/steering"):
                args = text[len("/steering"):].strip()
                if not args:
                    render.line("   /steering needs text — /steering the right IP is 10.0.0.5")
                else:
                    await ws.send_json({"t": "steering", "text": args})
                continue
            if text.startswith("/"):
                name, _, args = text[1:].partition(" ")
                await ws.send_json({"t": "command", "name": name, "args": args,
                                    "request_id": "c1"})
                continue
            await ws.send_json({"t": "submit", "text": text})

        pumping.cancel()
        await ws.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="letsClaw terminal client")
    ap.add_argument("--session", "-s", default="terminal", help="session name to attach to")
    ap.add_argument("--url", help="core URL (default from config: core.bind/core.port)")
    ap.add_argument("--token", "-t",
                     help="the core.token shared secret (default: the CLAW_TOKEN env var, else core.token in the local config.yaml)")
    args = ap.parse_args()

    try:
        config = load_config()
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)
    core_cfg = config.get("core", {})
    host = core_cfg.get("bind", "127.0.0.1")
    url = args.url or f"http://{host}:{int(core_cfg.get('port', 8770))}"
    # Same precedence the Discord bot uses: an explicit secret wins, then
    # the environment, then the core.token of the config this client just
    # read — a client on the same machine as its core needs no extra step.
    token = args.token or os.environ.get("CLAW_TOKEN") \
        or str(core_cfg.get("token") or "").strip()

    install_completer(known_models(config))
    render = Renderer()

    print("\nletsClaw Terminal Chat")
    print(f"Connected to the core at {url}, session '{args.session}'.")
    # Kept in step with COMMANDS above by hand, which it had not been: /reload
    # and /steering shipped without ever reaching this line.
    print("Commands: " + ", ".join(sorted(COMMANDS)))
    print("(Tab completes commands and model names)")

    code = 0
    try:
        code = asyncio.run(run_client(url, args.session, render, token))
    except KeyboardInterrupt:
        pass
    except Exception:
        # The os._exit below runs from the finally whatever happens, which
        # means it also runs past an unhandled exception — bypassing the
        # default handler that would have printed the traceback and exited
        # non-zero. A crash looked exactly like a clean quit: same "Bye!",
        # same status 0, nothing to tell a script or a human otherwise.
        traceback.print_exc()
        code = 1
    finally:
        print("\n\nBye! 👋  (the core keeps running)" if not code
              else "\n\nExiting after an error.")
        sys.stdout.flush()
        # The stdin thread is parked in a blocking tty read and cannot be joined.
        os._exit(code)


if __name__ == "__main__":
    main()
