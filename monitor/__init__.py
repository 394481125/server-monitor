"""Server Monitor application package."""

def create_app(*args, **kwargs):
    # Avoid importing Flask and background services when a small library module is tested.
    from .app import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["create_app"]
