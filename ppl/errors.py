from contextlib import contextmanager


class PplError(Exception):
    """Every failure carries a routable code, never free text alone."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
        # Set by role_output(): the error refuses a role's output itself.
        self.of_role_output = False

    def as_dict(self):
        return {"error_code": self.code, "message": self.message}


@contextmanager
def role_output():
    """Marks a PplError raised inside as a refusal of the role's output, which counts
    toward the retry cap, unlike a refusal of how the command was called
    [R-REFUSAL-RETRY-CAP]."""
    try:
        yield
    except PplError as err:
        err.of_role_output = True
        raise
