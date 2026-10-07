#!/usr/bin/env python3
"""discord_client.py — Discord client for the letsClaw core.

Start the core first; this attaches to it exactly as the terminal and the WebUI
do, over the same WebSocket protocol:

    ./serve.sh            then, in another shell:
    ./run_discord.sh

It is a thin client. The core owns every conversation; this process owns one
socket per active Discord channel and does nothing but translate between
Discord messages and core events. Almost no state lives here, which is why
restarting it costs nothing — the session on the core's side is the
conversation, and re-attaching by name restores it mid-stream.

Four things are less obvious than they look:

*   **A bot is a remote shell.** The tools are unconfined, so anyone who can get
    a message to this bot can run commands on the machine hosting the core.
    Anyone on Discord can DM a bot by default, and Discord's own per-channel
    permissions are the only wall in a guild. So `discord.users` is an
    allowlist of snowflake IDs that is *required*, not advisory: an empty list
    means nobody may talk to the bot, and that is the startup default.
    `mention_only` sits on top of it as hygiene — it keeps the bot quiet in a
    busy channel — and is not a security control.

*   **A channel is tracked by its snowflake, but named after itself.** Sessions
    are called `<server>-<channel>` so the WebUI sidebar reads like Discord
    does, while the snowflake stays the key: the bot remembers which name it
    last gave each channel id (state/discord_names.json), and when the two
    disagree it *renames the core session* rather than starting a new one. So
    renaming #esps in Discord carries its conversation across. The server
    prefix is not decoration — two servers each having a #general is the normal
    case, and without it their conversations would silently merge.
    `auto_names: false` goes back to bare snowflakes.

*   **Discord counts UTF-16 code units, not characters.** The chunker works in
    code points because that is what makes it testable, so everything outbound
    passes `_hard_split` on the way to the wire, where an astral character —
    most emoji — is re-counted as the two units Discord will charge for it.

*   **The reader loop must not block on Discord.** Answer text is buffered and
    posted once, at `turn_end`, so the only Discord calls on the event path are
    the rare ones (errors, notices, the rollover question). A stalled Discord
    API therefore cannot stall the core: at worst this client's subscriber
    queue overflows, the core drops it, and it reconnects and replays by seq.
"""

import argparse
import asyncio
import io
import json
import logging
import re
import secrets
import sys
import time
from urllib.parse import quote

import aiohttp

try:
    import discord
    from discord import app_commands
