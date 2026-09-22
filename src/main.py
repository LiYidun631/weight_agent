"""本地开发启动入口。"""

import logging

import uvicorn

from weight_agent.core.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info(
        "Starting Weight Agent on %s:%s (reload=%s)",
        settings.host,
        settings.port,
        settings.reload,
    )
    uvicorn.run(
        "weight_agent.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )
