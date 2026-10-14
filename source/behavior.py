"""The behavior layers: the Markdown that becomes the system prompt.

Three files stack into every system prompt — a shared base, one per model, and
one named for the session — plus the two directives the core sends on its own
initiative at the moment they apply. All of it is plain Markdown so it can be
tuned with /reload rather than a code change.

BehaviorStore owns the read-through cache of those files. The cache is reached
from two different threads: every read goes through asyncio.to_thread so a file
read never blocks the event loop, and so does the reconciliation /reload runs.
As a bare dict on SessionManager that was a live race — stale() iterated
.items() while another thread's read inserted into it, which is a
RuntimeError mid-reload, and the window is exactly when an attach and a reload
coincide. The lock below is what makes the class worth having over the dict.
"""
import logging
import threading

import paths

logger = logging.getLogger("letclaw.behavior")

CONCLUDE_MARKER = "## Forced conclusion"
CONCLUDE_FALLBACK = ("Your reasoning was cut off at the token limit. Stop "
                     "reasoning now and give your best final answer. If you "
                     "truly cannot conclude, say in one sentence what single "
                     "fact is missing.")

# Sent when a thinking pad has filled the context and is about to be compacted:
# the model writes down what it has established, and that note replaces the
# reasoning it summarises. Lives in base.md for the same reason as the above.
CHECKPOINT_MARKER = "## Checkpoint"
CHECKPOINT_FALLBACK = ("Your reasoning has filled the available room. Write "
                       "down everything you have established so far — every "
                       "intermediate value exactly as you computed it, every "
                       "assumption made, and what still has to be done. This "
                       "note replaces the reasoning it summarises, so whatever "
                       "you leave out is lost. This is not the final answer.")

# The base behavior — identity, working style and the tool notes — lives in
# plain Markdown (behavior.base_file) so it can be tuned without a code change.
# It is read once per core and shared by every session, and it is always in the
# system prompt: a missing file is a startup warning and an empty base, the
# same behavior as a missing per-model behavior file.
DEFAULT_BASE_FILE = "models/base.md"


def strip_directive_sections(base_behavior):
    """The base behavior minus the two blocks the core sends on its own.

    "## Checkpoint" and "## Forced conclusion" are templates, not behavior: each
    is extracted and sent as a *user* message at the one moment it applies. They
    live in base.md so they can be tuned without a code change, but they are
    also, redundantly, part of every system prompt — and a model that reads its
    whole system prompt as standing instruction will simply obey them. Claude
    Opus 5 over Argo answers nothing at all, having been told on every turn that
    "This is not the final answer".

    Local thinking models ignore them, so stripping is opt-in per model
    (models.<name>.strip_directives) rather than done for everyone. Extraction
    is unaffected: the directives still reach the model when they are meant to.
    """
    out, skipping = [], False
    for line in base_behavior.splitlines(keepends=True):
        if line.startswith("## "):
            skipping = line.strip() in (CHECKPOINT_MARKER, CONCLUDE_MARKER)
        if not skipping:
            out.append(line)
    return "".join(out).rstrip() + "\n"


def extract_conclude_directive(base_behavior):
    """The paragraph that forces a cut-off round to conclude. See extract_directive."""
    return extract_directive(base_behavior, CONCLUDE_MARKER, CONCLUDE_FALLBACK)


def extract_checkpoint_directive(base_behavior):
    """The paragraph that asks a full pad to distil itself. See extract_directive."""
    return extract_directive(base_behavior, CHECKPOINT_MARKER, CHECKPOINT_FALLBACK)


def extract_directive(base_behavior, marker, fallback):
    """The paragraph after `marker` in the base behavior file.

    These are messages the core sends the model on its own initiative, so they
    live in base.md — tunable with /reload, no code change. Without the heading,
    a short built-in sentence.
    """
    if marker not in base_behavior:
        return fallback
    _, _, rest = base_behavior.partition(marker)
    # The paragraph ends at the next heading or end of file. HTML comments are
    # notes to whoever edits the file, not words for the model.
    para = []
    in_comment = False
    for line in rest.splitlines():
        if line.startswith("#"):
            break
        if in_comment:
            in_comment = "-->" not in line
            continue
        if line.lstrip().startswith("<!--"):
            in_comment = "-->" not in line
            continue
        para.append(line)
    text = "\n".join(para).strip()
    return text or CONCLUDE_FALLBACK


