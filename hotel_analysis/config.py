import os
from pathlib import Path
from dotenv import load_dotenv

# .env dosyasını hotel_analysis/ ile aynı dizinden veya proje kökünden yükle
_here = Path(__file__).parent
load_dotenv(_here / ".env", override=False)
load_dotenv(_here.parent / ".env", override=False)

# Database
PROD_DB_URL = os.environ["PC_DB_URL"]

EUROPROTEL_DB_URL = os.environ["PC_EUROPROTEL_DB_URL"]

# Email — reservation_code pipeline
EMAIL_USER = os.environ["PC_EMAIL_USER"]
EMAIL_PASS = os.environ["PC_EMAIL_PASS"]
IMAP_HOST = "imap.gmail.com"

# Email — history-forecast / market_segment pipeline
HF_EMAIL_USER = os.environ["PC_HF_EMAIL_USER"]
HF_EMAIL_PASS = os.environ["PC_HF_EMAIL_PASS"]

# Booking API
API_BASE_URL = "https://bookingapi.pricing-coach.com/api/v1"
API_AUTH_URL = f"{API_BASE_URL}/authenticate"
API_UPLOAD_URL = f"{API_BASE_URL}/booking/hotel/upload-file"
API_DELETE_URL = f"{API_BASE_URL}/booking/hotel/delete-file-data"
API_EMAIL = os.environ["PC_API_EMAIL"]
API_PASSWORD = os.environ["PC_API_PASSWORD"]

# Log API
LOG_API_BASE_URL = "https://bookingapi-py.pricing-coach.com/api/v1"
LOG_API_URL = f"{LOG_API_BASE_URL}/booking/integration-log/save"
LOG_AUTH_URL = f"{LOG_API_BASE_URL}/auth/token"
MARKET_ANALYSIS_INSERT_URL = f"{LOG_API_BASE_URL}/market-analysis/insert"
ROOM_TYPES_BY_HOTEL_URL = f"{LOG_API_BASE_URL}/roomanalysis/room-types"

# Paths
BASE_DIR = r"C:\Users\Administrator\Documents\pricing-coach"
HOTELS_DOC_DIR = os.path.join(BASE_DIR, "hotels-document")
RESERVATION_DIR = os.path.join(HOTELS_DOC_DIR, "reservation-report")
DAILY_UPLOAD_DIR = os.path.join(RESERVATION_DIR, "daily_upload")
LOOKUP_DIR = os.path.join(BASE_DIR, "hotels-code", "reservation_code")
