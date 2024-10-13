"""Simple wrappers for a few of the unique failure states I've observed while dev/test"""


class NoAuthTokenError(Exception):
    """Exception for missing/required auth."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message, status_code, payload)

class ModemNotOkError(Exception):
    """Exception for non-200/OK responses from modem."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message, status_code, payload)
