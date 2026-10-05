import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hotel_analysis.config import EMAIL_USER, EMAIL_PASS
from EmailIntegration import EmailIntegration
from res_code import ResCode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Europrotel reservation islemi basliyor...")

    res_code = ResCode()

    mail = EmailIntegration(EMAIL_USER, EMAIL_PASS)
    mail.save_attachment(res_code.get_email_mappings())

    res_code.process_hotels()

    logger.info("Europrotel reservation islemi tamamlandi.")
