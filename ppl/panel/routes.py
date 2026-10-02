"""Every route the studio registers, in one table: the /api routes it adds and
the /static files. The run routes the panel had before the studio stay in
server.Handler as they were."""
from . import api_core, api_files, api_project, api_routing, api_setup, api_static
from .router import Router


def build():
    router = Router()
    api_static.register(router)
    api_core.register(router)
    api_setup.register(router)
    api_routing.register(router)
    api_project.register(router)
    api_files.register(router)
    return router
