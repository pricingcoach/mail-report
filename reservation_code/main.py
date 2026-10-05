import sys
import os
import logging
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from EmailIntegration import EmailIntegration
from HotelUpdate import HotelUpdate

# Configure logging for all modules
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
    ]
)

# ── Ayarlar ─────────────────────────────────────────────────
# CLI:     python main.py --no-mail --hotel 00479
# Env var: SKIP_MAIL=true ONLY_HOTELS=00479 python main.py
# ────────────────────────────────────────────────────────────

_env_skip_mail = os.environ.get("SKIP_MAIL", "false").lower() == "true"
_env_only_hotels = os.environ.get("ONLY_HOTELS", None)

parser = argparse.ArgumentParser(description="Reservation Code Pipeline")
parser.add_argument("--no-mail", action="store_true",
                    help="Email okumadan sadece dizindeki dosyalari isle")
parser.add_argument("--hotel", type=str, default=None,
                    help="Sadece belirtilen hotel_id'leri isle (virgul ile ayir: 00479,1555)")
args = parser.parse_args()

# Env var → CLI arg (CLI varsa CLI kazanır)
if _env_skip_mail and not args.no_mail:
    args.no_mail = True
if _env_only_hotels and not args.hotel:
    args.hotel = _env_only_hotels

logger = logging.getLogger(__name__)

if args.no_mail:
    logger.warning("*** SKIP_MAIL aktif — email okuma DEVRE DISI! Sadece dizindeki dosyalar islenir. ***")
if args.hotel:
    logger.warning("*** ONLY_HOTELS aktif — sadece su oteller islenir: %s ***", args.hotel)

hotel_update = HotelUpdate()

if not args.no_mail:
    mail = EmailIntegration()
    mail.save_attachment(hotel_update.get_email_mappings())

    # Royan email attachment download (uncomment when needed)
    # royan_df = hotel_update.get_royan_email_mappings()
    # if not royan_df.empty:
    #     royan_mail = EmailIntegration()
    #     royan_mail.save_royan_attachment(royan_df)

hotel_ids = None
if args.hotel:
    hotel_ids = [h.strip() for h in args.hotel.split(",")]

hotel_update.process_hotels(hotel_ids=hotel_ids, skip_date_check=args.no_mail)
