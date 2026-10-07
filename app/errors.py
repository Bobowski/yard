class YardError(Exception):
    """One API failure. status, code, and message are the JSON body."""

    def __init__(self, status: int, code: str, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.exit_code = exit_code
