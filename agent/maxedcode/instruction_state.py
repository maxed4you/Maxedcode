"""Instruction-state: which path-scoped rules and folder docs the fold has already committed.

Rewind used to throw the whole fold away, which also threw away the baseline the cached prefix is
built on. opencode (`session/instruction-state.ts`) drops the fold only when a committed revert
really removed an update or the start of the epoch; otherwise the baseline — and with it the cache —
survives. So a rewind must know where each entry was injected, not just which files were seen.

An entry is (kind, file, seq): seq is the message index where that text was inserted. Rewinding to a
boundary keeps entries injected before it, and reports whether anything was actually removed.
"""

class InstructionState:
    def __init__(self):
        self.entries = []          # (kind, file, seq) — committed injections, in load order
        self.epoch = 0             # first message index of the current fold

    def add(self, kind, file, seq):
        self.entries.append((kind, str(file).lower(), seq))

    def seen(self, kind=None):
        if kind is None:
            return {e[1] for e in self.entries}
        return {e[1] for e in self.entries if e[0] == kind}

    def already(self, kind, file):
        return str(file).lower() in self.seen(kind)

    def rewind(self, boundary):
        """Drop entries injected at or after boundary. True only if the fold really changed."""
        before = len(self.entries)
        self.entries = [e for e in self.entries if e[2] < boundary]
        if len(self.entries) == before:
            return False
        if boundary <= self.epoch:
            self.epoch = boundary
        return True

    def shift(self, removed):
        """Messages at these (sorted) indices were deleted: move later entries back to their new index."""
        if removed:
            self.entries = [(k, f, seq - sum(1 for r in removed if r < seq)) for k, f, seq in self.entries]

    def compacted(self, cut, start):
        """Messages before cut were folded into a summary that now ends at index start.

        Their docs/rules are no longer visible to the model, so they are forgotten and come again on the
        next touch of such a file (DeepSeek Harness decides "already sent" by what the model sees). Before,
        the model worked without the folder's rules for the rest of the session after a compaction."""
        self.entries = [(k, f, seq - cut + start) for k, f, seq in self.entries if seq >= cut]

    def count(self):
        return len(self.entries)
