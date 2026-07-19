from dataclasses import dataclass, field


@dataclass
class YandexAuth:
    token: str | None = field(default=None, repr=False)
    cloud_org_id: str | None = None
    org_id: str | None = None
