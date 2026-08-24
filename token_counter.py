"""Token counter module — estimate token usage for messages and full context.

Uses tiktoken (cl100k_base) when available; otherwise falls back to a
chars/4 heuristic so the app never depends on the package.

Note: cl100k_base is a GPT-family encoding, so counts for Qwen are an
approximation (typically within ~10-20%). Good enough for budget display
and compaction triggers, not for billing.
"""

import logging

logger = logging.getLogger("letclaw.tokens")

# Per-message overhead (role markers, separators) — OpenAI cookbook estimate
_MESSAGE_OVERHEAD = 4
# End-of-conversation tokens
_CONVERSATION_TAIL = 3

_encoder = None
_encoder_checked = False


def _get_encoder():
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception as e:
        logger.warning(f"tiktoken unavailable, using char estimate: {e}")
        return None


def _enc():
    global _encoder, _encoder_checked
    if not _encoder_checked:
        _encoder = _get_encoder()
        _encoder_checked = True
    return _encoder


def count_text(text: str) -> int:
    """Estimate tokens in a plain string."""
    if not text:
        return 0
    enc = _enc()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, len(text) // 4)


def count_message(msg: dict) -> int:
    """Estimate tokens for one chat message (content + framing overhead)."""
    return count_text(msg.get("content", "")) + _MESSAGE_OVERHEAD


def count_messages(messages: list) -> int:
    """Estimate total tokens for a full message list (the whole context)."""
    return sum(count_message(m) for m in messages) + _CONVERSATION_TAIL


def message_breakdown(messages: list) -> list:
    """Per-message content token counts: [(role, tokens), ...]."""
    return [(m.get("role", "?"), count_text(m.get("content", ""))) for m in messages]
