"""Ephemeral private files are removed on completion, disconnect, and failure."""
import shutil

from fastapi.responses import FileResponse


class EphemeralFileResponse(FileResponse):
    def __init__(self, path, *, private_root, **kwargs):
        self.private_root = private_root
        super().__init__(path, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            shutil.rmtree(self.private_root, ignore_errors=True)
