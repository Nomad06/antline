class PplError(Exception):
    """Every failure carries a routable code, never free text alone."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self):
        return {"error_code": self.code, "message": self.message}
