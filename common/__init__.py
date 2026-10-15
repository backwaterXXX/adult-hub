"""Adult-Hub common package: shared config loader and contracts.

Installed into site-packages of both images (resolver, sync) so bind
mounts of ./resolver:/app and ./sync:/app cannot shadow it. Locally it
is imported from the repo root (``from common.config_loader import ...``).
"""

__all__ = ["config_loader", "image_utils"]
