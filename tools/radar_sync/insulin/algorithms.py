"""
Algorithm Implementations - IV and Basal Bolus algorithms
"""
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


class AlgorithmSelector:
    """Determines which algorithm to use based on patient conditions"""
    
    @staticmethod
    def determine_algorithm_type(data: Dict) -> str:
        """
        Determine which algorithm to use
        
        Returns:
            str: "iv" or "basal_bolus"
        """
        route = data["route"]
        dual_inotropes = data["Dual inotropes"]
        grbs_values = [data[f"GRBS{i}"] for i in range(1, 6)]
        
        logger.info("\n🔍 STEP 2: Algorithm Selection")
        logger.info(f"  Current Route: {route}")
        logger.info(f"  Dual Inotropes: {dual_inotropes}")
        logger.info(f"  GRBS Values: {grbs_values}")
        
        # Priority 1: Dual Inotropes always use IV
        if dual_inotropes:
            logger.info("  → Condition: Dual Inotropes = True")
            logger.info("  ✓ Decision: Use IV algorithm (Dual Inotropes requirement)")
            return "iv"
        
        # Priority 2: SC route conditions
        if route == "sc":
            high_grbs_count = sum(1 for grbs in grbs_values if grbs > 350)
            logger.info(f"  → High GRBS (>350) count: {high_grbs_count}")
            
            if high_grbs_count >= 2:
                logger.info(f"  → Condition: {high_grbs_count} GRBS values > 350")
                logger.info("  ✓ Decision: Use IV algorithm (2+ high GRBS values)")
                return "iv"
            else:
                logger.info("  ✓ Decision: Use Basal Bolus algorithm")
                
        elif route == "iv":
            grbs_1_4 = grbs_values[:4]
            if not all(150 <= val <= 180 for val in grbs_1_4):
                logger.info(f"  → GRBS1-4 not all in 150-180 range: {grbs_1_4}")
                logger.info("  ✓ Decision: Continue with IV algorithm")
                return "iv"
            else:
                logger.info(f"  → GRBS1-4 all in 150-180 range: {grbs_1_4}")
                logger.info("  ✓ Decision: Switch to Basal Bolus")
        
        logger.info("  ✓ Final Decision: Basal Bolus algorithm")
        return "basal_bolus"


class TimingCalculator:
    """Calculates next GRBS check timing"""
    
    @staticmethod
    def calculate_next_check_time(data: Dict, route: str) -> int:
        """
        Calculate next GRBS check time in hours
        
        Returns:
            int: Hours until next check
        """
        diet_order = data["diet_order"]
        
        if route == "iv":
            grbs_values = [data[f"GRBS{i}"] for i in range(1, 5)]
            if all(140 <= val <= 180 for val in grbs_values):
                return 2  # 2nd hourly if controlled
            return 1  # Hourly otherwise
        else:  # SC route
            if diet_order == "NPO":
                return 4  # 4th hourly for fasting
            else:
                return 6  # 6th hourly for others


class DoseFinder:
    """Utility class for finding doses based on levels and GRBS"""
    
    @staticmethod
    def find_iv_rate_for_level_and_grbs(level: int, current_grbs: float, iv_algorithm: Dict) -> float:
        """Find IV insulin rate for given level and GRBS"""
        if level not in iv_algorithm:
            return 1.0
        
        grbs_table = iv_algorithm[level]
        for (grbs_min, grbs_max), rate in grbs_table.items():
            if grbs_min <= current_grbs <= grbs_max:
                return rate
        
        if grbs_table:
            return list(grbs_table.values())[0]
        return 1.0
    
    @staticmethod
    def find_basal_dose_for_level_and_grbs(level: int, current_grbs: float, basal_algorithm: Dict) -> int:
        """Find basal insulin dose for given level and GRBS"""
        if level not in basal_algorithm:
            return 2
        
        grbs_table = basal_algorithm[level]
        for (grbs_min, grbs_max), dose in grbs_table.items():
            if grbs_min <= current_grbs <= grbs_max:
                return dose
        
        if grbs_table:
            return list(grbs_table.values())[0]
        return 2
    
    @staticmethod
    def get_iv_action(rate: float) -> str:
        """Get action description for IV rate"""
        if rate == 0:
            return "Turn off insulin"
        elif rate <= 1:
            return "Maintain current rate"
        elif rate >= 40:
            return "Maximum rate"
        else:
            return "Increase rate"
    
    @staticmethod
    def get_basal_action(dose: int) -> str:
        """Get action description for basal dose"""
        if dose == 0:
            return "No insulin"
        elif dose <= 2:
            return "Low dose"
        elif dose <= 6:
            return "Medium dose"
        elif dose <= 12:
            return "High dose"
        elif dose <= 20:
            return "Very high dose"
        else:
            return "Critical dose"


class TransitionRules:
    """Handles level transitions for both algorithms"""
    
    @staticmethod
    def apply_iv_transition_rules(current_level: int, grbs_values: List[float]) -> int:
        """Apply IV algorithm transition rules"""
        if len(grbs_values) < 2:
            return current_level
        
        current_grbs = grbs_values[0]
        previous_grbs = grbs_values[1]
        
        # Move UP: Blood glucose > 150 AND (increased OR decreased 0-60)
        if current_grbs > 150:
            if current_grbs > previous_grbs or (previous_grbs - current_grbs) <= 60:
                new_level = min(current_level + 1, 5)
                logger.info(f"  → Moving UP from level {current_level} to {new_level}")
                return new_level
        
        # Move DOWN: Blood glucose < 110
        if current_grbs < 110:
            new_level = max(current_level - 1, 1)
            logger.info(f"  → Moving DOWN from level {current_level} to {new_level}")
            return new_level
        
        # Maintain
        logger.info(f"  → MAINTAINING level {current_level}")
        return current_level
    
    @staticmethod
    def apply_basal_bolus_transition_rules(current_level: int, grbs_values: List[float]) -> int:
        """Apply basal bolus algorithm transition rules"""
        if len(grbs_values) < 2:
            return current_level
        
        readings_above_180 = sum(1 for grbs in grbs_values if grbs > 180)
        readings_below_140 = sum(1 for grbs in grbs_values if grbs > 0 and grbs < 140)
        
        logger.info(f"  → Readings above 180: {readings_above_180}")
        logger.info(f"  → Readings below 140: {readings_below_140}")
        
        # Move UP: Two or more readings above 180
        if readings_above_180 >= 2:
            new_level = min(current_level + 1, 7)
            logger.info(f"  → Moving UP from level {current_level} to {new_level}")
            return new_level
        
        # Move DOWN: Any reading less than 140
        if readings_below_140 >= 1:
            new_level = max(current_level - 1, 1)
            logger.info(f"  → Moving DOWN from level {current_level} to {new_level}")
            return new_level
        
        logger.info(f"  → MAINTAINING level {current_level}")
        return current_level

