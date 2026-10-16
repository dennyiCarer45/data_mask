class PrivacyError(Exception):
    """Only fixed, public error codes may cross the API boundary."""

    def __init__(self, code: str = "privacy_validation_failed", status: int = 422):
        self.code = code
        self.status = status
        super().__init__(code)
