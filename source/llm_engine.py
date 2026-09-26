"""LLM engine — OpenAI-compatible wrapper for chat completions.

Supports vllm (localhost) and any OpenAI-compatible API.
Config-driven: base_url, api_key and sampling parameters come from the
model's own entry under models: in config.yaml.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from openai import AsyncOpenAI

logger = logging.getLogger("letclaw.engine")

# The thinking block a resumed round is handed back inside. A pad is re-sent as
# an unclosed THINK_OPEN + pad, so the model carries on mid-sentence instead of
# starting the question over; THINK_CLOSE ends the thinking phase and is what
# makes the answer's budget separate — past the tag the model cannot reason.
THINK_OPEN = "<think>\n"
THINK_CLOSE = "\n</think>\n\n"


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

    def __init__(self, base_url: str, api_key: str, default_model: str,
                 timeout: float | None = None, **kwargs):
        base_url = base_url.rstrip("/")
        # The client's default read timeout is 600 s — a round that runs longer
        # would die in the HTTP layer, so a long turn_timeout must reach here.
        # None = the client default.
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "sk-not-needed",
                                  timeout=timeout)
        self.default_model = default_model
        self.extra_params = kwargs
        # Ask for streamed usage until a server tells us it doesn't understand.
        self._usage_opt = True
        # Output cap for a thinking model: the max_tokens a forced-conclusion
        # round runs under when a round dies with finish_reason=length and no
        # text (see Session._turn). 0 = forced rounds keep the configured cap.
        self.max_output = 0
        # Hard wall for one turn, in seconds (models.<name>.turn_timeout).
        # 0 = no wall. The engine is per model, so the wall is per model.
        self.turn_timeout = 0
        # Whether a cut-off round is resumed at all (models.<name>.reasoning).
        # False = no pad: a round that runs out mid-thought is simply the answer.
        # There is no round budget — the pad grows until time or context stops it.
        self.reasoning = True
        # How a cut-off round is resumed (models.<name>.resume_mode):
        #   chat - OpenAI-compatible prefill (SGLang: continue_final_message)
        #   raw  - llama.cpp /apply-template + /completion
        #   off  - no resume; the base.md directive is the fallback
        #   auto - probe once, lazily, and cache the answer in _resume_probed
        self.resume_mode = "auto"
        self._resume_probed = None
        # Passed through as chat_template_kwargs. Qwen3 templates turn this into
        # a system directive; llama.cpp defaults to xhigh, which is a large part
        # of why rounds run out of budget mid-reasoning. None = leave it alone.
        self.reasoning_effort = None
        self.base_url = base_url
        self.api_key = api_key
        self._http_timeout = timeout
        self._session = None  # aiohttp session for the raw transport, made lazily

    def get_model_params(self, model_override: str | None = None) -> dict:
        """Return kwargs dict for api.chat.completions.create()."""
        model = model_override or self.default_model
        params = {"model": model}
        params.update({k: v for k, v in self.extra_params.items() if v is not None})
        if self.reasoning_effort:
            # Non-standard, so it travels in extra_body rather than as a kwarg.
            params["extra_body"] = {
                "chat_template_kwargs": {"reasoning_effort": self.reasoning_effort}}
        return params

    def _template_kwargs(self) -> dict:
        """chat_template_kwargs for the raw transport, or {}."""
        return ({"reasoning_effort": self.reasoning_effort}
                if self.reasoning_effort else {})

    async def _http(self):
        """Shared aiohttp session for the raw (llama.cpp) transport."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=self._http_timeout)
            headers = ({"Authorization": f"Bearer {self.api_key}"}
                       if self.api_key else {})
            # force_close: llama.cpp hangs up on idle keep-alive connections
            # between rounds, and a reused dead socket surfaces as
            # ServerDisconnectedError mid-turn. A fresh connection per round costs
            # nothing next to a generation call, and server-side prompt caching
            # (cache_prompt) is unaffected by it.
            self._session = aiohttp.ClientSession(
                timeout=timeout, headers=headers,
                connector=aiohttp.TCPConnector(force_close=True))
        return self._session

    async def resume_transport(self) -> str:
        """Which resume transport this server supports: 'chat', 'raw' or 'off'.

        Resolved once and cached. 'auto' asks the server rather than guessing:
        /apply-template is a llama.cpp endpoint, and llama.cpp ignores an
        assistant prefill on /v1/chat/completions, so its presence picks 'raw'.
        Anything else is assumed OpenAI-compatible enough for 'chat'; a server
        that turns out not to be degrades to the base.md directive on first use.
        """
        if self.resume_mode != "auto":
            return self.resume_mode
        if self._resume_probed is not None:
            return self._resume_probed
        mode = "chat"
        try:
            session = await self._http()
            async with session.post(f"{self.base_url.removesuffix('/v1')}/apply-template",
                                    json={"messages": [{"role": "user", "content": "x"}]},
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200 and "prompt" in (await r.json()):
                    mode = "raw"
        except Exception as e:
            logger.debug(f"resume probe fell back to chat: {e}")
        self._resume_probed = mode
        logger.info(f"resume transport for {self.default_model}: {mode}")
        return mode

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
                              on_text=None, on_reasoning=None,
                              overrides: dict | None = None,
                              prefill: str | None = None) -> ChatResult:
        """Chat completion with optional tool calling.

        on_text: if given (and streaming is used), called with each text
        chunk as it arrives; tool-call deltas are accumulated silently.
        on_reasoning: same, but for reasoning/thinking chunks (thinking models).
        overrides: request parameters for this one call only (e.g. a raised
        max_tokens for a forced-conclusion round); the engine's defaults are
        never touched.
        prefill: a partial assistant message the model continues instead of
        starting a new one — how a cut-off thinking pad is resumed mid-sentence.
        Returns a ChatResult with .text, .tool_calls and .reasoning.
        """
        params = self.get_model_params(model)
        if overrides:
            params.update(overrides)
        if tools:
            params["tools"] = tools
        if prefill is not None:
            # The server must continue this assistant message rather than open a
            # fresh one, so no generation prompt is added after it.
            messages = list(messages) + [{"role": "assistant", "content": prefill}]
            extra = dict(params.get("extra_body") or {})
            extra.update(continue_final_message=True, add_generation_prompt=False)
            params["extra_body"] = extra
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

    async def _apply_template(self, messages: list) -> str:
        """Render messages to a raw prompt with the server's own chat template.

        Saves hand-rolling the model's template. The Qwen3 template already ends
        the rendered prompt with the assistant turn's opening `<think>`, so a pad
        appends directly onto it.
        """
        session = await self._http()
        body = {"messages": messages}
        if kw := self._template_kwargs():
            body["chat_template_kwargs"] = kw
        async with session.post(f"{self.base_url.removesuffix('/v1')}/apply-template",
                                json=body) as r:
            r.raise_for_status()
            return (await r.json())["prompt"]

    async def _raw_complete(self, prompt: str, max_tokens: int | None,
                            on_text=None, on_reasoning=None,
                            in_reasoning: bool = True) -> ChatResult:
        """Stream llama.cpp /completion from a raw prompt.

        /completion has no tool calling and no reasoning_content — it returns one
        text stream with `</think>` inline — so chunks are split on that tag:
        before it is reasoning, after it is the answer. in_reasoning says which
        side of the tag the prompt left us on: a growing pad opens inside the
        block, a closing round has already written `</think>` and is pure answer.
        """
        session = await self._http()
        body = {"prompt": prompt, "stream": True, "cache_prompt": True}
        if max_tokens:
            body["n_predict"] = max_tokens
        if kw := self._template_kwargs():
            body["chat_template_kwargs"] = kw

        reason_parts, text_parts = [], []
        buf = ""           # holds back a partial `</think>` split across chunks
        finish, usage = None, {}
        close_tag = "</think>"

        async with session.post(f"{self.base_url.removesuffix('/v1')}/completion",
                                json=body) as r:
            r.raise_for_status()
            async for line in r.content:
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if d.get("stop"):
                    # stop_type is "limit" | "eos" | "word" | "none" on current
                    # builds; stopped_limit is the older spelling.
                    hit_cap = (d.get("stop_type") == "limit") or d.get("stopped_limit")
                    finish = "length" if hit_cap else "stop"
                    usage = {"prompt_tokens": d.get("tokens_evaluated"),
                             "completion_tokens": d.get("tokens_predicted")}
                chunk = d.get("content") or ""
                if not chunk:
                    continue
                if not in_reasoning:
                    text_parts.append(chunk)
                    if on_text:
                        on_text(chunk)
                    continue
                buf += chunk
                if close_tag in buf:
                    head, _, tail = buf.partition(close_tag)
                    # The newline before the tag is the tag's, not the pad's.
                    head = head.rstrip("\n")
                    if head:
                        reason_parts.append(head)
                        if on_reasoning:
                            on_reasoning(head)
                    in_reasoning, buf = False, ""
                    tail = tail.lstrip("\n")
                    if tail:
                        text_parts.append(tail)
                        if on_text:
                            on_text(tail)
                elif len(buf) > len(close_tag):
                    # Keep back only as much as a split tag could still occupy.
                    emit, buf = buf[:-len(close_tag)], buf[-len(close_tag):]
                    reason_parts.append(emit)
                    if on_reasoning:
                        on_reasoning(emit)
        if buf:  # stream ended mid-thought; the tail is reasoning
            reason_parts.append(buf)
            if on_reasoning:
                on_reasoning(buf)
        return ChatResult("".join(text_parts), [], finish,
                          reasoning="".join(reason_parts),
                          prompt_tokens=usage.get("prompt_tokens"),
                          completion_tokens=usage.get("completion_tokens"))

    async def resume_round(self, messages: list, pad: str, closing: bool,
                           max_tokens: int | None = None,
                           on_text=None, on_reasoning=None,
                           tools: list | None = None) -> ChatResult:
        """One round of a thinking pad: resume `pad` instead of re-asking.

        closing=False grows the pad — the block is handed back unclosed, so the
        model carries on mid-sentence. closing=True ends thinking: we write
        `</think>` ourselves, which both guarantees an answer (models often
        never close the block on their own) and makes max_tokens here buy answer
        tokens only, since the model is already past the tag.
        tools belong to the closing round alone, and only here on the chat
        transport: /completion streams plain text, so a tool call made there
        could not be parsed back out. The caller decides; raw simply ignores it.
        """
        prefill = THINK_OPEN + pad + (THINK_CLOSE if closing else "")
        mode = await self.resume_transport()
        if mode == "raw":
            prompt = await self._apply_template(messages) + pad + (
                THINK_CLOSE if closing else "")
            return await self._raw_complete(prompt, max_tokens,
                                            on_text=on_text, on_reasoning=on_reasoning,
                                            in_reasoning=not closing)
        return await self.chat_with_tools(
            messages, tools=tools, on_text=on_text, on_reasoning=on_reasoning,
            overrides={"max_tokens": max_tokens} if max_tokens else None,
            prefill=prefill)

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
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception as e:
                logger.debug(f"Ignoring error while closing raw HTTP session: {e}")

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
