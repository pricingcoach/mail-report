import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from RadissonReservationClass import RadissonReservationClass

_JSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hotel_analysis", "hotel_configs.json"
)


def _load_hotel_list_from_json(json_path):
    """hotel_configs.json'daki 'radisson' bloklarından hotel_id listesini oluşturur."""
    with open(json_path, encoding="utf-8") as f:
        configs = json.load(f)
    hotel_ids = []
    for hotel_key, hotel in configs.items():
        if hotel_key.startswith("_"):
            continue
        radisson = hotel.get("radisson")
        if radisson and radisson.get("hotel_id"):
            hotel_ids.append(radisson["hotel_id"])
    return hotel_ids


def main():
    """Main function to run the processor"""
    hotel_list = _load_hotel_list_from_json(_JSON_PATH)
    print(f"Loaded {len(hotel_list)} Radisson hotels from hotel_configs.json: {hotel_list}")

    for hotel_id in hotel_list:
        print(f"Processing hotel ID: {hotel_id}")
        processor = RadissonReservationClass(hotel_id=hotel_id)
        processor.process_all()

if __name__ == "__main__":
    main()