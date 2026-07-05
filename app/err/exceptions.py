"""Simple wrappers for a few of the unique failure states I've observed while dev/test"""


class NoAuthTokenError(Exception):
    """Exception for missing/required auth."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message, status_code, payload)


class ModemHtmlError(Exception):
    """The modem returned HTML we could not parse as expected.

    e.g. an element we rely on is missing -- an unexpected page or a
    firmware/markup change, as opposed to an expected empty/placeholder value."""


class ModemNotOkError(Exception):
    """Exception for non-200/OK responses from modem."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message, status_code, payload)
