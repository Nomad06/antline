"""Every /api route the studio adds, in one table. The run routes the panel
had before the studio stay in server.Handler as they were."""
from . import api_core, api_routing, api_setup
from .router import Router


def build():
    router = Router()
    api_core.register(router)
    api_setup.register(router)
    api_routing.register(router)
    return router
