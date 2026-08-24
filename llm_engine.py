"""LLM engine — OpenAI-compatible wrapper for chat completions.

Supports vllm (localhost) and any OpenAI-compatible API.
Config-driven: provider, base_url, api_key come from config.yaml.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

logger = logging.getLogger("letclaw.engine")


@dataclass
class ChatResult:
    """Structured chat response: text and/or tool calls."""
    text: str
    tool_calls: list = field(default_factory=list)  # [{"id", "name", "arguments"(json str)}]
    finish_reason: str | None = None

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


class LLMEngine:
    """Wrapper around async OpenAI client for chat completions."""

    def __init__(self, base_url: str, api_key: str, default_model: str, **kwargs):
        base_url = base_url.rstrip("/")
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "sk-not-needed")
        self.default_model = default_model
        self.extra_params = kwargs

    def get_model_params(self, model_override: str | None = None) -> dict:
        """Return kwargs dict for api.chat.completions.create()."""
        model = model_override or self.default_model
        params = {"model": model}
        params.update({k: v for k, v in self.extra_params.items() if v is not None})
        return params

    async def chat(self, messages: list, model: str | None = None) -> str:
        """Send a chat completion request. Returns the assistant message text."""
        params = self.get_model_params(model)
        try:
            response = await self.client.chat.completions.create(
                messages=messages,
                **params,
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"LLM chat failed: {e}")
            raise

    async def chat_with_stream(self, messages: list, model: str | None = None):
        """Generator that yields chunks from a streaming completion."""
        params = self.get_model_params(model)
        try:
            response = await self.client.chat.completions.create(
                messages=messages,
                stream=True,
                **params,
            )
            async for chunk in response:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            logger.error(f"LLM stream failed: {e}")
            raise

    async def chat_with_tools(self, messages: list, model: str | None = None,
                              tools: list | None = None,
                              on_text=None) -> ChatResult:
        """Chat completion with optional tool calling.

        on_text: if given (and streaming is used), called with each text
        chunk as it arrives; tool-call deltas are accumulated silently.
        Returns a ChatResult with .text and .tool_calls.
        """
        params = self.get_model_params(model)
        if tools:
            params["tools"] = tools
        use_stream = on_text is not None
        try:
            if not use_stream:
                response = await self.client.chat.completions.create(
                    messages=messages, **params)
                choice = response.choices[0]
                msg = choice.message
                tool_calls = []
                for tc in (msg.tool_calls or []):
                    tool_calls.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "",
                    })
                return ChatResult(msg.content or "", tool_calls, choice.finish_reason)

            response = await self.client.chat.completions.create(
                messages=messages, stream=True, **params)
            text_parts = []
            pending = {}  # index -> accumulated tool call
            finish = None
            try:
                async for chunk in response:
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if choice.finish_reason:
                        finish = choice.finish_reason
                    if delta.content:
                        text_parts.append(delta.content)
                        on_text(delta.content)
                    for tc in (delta.tool_calls or []):
                        slot = pending.setdefault(
                            tc.index, {"id": None, "name": None, "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        fn = tc.function
                        if fn and fn.name:
                            slot["name"] = fn.name
                        if fn and fn.arguments:
                            slot["arguments"] += fn.arguments
            finally:
                # Release the underlying connection (not always auto-closed)
                try:
                    await response.close()
                except Exception:
                    pass
            tool_calls = [pending[i] for i in sorted(pending)]
            return ChatResult("".join(text_parts), tool_calls, finish)
        except Exception as e:
            logger.error(f"LLM tools call failed: {e}")
            raise

    async def close(self):
        """Release HTTP client resources (call once when the session ends)."""
        await self.client.close()

    async def load_behavior(self, path: str | Path) -> str:
        """Load a behavior file (.md) and return its contents."""
        p = Path(path) if isinstance(path, str) else path
        if not p.exists():
            logger.warning(f"Behavior file not found: {path}")
            return ""
        return p.read_text()


# ─── Convenience helpers ───────────────────────────────────────────────

def build_system_prompt(
    bot_name: str = "letsClaw",
    behavior: str = "",
) -> str:
    """Build a system prompt from config fragments."""
    parts = [
        f"You are {bot_name}, a lightweight technical agent engine.",
        "",
    ]
    if behavior:
        parts.append("=== BEHAVIOR ===")
        parts.append(behavior)
        parts.append("")
    return "\n".join(parts)
