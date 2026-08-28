#!/usr/bin/env python3
"""letsClaw Terminal Chat — interactive LLM chat in your terminal."""

import argparse
import asyncio
import sys
from pathlib import Path

try:
    import readline  # enables arrow-key / history editing in input() on Linux & macOS
except ImportError:
    readline = None  # Windows has no readline; input() still works, just without arrow keys

import yaml

import session
from llm_engine import LLMEngine
from token_counter import count_messages, count_text, count_tools
from tools import build_tools
from ui import thinking_indicator

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config():
    if not CONFIG_PATH.exists():
        print(f"❌ No config.yaml at {CONFIG_PATH}")
        print("   Copy config.example.yaml → config.yaml and customize.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def known_models(config):
    models_cfg = config.get("models", {})
    return [k for k in models_cfg if k != "default_model"]


PROMPT = "👤 "  # shared with the completer, which redraws it after listing matches

DEFAULT_CONTEXT_LENGTH = 12000  # when a model entry doesn't state its own

# Rollover defaults, overridable under conversation: in config.yaml
DEFAULT_ROLLOVER_PCT = 90
DEFAULT_ROLLOVER_MODE = "auto"  # auto | ask | off
HANDOFF_MAX_TOKENS = 800   # a handoff is a paragraph or two, not an essay
HANDOFF_MIN_TOKENS = 256   # below this there is no room to say anything useful
HANDOFF_MARGIN = 200       # stay clear of the window edge

# Servers that report usage give us the exact number. For those that don't, the
# local estimate runs low — measured at ~0.68 of the real prompt for Qwen3 on
# llama.cpp, because cl100k mis-sizes the tokens and neither the chat-template
# markers nor the server's own rendering of the tool schemas are visible to us.
# Scale the guess up: rolling over early costs a summary, rolling over late
# costs a rejected request.
ESTIMATE_SCALE = 1.6

COMMANDS = {
    "/quit": "leave letsClaw",
    "/exit": "leave letsClaw",
    "/clear": "discard the conversation and start over",
    "/new": "archive the session, keep the objective, start fresh",
    "/model": "list models, or /model <name> to switch",
    "/info": "recent history and context usage",
    "/behavior": "print the loaded behavior file",
    "/reasoning": "toggle live display of the model's thinking",
}


def _bind_tab_to_complete():
    """Bind TAB to readline's completion function.

    CPython leaves plain TAB inserting a literal tab, so completion is dead
    until we rebind it. The two readline flavors want different syntax, and
    they do not politely ignore each other's: hand GNU readline the libedit
    form and it binds the letter 'b' instead, so pick one.
    """
    if "libedit" in (readline.__doc__ or ""):  # macOS system Python
        readline.parse_and_bind("bind ^I rl_complete")
    else:  # GNU readline
        readline.parse_and_bind("tab: complete")


def install_completer(model_names):
    """Tab completion: /commands, and model names after '/model '. No-op without readline."""
    if readline is None:
        return
    # Whitespace-only delimiters so '/reaso' reaches the completer as one word
    # ('/' and '-' are ordinary characters in command and model names).
    readline.set_completer_delims(" \t\n")
    _bind_tab_to_complete()

    def candidates(text):
        """What may follow the cursor, decided by which word we're completing."""
        prior = readline.get_line_buffer()[:readline.get_begidx()].split()
        if not prior:  # first word of the line — a command
            return [c for c in COMMANDS if c.startswith(text)]
        if prior == ["/model"]:  # its single argument — a configured model
            return [m for m in model_names if m.startswith(text)]
        return []  # plain prose, or an argument we have nothing to offer for

    matches = []

    def completer(text, state):
        # readline asks for match 0, 1, 2 … until None; compute once on state 0.
        if state == 0:
            try:
                # Trailing space so an argument can be typed straight after the
                # command; CPython zeroes readline's own append character.
                matches[:] = [c + " " for c in candidates(text)]
            except Exception:
                matches[:] = []  # readline swallows exceptions — degrade to "no match"
        return matches[state] if state < len(matches) else None

    def show_matches(substitution, shown, longest):
        """Annotate the ambiguous-match listing with what each command does."""
        sys.stdout.write("\n")
        for m in (c.rstrip() for c in shown):
            desc = COMMANDS.get(m)
            sys.stdout.write(f"  {m.ljust(longest + 2)}{desc}\n" if desc else f"  {m}\n")
        # readline.redisplay() won't repaint after our direct writes, so put the
        # prompt and the half-typed line back by hand.
        sys.stdout.write(PROMPT + readline.get_line_buffer())
        sys.stdout.flush()

    readline.set_completer(completer)
    readline.set_completion_display_matches_hook(show_matches)


async def make_engine(config, model_name=None):
    """Build an engine for one configured model (registry name).

    Model entries are self-contained — each names its own server — so nothing
    is inherited from elsewhere in the config. Raises ValueError for an unknown
    name or an entry missing base_url. Async so it can be called both at
    startup and from inside the running REPL (runtime /model switching).
    """
    models_cfg = config.get("models", {})
    name = model_name or models_cfg.get("default_model")
    entry = models_cfg.get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"Unknown model '{name}'. Configured: {', '.join(known_models(config)) or '(none)'}")
    base_url = entry.get("base_url")
    if not base_url:
        raise ValueError(f"Model '{name}' has no base_url — every entry must name the server that serves it.")
    api_key = entry.get("api_key", "")
    engine = LLMEngine(
        base_url=base_url, api_key=api_key,
        default_model=entry.get("provider_name", name),
        temperature=entry.get("temperature"),
        max_tokens=entry.get("max_tokens"),
        top_p=entry.get("top_p"),
    )
    # Not a request parameter — kept on the engine for the context budget display.
    engine.context_length = entry.get("context_length", DEFAULT_CONTEXT_LENGTH)
    # Load behavior file
    behavior_file = entry.get("behavior_file", "models/default.md")
    engine.default_behavior = await engine.load_behavior(behavior_file)
    return engine, name


