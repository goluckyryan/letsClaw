# Coding posture: smallest complete change

Smallest complete change, in order: does it need to exist → is it already in this codebase → stdlib/platform → an installed dep → one line → the minimum that works. Lazy about the solution, never about the change.

Before editing, list everywhere the change must reach: callers, tests, fixtures, config, exports. A bug fix means grepping every caller first and fixing the root cause once in shared code. Code you move or merge keeps its error handling and validation. Between two options of equal size, take the one correct on edge cases.

Never cut: trust-boundary validation, data-loss error handling, security, accessibility, hardware calibration, anything the user asked for.

No speculative abstraction, config or boilerplate. Non-trivial new logic leaves one small test. A deliberate shortcut gets a `shortcut: <limit>, <when to upgrade>` comment.

End the answer with what was skipped or not checked, and the risk.
