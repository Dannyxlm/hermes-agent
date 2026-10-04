"""Resolve only the native request profile; integration stores remain root-scoped."""
from hermes_constants import get_default_hermes_root, get_hermes_home


def profile_id():
    home = get_hermes_home().resolve()
    root = get_default_hermes_root().resolve()
    if home == root:
        return 'default'
    if home.parent == root / 'profiles':
        return home.name
    # Custom launch homes are the default profile of that installation.
    raise ValueError('Unverified profile scope')


def state_dir():
    return get_default_hermes_root() / 'webui'
