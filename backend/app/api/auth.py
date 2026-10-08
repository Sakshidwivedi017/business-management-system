import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection

from app.auth.dependencies import get_current_user, to_authenticated_user
from app.auth.permissions import AuthenticatedUser
from app.auth.security import authenticate_user, create_access_token
from app.db.connection import get_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthenticatedUser


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, conn: Connection = Depends(get_connection)) -> LoginResponse:
    user = authenticate_user(conn, body.email, body.password)
    if user is None:
        logger.info("Login failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = create_access_token(user["id"], user["token_version"])
    logger.info("Login succeeded for user %s", user["id"])
    return LoginResponse(access_token=token, expires_in=expires_in, user=to_authenticated_user(user))


@router.get("/me", response_model=AuthenticatedUser)
def me(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    return user
