"""Local shopping pages for browser tasks."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

ASSETS = Path(__file__).resolve().parent / "sandbox_assets"
router = APIRouter(prefix="/sandbox", tags=["browser-sandbox"])


@router.get("/", include_in_schema=False)
@router.get("/product", include_in_schema=False)
@router.get("/cart", include_in_schema=False)
def page(request: Request):
    response = FileResponse(ASSETS / "index.html", media_type="text/html")
    case_id = request.query_params.get("case")
    if case_id is not None:
        if case_id == "legacy":
            response.delete_cookie("shoptrace_case", path="/sandbox")
        else:
            from .cases import get_case_public
            try:
                get_case_public(case_id)
            except ValueError:
                raise HTTPException(404, "Case not found") from None
            response.set_cookie("shoptrace_case", case_id, httponly=True, samesite="strict", path="/sandbox")
    return response


@router.get("/assets/{name}", include_in_schema=False)
def asset(name: str, request: Request):
    if name not in {"styles.css", "app.js", "catalog.json"}:
        raise HTTPException(404, "Asset not found")
    media = {"styles.css": "text/css", "app.js": "text/javascript", "catalog.json": "application/json"}
    if name == "catalog.json" and request.cookies.get("shoptrace_case"):
        from .cases import get_catalog
        try:
            catalog = get_catalog(request.cookies["shoptrace_case"])
        except ValueError:
            raise HTTPException(404, "Case not found") from None
        return JSONResponse(catalog, headers={"Cache-Control": "no-store", "Vary": "Cookie"})
    headers = {"Cache-Control": "no-store", "Vary": "Cookie"} if name == "catalog.json" else None
    return FileResponse(ASSETS / name, media_type=media[name], headers=headers)
