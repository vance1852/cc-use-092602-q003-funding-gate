"""门禁服务向 API 和命令行暴露的稳定错误。"""


class FundingError(RuntimeError):
    code = "funding_error"
    status = 400


class NotFound(FundingError):
    code = "not_found"
    status = 404


class Conflict(FundingError):
    code = "conflict"
    status = 409


class Forbidden(FundingError):
    code = "forbidden"
    status = 403


class InvalidState(FundingError):
    code = "invalid_state"
    status = 409


class ValidationFailed(FundingError):
    code = "validation_failed"
    status = 422
