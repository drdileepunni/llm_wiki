#!/usr/bin/env python3
"""
Export all data for a given CPMRN from patients, taskListsTable, and chat collections.

Exports three JSON files into /home/data/jsons/<CPMRN>/:
  - patients.json     — all patient records for the CPMRN
  - tasks.json        — all tasks from taskListsTable for the CPMRN
  - chat.json         — all chat messages where identifier matches CPMRN:<encounters>

Usage:
    python scripts/export_patient_data.py --cpmrn <CPMRN>
"""

import sys
import os
import json
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = '/home'
if os.path.exists(PROJECT_ROOT) and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from config import LOCAL_ROOT_DIR, MONGODB_URI
    from pymongo import MongoClient
    from bson import json_util
    logger.info("Successfully imported project modules")
except ImportError as e:
    logger.error(f"Failed to import project modules: {e}")
    sys.exit(1)


def get_db():
    if not MONGODB_URI:
        logger.error("MONGODB_URI not set")
        sys.exit(1)
    logger.info("Connecting to MongoDB...")
    client = MongoClient(MONGODB_URI)
    client.admin.command('ping')
    logger.info("Successfully connected to MongoDB")
    db_name = MONGODB_URI.split('/')[-1].split('?')[0]
    return client, client[db_name]


def export_to_json(data, output_path):
    with open(output_path, 'w') as f:
        f.write(json_util.dumps(data, indent=2))
    logger.info(f"Exported {len(data)} documents to {output_path}")


def main():
    parser = argparse.ArgumentParser(description='Export all data for a CPMRN + encounters')
    parser.add_argument('--cpmrn', required=True, help='Patient CPMRN (e.g. INAPANT351180)')
    parser.add_argument('--encounters', required=True, type=int, help='Encounter number (e.g. 1)')
    args = parser.parse_args()

    cpmrn = args.cpmrn.strip().upper()
    encounters = args.encounters

    logger.info("=" * 70)
    logger.info(f"Exporting data for CPMRN: {cpmrn}, encounters: {encounters}")
    logger.info("=" * 70)

    # Output directory: <LOCAL_ROOT_DIR>/data/jsons/
    output_dir = os.path.join(LOCAL_ROOT_DIR, 'data', 'jsons')
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    client, db = get_db()

    try:
        # --- Patients ---
        logger.info("Fetching patients collection...")
        patients = list(db['patients'].find({'CPMRN': cpmrn, 'encounters': encounters}))
        export_to_json(patients, os.path.join(output_dir, 'patients.json'))
        logger.info(f"Found {len(patients)} patient record(s)")

        chat_identifier = f"{cpmrn}:{encounters}"

        # --- Tasks ---
        logger.info("Fetching tasks collection...")
        tasks = list(db['taskListsTable'].find({'CPMRN': cpmrn, 'encounters': encounters}))
        export_to_json(tasks, os.path.join(output_dir, 'tasks.json'))

        # --- Chat ---
        logger.info("Fetching chat collection...")
        chats = list(db['chats'].find({'identifier': chat_identifier}))
        export_to_json(chats, os.path.join(output_dir, 'chat.json'))

        logger.info("=" * 70)
        logger.info(f"Export complete for CPMRN: {cpmrn}, encounters: {encounters}")
        logger.info(f"  patients : {len(patients):>6,} record(s)")
        logger.info(f"  tasks    : {len(tasks):>6,} record(s)")
        logger.info(f"  chat     : {len(chats):>6,} record(s)")
        logger.info(f"  output   : {output_dir}")
        logger.info("=" * 70)

    except Exception as e:
        logger.error(f"Export failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        client.close()
        logger.info("MongoDB connection closed")


if __name__ == "__main__":
    main()
