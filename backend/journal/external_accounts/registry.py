from .errors import unsupported_account_provider
from .providers.bangumi import BangumiAccountProvider

_PROVIDERS = {"bangumi": BangumiAccountProvider()}


def get_account_provider(provider_slug):
    slug = str(provider_slug or "").strip().lower()
    provider = _PROVIDERS.get(slug)
    if provider is None:
        raise unsupported_account_provider(slug)
    return provider