class BehaviorStore:
    """Read-through cache of the behavior files, shared across threads.

    Keys are the strings the callers pass: a config-relative path for the base
    and model layers, and str(resolved path) for the session layer. Every path
    ever consulted stays a key, a missing file caching "" — which is what lets
    stale() notice a session .md created after the session attached.

    `config` is the live config dict. SessionManager reloads by clearing and
    updating that dict in place rather than rebinding it, so holding the
    reference here stays correct across a /reload.
    """

    def __init__(self, config):
        self.config = config
        self._texts = {}
        # Held for dict access only, never across a file read. Two threads
        # racing the same uncached path will both read the file and store the
        # same text, which costs one redundant read and is otherwise harmless;
        # holding the lock across the I/O would serialise every behavior read
        # in the thread pool to buy nothing.
        self._lock = threading.Lock()

    # ------------------------------------------------------------ cached reads

    def read(self, path):
        """Cached read, synchronous: a missing file is a warning and ""."""
        with self._lock:
            if path in self._texts:
                return self._texts[path]
        p = paths.resolve(path)
        text = p.read_text() if p.exists() else ""
        if not p.exists():
            logger.warning("behavior file not found: %s", p)
        with self._lock:
            return self._texts.setdefault(path, text)

    def read_session(self, path):
        """Cached read of a session layer. Quietly "" when absent.

        Unlike the other two: most sessions have no file and are not meant to,
        so a warning per attach would be noise.
        """
        key = str(path)
        with self._lock:
            if key in self._texts:
                return self._texts[key]
        text = path.read_text() if path.exists() else ""
        with self._lock:
            return self._texts.setdefault(key, text)

    # ------------------------------------------------------------- which files

    def session_path(self, session_name):
        """models/sessions/<session>.md for this session, or None.

        None when the layer is switched off (no behavior.session_dir), or when
        the name will not make a safe filename.

        Session names are not validated on attach — server.py takes ?session=
        from the query and mgr.get() creates whatever it is handed — so a
        session called `../../etc/passwd` can exist. This is the one place a
        session name becomes a path that is *read*, so it is guarded twice: the
        name is slugged to the same character set session._safe() allows, and
        the result is then required to sit directly in session_dir. Either
        alone would do; both is cheap, and the failure mode is an arbitrary
        file landing in a system prompt.
        """
        configured = (self.config.get("behavior", {}) or {}).get("session_dir")
        if not configured:
            return None
        slug = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in str(session_name))[:64]
        if not slug or set(slug) <= {".", "_"}:
            return None
        root = paths.resolve(configured)
        candidate = (root / f"{slug}.md").resolve()
        if candidate.parent != root.resolve():
            logger.warning("session %r does not make a safe behavior filename "
                           "— skipping its layer", session_name)
            return None
        return candidate

    def keys_for(self, entry, session_name=None):
        """The cache keys layers_sync would read for this session.

        Same strings read() and read_session() key on, so a caller can ask
        which of its own three files a refresh touched.
        """
        keys = [self.config.get("behavior", {}).get("base_file") or DEFAULT_BASE_FILE,
                entry.get("behavior_file", "models/default.md")]
        path = self.session_path(session_name) if session_name else None
        if path is not None:
            keys.append(str(path))
        return keys

    def layers_sync(self, entry, session_name=None):
        """(base, model, session) behavior — the three layers of the prompt.

        The shared base file, then the model's own, then one named for the
        session. All cached; a missing file is "" (warned), which is what makes
        the third layer optional per session rather than per install.
        """
        base = self.read(
            self.config.get("behavior", {}).get("base_file") or DEFAULT_BASE_FILE)
        # Per model, because it is a property of how the model reads a prompt,
        # not of the prompt. Applied after the cached read so the cache still
        # holds the file as written.
        if entry.get("strip_directives"):
            base = strip_directive_sections(base)
        model = self.read(entry.get("behavior_file", "models/default.md"))

        session = ""
        path = self.session_path(session_name) if session_name else None
        if path is not None:
            session = self.read_session(path)
        return base, model, session

    # ------------------------------------------------------- reconciling /reload

    def stale(self):
        """Cache keys whose file on disk no longer matches what was read.

        Content, not mtime: an editor that writes a file back byte-identical
        should not be reported as a change, and these are a handful of small
        local files, so reading them all costs less than being wrong. Every
        path ever consulted is a key — a missing file caches "" — so a
        session .md created after the session attached is caught too.

        A file that has since been deleted compares as "", which is what
        read() would cache for it now.
        """
        # Snapshot under the lock and compare outside it. Iterating the live
        # dict was the bug: a read on another thread inserting mid-loop raises
        # "dictionary changed size during iteration" and takes the reload with
        # it. A key added after this snapshot was just read from disk, so it is
        # current by construction and nothing is missed by leaving it out.
        with self._lock:
            snapshot = list(self._texts.items())
        stale = []
        for key, cached in snapshot:
            p = paths.resolve(key)
            try:
                current = p.read_text() if p.exists() else ""
            except OSError as e:
                # Not stale, not fatal: keep the cached text and say so. The
                # alternative is refusing the whole reload over one unreadable
                # file that may not even be in use any more.
                logger.warning("behavior file unreadable, keeping cached: %s: %s", p, e)
                continue
            if current != cached:
                stale.append(key)
        return stale

    def refresh_sync(self):
        """Re-read every changed behavior file into the cache. Returns the keys.

        A file that still exists but has gone *empty* having had content is
        treated as a save in progress, not a deletion: the cached text is kept
        and the caller warned. Truncation in general cannot be detected — empty
        is the case that would otherwise blank a session's instructions, and it
        is the state an editor passes through on the way to writing a file out.

        A file that is *gone* is a deletion and is applied. The old guard tested
        only the text, so the two looked identical and a deleted layer was never
        dropped: stale() reported it on every reload, refresh skipped it every
        time with that warning, and the layer stayed in the prompt until the
        core restarted. exists() is the thing that tells them apart.
        """
        refreshed = []
        for key in self.stale():
            p = paths.resolve(key)
            exists = p.exists()
            try:
                text = p.read_text() if exists else ""
            except OSError as e:
                logger.warning("behavior file unreadable, keeping cached: %s: %s", p, e)
                continue
            with self._lock:
                had = self._texts.get(key, "")
                mid_save = exists and not text.strip() and had.strip()
                if not mid_save:
                    self._texts[key] = text
            if mid_save:
                logger.warning("behavior file %s is empty — keeping the cached text "
                               "(a save in progress?)", p)
                continue
            refreshed.append(key)
        return refreshed
