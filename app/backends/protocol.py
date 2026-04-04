from typing import Protocol, runtime_checkable


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
        "images":        list,  # [{"name": str, "status": str}, ...]
        "update_path":   str,   # opaque path: "{backend_key}/{ref}"
      }
    """

    BACKEND_KEY: str

    async def get_stacks_with_update_status(
        self, dockerhub_creds: dict | None = None
    ) -> list[dict]: ...

    async def update_stack(self, ref: str) -> None: ...
