from aiogram import Router
from app.handlers.admin import create_admin_router
from app.handlers.user import create_user_router
from app.middlewares.errors import UserFacingErrors


def create_router(service, settings) -> Router:
    router = Router(name='monstal')
    router.message.middleware(UserFacingErrors())
    router.callback_query.middleware(UserFacingErrors())
    router.include_router(create_admin_router(service, settings))
    router.include_router(create_user_router(service, settings))
    return router
