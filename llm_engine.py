"""LLM engine — OpenAI-compatible wrapper for chat completions.

Supports vllm (localhost) and any OpenAI-compatible API.
Config-driven: base_url, api_key and sampling parameters come from the
model's own entry under models: in config.yaml.
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
    reasoning: str = ""  # model's reasoning/thinking content (thinking models)
    # Server-reported token usage; None when the server doesn't supply it.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)

    @property
    def context_used(self) -> int | None:
        """Exact size of the context after this turn, or None if unreported."""
        if self.prompt_tokens is None:
            return None
        return self.prompt_tokens + (self.completion_tokens or 0)


class LLMEngine:
    """Wrapper around async OpenAI client for chat completions."""

    def __init__(self, base_url: str, api_key: str, default_model: str, **kwargs):
        base_url = base_url.rstrip("/")
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "sk-not-needed")
        self.default_model = default_model
        self.extra_params = kwargs
        # Ask for streamed usage until a server tells us it doesn't understand.
        self._usage_opt = True

    def get_model_params(self, model_override: str | None = None) -> dict:
        """Return kwargs dict for api.chat.completions.create()."""
        model = model_override or self.default_model
        params = {"model": model}
        params.update({k: v for k, v in self.extra_params.items() if v is not None})
        return params

    async def chat(self, messages: list, model: str | None = None, **overrides) -> str:
        """Send a chat completion request. Returns the assistant message text.

        **overrides adjust request parameters for this one call (e.g. a small
        max_tokens for a summary). get_model_params() hands back a fresh dict,
        so the engine's configured defaults are never touched.
        """
        params = self.get_model_params(model)
        params.update(overrides)
        try:
            response = await self.client.chat.completions.create(
                messages=messages,
                **params,
            )
            return response.choices[0].message.content or ""
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
                              on_text=None, on_reasoning=None) -> ChatResult:
        """Chat completion with optional tool calling.

        on_text: if given (and streaming is used), called with each text
        chunk as it arrives; tool-call deltas are accumulated silently.
        on_reasoning: same, but for reasoning/thinking chunks (thinking models).
        Returns a ChatResult with .text, .tool_calls and .reasoning.
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
                usage = getattr(response, "usage", None)
                return ChatResult(msg.content or "", tool_calls, choice.finish_reason,
                                  reasoning=getattr(msg, "reasoning_content", None) or "",
                                  prompt_tokens=getattr(usage, "prompt_tokens", None),
                                  completion_tokens=getattr(usage, "completion_tokens", None))

            create_kw = dict(messages=messages, stream=True, **params)
            if self._usage_opt:
                # Asks for a final usage-only chunk; vLLM and llama.cpp both honour it.
                create_kw["stream_options"] = {"include_usage": True}
            try:
                response = await self.client.chat.completions.create(**create_kw)
            except Exception as e:
                if not self._usage_opt or "stream_options" not in str(e):
                    raise
                logger.warning("server rejected stream_options; token counts will be estimated")
                self._usage_opt = False
                create_kw.pop("stream_options")
                response = await self.client.chat.completions.create(**create_kw)
            text_parts = []
            reason_parts = []
            pending = {}  # index -> accumulated tool call
            finish = None
            usage = None
            try:
                async for chunk in response:
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    if not chunk.choices:
                        continue  # the usage-only chunk carries an empty choices list
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if choice.finish_reason:
                        finish = choice.finish_reason
                    rchunk = getattr(delta, "reasoning_content", None)
                    if rchunk:
                        reason_parts.append(rchunk)
                        if on_reasoning:
                            on_reasoning(rchunk)
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
            return ChatResult("".join(text_parts), tool_calls, finish,
                              reasoning="".join(reason_parts),
                              prompt_tokens=getattr(usage, "prompt_tokens", None),
                              completion_tokens=getattr(usage, "completion_tokens", None))
        except Exception as e:
            logger.error(f"LLM tools call failed: {e}")
            raise

    async def close(self):
        """Release HTTP client resources (call once when the session ends).

        Swallows teardown noise: httpx/httpcore pools can raise while closing
        idle keep-alive connections after the session is over (e.g. httpcore2
        'generator didn't stop after athrow()' during pool shutdown).
        """
        try:
            await self.client.close()
        except Exception as e:
            logger.debug(f"Ignoring error while closing LLM client: {e}")

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
