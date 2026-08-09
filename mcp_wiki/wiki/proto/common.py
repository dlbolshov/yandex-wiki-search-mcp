from dataclasses import dataclass, field


@dataclass
class YandexAuth:
    token: str | None = field(default=None, repr=False)
    cloud_org_id: str | None = None
    org_id: str | None = None


def select_org(
    auth: YandexAuth | None,
    *,
    default_org_id: str | None,
    default_cloud_org_id: str | None,
) -> tuple[str | None, str | None]:
    """Pick the organization for a request as ``(org_id, cloud_org_id)``.

    Per-request auth replaces the organization *as a unit*. Picking each id
    independently would pair a request's cloud_org_id with the server-wide
    org_id and fail as "only one of" — so under OAuth a client could not
    select a cloud organization on a server that has a plain org configured
    as its default.

    Shared so that everything reporting or using the organization agrees:
    the client builds request headers from this, and the configuration
    resource answers from it, instead of re-deriving the rule and drifting.
    """
    if auth and (auth.org_id or auth.cloud_org_id):
        return auth.org_id, auth.cloud_org_id
    return default_org_id, default_cloud_org_id
