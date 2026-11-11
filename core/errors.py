"""사용자 입력·키·공급자 원문을 노출하지 않는 오류."""


class AnalysisError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code, self.code, self.message = status_code, code, message
        super().__init__(code)
