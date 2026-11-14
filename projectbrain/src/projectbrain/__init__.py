__version__ = "1.3.0"
__all__ = ["ProjectBrain", "__version__"]


def __getattr__(name: str):
    if name == "ProjectBrain":
        from .brain import ProjectBrain
        return ProjectBrain
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
