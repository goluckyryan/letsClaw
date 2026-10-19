"""LLM engine — OpenAI-compatible wrapper for chat completions.

Supports vllm (localhost) and any OpenAI-compatible API.
Config-driven: base_url, api_key and sampling parameters come from the
model's own entry under models: in config.yaml.
"""

import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from openai import AsyncOpenAI

logger = logging.getLogger("letclaw.engine")

# Tool-call markup, for the raw transport only. /completion has no tool-call
# parser — it hands back the chat template's own XML as plain text — so the
# parsing the chat endpoint would have done is done here instead. Two shapes are
# in the wild and which one appears is the template's choice, not ours:
#
#   <tool_call><function=exec><parameter=command>ls</parameter></function></tool_call>
#   <tool_call>{"name": "exec", "arguments": {"command": "ls"}}</tool_call>
#
# Deliberately confined to this transport. On the chat endpoint the server
# parses tool calls properly, so markup arriving as text there is the model
# *writing about* a tool call — prose, in an answer explaining itself — and
# executing that would be a real hazard.
TOOL_OPEN, TOOL_CLOSE = "<tool_call>", "</tool_call>"
_FUNC_RE = re.compile(r"<function=([^>\s]+)\s*>(.*)</function>", re.S)
_PARAM_RE = re.compile(r"<parameter=([^>\s]+)\s*>(.*?)</parameter>", re.S)

# The thinking block a resumed round is handed back inside. A pad is re-sent as
# an unclosed THINK_OPEN + pad, so the model carries on mid-sentence instead of
# starting the question over; THINK_CLOSE ends the thinking phase and is what
# makes the answer's budget separate — past the tag the model cannot reason.
THINK_OPEN = "<think>\n"
THINK_CLOSE = "\n</think>\n\n"


class InlineThinkSplitter:
    """Separate an inline thinking block from the answer that follows it.

    Reasoning is supposed to arrive in `reasoning_content`, apart from the
    answer, so that letsClaw can keep it out of history: the whole notepad
    design rests on thinking being free because it never joins the
    conversation. Two llama.cpp settings break that — `--reasoning-format
    none` sends the trace in `message.content`, and `deepseek-legacy` sends it
    in both — and on that path an unseparated blob is appended to history as
    answer text and re-sent on every later turn. A 20k-token thought then
    costs 20k tokens of context forever instead of nothing.

    `_raw_complete` has always split this; the chat transport never did, and
    the chat transport is the one that carries ordinary rounds. Both go
    through here now, because two implementations of one tag-scanner is two
    places for the boundary cases to disagree — and they already did: the
    copy in `_raw_complete` leaked the tag's own trailing newlines into the
    answer whenever the tag ended exactly on a chunk boundary.

    `start` says where the stream begins, which is not the same question on
    the two transports:

    | start | used by | meaning |
    |---|---|---|
    | `deciding` | chat | an opening tag *might* be first; look for one |
    | `reasoning` | raw, growing pad | already inside the block — the prefill wrote the opening tag |
    | `passthrough` | raw, closing round | already past `</think>`; everything is answer |

    In `deciding` it engages only when the stream *opens* with the tag — that
    is what the template emits — so an answer that merely mentions it is
    passed through untouched. Costs nothing on a correctly configured server:
    there is no opening tag, the first chunk decides that, and everything
    after is forwarded unexamined.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self, start="deciding"):
        # deciding -> reasoning -> after_close -> done, or deciding -> passthrough
        assert start in ("deciding", "reasoning", "passthrough"), start
        self._state = start
        self._buf = ""
        # The newline after an opening tag belongs to the tag. It may arrive
        # in a later chunk than the tag did, so this outlives one feed().
        self._eat_open_newlines = False

    def feed(self, chunk):
        """One chunk in; `(text, reasoning)` out, either possibly empty."""
        if self._state in ("passthrough", "done"):
            return chunk, ""
        if self._state == "after_close":
            # The close tag ended exactly on a chunk boundary, so the newlines
            # that belong to the tag arrive in the next one. Drop them, then
            # stop looking.
            chunk = chunk.lstrip("\n")
            if not chunk:
                return "", ""
            self._state = "done"
            return chunk, ""
        if self._state == "deciding":
            self._buf += chunk
            head = self._buf.lstrip()
            if not head:
                return "", ""                      # whitespace only so far
            if head.startswith(self.OPEN):
                self._state, self._buf = "reasoning", ""
                self._eat_open_newlines = True
                return self._consume(head[len(self.OPEN):])
            if self.OPEN.startswith(head):
                return "", ""                      # still could become the tag
            self._state, out, self._buf = "passthrough", self._buf, ""
            return out, ""
        return self._consume(chunk)

    def _consume(self, chunk):
        """In the thinking block: emit reasoning, watch for the close tag."""
        if self._eat_open_newlines:
            # Mirror of `after_close`, at the other end of the block: the
            # thought does not begin with the tag's own line break.
            chunk = chunk.lstrip("\n")
            if not chunk:
                return "", ""
            self._eat_open_newlines = False
        self._buf += chunk
        if self.CLOSE in self._buf:
            head, _, tail = self._buf.partition(self.CLOSE)
            self._buf = ""
            # The newlines around the tag are the tag's, not the thought's and
            # not the answer's. When the tail is empty they are still coming.
            tail = tail.lstrip("\n")
            self._state = "done" if tail else "after_close"
            return tail, head.rstrip("\n")
        if len(self._buf) > len(self.CLOSE):
            # Hold back as much as a split close tag could still occupy — and
            # the newlines in front of it, which are the tag's if the tag is
            # what follows. Emitting those eagerly is how a thought ends up
            # with a trailing blank line that belongs to the markup.
            cut = len(self._buf) - len(self.CLOSE)
            while cut and self._buf[cut - 1] == "\n":
                cut -= 1
            if cut:
                out, self._buf = self._buf[:cut], self._buf[cut:]
                return "", out
        return "", ""

    def finish(self):
        """Flush whatever is held back. `(text, reasoning)`."""
        buf, self._buf = self._buf, ""
        if self._state == "reasoning":
            return "", buf        # ended mid-thought; the remainder is thinking
        if self._state == "after_close":
            return "", ""         # only the tag's own trailing newlines left
        return buf, ""            # never saw an opening tag; it was always text


def _split_inline_think(content):
    """`InlineThinkSplitter` over one complete string. `(text, reasoning)`."""
    s = InlineThinkSplitter()
    t1, r1 = s.feed(content)
    t2, r2 = s.finish()
    return t1 + t2, r1 + r2


class RoundInterrupted(Exception):
    """A /steering arrived while this round was streaming: stop the round now.

    Not a failure — the turn keeps running, the partial answer is kept, and
    the correction is handed to the next round — so chat_with_tools re-raises
    it before its generic handler, which would log it as a failed model call.
    """


def _partial_suffix(text, marker):
    """How much of `text`'s tail could still be the start of `marker`.

    A marker arrives split across stream chunks as often as not, so that much
    has to be held back rather than forwarded and regretted.
    """
    for n in range(min(len(text), len(marker) - 1), 0, -1):
        if text.endswith(marker[:n]):
            return n
    return 0


def parse_tool_block(block):
    """One <tool_call> body as {"id", "name", "arguments"}, or None.

    `arguments` is a JSON string, matching what the OpenAI client hands back,
    so a recovered call is indistinguishable downstream from a parsed one —
    including to truncated_tool_calls(), which re-reads it to decide whether
    the call finished generating.
    """
    block = block.strip()
    if not block:
        return None
    call = None
    if m := _FUNC_RE.search(block):
        params = {k: v.strip("\n") for k, v in _PARAM_RE.findall(m.group(2))}
        call = (m.group(1), json.dumps(params))
    elif block.startswith("{"):
        try:
            d = json.loads(block)
            name = d.get("name")
            args = d.get("arguments")
            if name:
                call = (name, args if isinstance(args, str)
                        else json.dumps(args or {}))
        except (ValueError, TypeError):
            return None
    if not call:
        return None
    # An id the model never sees but the tool-result message must echo back.
    return {"id": secrets.token_hex(12), "name": call[0], "arguments": call[1]}


class ToolMarkupFilter:
    """Splits a raw text stream into text to show and tool calls to run.

    Fed chunk by chunk, it returns only the text safe to forward: a complete
    <tool_call> block is taken out and kept, and a partial one is held back
    until it is one or the other. That is what stops a client rendering half an
    XML tag, and what stopped Discord posting the whole call as prose.
    """

    def __init__(self):
        self.buf = ""
        self.blocks = []
        self.inside = False

    def feed(self, chunk):
        self.buf += chunk
        out = []
        while True:
            if self.inside:
                end = self.buf.find(TOOL_CLOSE)
                if end < 0:
                    # Still inside the call; nothing here is for the reader. A
                    # stray <tool_call> that never closes swallows the rest of
                    # the round, which is the safe way round: better to show
                    # nothing than to show markup.
                    return "".join(out)
                self.blocks.append(self.buf[:end])
                self.buf = self.buf[end + len(TOOL_CLOSE):]
                self.inside = False
                continue
            start = self.buf.find(TOOL_OPEN)
            if start >= 0:
                out.append(self.buf[:start])
                self.buf = self.buf[start + len(TOOL_OPEN):]
                self.inside = True
                continue
            keep = _partial_suffix(self.buf, TOOL_OPEN)
            cut = len(self.buf) - keep
            out.append(self.buf[:cut])
            self.buf = self.buf[cut:]
            return "".join(out)

    def finish(self):
        """Whatever is left once the stream ends.

        An unterminated block is dropped: it never became a tool call, and it
        is not something a reader should see either.
        """
        if self.inside:
            self.buf = ""
            return ""
        rest, self.buf = self.buf, ""
        return rest

    def tool_calls(self):
        calls = [parse_tool_block(b) for b in self.blocks]
        return [c for c in calls if c]


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
        # What the server actually does, as opposed to what config assumes.
        # Probed once, lazily; see capabilities().
        self._caps = None
        # Whether the chat transport looks for an inline thinking block in
        # message.content. True until the probe says otherwise, because the
        # failure it guards against — a whole trace appended to history as the
        # answer — is far worse than its own failure mode, which is an answer
        # that genuinely opens with the markup being read as a thought.
        # capabilities() turns it off once it has seen the server use the
        # dedicated channel, which is the only proof that the markup in an
        # answer is the answer's.
        self.split_inline_think = True
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

    async def capabilities(self) -> dict:
        """What this server actually does, against what the config assumes.

        Three of letsClaw's load-bearing assumptions are the server's to keep,
        and none of them were checked:

        - **reasoning lands in `reasoning_content`.** Under llama.cpp's
          `--reasoning-format none` it lands in `message.content` instead, and
          `chat_with_tools` does no <think> stripping on that path — so the
          whole trace streams as answer text and is appended to history. The
          context fills with thinking that was supposed to never be there, and
          nothing says so.
        - **`context_length` fits inside the server's window.** Configure more
          than the server was started with and every overflow is the server's
          to handle: a 400, or, with --context-shift, silent truncation.
        - **usage is reported.** When it is withheld every context figure is a
          local estimate carrying a deliberate +60% pad, which is a very
          different number to act on.

        Resolved once and cached, like resume_transport(): a diagnostic may
        not cost a round per turn. Every failure degrades to "unknown" rather
        than raising — this reports on a turn, it does not gate one.
        """
        if self._caps is not None:
            return self._caps
        caps = {"reasoning_channel": "unknown", "server_ctx": None,
                "usage_reported": None, "warnings": []}

        # llama.cpp's /props. default_generation_settings.n_ctx is the per-slot
        # window — ctx-size divided by --parallel — which is what one request
        # actually gets, so it beats any total reported alongside it. Anything
        # that is not llama.cpp simply has no such endpoint, and says nothing.
        try:
            session = await self._http()
            async with session.get(f"{self.base_url.removesuffix('/v1')}/props",
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    props = await r.json()
                    gen = props.get("default_generation_settings") or {}
                    caps["server_ctx"] = gen.get("n_ctx") or props.get("n_ctx")
        except Exception as e:
            logger.debug(f"/props probe failed for {self.default_model}: {e}")

        configured = getattr(self, "context_length", 0)
        if caps["server_ctx"] and configured > caps["server_ctx"]:
            caps["warnings"].append(
                f"context_length {configured:,} exceeds the server's window "
                f"({caps['server_ctx']:,}) — prompts past it are the server's "
                f"to drop, not ours")

        # One tiny completion, to see which field the thinking comes back in.
        # Skipped when the model is not a thinking model: there is no reasoning
        # channel to get wrong, and a round spent proving it is a round wasted.
        if self.reasoning:
            try:
                params = self.get_model_params()
                params.update(max_tokens=64, temperature=0)
                # Its own timeout: the client's is turn_timeout, which can be
                # hours, and a diagnostic that hangs /info is worse than one
                # that gives up.
                r = await self.client.with_options(timeout=30).chat.completions.create(
                    messages=[{"role": "user", "content": "In one word: 2+2?"}],
                    **params)
                msg = r.choices[0].message
                content = msg.content or ""
                thinking = getattr(msg, "reasoning_content", None) or ""
                inline = THINK_OPEN.strip() in content
                caps["reasoning_channel"] = (
                    "both" if thinking and inline else
                    "separate" if thinking else
                    "inline" if inline else "silent")
                usage = getattr(r, "usage", None)
                caps["usage_reported"] = bool(usage and usage.prompt_tokens)
            except Exception as e:
                logger.debug(f"reasoning probe failed for {self.default_model}: {e}")

        # The probe acts on what it found rather than only reporting it. The
        # splitter defaults on, so "inline"/"both" only confirms it; the useful
        # verdict is the other one. A server that put this model's thinking in
        # the dedicated field will not also inline it, so markup arriving in
        # content from here on is the answer's own and must be left alone —
        # which matters most in a session about software that writes about
        # these tags. "unknown" and "silent" keep the guard: neither is proof.
        if caps["reasoning_channel"] in ("separate",):
            self.split_inline_think = False
        if caps["reasoning_channel"] in ("inline", "both"):
            caps["warnings"].append(
                f"this server returns thinking in message.content "
                f"({caps['reasoning_channel']}) — letsClaw is separating it so "
                f"it stays out of the context, but fix it at the source: on "
                f"llama.cpp that is --reasoning-format, and it wants auto or "
                f"deepseek, not none or deepseek-legacy")
        if caps["usage_reported"] is False:
            caps["warnings"].append(
                "this server withholds token usage — every context figure is "
                f"a local estimate carrying the +60% pad, shown with a ~")

        self._caps = caps
        for w in caps["warnings"]:
            logger.warning(f"{self.default_model}: {w}")
        logger.info(f"capabilities for {self.default_model}: "
                    f"reasoning={caps['reasoning_channel']} "
                    f"server_ctx={caps['server_ctx']} "
                    f"usage={caps['usage_reported']}")
        return caps

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
                reasoning = getattr(msg, "reasoning_content", None) or ""
                # Defensive: a server configured to inline its thinking sends
                # the whole trace here, and letsClaw would append it to history
                # as the answer. No-op when the content has no opening tag, and
                # off entirely once capabilities() has seen a dedicated channel.
                text = msg.content or ""
                if self.split_inline_think:
                    text, inline = _split_inline_think(text)
                    if inline:
                        reasoning = f"{reasoning}{inline}" if reasoning else inline
                return ChatResult(text, tool_calls, choice.finish_reason,
                                  reasoning=reasoning,
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
            # Same guard as the non-streaming path above, applied per delta so
            # a close tag split across two chunks is still found. None once the
            # probe has proved the server has a channel of its own for this.
            splitter = InlineThinkSplitter() if self.split_inline_think else None
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
                        dtext, dreason = (splitter.feed(delta.content)
                                          if splitter else (delta.content, ""))
                        if dreason:
                            reason_parts.append(dreason)
                            if on_reasoning:
                                on_reasoning(dreason)
                        if dtext:
                            text_parts.append(dtext)
                            on_text(dtext)
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
            # Flush anything held back against a close tag that never came.
            ftext, freason = splitter.finish() if splitter else ("", "")
            if freason:
                reason_parts.append(freason)
                if on_reasoning:
                    on_reasoning(freason)
            if ftext:
                text_parts.append(ftext)
                on_text(ftext)
            tool_calls = [pending[i] for i in sorted(pending)]
            return ChatResult("".join(text_parts), tool_calls, finish,
                              reasoning="".join(reason_parts),
                              prompt_tokens=getattr(usage, "prompt_tokens", None),
                              completion_tokens=getattr(usage, "completion_tokens", None))
        except RoundInterrupted:
            # A /steering stopped this round mid-stream. Not a model failure — the
            # turn re-plans with the correction — so it is not logged as one.
            raise
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
        text stream with `</think>` inline — so chunks are split on that tag by
        the same InlineThinkSplitter the chat transport uses. in_reasoning says
        which side of the tag the prompt left us on: a growing pad opens inside
        the block, a closing round has already written `</think>` and is pure
        answer, so it looks for nothing at all.
        """
        session = await self._http()
        body = {"prompt": prompt, "stream": True, "cache_prompt": True}
        if max_tokens:
            body["n_predict"] = max_tokens
        if kw := self._template_kwargs():
            body["chat_template_kwargs"] = kw

        reason_parts, text_parts = [], []
        finish, usage = None, {}
        splitter = InlineThinkSplitter(
            start="reasoning" if in_reasoning else "passthrough")
        # Does for tool calls what the chat endpoint's parser would: takes them
        # out of the text on the way past, so they can be run instead of read.
        markup = ToolMarkupFilter()

        def emit_text(part):
            visible = markup.feed(part)
            if visible:
                text_parts.append(visible)
                if on_text:
                    on_text(visible)

        def emit_reasoning(part):
            reason_parts.append(part)
            if on_reasoning:
                on_reasoning(part)

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
                dtext, dreason = splitter.feed(chunk)
                if dreason:
                    emit_reasoning(dreason)
                if dtext:
                    emit_text(dtext)
        # Flush what was held back against a close tag that never arrived. The
        # splitter decides which side it falls on: mid-thought it is reasoning.
        ftext, freason = splitter.finish()
        if freason:
            emit_reasoning(freason)
        if ftext:
            emit_text(ftext)
        tail = markup.finish()
        if tail:
            text_parts.append(tail)
            if on_text:
                on_text(tail)
        calls = markup.tool_calls()
        if calls:
            logger.info("recovered %d tool call(s) from /completion markup: %s",
                        len(calls), ", ".join(c["name"] for c in calls))
        return ChatResult("".join(text_parts), calls, finish,
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
        tools belong to the closing round alone, and are passed as such only on
        the chat transport: /completion takes no tool schemas. It does still
        make tool calls — the schemas reached it in the prompt the template
        built — and it writes them as the template's own markup in the text,
        which ToolMarkupFilter takes back out. So raw no longer drops them.
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
