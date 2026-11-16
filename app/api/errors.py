"""Stable JSON errors shared by every API route."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.api.schemas import ErrorBody


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.body = ErrorBody(code=code, message=message)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body.model_dump())

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        message = exc.errors()[0].get("msg", "Ungültige Anfrage.") if exc.errors() else str(exc)
        body = ErrorBody(code="invalid_request", message=message)
        return JSONResponse(status_code=422, content=body.model_dump())

    @app.exception_handler(KeyError)
    async def service_not_found(_request: Request, exc: KeyError) -> JSONResponse:
        body = ErrorBody(code="not_found", message=str(exc.args[0]) if exc.args else str(exc))
        return JSONResponse(status_code=404, content=body.model_dump())

    @app.exception_handler(ValueError)
    async def service_conflict(_request: Request, exc: ValueError) -> JSONResponse:
        body = ErrorBody(code="conflict", message=str(exc))
        return JSONResponse(status_code=409, content=body.model_dump())

    @app.exception_handler(RuntimeError)
    async def service_unavailable(_request: Request, exc: RuntimeError) -> JSONResponse:
        body = ErrorBody(code="service_unavailable", message=str(exc))
        return JSONResponse(status_code=503, content=body.model_dump())

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        body = ErrorBody(code=code, message=str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())
