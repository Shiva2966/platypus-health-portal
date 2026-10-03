class FieldError(Exception):
    """Validation failure with per-field messages -> HTTP 422 {detail, fields}. Handler is in main.py."""

    def __init__(self, fields: dict[str, str], detail: str = "Please check the highlighted fields."):
        self.fields = fields
        self.detail = detail
        super().__init__(detail)
