from __future__ import annotations

import logging

COMPONENT_LOG_FORMAT = '[%(name)s] %(levelname)s: %(message)s'
APPLICATION_LOGGER_PREFIXES = ('app', 'buyer')


def configure_component_logging() -> None:
    formatter = logging.Formatter(COMPONENT_LOG_FORMAT)
    uvicorn_logger = logging.getLogger('uvicorn')
    handlers = list(uvicorn_logger.handlers)
    if not handlers:
        handler = logging.StreamHandler()
        uvicorn_logger.addHandler(handler)
        handlers = [handler]

    for handler in handlers:
        handler.setFormatter(formatter)

    for logger_name in APPLICATION_LOGGER_PREFIXES:
        app_logger = logging.getLogger(logger_name)
        app_logger.handlers.clear()
        for handler in handlers:
            app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False
