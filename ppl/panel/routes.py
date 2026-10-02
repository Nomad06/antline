"""Every /api route the studio adds, in one table. The run routes the panel
had before the studio stay in server.Handler as they were."""
from . import api_core, api_project, api_routing, api_setup, api_static
from .router import Router


def build():
    router = Router()
    api_static.register(router)
    api_core.register(router)
    api_setup.register(router)
    api_routing.register(router)
    api_project.register(router)
    return router
