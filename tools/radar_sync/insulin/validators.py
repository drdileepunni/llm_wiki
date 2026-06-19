"""
Input Validators - Handles validation and conversion of input data
"""
from typing import Dict, Tuple


def validate_and_normalize_input(data: Dict) -> Tuple[bool, str]:
    """
    Validate input data and normalize to standard format
    
    Args:
        data: Input data dictionary
        
    Returns:
        tuple: (is_valid, error_message)
    """
    # Convert array format to individual fields if arrays are provided
    if "GRBS" in data and isinstance(data["GRBS"], list):
        convert_grbs_array_to_fields(data)
    
    if "Insulin" in data and isinstance(data["Insulin"], list):
        convert_insulin_array_to_fields(data)
    
    # Validate required field
    if "GRBS1" not in data:
        return False, "Missing required field: GRBS (at least one GRBS value)"
    
    # Validate GRBS1 is numeric
    try:
        float(data["GRBS1"])
    except (ValueError, TypeError):
        return False, f"Invalid GRBS value: {data['GRBS1']}"
    
    # Apply defaults for missing fields
    apply_defaults(data)
    
    # Validate and sanitize all fields
    sanitize_grbs_values(data)
    sanitize_insulin_values(data)
    sanitize_boolean_fields(data)
    sanitize_route(data)
    sanitize_diet_order(data)
    
    return True, "Valid input"


def convert_grbs_array_to_fields(data: Dict) -> None:
    """Convert GRBS array to individual GRBS1-5 fields"""
    grbs_array = data["GRBS"]
    # Pad with zeros if needed
    grbs_array = grbs_array + [0] * (5 - len(grbs_array))
    
    data["GRBS1"] = grbs_array[0] if len(grbs_array) > 0 else 0
    data["GRBS2"] = grbs_array[1] if len(grbs_array) > 1 else 0
    data["GRBS3"] = grbs_array[2] if len(grbs_array) > 2 else 0
    data["GRBS4"] = grbs_array[3] if len(grbs_array) > 3 else 0
    data["GRBS5"] = grbs_array[4] if len(grbs_array) > 4 else 0


def convert_insulin_array_to_fields(data: Dict) -> None:
    """Convert Insulin array to individual Insulin1-4 fields"""
    insulin_array = data["Insulin"]
    # Pad with zeros if needed
    insulin_array = insulin_array + [0] * (4 - len(insulin_array))
    
    data["Insulin1"] = insulin_array[0] if len(insulin_array) > 0 else 0
    data["Insulin2"] = insulin_array[1] if len(insulin_array) > 1 else 0
    data["Insulin3"] = insulin_array[2] if len(insulin_array) > 2 else 0
    data["Insulin4"] = insulin_array[3] if len(insulin_array) > 3 else 0


def apply_defaults(data: Dict) -> None:
    """Apply default values for missing fields"""
    defaults = {
        "GRBS2": 0, "GRBS3": 0, "GRBS4": 0, "GRBS5": 0,
        "Insulin1": 0, "Insulin2": 0, "Insulin3": 0, "Insulin4": 0,
        "CKD": False, "Dual inotropes": False,
        "route": "sc", "diet_order": "others"
    }
    
    for key, default_value in defaults.items():
        if key not in data:
            data[key] = default_value


def sanitize_grbs_values(data: Dict) -> None:
    """Validate and sanitize GRBS values"""
    grbs_fields = ["GRBS1", "GRBS2", "GRBS3", "GRBS4", "GRBS5"]
    for field in grbs_fields:
        try:
            data[field] = float(data[field])
        except (ValueError, TypeError):
            data[field] = 0


def sanitize_insulin_values(data: Dict) -> None:
    """Validate and sanitize insulin values"""
    insulin_fields = ["Insulin1", "Insulin2", "Insulin3", "Insulin4"]
    for field in insulin_fields:
        try:
            data[field] = float(data[field])
        except (ValueError, TypeError):
            data[field] = 0


def sanitize_boolean_fields(data: Dict) -> None:
    """Validate and sanitize boolean fields"""
    if not isinstance(data.get("CKD"), bool):
        data["CKD"] = False
    if not isinstance(data.get("Dual inotropes"), bool):
        data["Dual inotropes"] = False


def sanitize_route(data: Dict) -> None:
    """Validate and sanitize route field"""
    if data.get("route") not in ["iv", "sc"]:
        data["route"] = "sc"


def sanitize_diet_order(data: Dict) -> None:
    """Validate and sanitize diet order field"""
    if data.get("diet_order") not in ["NPO", "others"]:
        data["diet_order"] = "others"

