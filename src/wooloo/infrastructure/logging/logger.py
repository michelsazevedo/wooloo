"""
The application's single logger entrypoint.

"""

import structlog

logger = structlog.get_logger()
