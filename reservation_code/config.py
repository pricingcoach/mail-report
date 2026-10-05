import sys
import os

# Add parent directory to path for hotel_analysis imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Re-export from shared config for backward compatibility
from hotel_analysis.config import (
    PROD_DB_URL as DB_URL,
    EMAIL_USER, EMAIL_PASS, IMAP_HOST,
    API_BASE_URL, API_AUTH_URL, API_UPLOAD_URL, API_DELETE_URL,
    API_EMAIL, API_PASSWORD,
    LOG_API_BASE_URL, LOG_API_URL, LOG_AUTH_URL,
    BASE_DIR, HOTELS_DOC_DIR, RESERVATION_DIR, DAILY_UPLOAD_DIR, LOOKUP_DIR,
)
