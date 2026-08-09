from .errors import unsupported_provider
from .providers.bangumi import BangumiProvider

_PROVIDER_TYPES = {
    BangumiProvider.slug: BangumiProvider,
}


def normalize_provider_slug(value):
    return str(value or "").strip().lower()


def get_provider(value):
    slug = normalize_provider_slug(value)
    provider_type = _PROVIDER_TYPES.get(slug)
    if provider_type is None:
        raise unsupported_provider(slug)
    return provider_type()


def provider_slugs():
    return tuple(_PROVIDER_TYPES)
