"""Workspace HTTP exception mapping, independent of route registration."""
import traceback
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from ..storage import MetadataError, _now
from ..domain_errors import ConflictError
from ..project_manager import MigrationRequiredError, PlacementConflictError

def install_error_handlers(app, storage):
    def log_error(exc):
        try:
            with storage._path(".workbench", "web-errors.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n{_now()}\n{''.join(traceback.format_exception(exc))}\n")
        except OSError:
            pass

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "参数格式不正确，请检查任务信息后重试。"}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse({"detail": str(exc.detail) if exc.status_code in {400, 409, 413} else "资源不存在或请求无效。"}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        log_error(exc)
        return JSONResponse({"detail": "操作未完成，详细原因已保存到本地日志。请刷新后检查状态。"}, status_code=500)

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        from ..accounts import CreditInsufficient
        from ..capacity import CapacityExceeded
        if isinstance(exc, CapacityExceeded):
            return JSONResponse({"detail": str(exc), "status": "capacity_exceeded"}, status_code=429)
        if isinstance(exc, CreditInsufficient):
            return JSONResponse({"detail": str(exc), "status": "credit_insufficient", "expected_credit": exc.expected, "remaining_credit": exc.remaining}, status_code=402)
        if isinstance(exc, MetadataError):
            log_error(exc)
        if isinstance(exc, (ConflictError, MigrationRequiredError, PlacementConflictError)):
            return JSONResponse({"detail": str(exc)}, status_code=409)
        message = "本地数据校验失败，请检查本地日志。" if isinstance(exc, MetadataError) else "输入校验失败：请检查图片内容、大小、模式、比例和版本来源。"
        return JSONResponse({"detail": message}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing_resource(request, exc):
        return JSONResponse({"detail": "项目、任务或图片不存在，请刷新后重试。"}, status_code=404)