except ImportError:                                    # pragma: no cover
    print("❌ discord.py is not installed.\n"
          "   .venv/bin/pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(1)

import paths
from chunker import chunk_discord_text
from core import PROTOCOL_VERSION, load_config

logger = logging.getLogger("letclaw.discord")

DISCORD_LIMIT = 2000            # per message, in UTF-16 code units
DEFAULT_IDLE_DETACH_MIN = 60
DEFAULT_MAX_MESSAGES = 8        # before the answer goes up as a file instead
DEFAULT_MAX_LINES = 17
TYPING_REFRESH_S = 8            # Discord expires a typing indicator after 10
RECONNECT_MIN_S = 1
RECONNECT_MAX_S = 30
# A link that stayed up this long was healthy; the next failure starts its
# backoff from scratch rather than from wherever the last one left off.
RECONNECT_RESET_S = 30
SWEEP_INTERVAL_S = 60
DENY_QUIET_S = 600              # one refusal per stranger per 10 minutes
READY_TIMEOUT_S = 10
# Which session name each channel was last given, so a channel renamed
# while the bot was down is still recognised as the same channel.
NAMES_FILE = paths.resolve("state/discord_names.json")

YES = {"y", "yes"}
NO = {"n", "no"}

# Every command word the bot understands, including the ones it answers itself.
# Used to tell a real command from a message that merely opens with the
# alternative prefix — see ALT_PREFIX.
COMMAND_WORDS = {
    "info", "model", "models", "new", "rollover", "clear", "behavior", "stop",
    "reload", "help", "commands", "quit", "exit", "reasoning",
}
# Discord's own client captures a leading "/" into its slash-command picker, and
# with no application commands registered the message can end up never being
# sent at all. So "!" works too. It is accepted only in front of a word from
# COMMAND_WORDS, which leaves "!!!" and "!important" to reach the model as the
# ordinary text they are. ("@bot /model …" also works — once the mention is
# stripped the "/" is no longer what Discord saw at position 0.)
ALT_PREFIX = "!"

HELP = (
    "**letsClaw** — I hold one conversation per channel; the core keeps it "
    "when I restart.\n"
    "`!info` context and recent history · `!model [name]` list or switch · "
    "`!models` the list\n"
    "`!clear` forget the conversation · `!rollover` archive it and carry a "
    "handoff forward\n"
    "`!new` **wipe everything**, archives included · `!behavior` the loaded "
    "behavior files\n"
    "`!stop` interrupt the turn in progress · `!reload` re-read config.yaml · "
    "`!help` this\n"
    "_`/` works too, but Discord's own command picker often eats it before it "
    "is sent — `!` or `@me /command` always gets through._"
)


# ---- text on the way out ---------------------------------------------------


def _utf16_len(text):
    """What Discord will charge for `text`: astral characters count as two."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def _hard_split(text, limit=DISCORD_LIMIT):
    """Last-resort cut at the real wire limit.

    The chunker has already done the readable splitting in code points. This
    only ever fires on text an astral character pushed over the edge, and it
    cuts bluntly on purpose — there is no good place left to be clever.
    """
    if _utf16_len(text) <= limit:
        return [text]
    out, buf, size = [], [], 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if size + width > limit:
            out.append("".join(buf))
            buf, size = [], 0
        buf.append(ch)
        size += width
    if buf:
        out.append("".join(buf))
    return out


def _mention_summary(settings):
    mode = settings.mention_only
    if mode is True:
        return "mention-only"
    if mode is False:
        return "all messages"
    return f"mention-only ({mode}list of {len(settings.mention_list)})"


def _one_line(text):
    """Italics do not survive a newline in Discord; notices are one line."""
    return " ".join(str(text or "").split())


# ---- config ----------------------------------------------------------------


class Settings:
    """The `discord:` section, parsed once and validated loudly.

    Nothing here is looked up again at runtime, so a bad value is a startup
    error rather than a surprise three hours into a conversation.
    """

    def __init__(self, config):
        blob = config.get("discord") or {}
        core_cfg = config.get("core") or {}

        # Absent means enabled, so a config that never had the key is
        # unaffected. serve.sh reads the same flag before launching; a bot
        # started by hand (run_discord.sh) must refuse it too, or the flag
        # would only half-work.
        self.enabled = blob.get("enabled", True) is not False
        self.token = str(blob.get("token") or "").strip()
        self.core = self._core_url(blob.get("core"), core_cfg)
        # The bot loads the same config.yaml as the core, so the shared secret
        # is already here; `core_token` overrides it for a bot on another host.
        self.core_token = str(blob.get("core_token")
                              or core_cfg.get("token") or "").strip()

        self.users = self._snowflakes(blob.get("users"))
        self.mention_only = self._mention_mode(blob.get("mention_only", True))
        self.mention_list = [self._matcher(c) for c in
                             (blob.get("mention_list") or [])]
        # Name sessions after the server and channel instead of the snowflake.
        # See _resolve_session for what happens when a channel is renamed under
        # a session that already exists.
        self.auto_names = bool(blob.get("auto_names", True))
        self.idle_detach_min = int(blob.get("idle_detach_min")
                                   or DEFAULT_IDLE_DETACH_MIN)
        self.max_messages = max(1, int(blob.get("max_messages")
                                       or DEFAULT_MAX_MESSAGES))
        self.max_lines = max(1, int(blob.get("max_lines") or DEFAULT_MAX_LINES))

    @staticmethod
    def _core_url(raw, core_cfg):
        """Accept ws:// or http://, or build one from the core's own bind/port."""
        if not raw:
            host = core_cfg.get("bind") or "127.0.0.1"
            # 0.0.0.0 is how a core says "every interface"; it is not an address
            # a client can dial, so reach it the way any other local client does.
            if host in ("0.0.0.0", "::"):
                host = "127.0.0.1"
            return f"ws://{host}:{int(core_cfg.get('port', 8770))}"
        url = str(raw).strip().rstrip("/")
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        return url

    @staticmethod
    def _mention_mode(raw):
        """true | false | "white" | "black"."""
        if isinstance(raw, bool):
            return raw
        mode = str(raw).strip().lower()
        if mode in ("white", "black"):
            return mode
        logger.warning("discord.mention_only: %r is not true, false, white or "
                       "black — treating it as true", raw)
        return True

    @staticmethod
    def _matcher(raw):
        """One mention_list entry: a snowflake, "channel", or "server/channel"."""
        text = str(raw).strip().lstrip("#")
        if text.isdigit():
            return int(text)
        return text.lower()

    def lists(self, channel, guild):
        """Whether this channel is named in mention_list.

        Entries match by snowflake, by channel name, or by "server/channel"
        when two servers share a channel name — compared as slugs, so
        "#Ptolemy-Translation" and "ptolemy-translation" are the same thing.
        """
        name = slugify(getattr(channel, "name", ""))
        qualified = f"{slugify(getattr(guild, 'name', ''))}/{name}"
        for entry in self.mention_list:
            if isinstance(entry, int):
                if entry == getattr(channel, "id", None):
                    return True
                continue
            if "/" in entry:
                server, _, chan = entry.partition("/")
                if f"{slugify(server)}/{slugify(chan)}" == qualified:
                    return True
            elif slugify(entry) == name:
                return True
        return False

    def needs_mention(self, channel, guild):
        """Must the bot be addressed to answer in this channel?

        true/false decide it outright. "white" means the rule applies only to
        the channels in mention_list; "black" means it applies everywhere
        except them. DMs never need a mention and never reach here.
        """
        if self.mention_only is True or self.mention_only is False:
            return self.mention_only
        listed = self.lists(channel, guild)
        return listed if self.mention_only == "white" else not listed

    @staticmethod
    def _snowflakes(raw):
        """Discord ids, however YAML happened to render them."""
        out = set()
        for item in raw or []:
            try:
                out.add(int(str(item).strip()))
            except ValueError:
                logger.warning("discord.users: %r is not a snowflake — ignored", item)
        return out

    def check(self):
        """Fatal problems first, then the ones worth shouting about."""
        if not self.enabled:
            raise ValueError(
                "discord.enabled is false — the bot is switched off in "
                "config.yaml. Set it to true to run, or flip it back to "
                "false when you are done so serve.sh leaves it off.")
        if not self.token:
            raise ValueError(
                "discord.token is not set — create a bot at "
                "https://discord.com/developers/applications, copy its token "
                "into config.yaml under discord.token, and switch on the "
                "Message Content Intent while you are there")
        if not self.users:
            logger.warning("discord.users is empty — every message will be "
                           "refused. Add the snowflake IDs allowed to talk to "
                           "this bot.")


def slugify(text):
    """A Discord name as a session name: lowercase, no spaces, no surprises.

    Apostrophes are dropped rather than turned into a separator, so "Dudu's
    Den" reads as `dudus-den` and not `dudu-s-den`. Underscores survive because
    channels genuinely use them (#analysis_11c).
    """
    text = re.sub(r"['‘’]", "", str(text or "").lower())
    text = re.sub(r"[^a-z0-9_]+", "-", text)
    return text.strip("-")


def auto_session_name(channel, guild):
    """`<server>-<channel>`, which is unique across servers and stays readable.

    Channel names alone collide — two servers both having #general is the
    normal case, not the exceptional one — and a collision would silently
    merge two conversations into one session.
    """
    name = f"{slugify(getattr(guild, 'name', ''))}-{slugify(getattr(channel, 'name', ''))}"
    name = re.sub(r"-{2,}", "-", name).strip("-")
    # The core refuses a name over 64 characters, and a trailing dash from the
    # cut would be untidy in the sidebar.
    return name[:64].rstrip("-") or f"discord-{channel.id}"


def session_for(channel, guild, user_id):
    """Discord identity → core session name.

    Snowflakes, never names: a channel renamed in Discord keeps its
    conversation. A thread's `channel.id` is the thread's own id, so threads
    fall out of this with no special case.

    Takes the pieces rather than a message, because a slash command arrives as
    an Interaction and must land on the very same session a typed message would.
    """
    if guild is None:
        return f"discord-dm-{user_id}"
    return f"discord-{channel.id}"


def session_name(message):
    return session_for(message.channel, message.guild, message.author.id)


# ---- deciding what to do with an inbound message ---------------------------


def _is_reply_to_me(message, me):
    ref = getattr(message, "reference", None)
    resolved = getattr(ref, "resolved", None) if ref else None
    # Duck-typed rather than an isinstance check: a message Discord could not
    # resolve comes back as None or as a DeletedReferencedMessage, and neither
    # carries an `author` — which is the whole question being asked here.
    # Either way it counts as "not addressed": a mention always works, and
    # guessing the other way would have the bot answer messages never meant
    # for it.
    author = getattr(resolved, "author", None)
    return getattr(author, "id", None) == me.id


def _strip_mentions(content, me):
    return re.sub(rf"<@!?{me.id}>", " ", content or "").strip()


def _command_body(text):
    """The command in `text` without its prefix, or None if it is not one.

    "/" always counts, so an unknown "/foo" still earns the "unknown command"
    reply rather than being quietly asked of the model. "!" counts only in
    front of a real command word, so ordinary text keeps working.
    """
    if text.startswith("/"):
        return text[1:]
    if text.startswith(ALT_PREFIX):
        body = text[len(ALT_PREFIX):]
        if body.partition(" ")[0].strip().lower() in COMMAND_WORDS:
            return body
    return None


def classify(message, settings, me):
    """What to do with one inbound message: (action, text).

    ignore — not for us, and we say nothing at all
    deny   — addressed to us by someone who is not on the allowlist
    text   — go ahead; `text` is the message with our own mention removed

    Split out from the bot so the gate can be tested against fake messages
    without a token, a gateway or a network.
    """
    if message.author.bot:
        # Includes this bot's own messages. Two letsClaw bots in one channel
        # would otherwise talk to each other until someone pulled the plug.
        return "ignore", ""

    is_dm = message.guild is None
    addressed = is_dm or me in message.mentions or _is_reply_to_me(message, me)

    if message.author.id not in settings.users:
        # Only answer a stranger who was actually talking to us; a channel the
        # bot merely sits in should not get a refusal for every passing message.
        return ("deny", "") if addressed else ("ignore", "")

    if not is_dm and settings.needs_mention(message.channel, message.guild) \
            and not addressed:
        return "ignore", ""

    text = _strip_mentions(message.content, me)
    return ("text", text) if text else ("ignore", "")


# ---- one socket to the core ------------------------------------------------


class CoreLink:
    """One WebSocket to the core, for one session.

    Reconnects on its own with backoff and resumes by `last_seq`, so a core
    restart, a dropped link or an overflow disconnect costs nothing that the
    Discord side has to know about.
    """

    def __init__(self, url, session, http, *, token=None, model=None,
                 on_event=None):
        self.url = url
        self.session = session
        self.http = http
        self.token = token
        self.model = model
        self.on_event = on_event
        self.last_seq = 0
        self.last_used = time.monotonic()
        self.ws = None
        self._task = None
        self._ready = asyncio.Event()
        self._closing = False

    # -- lifecycle --

    def start(self):
        self._task = asyncio.create_task(self._run(), name=f"link:{self.session}")

    async def close(self):
        self._closing = True
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def touch(self):
        self.last_used = time.monotonic()

    async def wait_ready(self, timeout=READY_TIMEOUT_S):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- the connection --

    def _endpoint(self):
        # safe="" — quote() leaves "/" alone by default, and a session name is a
        # query value, not a path. Aliases already refuse a slash; this is what
        # makes that a policy rather than the only thing holding the URL together.
        url = (f"{self.url}/ws?session={quote(self.session, safe='')}"
               f"&last_seq={self.last_seq}"
               # The thinking text is never shown in Discord, so there is no
               # reason to ship it: the core withholds it and sends only the
               # running token count, which nothing here draws either.
               "&reasoning=0")
        if self.model:
            url += f"&model={quote(str(self.model))}"
        return url

    async def _run(self):
        delay = RECONNECT_MIN_S
        while not self._closing:
            started = time.monotonic()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("session %s: link to the core failed (%s: %s)",
                               self.session, type(e).__name__, e)
            self._ready.clear()
            self.ws = None
            if self._closing:
                break
            if time.monotonic() - started >= RECONNECT_RESET_S:
                delay = RECONNECT_MIN_S
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def _connect_once(self):
        headers = ({"Authorization": f"Bearer {self.token}"} if self.token else {})
        async with self.http.ws_connect(self._endpoint(), heartbeat=20,
                                        headers=headers) as ws:
            self.ws = ws
            self._ready.set()
            logger.info("session %s: attached to the core", self.session)
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    event = msg.json()
                except ValueError:
                    logger.debug("session %s: malformed event ignored", self.session)
                    continue
                seq = event.get("seq")
                if isinstance(seq, int):
                    self.last_seq = max(self.last_seq, seq)
                if self.on_event is None:
                    continue
                try:
                    await self.on_event(event)
                except Exception:
                    # One unrenderable event must never take down the stream.
                    logger.exception("session %s: could not handle %r",
                                     self.session, event.get("t"))
        logger.info("session %s: the core closed the connection", self.session)

    # -- sending --

    async def _send(self, payload):
        ws = self.ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_json(payload)
            self.touch()
            return True
        except Exception as e:
            logger.warning("session %s: send failed (%s)", self.session, e)
            return False

    async def submit(self, text, origin=None):
        return await self._send({"t": "submit", "text": text, "origin": origin})

    async def command(self, name, args="", request_id=None):
        # The id matters when a slash command is waiting on this particular
        # reply: the core echoes it back, which is how the answer finds its way
        # to the person who asked instead of only to the channel.
        return await self._send({"t": "command", "name": name, "args": args,
                                 "request_id": request_id or f"d{self.last_seq}"})

    async def stop(self):
        return await self._send({"t": "stop"})

    async def rollover_reply(self, request_id, yes):
        return await self._send({"t": "rollover_reply",
                                 "request_id": request_id, "yes": bool(yes)})


# ---- one channel ↔ one session ---------------------------------------------


class ChannelBridge:
    """Renders core events into a Discord channel.

    Holds only what the core does not: the answer accumulating this turn, the
    id of an unanswered rollover question, and the typing task. Everything else
    is replayed on attach.
    """

    def __init__(self, channel, link, settings):
        self.channel = channel
        self.link = link
        self.settings = settings
        self.answer = []
        self.pending_ask = None
        self.busy = False
        self._typing = None
        self._warned_proto = False
        # request_id → future, for a slash command awaiting its own reply.
        self.waiters = {}

    # -- outbound --

    async def say(self, text):
        for piece in _hard_split(text):
            try:
                await self.channel.send(piece)
            except discord.HTTPException as e:
                logger.warning("session %s: Discord refused a message (%s)",
                               self.link.session, e)
                return

    async def _post_answer(self):
        text = "".join(self.answer).strip()
        self.answer.clear()
        if not text:
            # A cancelled turn, or one that produced only tool calls. The notice
            # or error that explains it has already gone out on its own.
            return

        chunks = chunk_discord_text(text, max_chars=DISCORD_LIMIT,
                                    max_lines=self.settings.max_lines)
        if len(chunks) <= self.settings.max_messages:
            for chunk in chunks:
                await self.say(chunk)
            return

        # Long enough that posting it all would flood the channel. Send the
        # opening of the answer so the thread still reads, and attach the whole
        # thing — unchunked, so the Markdown is intact — rather than truncate.
        for chunk in chunks[:self.settings.max_messages - 1]:
            await self.say(chunk)
        try:
            data = io.BytesIO(text.encode("utf-8"))
            await self.channel.send(
                f"_…{len(chunks) - self.settings.max_messages + 1} more messages "
                f"worth — the full answer is attached._",
                file=discord.File(data, filename="answer.md"))
        except discord.HTTPException as e:
            logger.warning("session %s: could not attach the answer (%s)",
                           self.link.session, e)
            await self.say("_…the rest of the answer would not upload._")

    # -- typing --

    def _start_typing(self):
        self._stop_typing()
        self._typing = asyncio.create_task(self._typing_loop())

    def _stop_typing(self):
        if self._typing is not None:
            self._typing.cancel()
            self._typing = None

    async def _typing_loop(self):
        """Hold the indicator up for as long as the turn runs.

        Refreshed rather than held open in a context manager: a turn can run for
        hours, and a cancellable loop is easier to reason about than a context
        manager parked on an event.
        """
        try:
            while True:
                try:
                    await self.channel.typing()
                except Exception:
                    return          # lost permissions, or Discord is unhappy
                await asyncio.sleep(TYPING_REFRESH_S)
        except asyncio.CancelledError:
            pass

    # -- events --

    async def handle(self, event):
        t = event.get("t")
        if t == "hello":
            await self._hello(event)
        elif t == "turn_start":
            self.busy = True
            self.answer.clear()
            self._start_typing()
        elif t == "text":
            self.answer.append(event.get("delta") or "")
        elif t == "turn_end":
            self.busy = False
            self._stop_typing()
            await self._post_answer()
        elif t == "busy":
            await self.say(f"⏳ {_one_line(event.get('reason')) or 'mid-turn'}")
        elif t == "error":
            await self.say(f"⚠️ {_one_line(event.get('msg')) or 'something failed'}")
        elif t == "notice":
            if event.get("level") == "warn":
                await self.say(f"_⚠️ {_one_line(event.get('text'))}_")
        elif t == "rollover_start":
            await self.say(f"_🔄 rolling over — {_one_line(event.get('reason'))}_")
        elif t == "rollover_done":
            text = event.get("handoff_text")
            if text:
                # The compaction summary: what the fresh window carries. say()
                # hard-splits past the 2000-unit wire limit, so a long handoff
                # is posted as a sequence of messages, not lost.
                await self.say(f"🧠 *handoff — what the new window carries:*\n{text}")
            await self.say(f"_✨ fresh window — {event.get('used')}/"
                           f"{event.get('budget')} tok_")
        elif t == "rollover_ask":
            await self._ask(event)
        elif t == "session_state":
            await self._state(event)
        elif t == "response":
            await self._response(event)

    async def _hello(self, event):
        if event.get("proto") != PROTOCOL_VERSION and not self._warned_proto:
            self._warned_proto = True
            await self.say(f"⚠️ the core speaks protocol {event.get('proto')}, "
                           f"this client speaks {PROTOCOL_VERSION} — update one.")
        # Anything that happened while this client was detached or reconnecting.
        for missed in event.get("missed") or []:
            await self.handle(missed)
        if event.get("gap"):
            await self.say("_(some earlier output was missed while detached)_")
        if event.get("busy"):
            self.busy = True
            self._start_typing()
            await self.say("_⏳ a turn is already running in this session_")

    async def _ask(self, event):
        self.pending_ask = event.get("request_id")
        await self.say(
            f"⚠️ Context at {event.get('used')}/{event.get('budget')} "
            f"({event.get('percent')}%). Roll over to a new session?\n"
            f"Reply **yes** or **no** — no answer in {event.get('timeout_s')}s "
            f"keeps it.")

    async def _state(self, event):
        what = event.get("what")
        if what == "cleared":
            note = "_🧹 history cleared_"
            if event.get("record"):
                note = f"_🧹 history cleared — what was said is in {event['record']}_"
            await self.say(note)
        elif what == "wiped":
            files = event.get("files") or 0
            await self.say(f"_🔥 everything wiped — conversation and "
                           f"{files} archive file(s)_")
        elif what == "model":
            await self.say(f"_🤖 now on {event.get('model')} — budget "
                           f"{event.get('budget')} tok_")
        elif what == "reloaded":
            await self.say(f"_♻️ config reloaded — {event.get('model')}, budget "
                           f"{event.get('budget')} tok_")

    def render_response(self, event):
        """A command reply as text, or "" when a broadcast says it instead.

        Only the replies carrying something to show render. `/clear`, `/new`
        and `/model <name>` each broadcast a `session_state` or rollover event
        as well, and printing both would say everything twice.

        Separate from posting it, because a slash command renders the same
        reply into its own interaction rather than into the channel.
        """
        if not event.get("ok", True):
            return f"⚠️ {_one_line(event.get('error'))}"
        if "info" in event:
            return _format_info(event["info"])
        if "behavior" in event:
            base = event.get("base") or ""
            behavior = event.get("behavior") or ""
            per_session = event.get("session_behavior") or ""
            if not (base or behavior or per_session):
                return "_no behavior file loaded_"
            out = []
            if base:
                out.append(f"**Base behavior**\n{base}")
            if behavior:
                model = f" ({event['model']})" if event.get("model") else ""
                out.append(f"**Model behavior**{model}\n{behavior}")
            if per_session:
                where = f" ({event['session']})" if event.get("session") else ""
                out.append(f"**Session behavior**{where}\n{per_session}")
            return "\n\n".join(out)
        if "configured" in event:
            listed = ", ".join(f"`{m}`" for m in event.get("configured") or [])
            return (f"🤖 current: **{event.get('current')}**\n"
                    f"📋 configured: {listed or '(none)'}")
        if "changed" in event:
            changed = event.get("changed") or []
            if not changed:
                return f"_♻️ {event.get('note') or 'nothing changed'}_"
            body = "\n".join(f"  {line}" for line in changed)
            return f"♻️ **config reloaded**\n```\n{body}\n```"
        return ""

    async def _response(self, event):
        # Claimed by the slash command that asked for it, which will render it
        # into its own reply — posting it to the channel too would duplicate it.
        waiter = self.waiters.pop(event.get("request_id"), None)
        if waiter is not None and not waiter.done():
            waiter.set_result(event)
            return
        text = self.render_response(event)
        if text:
            await self._post_text(text)

    async def run_command(self, name, args="", timeout=30):
        """Send a command and wait for its own reply. None if none came.

        The core answers a command to the asking client only, stamped with the
        request_id it was given — so this is just a matter of listening for the
        id it sent.
        """
        request_id = f"i{secrets.token_hex(6)}"
        future = asyncio.get_running_loop().create_future()
        self.waiters[request_id] = future
        try:
            if not await self.link.command(name, args, request_id=request_id):
                return None
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.waiters.pop(request_id, None)

    async def _post_text(self, text):
        for chunk in chunk_discord_text(text, max_chars=DISCORD_LIMIT,
                                        max_lines=self.settings.max_lines):
            await self.say(chunk)

    async def close(self):
        self._stop_typing()
        await self.link.close()


def _format_info(info):
    used, budget = info.get("used") or 0, info.get("budget") or 0
    pct = (used * 100) // budget if budget else 0
    approx = "~" if info.get("estimated", True) else ""
    roll = info.get("rollover") or {}
    lines = [
        f"🤖 **{info.get('model')}** @ {info.get('base_url')}",
        f"🆔 session `{info.get('session')}` (id `{info.get('session_id')}`)",
        f"💬 {info.get('messages')} messages",
        f"⚡ context {approx}{used}/{budget} tok ({pct}%) · "
        f"{info.get('tools_tokens')} tok of tool schemas",
    ]
    if info.get("output_total") is not None:
        lines.append(f"📤 {info['output_total']} tok generated in this session")
    if roll.get("percent"):
        lines.append(f"🔄 rollover {roll.get('mode')} at {roll['percent']}% "
                     f"({roll.get('trip')} tok) · {roll.get('count') or 0} so far")
    else:
        lines.append(f"🔄 rollover disabled · {roll.get('count') or 0} so far")
    return "\n".join(lines)


# ---- the model picker ------------------------------------------------------

# Discord's own ceiling on a select menu. More models than this and the last
# ones would be silently missing, so the message says so and `/model <name>`
# (which autocompletes) remains the way to reach them.
SELECT_LIMIT = 25
PICKER_TIMEOUT_S = 180


class ModelPicker(discord.ui.View):
    """A dropdown of the configured models, for `/model` with no name given.

    The list is the core's own, fetched when the command runs rather than baked
    in when the command was registered — so a model added by a config reload
    shows up without restarting the bot.
    """

    def __init__(self, bridge, user_id, models, current, max_lines):
        super().__init__(timeout=PICKER_TIMEOUT_S)
        self.bridge = bridge
        self.user_id = user_id
        self.max_lines = max_lines
        self.message = None
        shown = list(models[:SELECT_LIMIT])
        # Past 25 the tail is cut, and the model actually in use could be in it —
        # a picker that does not show where you already are is worse than one
        # that reorders slightly, so it comes to the front.
        if current and current in models and current not in shown:
            shown = [current] + shown[:SELECT_LIMIT - 1]
        options = [
            discord.SelectOption(label=name[:100], value=name[:100],
                                 description=("in use here" if name == current
                                              else None),
                                 default=(name == current))
            for name in shown
        ]
        self.select = discord.ui.Select(placeholder="Pick a model…",
                                        options=options, min_values=1,
                                        max_values=1)
        self.select.callback = self._picked
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        """One person's picker, not the channel's.

        Without this anyone who can see the message could move someone else's
        conversation onto another model — and in a guild channel the allowlist
        is the only thing that would have stopped them.
        """
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "That picker belongs to someone else — run `/model` yourself.",
                ephemeral=True)
            return False
        return True

    async def _picked(self, interaction):
        choice = self.select.values[0]
        # Answer within Discord's three seconds and drop the dropdown in the
        # same move; switching models can take longer than that.
        await interaction.response.edit_message(
            content=f"_switching to `{choice}`…_", view=None)
        self.stop()
        event = await self.bridge.run_command("model", choice)
        if event is None:
            await interaction.edit_original_response(
                content="_⚠️ the core did not answer in time._")
            return
        if not event.get("ok", True):
            await interaction.edit_original_response(
                content=f"⚠️ {_one_line(event.get('error'))}")
            return
        budget = event.get("budget")
        await interaction.edit_original_response(
            content=f"🤖 now on **{event.get('model', choice)}**"
                    + (f" — context budget {budget:,} tok" if budget else ""))

    async def on_timeout(self):
        # Leave no dropdown that looks live but is not.
        if self.message is not None:
            try:
                await self.message.edit(content="_model picker expired — "
                                                "run `/model` again._", view=None)
            except discord.HTTPException:
                pass


