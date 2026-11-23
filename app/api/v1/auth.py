from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.responses import RedirectResponse

from app.api.dependencies import check_csrf, get_auth_service, require_user
from app.schemas.auth import UserProfile
from app.services.auth import OAUTH_TTL_SECONDS, AuthService

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.get("/github", response_class=RedirectResponse, status_code=302)
def github_login(
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
    next: Annotated[str, Query(max_length=1024)] = "/dashboard",
) -> RedirectResponse:
    login = service.start(next)
    settings = request.app.state.settings
    response = RedirectResponse(
        login.url,
        status_code=302,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )
    response.set_cookie(
        settings.oauth_cookie_name,
        login.browser_token,
        max_age=OAUTH_TTL_SECONDS,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/github/callback", response_class=RedirectResponse, status_code=303)
def github_callback(
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
    code: Annotated[str | None, Query(max_length=512)] = None,
    state: Annotated[str | None, Query(max_length=128)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> RedirectResponse:
    settings = request.app.state.settings
    login = service.finish(
        state,
        request.cookies.get(settings.oauth_cookie_name),
        None if error else code,
        old_session=request.cookies.get(settings.session_cookie_name),
    )
    response = RedirectResponse(
        settings.frontend_origin + login.next_path,
        status_code=303,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )
    response.set_cookie(
        settings.session_cookie_name,
        login.token,
        max_age=settings.session_ttl_seconds,
        expires=login.expires_at,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        settings.oauth_cookie_name,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.get("/me", response_model=UserProfile)
def me(user: Annotated[UserProfile, Depends(require_user)]) -> UserProfile:
    return user


@router.post(
    "/logout", status_code=204, response_class=Response, dependencies=[Depends(check_csrf)]
)
def logout(
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    settings = request.app.state.settings
    service.logout(request.cookies.get(settings.session_cookie_name))
    for name in (settings.session_cookie_name, settings.oauth_cookie_name):
        response.delete_cookie(
            name,
            path="/",
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="lax",
        )
    response.headers["Cache-Control"] = "no-store"
