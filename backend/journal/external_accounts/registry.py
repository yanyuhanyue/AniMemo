from .errors import unsupported_account_provider
from .providers import BangumiAccountProvider


_PROVIDERS = (BangumiAccountProvider(),)
_PROVIDERS_BY_SLUG = {provider.slug: provider for provider in _PROVIDERS}


def iter_account_providers():
    return _PROVIDERS


def get_account_provider(provider_slug):
    slug = str(provider_slug or "").strip().lower()
    provider = _PROVIDERS_BY_SLUG.get(slug)
    if provider is None:
        raise unsupported_account_provider(slug)
    return provider
