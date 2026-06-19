"""
Main Recommendation Engine - Orchestrates the insulin recommendation process
"""
import json
import logging
from typing import Dict

from .config_loader import load_algorithm_config
from .validators import validate_and_normalize_input
from .algorithms import (
    AlgorithmSelector, TimingCalculator, DoseFinder, TransitionRules
)

logger = logging.getLogger(__name__)


class InsulinRecommendationEngine:
    """Main engine for insulin dose calculations"""
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.InsulinRecommendationEngine")
        self.iv_algorithm, self.basal_bolus_algorithm = load_algorithm_config()
    
    def recommend_insulin_dose(self, data: Dict) -> Dict:
        """
        Main method to recommend insulin dose
        
        Args:
            data: Input data dictionary
            
        Returns:
            dict: Recommendation result
        """
        self.logger.info("Starting insulin dose recommendation process")
        self.logger.info(f"Input data: {json.dumps(data, indent=2)}")
        
        # Step 1: Validate input
        is_valid, error_msg = validate_and_normalize_input(data)
        if not is_valid:
            self.logger.error(f"Input validation failed: {error_msg}")
            return {"error": f"Invalid input: {error_msg}"}
        
        # Step 2: Determine algorithm type
        algorithm_type = AlgorithmSelector.determine_algorithm_type(data)
        self.logger.info(f"Selected algorithm: {algorithm_type}")
        
        # Step 3: Calculate dose
        if algorithm_type == "iv":
            result = self._calculate_iv_insulin_dose(data)
        else:
            result = self._calculate_basal_bolus_dose(data)
        
        self.logger.info(f"Recommendation result: {json.dumps(result, indent=2)}")
        return result
    
    def _calculate_iv_insulin_dose(self, data: Dict) -> Dict:
        """Calculate insulin dose using IV infusion algorithm"""
        self.logger.info("\n💉 STEP 3: IV Infusion Algorithm")
        
        grbs_values = [data[f"GRBS{i}"] for i in range(1, 6)]
        insulin_values = [data[f"Insulin{i}"] for i in range(1, 5)]
        
        valid_grbs = [g for g in grbs_values if g > 0]
        grbs_count = len(valid_grbs)
        current_grbs = grbs_values[0] if grbs_values[0] > 0 else (valid_grbs[0] if valid_grbs else 0)
        
        self.logger.info(f"  Current GRBS: {current_grbs}")
        self.logger.info(f"  Number of GRBS readings: {grbs_count}")
        self.logger.info(f"  Previous Insulin doses: {insulin_values}")
        
        # Determine starting level
        level = self._determine_iv_starting_level(
            grbs_count, insulin_values, current_grbs, data.get("Dual inotropes", False)
        )
        
        # Find rate and action
        insulin_rate = DoseFinder.find_iv_rate_for_level_and_grbs(
            level, current_grbs, self.iv_algorithm
        )
        action = DoseFinder.get_iv_action(insulin_rate)
        
        self.logger.info(f"  ✓ Final Level: {level}")
        self.logger.info(f"  ✓ Insulin Rate: {insulin_rate} IU/hr")
        self.logger.info(f"  ✓ Action: {action}")
        
        # Calculate next check time
        next_check_hours = TimingCalculator.calculate_next_check_time(data, "iv")
        self.logger.info(f"  ✓ Next GRBS Check: {next_check_hours} hours")
        
        return {
            "Suggested_insulin_dose": insulin_rate,
            "Suggested_route": "iv",
            "next_grbs_after": next_check_hours,
            "algorithm_used": "IV Infusion",
            "level": level,
            "action": action,
            "unit": "IU/hr"
        }
    
    def _calculate_basal_bolus_dose(self, data: Dict) -> Dict:
        """Calculate insulin dose using basal bolus algorithm"""
        self.logger.info("\n💉 STEP 3: Basal Bolus Algorithm")
        
        grbs_values = [data[f"GRBS{i}"] for i in range(1, 6)]
        insulin_values = [data[f"Insulin{i}"] for i in range(1, 5)]
        
        valid_grbs = [g for g in grbs_values if g > 0]
        grbs_count = len(valid_grbs)
        current_grbs = grbs_values[0]
        
        self.logger.info(f"  Current GRBS: {current_grbs}")
        self.logger.info(f"  Number of GRBS readings: {grbs_count}")
        
        # Determine starting level
        level = self._determine_basal_starting_level(
            grbs_count, insulin_values, current_grbs, grbs_values
        )
        
        # Find dose and action
        insulin_dose = DoseFinder.find_basal_dose_for_level_and_grbs(
            level, current_grbs, self.basal_bolus_algorithm
        )
        action = DoseFinder.get_basal_action(insulin_dose)
        
        self.logger.info(f"  ✓ Final Level: {level}")
        self.logger.info(f"  ✓ Insulin Dose: {insulin_dose} IU")
        self.logger.info(f"  ✓ Action: {action}")
        
        # Calculate next check time
        next_check_hours = TimingCalculator.calculate_next_check_time(data, "sc")
        self.logger.info(f"  ✓ Next GRBS Check: {next_check_hours} hours")
        
        return {
            "Suggested_insulin_dose": insulin_dose,
            "Suggested_route": "subcutaneous",
            "next_grbs_after": next_check_hours,
            "algorithm_used": "Basal Bolus",
            "level": level,
            "action": action,
            "unit": "IU"
        }
    
    def _determine_iv_starting_level(self, grbs_count: int, insulin_values: list, 
                                      current_grbs: float, dual_inotropes: bool) -> int:
        """Determine starting level for IV algorithm"""
        has_previous_insulin = any(ins > 0 for ins in insulin_values)
        
        # No previous insulin or insufficient data
        if grbs_count <= 1 and not has_previous_insulin:
            self.logger.info("  → 0 or 1 GRBS readings detected")
            self.logger.info("  → Defaulting to Level 2")
            return 2
        
        if not has_previous_insulin:
            self.logger.info("  → No previous insulin doses detected")
            self.logger.info("  → Starting at level 2")
            return 2
        
        # Dual inotropes: always start at Level 2
        if dual_inotropes:
            self.logger.info("  → Dual Inotropes: starting at Level 2")
            return 2
        
        # Match to previous insulin level
        return self._match_iv_level_to_previous_dose(insulin_values[0], current_grbs)
    
    def _determine_basal_starting_level(self, grbs_count: int, insulin_values: list,
                                         current_grbs: float, grbs_values: list) -> int:
        """Determine starting level for Basal Bolus algorithm"""
        has_previous_insulin = any(ins > 0 for ins in insulin_values)
        
        # Insufficient data
        if grbs_count <= 1:
            self.logger.info("  → 0 or 1 GRBS readings detected")
            self.logger.info("  → Defaulting to Level 2")
            return 2
        
        # No previous insulin
        if not has_previous_insulin:
            self.logger.info("  → No previous insulin doses detected")
            self.logger.info("  → Starting at level 2")
            return 2
        
        # Match to previous insulin level and apply transitions
        level = self._match_basal_level_to_previous_dose(insulin_values[0], current_grbs)
        old_level = level
        level = TransitionRules.apply_basal_bolus_transition_rules(level, grbs_values)
        
        if level != old_level:
            self.logger.info(f"  → Level adjusted from {old_level} to {level}")
        
        return level
    
    def _match_iv_level_to_previous_dose(self, previous_dose: float, current_grbs: float) -> int:
        """Find IV level that matches previous dose"""
        best_match_level = 2
        min_diff = float('inf')
        
        self.logger.info(f"  → Previous insulin rate: {previous_dose} IU/hr")
        
        for level_num in self.iv_algorithm.keys():
            dose_for_level = DoseFinder.find_iv_rate_for_level_and_grbs(
                level_num, current_grbs, self.iv_algorithm
            )
            diff = abs(dose_for_level - previous_dose)
            
            if diff < min_diff:
                min_diff = diff
                best_match_level = level_num
        
        dose_at_level = DoseFinder.find_iv_rate_for_level_and_grbs(
            best_match_level, current_grbs, self.iv_algorithm
        )
        self.logger.info(f"  → Matched to Level {best_match_level} (gives {dose_at_level} IU/hr)")
        
        return best_match_level
    
    def _match_basal_level_to_previous_dose(self, previous_dose: float, current_grbs: float) -> int:
        """Find Basal level that matches previous dose"""
        best_match_level = 2
        min_diff = float('inf')
        
        self.logger.info(f"  → Previous insulin dose: {previous_dose} IU")
        
        for level_num in self.basal_bolus_algorithm.keys():
            dose_for_level = DoseFinder.find_basal_dose_for_level_and_grbs(
                level_num, current_grbs, self.basal_bolus_algorithm
            )
            diff = abs(dose_for_level - previous_dose)
            
            if diff < min_diff:
                min_diff = diff
                best_match_level = level_num
        
        dose_at_level = DoseFinder.find_basal_dose_for_level_and_grbs(
            best_match_level, current_grbs, self.basal_bolus_algorithm
        )
        self.logger.info(f"  → Matched to Level {best_match_level} (gives {dose_at_level} IU)")
        
        return best_match_level

