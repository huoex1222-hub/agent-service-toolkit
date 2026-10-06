import logging
import sys

import uvicorn
from dotenv import load_dotenv

from core import settings

# override=True: .env wins over vars already in the process environment. Without it a
# HTTP_PROXY injected by the launcher (VS Code's proxy support picks up the Windows system
# proxy) silently shadows the one in .env, because dotenv only fills in missing keys.
load_dotenv(override=True)

if __name__ == "__main__":
    root_logger = logging.getLogger()
    if root_logger.handlers:
        print(
            f"Warning: Root logger already has {len(root_logger.handlers)} handler(s) configured. "
            f"basicConfig() will be ignored. Current level: {logging.getLevelName(root_logger.level)}"
        )

    logging.basicConfig(level=settings.LOG_LEVEL.to_logging_level())
    # psycopg's async mode can't run on Windows' default ProactorEventLoop. uvicorn >= 0.36
    # builds the loop from a factory rather than consulting the event loop policy, so the
    # selection has to go through `loop` as a dotted path.
    # https://www.psycopg.org/psycopg3/docs/advanced/async.html#asynchronous-operations
    loop = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"
    uvicorn.run(
        "service:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.is_dev(),
        timeout_graceful_shutdown=settings.GRACEFUL_SHUTDOWN_TIMEOUT,
        loop=loop,
    )
