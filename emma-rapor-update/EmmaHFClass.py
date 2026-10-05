import json
import os
import sys
from difflib import get_close_matches

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import DriveIntegration
from EmmaAnalysis import EmmaAnalysis
from hotel_analysis.fetch_lookup import fetch_emma_capacity

# lookup.xlsx: oda tipi eşleştirme için (emma-rapor-update dizininde bulunur)
_LOOKUP_FILE = os.path.join(os.path.dirname(__file__), "lookup.xlsx")

_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "hotel_analysis", "hotel_configs.json")


def _load_emma_configs_from_json(json_path):
    """hotel_configs.json'daki 'emma' bloklarından hotel_configs dict'ini oluşturur."""
    with open(json_path, encoding="utf-8") as f:
        configs = json.load(f)
    result = {}
    for hotel_key, hotel in configs.items():
        if hotel_key.startswith("_"):
            continue
        emma = hotel.get("emma")
        if not emma:
            continue
        company_id = emma["company_id"]
        result[company_id] = {
            "folder": emma["folder"],
            "capacity": emma.get("capacity"),  # fallback — API başarısızsa kullanılır
        }
    return result


class EmmaHFClass:
    def __init__(self, collection_date=None, file_date=None):

        self.hotel_configs = _load_emma_configs_from_json(_JSON_PATH)
        self.id_array = list(self.hotel_configs.keys())
        self.current_company_id = 52
        self.current_index = 0
        self.file_date = file_date
        self.collection_date = collection_date
        self.process_cycles = len(self.id_array)

        self.lookupDf = pd.read_excel(_LOOKUP_FILE)
        self.capacity_map = self._build_capacity_map()

        # Initialize data containers
        self.data_room_type = None
        self.directory = None

        # File extensions to process
        self.file_extensions = ['.xls', '.XLSX', '.xlsx']

    def _build_capacity_map(self):
        """Tüm Emma company_id'leri için API'den kapasite çeker.

        Returns:
            { company_id: {'total': int, 'by_room_type_id': {id: capacity}} }
        'no_show' kategorisi endpoint tarafında filtrelenmiş gelir.
        JSON'daki capacity değeri API başarısızsa fallback olarak kullanılır.
        """
        capacity_map = {}
        for company_id in self.id_array:
            df = fetch_emma_capacity(company_id)
            if not df.empty and 'capacity' in df.columns and 'room_type_id' in df.columns:
                valid = df[df['capacity'].notna() & (df['capacity'] > 0)]
                capacity_map[company_id] = {
                    'total': int(valid['capacity'].sum()),
                    'by_room_type_id': valid.set_index('room_type_id')['capacity'].to_dict(),
                }
                print(f"Kapasite yuklendi: company_id={company_id}, total={capacity_map[company_id]['total']}")
            else:
                fallback = self.hotel_configs[company_id].get('capacity')
                if fallback:
                    capacity_map[company_id] = {'total': fallback, 'by_room_type_id': {}}
                    print(f"Kapasite fallback (JSON): company_id={company_id}, total={fallback}")
        return capacity_map

    def get_current_hotel_config(self):
        return self.hotel_configs[self.current_company_id]

    def change_hotel_id(self):
        if self.current_index < len(self.id_array) - 1:
            self.current_index += 1
            self.current_company_id = self.id_array[self.current_index]
            return True
        return False

    def reset_hotel_sequence(self):
        self.current_index = 0
        self.current_company_id = self.id_array[0]

    def setup_current_hotel(self):
        config = self.get_current_hotel_config()
        folder = config["folder"]

        print(f"Setting up hotel: {self.current_company_id}")
        if self.check_directory_exists():
            try:
                for file in list(self.files):
                    if 'occroomtype' in file['name']:
                        self.data_room_type = DriveIntegration.main(file['id'], 'csv')
                        if self.data_room_type is None:
                            print(f"ERROR: Failed to load occroomtype file: {file['name']}")
                            return False
                        self.room_type_id = file['id']
                        self.files.remove(file)
                        break

                if self.data_room_type is None:
                    print(f"ERROR: No 'occroomtype' file found for hotel {folder}")
                    return False

                print(f"Hotel {folder} setup completed")
                return True

            except Exception as e:
                print(f"Error setting up hotel {folder}: {str(e)}")
                return False

    def check_directory_exists(self):
        self.files = DriveIntegration.get_file_names(self.get_current_hotel_config()['folder'], self.file_date)
        exists = self.files[0]['name'] if self.files else None
        if exists:
            config = self.get_current_hotel_config()
            print(f'{config["folder"]} files exist')
        else:
            try:
                print(f'Files do not exist: {self.files["name"]}')
            except Exception:
                pass
        return exists

    def find_next_available_hotel(self):
        original_company_id = self.current_company_id

        while True:
            if self.setup_current_hotel():
                return True

            if not self.change_hotel_id():
                break

            if self.current_company_id == original_company_id:
                break

        print("No available hotel directories found")
        return False

    def process_file_name_matching(self, filename):
        file_base = os.path.splitext(filename)[0]
        cleaned_name = file_base.split("-")[0]

        company_lookup = self.lookupDf.loc[
            self.lookupDf["company_id"] == self.current_company_id
        ].reset_index(drop=True)

        if cleaned_name in company_lookup['name'].values:
            closest_match = [cleaned_name]
        else:
            closest_match = get_close_matches(
                cleaned_name,
                company_lookup['name'],
                n=1,
                cutoff=0.6
            )

        print(f"Filename: {filename}")
        print(f"Cleaned name: {cleaned_name}")
        print(f"Closest match: {closest_match}")

        return closest_match, company_lookup

    def process_hotel_files(self, files):
        for file in files:
            closest_match, company_lookup = self.process_file_name_matching(file['name'])
            if 'occ forecast' in file['name']:
                # Hotel-wide processing
                data = DriveIntegration.main(file['id'], 'xlsx')
                if closest_match:
                    matched_name = closest_match[0]
                    matched_data = company_lookup[company_lookup['name'] == matched_name].iloc[0]

                    hotel_booking_room_type_id = matched_data['hotel_booking_room_type_id']
                    cap_data = self.capacity_map.get(self.current_company_id, {})
                    inventory_room = cap_data.get('total') or self.hotel_configs[self.current_company_id].get('capacity')

                    if inventory_room is None:
                        print(f"HATA: company_id={self.current_company_id} için capacity bulunamadi.")
                        return

                    print(f"File: {file['name']}, Matched name: {matched_name}")
                    print(f"company_id: {self.current_company_id}, hotel_booking_room_type_id: {hotel_booking_room_type_id}, inventory_room: {inventory_room}")

                    data["company_id"] = self.current_company_id

                    analyze_process = EmmaAnalysis(
                        self.data_room_type, data, self.current_company_id,
                        hotel_booking_room_type_id, inventory_room, matched_name, self.collection_date
                    )
                    analyze_process.preprocess_data()
                    analyze_process.add_additional_columns()
                    analyze_process.reorder_columns()
                    analyze_process.finalize_occ()
                    analyze_process.manipulate_values()
                    analyze_process.insert_room_analysis()
                    DriveIntegration.move_to_archive(file['id'], self.get_current_hotel_config()['folder'])
                else:
                    print(f"File: {file['name']} - no matching name found.")
            else:
                # Room type processing
                if 'occroomtype' not in file['name']:
                    data_room_type_select = DriveIntegration.main(file['id'], 'xlsx')

                    if closest_match:
                        matched_name = closest_match[0]
                        matched_data = company_lookup[company_lookup['name'] == matched_name].iloc[0]

                        hotel_booking_room_type_id = matched_data['hotel_booking_room_type_id']
                        cap_data = self.capacity_map.get(self.current_company_id, {})
                        inventory_room = (
                            cap_data.get('by_room_type_id', {}).get(hotel_booking_room_type_id)
                            or matched_data.get('inventory_rooms')
                            or 0
                        )

                        print(f"File: {file['name']}, Matched name: {matched_name}")
                        print(f"company_id: {self.current_company_id}, hotel_booking_room_type_id: {hotel_booking_room_type_id}, inventory_room: {inventory_room}")

                        analyze_process = EmmaAnalysis(
                            self.data_room_type, data_room_type_select, self.current_company_id,
                            hotel_booking_room_type_id, inventory_room, matched_name, self.collection_date
                        )
                        analyze_process.preprocess_data()
                        analyze_process.add_additional_columns()
                        analyze_process.reorder_columns()
                        analyze_process.finalize_data()
                        analyze_process.manipulate_values()
                        analyze_process.insert_room_analysis()
                        DriveIntegration.move_to_archive(file['id'], self.get_current_hotel_config()['folder'])
                    else:
                        print(f"File: {file} - no matching name found.")

    def run_single_cycle(self):
        if self.setup_current_hotel():
            return self.process_hotel_files(files=self.files)
        else:
            return self.find_next_available_hotel() and self.process_hotel_files(files=self.files)

    def run(self):
        print(f"Starting Emma Multi-Hotel processing")
        print(f"Processing {self.process_cycles} cycles")
        print(f"Hotel sequence: {self.id_array}")

        try:
            if not self.find_next_available_hotel():
                print("No available hotels found for processing")
                return

            for cycle in range(self.process_cycles):
                print(f"\n=== Processing Cycle {cycle + 1}/{self.process_cycles} ===")
                print(f"Current hotel: {self.current_company_id} ({self.get_current_hotel_config()['folder']})")

                self.run_single_cycle()
                DriveIntegration.move_to_archive(self.room_type_id, self.get_current_hotel_config()['folder'])

                if self.current_company_id != 2640:
                    if not self.change_hotel_id():
                        print("Reached end of hotel sequence")
                        break

                    if not self.setup_current_hotel():
                        if not self.find_next_available_hotel():
                            print("No more available hotels")
                            break

            print("\nEmma Multi-Hotel processing completed.")

        except Exception as e:
            print(f"Error in main processing: {str(e)}")
            raise

    def process_specific_hotel(self, company_id):
        if company_id not in self.hotel_configs:
            print(f"Invalid company ID: {company_id}")
            return False

        original_company_id = self.current_company_id
        self.current_company_id = company_id
        self.current_index = self.id_array.index(company_id)

        try:
            print(f"Processing specific hotel: {company_id}")
            success = self.run_single_cycle()
            return success
        finally:
            self.current_company_id = original_company_id
            self.current_index = self.id_array.index(original_company_id)

    def get_processing_summary(self):
        summary = []
        for company_id, config in self.hotel_configs.items():
            summary.append({
                'company_id': company_id,
                'folder': config["folder"],
            })
        return summary