SYSTEM_PROMPT = """You are letsClaw, a lightweight technical agent engine.
You are helpful, concise, and direct. Use markdown for code blocks.
"""

TOOLS_NOTE = """
=== TOOLS ===
You can call tools to inspect and modify the real environment:
- exec: run shell commands
- read_file / write_file / list_dir: file access
Prefer tools over guessing when asked about commands, files, or system state.
Work iteratively: call a tool, inspect the result, continue until the task is
done, then give one concise final answer. Do not stop to ask for permission
on read-only operations."""


def new_history(engine, tools_note="", carryover=""):
    """Build a fresh history with a single system message (vLLM requires system at index 0 only).

    carryover is the handoff from a rolled-over session. It has to live inside
    this one system message rather than beside it, for the same index-0 reason.
    """
    content = SYSTEM_PROMPT
    behavior = getattr(engine, "default_behavior", "")
    if behavior:
        content += "\n=== BEHAVIOR ===\n" + behavior
    if tools_note:
        content += "\n" + tools_note
    if carryover:
        content += "\n\n=== CONTINUED SESSION ===\n" + carryover
    return [{"role": "system", "content": content}]


async def roll_over(engine, history, model_name, config, tools, trip, used=None, reason=""):
    """Archive the session, distil a handoff, and return a fresh history.

    Order matters: the transcript is written before anything that can fail, so a
    broken summariser or a full disk costs the summary, never the conversation.
    """
    budget = getattr(engine, "context_length", DEFAULT_CONTEXT_LENGTH)
    used = used if used is not None else count_messages(history)
    print(f"\n\U0001f504 Rolling over{reason}.")

    try:
        transcript = session.save_transcript(history, model_name, config)
        print(f"   \U0001f4be Transcript  {transcript}")
    except OSError as e:
        transcript = None
        print(f"   \u26a0\ufe0f  Could not save transcript: {e}")

    # The summary request is itself a near-full prompt, so its reply has to fit
    # in what's left of the window.
    summary_max = min(HANDOFF_MAX_TOKENS, budget - used - HANDOFF_MARGIN)
    handoff = ""
    if summary_max >= HANDOFF_MIN_TOKENS:
        ind = thinking_indicator("summarising")
        ind.start()
        try:
            handoff = await session.build_handoff(engine, history, max_tokens=summary_max)
        except Exception as e:
            print(f"   \u26a0\ufe0f  Handoff failed ({e}) — carrying the last exchange only")
        finally:
            ind.stop()
    else:
        print("   \u26a0\ufe0f  No headroom left to summarise — carrying the last exchange only")

    if handoff:
        try:
            path = session.append_handoff(handoff, model_name, transcript, config)
            print(f"   \U0001f9e0 Handoff     {path}")
        except OSError as e:
            print(f"   \u26a0\ufe0f  Could not append handoff: {e}")

    fresh = new_history(engine, TOOLS_NOTE if tools else "",
                        carryover=session.format_carryover(handoff, transcript))
    tail = session.last_exchange(history)
    if tail and (not trip or count_messages(fresh + tail) < trip):
        fresh += tail
    elif tail:
        print("   \u26a0\ufe0f  Dropped the carried exchange — it would not fit the new window")
    print(f"   \u2728 New session  {count_messages(fresh)}/{budget} tok")
    return fresh


