class DomainError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422, field_errors=None):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status
        self.field_errors = field_errors or []

    def __reduce__(self):
        # Pure review calculations return errors across a spawned process boundary.
        return (DomainError, (self.code, self.detail, self.status, self.field_errors))
