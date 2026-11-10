"""Minimal stand-in for the `loguru` package.

The vendored `mobi` package only ever calls ``logger.debug(...)``.
This shim provides the same call surface as no-ops so the app does
not need the real loguru dependency on the NAS. Anything logged is
dropped silently; the app reports errors through its own UI.
"""


class _Logger:
    def _noop(self, *args, **kwargs):
        return None

    debug = _noop
    info = _noop
    warning = _noop
    error = _noop
    exception = _noop
    critical = _noop
    success = _noop
    trace = _noop

    def add(self, *args, **kwargs):
        return 0

    def remove(self, *args, **kwargs):
        return None

    def disable(self, *args, **kwargs):
        return None

    def enable(self, *args, **kwargs):
        return None

    def bind(self, *args, **kwargs):
        return self

    def opt(self, *args, **kwargs):
        return self


logger = _Logger()
