"""Turn what the person typed into API ids."""

from ssc_cli.api import ApiClient
from ssc_cli.errors import APP_NOT_FOUND, ENVIRONMENT_NOT_FOUND, local_error
from ssc_cli.models import AppOut, EnvironmentOut

APP_PREFIX = "app_"


def resolve_app(client: ApiClient, ref: str) -> AppOut:
    """An ``app_`` id, or a slug looked up in the org's app list."""
    if ref.startswith(APP_PREFIX):
        return client.get_app(ref)
    for app in client.list_apps().apps:
        if app.slug == ref:
            return client.get_app(app.id)
    raise local_error(
        APP_NOT_FOUND,
        "No such app.",
        f"No app with slug or id {ref!r} is visible to you. Run `ssc apps` to list them.",
    )


def environment(app: AppOut, name: str) -> EnvironmentOut:
    for env in app.environments:
        if env.name == name:
            return env
    raise local_error(
        ENVIRONMENT_NOT_FOUND,
        "No such environment.",
        f"App {app.slug} has no {name!r} environment.",
    )
