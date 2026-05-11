import logging

from hikvision_viewer.main import main
from hikvision_viewer.logging_utils import configure_logging

if __name__ == "__main__":
    configure_logging()
    logging.getLogger(__name__).info("Launching package entrypoint __main__")
    main()
