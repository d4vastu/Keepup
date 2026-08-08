from typing import Protocol, runtime_checkable


class StackUpdateError(RuntimeError):
    """A redeploy failed, carrying the output captured before it did.

    Without this the output of a *failed* redeploy is lost — the caller only
    ever sees ``str(exc)`` — which is precisely the case the activity log
    exists to explain. Subclasses RuntimeError so existing handlers that catch
    the broader type keep working.
    """

    def __init__(self, message: str, lines: list[str] | None = None):
        super().__init__(message)
        self.lines = list(lines or [])


@runtime_checkable
class ContainerBackend(Protocol):
    """
    Structural protocol for any container management backend.

    Stack dict shape returned by get_stacks_with_update_status:
      {
        "id":            str,   # opaque, unique within this backend
        "name":          str,   # human-readable stack/project name
        "endpoint_id":   str,   # host/environment identifier
        "endpoint_name": str,   # human-readable host/environment label
        "update_status": str,   # "update_available" | "up_to_date" | "unknown" | "mixed"
        "unknown_reason": str | None,  # coarse reason when update_status == "unknown"
        "images":        list,  # [{"name": str, "status": str, "reason": str | None}, ...]
        "update_path":   str,   # opaque path: "{backend_key}/{ref}"
      }
    """

    BACKEND_KEY: str

    async def get_stacks_with_update_status(
        self, dockerhub_creds: dict | None = None
    ) -> list[dict]: ...

    async def update_stack(self, ref: str) -> list[str]:
        """Pull and redeploy ``ref``. Returns the captured command output."""
        ...

    async def describe_ref(self, ref: str) -> str:
        """Human-readable name for ``ref``, for labelling a run.

        Portainer refs are ``{stack_id}:{endpoint_id}`` and carry no name at
        all, so the caller cannot derive one by parsing. Implementations should
        fall back to the ref rather than raising — a run must never fail
        because it could not be named nicely.
        """
        ...
