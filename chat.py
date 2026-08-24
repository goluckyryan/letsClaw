#!/usr/bin/env python3
"""letsClaw Terminal Chat — interactive LLM chat in your terminal."""

import argparse
import asyncio
import sys
from pathlib import Path

try:
    import readline  # enables arrow-key / history editing in input() on Linux & macOS
except ImportError:
    pass  # Windows has no readline; input() still works, just without arrow keys

import yaml

from llm_engine import LLMEngine
from token_counter import count_messages, count_text
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


async def make_engine(config, model_name=None):
    """Build an engine for one configured model (registry name).

    Raises ValueError for unknown names. Async so it can be called both at
    startup and from inside the running REPL (runtime /model switching).
    """
    llm_cfg = config.get("llm", {})
    models_cfg = config.get("models", {})
    name = model_name or models_cfg.get("default_model")
    entry = models_cfg.get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"Unknown model '{name}'. Configured: {', '.join(known_models(config)) or '(none)'}")
    base_url = entry.get("base_url") or llm_cfg.get("base_url", "http://localhost:8000/v1")
    api_key = entry.get("api_key", llm_cfg.get("api_key", ""))
    engine = LLMEngine(
        base_url=base_url, api_key=api_key,
        default_model=entry.get("provider_name", name),
        temperature=entry.get("temperature"),
        max_tokens=entry.get("max_tokens"),
        top_p=entry.get("top_p"),
    )
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


def new_history(engine, tools_note=""):
    """Build a fresh history with a single system message (vLLM requires system at index 0 only)."""
    content = SYSTEM_PROMPT
    behavior = getattr(engine, "default_behavior", "")
    if behavior:
        content += "\n=== BEHAVIOR ===\n" + behavior
    if tools_note:
        content += "\n" + tools_note
    return [{"role": "system", "content": content}]


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
    budget = conv_cfg.get("context_window_tokens", 12000)
    tools_cfg = config.get("tools", {})
    schemas = [t.spec for t in tools]
    max_tool_rounds = int(tools_cfg.get("max_iterations", 8)) if tools else 1

    try:
        print(f"\n🤖 Model: {model_name} @ {engine.client.base_url}")
        print("=" * 50)
    except Exception:
        pass

    print("\nletsClaw Terminal Chat")
    print("Type your message and press Enter.")
    print("Commands: /quit, /clear, /model [name], /info, /behavior")
    print("💬 ")

    while True:
        try:
            user_input = input("👤 ").strip()
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
            print("\n🧹 History cleared.")
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
                    # Refresh system/behavior prompt, keep the conversation
                    sys_msg = new_history(new_engine, TOOLS_NOTE if tools else "")[0]
                    history = [sys_msg] + [m for m in history if m["role"] != "system"]
                    print(f"\n🤖 Switched to {new_name} @ {engine.client.base_url}")
                    print("   Conversation kept; system/behavior prompt refreshed.")
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
            total = count_messages(history)
            pct = (total * 100) // budget if budget else 0
            print(f"  ⚡ Context: {total}/{budget} tok ({pct}%)")
            continue
        elif user_input == "/behavior":
            if hasattr(engine, 'default_behavior') and engine.default_behavior:
                print(f"\n📄 Behavior file:\n{engine.default_behavior}")
            else:
                print("\nNo behavior file loaded.")
            continue

        # Add user message
        history.append({"role": "user", "content": user_input})
        user_tok = count_text(user_input)

        # Agent loop: execute tool calls until the model gives a plain answer
        tools_used = 0
        last_ttft = None
        full_response = ""
        had_error = False
        try:
            for _ in range(max_tool_rounds):
                ind = thinking_indicator()
                ind.start()
                first = {"v": True}
                result = await engine.chat_with_tools(history, tools=schemas,
                                                      on_text=make_on_text(ind, first))
                ind.stop()
                if ind.ttft is not None:
                    last_ttft = ind.ttft
                if not first["v"]:  # text was streamed this round — terminate the line
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
                if not first["v"]:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
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
                total = count_messages(history)
                pct = (total * 100) // budget if budget else 0
                warn = "  ⚠️ over budget" if budget and total > budget else ""
                ttft = f" ttft {last_ttft:.1f}s ·" if last_ttft is not None else ""
                tparts = f" · {tools_used} tool call{'s' if tools_used != 1 else ''}" if tools_used else ""
                print(f"⚡{ttft} user {user_tok} + assistant {asst_tok} tok{tparts} · context {total}/{budget} ({pct}%){warn}")
            else:
                print("🐱 (no answer — the model likely burned its token budget on reasoning; try again)")

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
    tools = build_tools(config.get("tools", {}))
    try:
        engine, model = asyncio.run(make_engine(config, args.model))
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    asyncio.run(chat(engine, model, config, tools))


if __name__ == "__main__":
    main()
