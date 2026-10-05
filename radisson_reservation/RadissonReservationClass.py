import json
import os
import pandas as pd
import numpy as np
from datetime import datetime
from HotelDataAnalyzeClass import HotelDataAnalyzeClass
from hotel_analysis.fetch_lookup import FetchLookup
import DriveIntegration

_JSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hotel_analysis", "hotel_configs.json"
)


def _load_radisson_configs_from_json(json_path):
    """hotel_configs.json'daki 'radisson' bloklarından hotel_id→config dict'ini oluşturur."""
    with open(json_path, encoding="utf-8") as f:
        configs = json.load(f)
    result = {}
    for hotel_key, hotel in configs.items():
        if hotel_key.startswith("_"):
            continue
        radisson = hotel.get("radisson")
        if not radisson:
            continue
        hotel_id = radisson["hotel_id"]
        result[hotel_id] = {
            "name": radisson["hotel_name"],
            "folder": radisson["folder"],
        }
    return result


class RadissonReservationClass:

    _hotel_configs = None  # class-level cache to avoid repeated JSON reads

    @classmethod
    def _get_hotel_configs(cls):
        if cls._hotel_configs is None:
            cls._hotel_configs = _load_radisson_configs_from_json(_JSON_PATH)
        return cls._hotel_configs

    def __init__(self, hotel_id="00229"):
        """
        Args:
            hotel_id (str): Hotel ID code
        """
        self.hotel_id = hotel_id
        self.today = datetime.now()
        self.date = self.today.strftime("%d%m%Y")
        # self.date = '08092025'

        # Initialize data containers
        self.data_reservation = None
        self.data_cancel = None
        self.lookup = None
        self.reservation_file = f'ReservationsActivityReport{self.date}'
        self.cancel_file = f'PendingReservationsByStatus{self.date}'

        self.my_instance = HotelDataAnalyzeClass()
        self.fetch = FetchLookup()

        # Set hotel configuration
        self._set_hotel_config()

    def _set_hotel_config(self):
        """Set hotel-specific configuration based on hotel_id"""
        hotel_configs = self._get_hotel_configs()

        if self.hotel_id not in hotel_configs:
            raise ValueError(f"Hotel ID {self.hotel_id} not supported (not found in hotel_configs.json)")

        config = hotel_configs[self.hotel_id]
        self.hotel_name = config["name"]
        self.folder = config["folder"]
        self.lookup = self.fetch.fetch_data(self.hotel_id)

    def load_data(self):
        """Load all required data files"""
        print(f"Loading data for {self.hotel_name} (ID: {self.hotel_id})")
        print(f"Processing date: {self.date}")
        print("=" * 60)
        
        try:
            self.files = DriveIntegration.get_file_names(self.folder, self.date)
            
            if not self.files or len(self.files) == 0:
                print(f"Error: No files found for date {self.date} in folder {self.folder}")
                return False

            files_df = pd.DataFrame(self.files)
            
            if 'name' not in files_df.columns:
                print(f"Error: Files structure is invalid: {self.files}")
                return False

            files_df['base_name'] = files_df['name'].str.replace(r'\.(xlsx|XLSX)$', '', regex=True).str.replace(r'_\d+$', '', regex=True)
            
            self.reservation_file = files_df[files_df['base_name'] == self.reservation_file]
            self.cancel_file = files_df[files_df['base_name'] == self.cancel_file]
            
            if len(self.reservation_file) == 0:
                print(f"Error: Reservation file not found. Looking for: {self.reservation_file}")
                print(f"Available files: {files_df['base_name'].tolist()}")
                return False
                
            if len(self.cancel_file) == 0:
                print(f"Error: Cancel file not found. Looking for: {self.cancel_file}")
                print(f"Available files: {files_df['base_name'].tolist()}")
                return False
            
            self.data_reservation = DriveIntegration.main(self.reservation_file['id'].values[0], 'xlsx')
            # Load cancel data
            self.data_cancel = DriveIntegration.main(self.cancel_file['id'].values[0], 'xlsx')
            # Load lookup data
            
            return True
            
        except FileNotFoundError as e:
            print(f"Error: Required file not found - {e}")
            return False
        except Exception as e:
            print(f"Error loading data: {e}")
            return False
        
    def check_data_quality(self):
        print("\nChecking data quality...")
        
        # Check for null booking numbers
        if 'Reservation No.' not in self.data_reservation.columns:
            print("✗ 'Reservation No.' column not found")
            return False
        
        # Remove rows with null booking numbers
        self.data_reservation = self.data_reservation[self.data_reservation['Reservation No.'].notna()]
          
    
    def standardize_columns(self):
        print("\nStandardizing data format...")
        
        # Define expected column order
        swap_list = [
            'Reservation No.', 'Room', 'Room Rev.', 'Currency', 'Crea. Date',
            'Status', 'Arrival Date', 'RN', 'Departure', 'Main client', 'Room Type',
            'Board', 'In-House', 'Segment', 'Subsegment'
        ]

        # Reindex columns to standard order
        self.data_reservation = self.data_reservation.reindex(columns=swap_list)

        # Rename columns to standard format
        self.data_reservation.columns = [
            'booking_no', 'rooms', 'total_amount', 'currency', 'booking_date',
            'status', 'arrival_date', 'night', 'departure_date', 'point_of_sale', 'room_type',
            'extra_services', 'guests', 'reference_source', 'special_offer'
        ]
        
        print("✓ Columns standardized")
    
    def clean_basic_data(self):
        print("\nPerforming basic data cleaning...")
        
        # Remove rows with null booking numbers
        initial_count = len(self.data_reservation)
        self.data_reservation = self.data_reservation[self.data_reservation['booking_no'].notna()]
        
        # Fill missing room types
        self.data_reservation['room_type'].fillna('PM', inplace=True)
        
        # Convert booking_no to integer
        self.data_reservation['booking_no'] = self.data_reservation['booking_no'].astype(int)
        
        cleaned_count = initial_count - len(self.data_reservation)
        if cleaned_count > 0:
            print(f"✓ Removed {cleaned_count} invalid records")
        else:
            print("✓ No invalid records found")
    
    def calculate_metrics(self):
        print("\nCalculating guest and night metrics...")
        
        # Calculate guests
        self.data_reservation = self.my_instance.guestCalculate(self.data_reservation)
        print("✓ Guest calculation completed")
    
    def apply_business_rules(self):
        """Apply hotel-specific business rules"""
        print("\nApplying business rules...")
        
        # Handle complimentary stays  
        complimentary_mask = (
            (self.data_reservation["rooms"] > 0) & 
            (self.data_reservation["guests"] > 0) & 
            (self.data_reservation["night"] > 0) & 
            (self.data_reservation["total_amount"] == 0)
        )
        self.data_reservation.loc[complimentary_mask, "special_offer"] = "Complimentary"
        
        # Standardize subsegment values
        subsegment_mapping = {
            "NO-SHOWS, CANCELLATIONS & DAY-USE": "Complimentary",
            "COMPLIMENTARY / HOUSE USE": "Complimentary"
        }
        
        for old_val, new_val in subsegment_mapping.items():
            self.data_reservation.loc[self.data_reservation["special_offer"] == old_val, "special_offer"] = new_val
        
        # Handle negative amounts for non-complimentary stays
        negative_mask = (
            (self.data_reservation["special_offer"] != "Complimentary") & 
            (self.data_reservation["total_amount"] < 0)
        )
        self.data_reservation.loc[negative_mask, "total_amount"] = 0
        
        # Remove invalid room records
        self.data_reservation = self.data_reservation.loc[self.data_reservation["rooms"] > 0].reset_index(drop=True)

        # Standardize text case
        self.data_reservation['special_offer'] = self.data_reservation['special_offer'].str.title()
        self.data_reservation['reference_source'] = self.data_reservation['reference_source'].str.title()
        
        print("✓ Business rules applied")
    
    def clean_room_codes(self):
        print("\nCleaning room codes...")
        
        # Clean room codes in data
        self.data_reservation['room_type'] = self.data_reservation['room_type'].str.replace(
            r'[-]+[\d\w]*', '', regex=True
        )
        self.data_reservation['room_type'] = self.data_reservation['room_type'].str.strip()
        
        # Clean room codes in lookup
        self.lookup['room_type'] = self.lookup['room_type'].str.strip()
        self.lookup['room_code'] = self.lookup['room_code'].str.strip()
        
        print("✓ Room codes cleaned")
    
    def match_room_types(self):
        print("\nMatching room types...")
        
        # Rename column for matching
        self.data_reservation = self.data_reservation.rename(columns={"room_type": "room_code"})
        
        # Apply room type matching
        self.data_reservation = self.my_instance.roomTypeMatch(self.data_reservation, self.lookup)
        print("✓ Room type matching completed")
    
    def apply_currency_conversion(self):
        print("\nChecking currency conversion...")
        
        self.data_reservation = self.my_instance.currencyCheck(self.data_reservation)
        print("✓ Currency conversion completed")
    
    def format_final_data(self):
        self.data_reservation = self.data_reservation[[
            'booking_no', 'rooms', 'total_amount', 'currency', 'booking_date',
            'status', 'arrival_date', 'night', 'departure_date', 'point_of_sale',
            'extra_services', 'reference_source', 'special_offer', 'guests', 'room_type'
        ]]
    
    def create_master_data(self):
        print("\nCreating master data...")
        
        self.data_reservation = self.my_instance.create_master_data(
            self.data_reservation, self.hotel_id, self.hotel_name
        )
        DriveIntegration.move_to_archive(self.reservation_file['id'].values[0], self.folder)
    
    def process_cancel_data(self):
        print("\nProcessing cancel data...")
        
        try:
            # Add hotel_id to cancel data
            self.data_cancel["hotel_id"] = self.hotel_id
            
            # Extract relevant columns
            df_cancel = self.data_cancel[["hotel_id", "Reserv."]]
            df_cancel.columns = ['hotel_id', 'booking_no']
            
            # Process cancel data using analyzer
            data_ek_cancel = self.my_instance.resultEkstraCancel(df_cancel, self.hotel_id)
            print("✓ Cancel data processing completed")
            DriveIntegration.move_to_archive(self.cancel_file['id'].values[0], self.folder)
                
        except Exception as e:
            print(f"⚠ Cancel data processing failed: {e}")
    
    def process_all(self):
        """Run the complete processing pipeline"""
        print(f"Starting Radisson Company Data Processing")
        print(f"Hotel: {self.hotel_name} (ID: {self.hotel_id})")
        print("=" * 60)
        
        try:
            # Load and validate data
            if not self.load_data():
                print("\n✗ Data loading failed, skipping hotel")
                return False
            
            # Process data step by step
            self.check_data_quality()
            self.standardize_columns()
            self.clean_basic_data()
            self.calculate_metrics()
            self.apply_business_rules()
            self.clean_room_codes()
            self.match_room_types()
            self.apply_currency_conversion()
            self.format_final_data()
            self.create_master_data()
            self.process_cancel_data()
            
            print("\n✓ Processing completed successfully!")
            return True
            
        except Exception as e:
            print(f"\n✗ Processing failed: {e}")
            return False


