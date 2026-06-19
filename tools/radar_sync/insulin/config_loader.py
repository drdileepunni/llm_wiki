"""
Configuration Loader - Handles loading algorithm configurations from CSV
"""
import csv
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_DEFAULT_CSV = Path(__file__).resolve().parent / "algorithm_config.csv"


def load_algorithm_config(csv_file=None) -> tuple:
    """
    Load algorithm configurations from CSV file.

    Defaults to algorithm_config.csv in the same directory as this module,
    so it works regardless of the caller's working directory.
    """
    if csv_file is None:
        csv_file = str(_DEFAULT_CSV)

    iv_algorithm = {}
    basal_bolus_algorithm = {}

    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                algorithm = row['algorithm']
                level = int(row['level'])
                grbs_range_str = row['grbs_range']
                dose = float(row['dose'])

                grbs_min, grbs_max = parse_grbs_range(grbs_range_str)
                grbs_key = (grbs_min, grbs_max)

                if algorithm == 'IV':
                    if level not in iv_algorithm:
                        iv_algorithm[level] = {}
                    iv_algorithm[level][grbs_key] = dose

                elif algorithm == 'Basal':
                    if level not in basal_bolus_algorithm:
                        basal_bolus_algorithm[level] = {}
                    basal_bolus_algorithm[level][grbs_key] = int(dose)

        logger.info("Loaded algorithm configurations from %s", csv_file)

    except FileNotFoundError:
        logger.error("CSV file %s not found. Using default values.", csv_file)
        return get_default_config()
    except Exception as e:
        logger.error("Error loading algorithm config: %s. Using default values.", e)
        return get_default_config()

    return iv_algorithm, basal_bolus_algorithm


def parse_grbs_range(grbs_range_str: str) -> tuple:
    """Parse GRBS range string into (min, max) tuple. Examples: '<110', '110-129', '>400'"""
    if '<' in grbs_range_str:
        grbs_max = int(grbs_range_str.replace('<', '').strip())
        grbs_min = 0
    elif '>' in grbs_range_str:
        grbs_min = int(grbs_range_str.replace('>', '').strip())
        grbs_max = 1000
    else:
        parts = grbs_range_str.split('-')
        grbs_min = int(parts[0])
        grbs_max = int(parts[1])

    return grbs_min, grbs_max


def get_default_config() -> tuple:
    iv_algorithm = {
        1: {(0, 110): 0},
        2: {(111, 150): 1.0},
        3: {(151, 200): 2.0},
        4: {(201, 250): 3.0},
        5: {(251, 300): 4.0},
    }

    basal_bolus_algorithm = {
        1: {(0, 140): 0},
        2: {(141, 180): 2},
        3: {(181, 220): 4},
        4: {(221, 260): 6},
        5: {(261, 300): 8},
        6: {(301, 350): 16},
        7: {(351, 1000): 12},
    }

    return iv_algorithm, basal_bolus_algorithm