def make_on_text(ind, first: dict):
    """Build a streaming callback: first chunk clears the spinner, then print."""
    def on_text(chunk):
        if first["v"]:
            ind.stop()  # clear spinner line, record TTFT
            sys.stdout.write("\n🐱 ")
            sys.stdout.flush()
            first["v"] = False
        sys.stdout.write(chunk)
        sys.stdout.flush()
    return on_text


async def chat(engine, model_name, config, tools):
    history = new_history(engine, TOOLS_NOTE if tools else "")
    conv_cfg = config.get("conversation", {})
    budget = engine.context_length  # per-model; re-read whenever /model switches
    rollover_pct = int(conv_cfg.get("rollover_at_percent", DEFAULT_ROLLOVER_PCT) or 0)
    rollover_mode = str(conv_cfg.get("rollover_mode", DEFAULT_ROLLOVER_MODE)).lower()
    if rollover_mode not in ("auto", "ask", "off"):
        print(f"⚠️  Unknown rollover_mode {rollover_mode!r} — using 'auto'.")
        rollover_mode = "auto"
    declined_at = None   # ask mode: context size when the user last said no
    warned_over = False  # off mode: warn once per session, not every turn
    show_reasoning = False  # /reasoning toggles live display of thinking content
    tools_cfg = config.get("tools", {})
    schemas = [t.spec for t in tools]
    tools_tok = count_tools(schemas)  # schemas ride along on every request

    def estimate(msgs):
        """Deliberately conservative context size, for servers that hide usage."""
        return int((count_messages(msgs) + tools_tok) * ESTIMATE_SCALE)
    max_tool_rounds = int(tools_cfg.get("max_iterations", 8)) if tools else 1

    try:
        print(f"\n🤖 Model: {model_name} @ {engine.client.base_url}")
        print("=" * 50)
    except Exception:
        pass

    print("\nletsClaw Terminal Chat")
    print("Type your message and press Enter.")
    print("Commands: /quit, /clear, /new, /model [name], /info, /behavior, /reasoning")
    print("(Tab completes commands and model names)")
    print("💬 ")

    while True:
        try:
            user_input = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nBye! 👋")
            break

        if not user_input:
            continue

        # Commands
        if user_input in ("/quit", "/exit"):
            print("\nBye! 👋")
            break
        elif user_input == "/clear":
            history = new_history(engine, TOOLS_NOTE if tools else "")
            declined_at, warned_over = None, False
            print("\n🧹 History cleared (discarded — /new archives it instead).")
            continue
        elif user_input == "/new":
            trip = int(budget * rollover_pct / 100) if budget and rollover_pct else 0
            history = await roll_over(engine, history, model_name, config, tools,
                                      trip, reason=" on request")
            declined_at, warned_over = None, False
            continue
        elif user_input == "/model" or user_input.startswith("/model "):
            parts = user_input.split()
            if len(parts) == 2:
                # Switch model at runtime
                try:
                    new_engine, new_name = await make_engine(config, parts[1])
                except ValueError as e:
                    print(f"\n❌ {e}")
                else:
                    await engine.close()
                    engine = new_engine
                    model_name = new_name
                    budget = engine.context_length  # the new model's own window
                    # Refresh system/behavior prompt, keep the conversation
                    sys_msg = new_history(new_engine, TOOLS_NOTE if tools else "")[0]
                    history = [sys_msg] + [m for m in history if m["role"] != "system"]
                    print(f"\n🤖 Switched to {new_name} @ {engine.client.base_url}")
                    print(f"   Conversation kept; system/behavior prompt refreshed."
                          f" Context budget now {budget} tok.")
            else:
                print(f"\n🤖 Current: {model_name} @ {engine.client.base_url}")
                print(f"📋 Configured models: {', '.join(known_models(config)) or '(none)'}")
                print(f"   Switch now: /model <name>")
                try:
                    resp = await engine.client.models.list()
                    available = [m.id for m in resp.data[:10]]
                    print(f"📋 On this server: {', '.join(available)}")
                except Exception as e:
                    print(f"  (Could not list server models: {e})")
            continue
        elif user_input == "/info":
            print(f"\n🤖 Model: {model_name}")
            print(f"💬 History: {len(history)} messages")
            print(f"📜 Recent history:")
            for i in range(max(0, len(history) - 5), len(history)):
                msg = history[i]
                role = msg["role"]
                tok = count_text(msg["content"])
                content = msg["content"][:120] + ("..." if len(msg["content"]) > 120 else "")
                emoji = {"system": "🤖", "user": "👤", "tool": "🔧"}.get(role, "🐱")
                print(f"  [{i}] {emoji} {role}: {content} ({tok} tok)")
            total = estimate(history)
            pct = (total * 100) // budget if budget else 0
            print(f"  ⚡ Context: ~{total}/{budget} tok ({pct}%)   "
                  f"(~ conservative estimate; {tools_tok} tok of tool schemas)")
            if rollover_pct:
                trip = int(budget * rollover_pct / 100) if budget else 0
                print(f"  🔄 Rollover: {rollover_mode} at {rollover_pct}% ({trip} tok)")
            else:
                print("  🔄 Rollover: disabled  (/new still works)")
            continue
        elif user_input == "/behavior":
            if hasattr(engine, 'default_behavior') and engine.default_behavior:
                print(f"\n📄 Behavior file:\n{engine.default_behavior}")
            else:
                print("\nNo behavior file loaded.")
            continue
        elif user_input == "/reasoning":
            show_reasoning = not show_reasoning
            print(f"\n🧠 Reasoning display: {'ON' if show_reasoning else 'OFF'}"
                  f" (reasoning tokens are always counted separately in the stats line)")
            continue

        # Add user message
        history.append({"role": "user", "content": user_input})
        user_tok = count_text(user_input)

        # Agent loop: execute tool calls until the model gives a plain answer
        tools_used = 0
        last_ttft = None
        last_usage = None  # server-reported context size; None -> fall back to estimate
        full_response = ""
        had_error = False
        reasoning_parts = []
        try:
            for _ in range(max_tool_rounds):
                ind = thinking_indicator()
                ind.start()
                first = {"v": True}
                rfirst = {"v": True}
                def on_reasoning(chunk, ind=ind, rf=rfirst):
                    if rf["v"]:  # first thinking chunk: clear spinner, open the 🧠 line
                        ind.stop()
                        sys.stdout.write("\n🧠 ")
                        sys.stdout.flush()
                        rf["v"] = False
                    sys.stdout.write("\x1b[2m" + chunk + "\x1b[0m")  # dim
                    sys.stdout.flush()
                result = await engine.chat_with_tools(
                    history, tools=schemas,
                    on_text=make_on_text(ind, first),
                    on_reasoning=on_reasoning if show_reasoning else None)
                ind.stop()
                if ind.ttft is not None:
                    last_ttft = ind.ttft
                if result.context_used is not None:
                    last_usage = result.context_used
                if result.reasoning:
                    reasoning_parts.append(result.reasoning)
                if not first["v"] or not rfirst["v"]:  # something was streamed — terminate the line
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                if not result.wants_tool:
                    full_response = result.text
                    break
                # Record the assistant's tool-call turn
                history.append({
                    "role": "assistant",
                    "content": result.text or "",
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in result.tool_calls
                    ],
                })
                for tc in result.tool_calls:
                    tool = next((t for t in tools if t.name == tc["name"]), None)
                    if tool is None:
                        out = f"error: tool '{tc['name']}' is not enabled"
                    else:
                        out = await tool.run(tc["arguments"])
                    tools_used += 1
                    print(f"⚙️ {tc['name']} {tc['arguments'][:150]}")
                    print(f"   ↳ {len(out)} chars")
                    history.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
            else:
                # Tool budget exhausted — force a final answer without tools
                print(f"⚠️ {max_tool_rounds} tool rounds used — asking for a final answer…")
                ind = thinking_indicator()
                ind.start()
                first = {"v": True}
                result = await engine.chat_with_tools(history, on_text=make_on_text(ind, first))
                ind.stop()
                if ind.ttft is not None:
                    last_ttft = ind.ttft
                if result.context_used is not None:
                    last_usage = result.context_used
                if not first["v"]:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                if result.reasoning:
                    reasoning_parts.append(result.reasoning)
                full_response = result.text
        except Exception as e:
            had_error = True
            print(f"\n❌ Agent error: {e}")
            print()
            if tools_used == 0:
                # Nothing was executed — try a plain non-streaming reply
                try:
                    full_response = await engine.chat(history)
                    print(f"🐱 {full_response}")
                    print()
                    had_error = False
                except Exception as e2:
                    print(f"❌ Error: {e2}")
                    print()
                    history.pop()  # Remove the failed user message
                    continue
            # If tools already ran, the history is consistent — keep it

        if not had_error:
            if full_response:
                history.append({"role": "assistant", "content": full_response})
                asst_tok = count_text(full_response)
                reason_tok = count_text("".join(reasoning_parts))
                rtok = f" + reasoning {reason_tok} tok" if reason_tok else ""
                total = last_usage if last_usage is not None else estimate(history)
                approx = "" if last_usage is not None else "~"  # ~ marks an estimate
                pct = (total * 100) // budget if budget else 0
                warn = "  ⚠️ over budget" if budget and total > budget else ""
                ttft = f" ttft {last_ttft:.1f}s ·" if last_ttft is not None else ""
                tparts = f" · {tools_used} tool call{'s' if tools_used != 1 else ''}" if tools_used else ""
                print(f"⚡{ttft} user {user_tok} + assistant {asst_tok} tok{rtok}{tparts} · context {approx}{total}/{budget} ({pct}%){warn}")
            else:
                print("🐱 (no answer — the model likely burned its token budget on reasoning; try again)")

        # Roll over, or trim. Safe here and nowhere earlier: the tool loop has
        # fully drained, so no tool_call/tool_result pair can be split.
        trip = int(budget * rollover_pct / 100) if budget and rollover_pct else 0
        rolled = False
        if trip and not had_error:
            used = last_usage if last_usage is not None else estimate(history)
            if used >= trip:
                pct_now = used * 100 // budget
                if rollover_mode == "auto":
                    history = await roll_over(engine, history, model_name, config, tools,
                                              trip, used, reason=f" — context at {pct_now}%")
                    rolled = True
                elif rollover_mode == "ask":
                    # Only re-ask once the context has grown meaningfully since a
                    # refusal, otherwise every remaining turn ends in a prompt.
                    if declined_at is None or used >= declined_at + max(1, budget // 20):
                        print(f"\n⚠️  Context at {used}/{budget} ({pct_now}%).")
                        try:
                            answer = input("   Roll over to a new session? [y/N] ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            answer, _ = "n", print()
                        if answer in ("y", "yes"):
                            history = await roll_over(engine, history, model_name, config,
                                                      tools, trip, used,
                                                      reason=f" — context at {pct_now}%")
                            rolled = True
                        else:
                            declined_at = used
                            print("   Keeping it. /new rolls over whenever you're ready.")
                elif not warned_over:  # off
                    print(f"   ⚠️ past {rollover_pct}% of the window — /new starts a fresh session")
                    warned_over = True
        if rolled:
            declined_at, warned_over = None, False
            # A window too small to hold even a fresh session would otherwise roll
            # over on every single turn. Say so once and stop trying.
            if trip and count_messages(history) >= trip:
                print(f"   ⚠️  The fresh session is already past {rollover_pct}% of "
                      f"{budget} tok — context_length is too small for rollover to help.")
                print("      Automatic rollover disabled for this session; /new still works.")
                rollover_pct = 0
        else:
            # Trim history to prevent overflow
            max_hist = conv_cfg.get("max_history_messages", 100)
            if len(history) > max_hist:
                # Keep system messages + recent conversation
                system_msgs = [m for m in history if m["role"] == "system"]
                conversation_msgs = [m for m in history if m["role"] != "system"]
                keep = conversation_msgs[-max_hist:] if len(conversation_msgs) > max_hist else conversation_msgs
                # Never start with a tool result whose assistant tool-call was trimmed
                while keep and keep[0]["role"] == "tool":
                    keep = keep[1:]
                history = system_msgs + keep

    await engine.close()


def main():
    parser = argparse.ArgumentParser(description="letsClaw Terminal Chat")
    parser.add_argument("--model", "-m", help="Model name from the models: registry in config.yaml")
    args = parser.parse_args()

    config = load_config()
    install_completer(known_models(config))
    tools = build_tools(config.get("tools", {}))
    try:
        engine, model = asyncio.run(make_engine(config, args.model))
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    asyncio.run(chat(engine, model, config, tools))


if __name__ == "__main__":
    main()
