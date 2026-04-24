import logging

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    logger = logging.getLogger(__name__)
    logger.info("Start Werewolf server: 0.0.0.0:8765")
    uvicorn.run(
        "server.app:app",
        host="0.0.0.0",
        port=8765,
        reload=True,
        reload_includes=["server/*", "templates/*", "config/"],
        log_level="error",
    )


if __name__ == "__main__":
    main()