# ---- the bot ---------------------------------------------------------------


class LetsClawBot(discord.Client):

    def __init__(self, settings):
        intents = discord.Intents.default()
        # Privileged, and the bot is useless without it: without Message Content
        # every message arrives with an empty `content`. Switch it on under
        # Bot → Privileged Gateway Intents in the developer portal.
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.bridges = {}
        self._core_http = None
        self._denied_at = {}
        self._sweeper = None
        self._synced = False
        self._names = {}
        self._models = ([], 0.0)          # (names, when) for /model autocomplete
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    async def setup_hook(self):
        self._core_http = aiohttp.ClientSession()
        self._load_names()
        self._sweeper = asyncio.create_task(self._sweep(), name="idle-sweep")

    async def close(self):
        if self._sweeper is not None:
            self._sweeper.cancel()
        for bridge in list(self.bridges.values()):
            try:
                await bridge.close()
            except Exception:
                logger.debug("bridge close failed", exc_info=True)
        self.bridges.clear()
        if self._core_http is not None:
            await self._core_http.close()
        await super().close()

    async def on_ready(self):
        logger.info("connected to Discord as %s (%d allowed user(s))",
                    self.user, len(self.settings.users))
        print(f"🤖 Discord: connected as {self.user}")
        print(f"   core:    {self.settings.core}")
        print(f"   allowed: {len(self.settings.users)} user(s)"
              + ("  ⚠️  nobody — every message will be refused"
                 if not self.settings.users else ""))
        self._report_mention_policy()
        await self._sync_commands()
        await self.migrate_names()
        sys.stdout.flush()

    def _report_mention_policy(self):
        """Say which channels actually need addressing, once, at startup.

        "white" and "black" are only as clear as whoever read them last, so
        rather than ask anyone to trust the wording this prints the resolved
        answer for every channel the bot can see.
        """
        if self.settings.mention_only in (True, False):
            return
        seen = [(g, c) for g in self.guilds
                for c in getattr(g, "text_channels", [])]
        # Two servers with a #general would otherwise print as the same row
        # twice, which is precisely the confusion this is here to remove.
        repeated = {c.name for i, (_, c) in enumerate(seen)
                    if any(o.name == c.name for j, (_, o) in enumerate(seen)
                           if j != i)}
        quiet, free = [], []
        for guild, channel in seen:
            label = (f"{guild.name}/#{channel.name}" if channel.name in repeated
                     else f"#{channel.name}")
            (quiet if self.settings.needs_mention(channel, guild)
             else free).append(label)
        print(f"   must @mention in: {', '.join(quiet) or '(none)'}")
        print(f"   answers freely in: {', '.join(free) or '(none)'}")

    async def _sync_commands(self):
        """Publish the slash commands. Once per process, not once per reconnect.

        Per guild as well as globally: a guild sync takes effect immediately,
        while the global one — the copy that makes the commands work in DMs —
        can take Discord up to an hour to roll out.
        """
        if self._synced:
            return
        self._synced = True
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("slash commands published to %s", guild.name)
            except discord.HTTPException as e:
                logger.warning("could not publish commands to %s: %s", guild.name, e)
        try:
            await self.tree.sync()
        except discord.HTTPException as e:
            logger.warning("global command sync failed (DMs may lag): %s", e)
        print(f"   commands: /{'  /'.join(sorted(c.name for c in self.tree.get_commands()))}")

    async def model_names(self):
        """Configured model names, for /model's autocomplete. Cached briefly."""
        names, when = self._models
        if names and time.monotonic() - when < 60:
            return names
        url = (self.settings.core.replace("wss://", "https://")
                                 .replace("ws://", "http://")) + "/models"
        headers = ({"Authorization": f"Bearer {self.settings.core_token}"}
                   if self.settings.core_token else {})
        try:
            async with self._core_http.get(
                    url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                body = await r.json()
            names = list(body.get("configured") or [])
            self._models = (names, time.monotonic())
        except Exception as e:
            # Autocomplete is a convenience; failing it must not fail the command.
            logger.debug("could not fetch model names: %s", e)
        return names

    # -- idle detach --

    async def _sweep(self):
        """Drop sockets for channels nobody has used in a while.

        The conversation is not lost: the core keeps the session and its live
        file, so the next message re-attaches and picks it straight back up.
        """
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            cutoff = time.monotonic() - self.settings.idle_detach_min * 60
            for key, bridge in list(self.bridges.items()):
                if bridge.busy or bridge.link.last_used > cutoff:
                    continue
                logger.info("session %s: idle, detaching", bridge.link.session)
                self.bridges.pop(key, None)
                try:
                    await bridge.close()
                except Exception:
                    logger.debug("idle close failed", exc_info=True)

    # -- inbound --

    # -- naming --

    def _core_url(self):
        return (self.settings.core.replace("wss://", "https://")
                                  .replace("ws://", "http://"))

    def _auth(self):
        return ({"Authorization": f"Bearer {self.settings.core_token}"}
                if self.settings.core_token else {})

    def _load_names(self):
        """What each channel's session was called last time this bot ran.

        Without it a channel renamed while the bot was down would look like a
        brand new channel, and its conversation would be left behind under the
        old name.
        """
        try:
            self._names = json.loads(NAMES_FILE.read_text())
        except (OSError, ValueError):
            self._names = {}

    def _save_names(self):
        try:
            NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
            NAMES_FILE.write_text(json.dumps(self._names, indent=1))
        except OSError as e:
            # Losing this costs a rename being missed, never a conversation.
            logger.warning("could not save the channel-name map: %s", e)

    async def _rename_core_session(self, old, new):
        """Move a session to a new name. True if it is now called `new`."""
        try:
            async with self._core_http.post(
                    f"{self._core_url()}/sessions/{quote(old, safe='')}/rename",
                    headers=self._auth(), json={"to": new},
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    logger.info("renamed session %r -> %r to follow Discord",
                                old, new)
                    return True
                if r.status == 404:
                    return True     # nothing there to move; `new` is free to use
                body = (await r.json()).get("error", "")
                logger.warning("could not rename %r -> %r (%s: %s)",
                               old, new, r.status, body)
                return False
        except Exception as e:
            logger.warning("rename %r -> %r failed: %s", old, new, e)
            return False

    async def migrate_names(self):
        """Bring existing sessions onto their auto names, at startup.

        Without this the rename only happens when a channel next gets a
        message, so the sidebar would go on showing raw snowflakes for channels
        nobody has spoken in yet. Driven by what the core actually has — one
        GET, then a rename only for sessions that really exist, so a channel
        with no conversation is left alone rather than conjured into one.
        """
        if not self.settings.auto_names:
            return
        try:
            async with self._core_http.get(
                    f"{self._core_url()}/sessions", headers=self._auth(),
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                existing = {s["name"] for s in (await r.json())["sessions"]}
        except Exception as e:
            logger.warning("could not list sessions to rename them: %s", e)
            return

        moved = 0
        for guild in self.guilds:
            for channel in getattr(guild, "text_channels", []):
                key = str(channel.id)
                desired = auto_session_name(channel, guild)
                previous = self._names.get(key) or f"discord-{channel.id}"
                if previous == desired or previous not in existing:
                    # Nothing named that on the core: there is no conversation
                    # to carry across, and the name will be right on first use.
                    continue
                if desired in existing:
                    logger.warning("cannot rename %r -> %r: that name is taken",
                                   previous, desired)
                    continue
                if await self._rename_core_session(previous, desired):
                    self._names[key] = desired
                    existing.discard(previous)
                    existing.add(desired)
                    moved += 1
        if moved:
            self._save_names()
            print(f"   renamed {moved} session(s) to match their channels")

    async def _resolve_session(self, channel, guild, user_id):
        """The session name for this channel, following Discord renames.

        Auto-naming, else the snowflake. When the computed name differs from
        the one used last time, the core session is renamed rather than
        abandoned — so renaming #esps in Discord carries its conversation
        across instead of starting an empty one.
        """
        if guild is None or not self.settings.auto_names:
            return session_for(channel, guild, user_id)

        key = str(channel.id)
        desired = auto_session_name(channel, guild)
        # No record yet means this bot has not named the channel before, so the
        # name to migrate from is the one it would have used: the snowflake.
        previous = self._names.get(key) or f"discord-{channel.id}"
        if previous != desired:
            if not await self._rename_core_session(previous, desired):
                # Busy, or the new name is taken. Keep talking to the session
                # that holds the conversation and try again next time.
                return previous
        if self._names.get(key) != desired:
            self._names[key] = desired
            self._save_names()
        return desired

    async def _bridge(self, channel, guild, user_id):
        """The bridge for this channel, opened on first use.

        Keyed on the channel, so a slash command and a typed message in the
        same place share one socket and one conversation.
        """
        key = channel.id
        bridge = self.bridges.get(key)
        if bridge is None:
            name = await self._resolve_session(channel, guild, user_id)
            link = CoreLink(self.settings.core, name, self._core_http,
                            token=self.settings.core_token)
            bridge = ChannelBridge(channel, link, self.settings)
            link.on_event = bridge.handle
            self.bridges[key] = bridge
            link.start()
            logger.info("channel %s → session %s", key, name)
        bridge.link.touch()
        return bridge

    async def bridge_for(self, message):
        return await self._bridge(message.channel, message.guild, message.author.id)

    async def _refuse(self, message):
        """Tell a stranger no — but only once in a while, per stranger.

        Without the quiet period a persistent stranger turns the bot into the
        thing flooding the channel on their behalf.
        """
        uid = message.author.id
        now = time.monotonic()
        logger.warning("refused %s (%s) in %s — not on discord.users",
                       message.author, uid,
                       "a DM" if message.guild is None else f"#{message.channel}")
        if now - self._denied_at.get(uid, -DENY_QUIET_S) < DENY_QUIET_S:
            return
        # Whoever is knocking decides how many keys this grows, so drop the ones
        # that have gone quiet rather than keep a row per stranger for ever.
        if len(self._denied_at) > 256:
            self._denied_at = {u: t for u, t in self._denied_at.items()
                               if now - t < DENY_QUIET_S}
        self._denied_at[uid] = now
        try:
            await message.channel.send(
                f"🚫 Not on this bot's allowlist. Your user ID is `{uid}` — the "
                f"owner can add it to `discord.users` in config.yaml.")
        except discord.HTTPException:
            pass

    async def on_message(self, message):
        action, text = classify(message, self.settings, self.user)
        if action == "ignore":
            return
        if action == "deny":
            await self._refuse(message)
            return

        bridge = await self.bridge_for(message)
        if not await bridge.link.wait_ready():
            await bridge.say(f"⚠️ cannot reach the core at {self.settings.core} "
                             f"— is `./serve.sh` running?")
            return

        # An outstanding rollover question takes precedence: while one is up,
        # a bare yes/no is an answer to it rather than a new turn.
        if bridge.pending_ask and text.strip().lower() in (YES | NO):
            request_id, bridge.pending_ask = bridge.pending_ask, None
            await bridge.link.rollover_reply(request_id,
                                             text.strip().lower() in YES)
            return

        body = _command_body(text)
        if body is not None:
            await self._command(bridge, body)
            return
        await bridge.link.submit(text, origin=f"discord:{message.author.name}")

    # -- slash commands --

    def _register_commands(self):
        """Discord-native /commands, so they appear in the client's picker.

        The same commands the text prefixes reach, going to the same session —
        the difference is only how Discord delivered them. Registered here at
        construction so the tree is populated before the gateway connects.
        """
        tree = self.tree

        def simple(name, description):
            # A closure over `name`, not a defaulted argument: discord.py reads
            # the callback's signature to build the command, and every
            # parameter it finds there has to be a real, annotated option.
            async def _run(interaction: discord.Interaction):
                await self.run_slash(interaction, name)
            _run.__name__ = f"cmd_{name}"
            tree.command(name=name, description=description)(_run)

        simple("info", "context, recent history and token usage")
        simple("models", "the configured model list")
        simple("rollover", "archive this conversation and carry a handoff forward")
        simple("clear", "forget this conversation, keep the archives")
        simple("new", "WIPE everything — this conversation and its archives")
        simple("behavior", "print the loaded behavior files")
        simple("stop", "interrupt the turn in progress")
        simple("reload", "re-read config.yaml into the running core")

        @tree.command(name="model",
                      description="switch this channel's model — pick from a list")
        @app_commands.describe(name="leave blank to choose from a dropdown")
        async def _model(interaction: discord.Interaction, name: str = ""):
            if name:
                await self.run_slash(interaction, "model", name)
            else:
                await self.show_model_picker(interaction)

        @_model.autocomplete("name")
        async def _model_autocomplete(interaction: discord.Interaction, current: str):
            current = (current or "").lower()
            return [app_commands.Choice(name=n, value=n)
                    for n in await self.model_names()
                    if current in n.lower()][:25]

        @tree.command(name="ask",
                      description="ask letsClaw something (same as just typing)")
        @app_commands.describe(text="your question")
        async def _ask(interaction: discord.Interaction, text: str):
            await self.run_slash(interaction, None, text)

    async def _open_bridge(self, interaction):
        """The bridge this interaction belongs to, or None having said why.

        Shared by every slash command: the allowlist, the deferral, working out
        which channel an Interaction came from, and reaching the core.
        """
        user = interaction.user
        if user.id not in self.settings.users:
            logger.warning("refused a slash command from %s (%s) — not on "
                           "discord.users", user, user.id)
            await interaction.response.send_message(
                f"🚫 Not on this bot's allowlist. Your user ID is `{user.id}` — "
                f"the owner can add it to `discord.users` in config.yaml.",
                ephemeral=True)
            return None
        await interaction.response.defer(thinking=True)

        channel = interaction.channel
        if channel is None and interaction.guild is None:
            channel = await self.create_dm(user)   # a DM Discord has not cached
        if channel is None:
            await interaction.followup.send("⚠️ cannot work out which channel "
                                            "this came from.")
            return None

        bridge = await self._bridge(channel, interaction.guild, user.id)
        if not await bridge.link.wait_ready():
            await interaction.followup.send(
                f"⚠️ cannot reach the core at {self.settings.core} "
                f"— is `./serve.sh` running?")
            return None
        return bridge

    async def show_model_picker(self, interaction):
        """`/model` with no name: a dropdown of what the core actually has."""
        bridge = await self._open_bridge(interaction)
        if bridge is None:
            return
        event = await bridge.run_command("model")        # no args = list them
        if event is None:
            await interaction.followup.send("_⚠️ the core did not answer in time._")
            return
        models = event.get("configured") or []
        if not models:
            await interaction.followup.send("_no models are configured._")
            return
        current = event.get("current")
        view = ModelPicker(bridge, interaction.user.id, models, current,
                           self.settings.max_lines)
        note = f"🤖 currently **{current}** — pick one to switch this channel."
        if len(models) > SELECT_LIMIT:
            note += (f"\n_showing the first {SELECT_LIMIT} of {len(models)}; "
                     f"use `/model <name>` for the rest._")
        view.message = await interaction.followup.send(note, view=view)

    async def run_slash(self, interaction, name, args=""):
        """One slash command, start to finish.

        Deferred immediately: Discord discards an interaction that is not
        answered within three seconds, and a turn takes rather longer than that.
        """
        bridge = await self._open_bridge(interaction)
        if bridge is None:
            return
        user = interaction.user

        if name is None:                          # /ask — an ordinary turn
            await bridge.link.submit(args, origin=f"discord:{user.name}")
            await interaction.followup.send(f"**{user.display_name}:** "
                                            f"{_one_line(args)[:1500]}")
            return
        if name == "stop":
            await bridge.link.stop()
            await interaction.followup.send("_⏹ stop sent_")
            return

        event = await bridge.run_command(name, args)
        if event is None:
            await interaction.followup.send("_⚠️ the core did not answer in time._")
            return
        # Several commands answer with a broadcast rather than a reply — the
        # channel already has the real news, so this only has to close the
        # interaction off.
        text = bridge.render_response(event) or "_done_"
        chunks = chunk_discord_text(text, max_chars=DISCORD_LIMIT,
                                    max_lines=self.settings.max_lines)
        for chunk in chunks[:self.settings.max_messages]:
            await interaction.followup.send(chunk)

    # -- text commands --

    async def _command(self, bridge, body):
        name, _, args = body.partition(" ")
        name = name.strip().lower()
        if name in ("help", "commands", ""):
            await bridge.say(HELP)
            return
        if name in ("quit", "exit"):
            await bridge.say("_Nothing to quit here — the core keeps the "
                             "conversation. `!clear` forgets it, `!rollover` "
                             "archives it, `!new` wipes it._")
            return
        if name == "reasoning":
            await bridge.say("_The thinking is not streamed to Discord — this "
                             "client asks the core not to send it. `/info` "
                             "shows what the turn cost._")
            return
        if name == "stop":
            await bridge.link.stop()
            return
        await bridge.link.command(name, args.strip())


# ---- entry point -----------------------------------------------------------


def _configure_logging(config):
    cfg = config.get("logging", {})
    level = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stderr)]
    path = cfg.get("file")
    if path:
        try:
            p = paths.resolve(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(p))
        except OSError as e:
            print(f"⚠️  cannot log to {path}: {e}", file=sys.stderr)
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # discord.py narrates every gateway heartbeat and resume at INFO, which at
    # the configured level would bury this client's own lines.
    logging.getLogger("discord").setLevel(logging.WARNING)
    # Two of the warnings it does emit are about voice support, which a text
    # bot will never use. Dropped by hand because they are worth losing and
    # everything else at WARNING is worth keeping.
    logging.getLogger("discord.client").addFilter(
        lambda r: "voice will NOT be supported" not in r.getMessage())


def main():
    ap = argparse.ArgumentParser(description="letsClaw Discord client")
    ap.add_argument("--config", help="config file (default: ./config.yaml)")
    ap.add_argument("--core", help="core URL (default: from discord.core, "
                                   "else core.bind/core.port)")
    args = ap.parse_args()

    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"❌ {e}")
        return 1
    _configure_logging(config)

    settings = Settings(config)
    if args.core:
        settings.core = Settings._core_url(args.core, config.get("core") or {})

    print("\nletsClaw Discord client")
    print(f"Core: {settings.core}   "
          f"{_mention_summary(settings)}   "
          f"idle detach {settings.idle_detach_min} min")
    sys.stdout.flush()
    # After the banner, so the console reads in the order things happened.
    # check() logs rather than prints: the warning belongs in the log file too.
    try:
        settings.check()
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    bot = LetsClawBot(settings)
    try:
        bot.run(settings.token, log_handler=None)
    except discord.LoginFailure:
        print("❌ Discord rejected the token in discord.token.")
        return 1
    except discord.PrivilegedIntentsRequired:
        print("❌ The Message Content Intent is off, so every message would "
              "arrive empty.\n"
              "   Developer portal → your app → Bot → Privileged Gateway "
              "Intents → Message Content Intent.")
        return 1
    except (aiohttp.ClientError, discord.GatewayNotFound, OSError) as e:
        # The lab core often sits on a network with no route out; saying so
        # beats forty lines of aiohttp traceback.
        print(f"❌ Could not reach Discord ({type(e).__name__}: {e}).\n"
              "   Check the network, and https_proxy if this host needs one.")
        return 1
    except KeyboardInterrupt:
        pass
    print("\nBye! 👋  (the core keeps running)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
