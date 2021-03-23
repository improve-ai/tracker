"""
AWS Batch worker
This script is intended to be used from inside a Docker container to process
jsonl files.
-------------------------------------------------------------------------------
Usage:
    python join_rewards.py
"""

# Built-in imports
from datetime import timedelta
from datetime import datetime
import logging
import json
import gzip
import io
import uuid
import sys
import shutil
import signal
import subprocess
import concurrent.futures
import boto3
import botocore
from itertools import groupby

# Local imports
from utils import load_records
from utils import name_no_ext
from utils import sort_records_by_timestamp
from config import DEFAULT_REWARD_KEY
from config import DATETIME_FORMAT
from config import DEFAULT_EVENTS_REWARD_VALUE
from config import DEFAULT_REWARD_VALUE
from config import INCOMING_PATH
from config import HISTORIES_PATH
from config import LOGGING_LEVEL
from config import LOGGING_FORMAT
from config import REWARD_WINDOW
from config import AWS_BATCH_JOB_ARRAY_INDEX
from config import REWARD_ASSIGNMENT_WORKER_COUNT
from config import TRAIN_BUCKET
from exceptions import InvalidTypeError
from exceptions import UpdateListenersError

# Setup logging
logging.basicConfig(format=LOGGING_FORMAT, level=LOGGING_LEVEL)

# Time window to add to a timestamp
window = timedelta(seconds=REWARD_WINDOW)

SIGTERM = False

# boto3 client must be pre-initialized for multi-threaded (see: https://github.com/boto/botocore/issues/1246)
worker_count = 50
s3client = boto3.client("s3", config=botocore.config.Config(max_pool_connections=worker_count))

def worker():
    """
    Identify the relevant folders that this worker should process, identify 
    the files that need to be processed and write the gzipped results.
    """

    print(f"Starting AWS Batch Array job.")

    node_id       = AWS_BATCH_JOB_ARRAY_INDEX
    node_count    = REWARD_ASSIGNMENT_WORKER_COUNT

    # identify the portion of incoming files to process in this node
    files_to_process = identify_incoming_files_to_process(INCOMING_PATH, node_id, node_count)
    
    # group the files by the hashed history id
    grouped_files = group_files_by_hashed_history_id(files_to_process)

    with concurrent.futures.ThreadPoolExecutor() as executor:
        executor.map(process_incoming_file_group, grouped_files)

    print(f"AWS Batch Array node {node_id} finished.")

def process_incoming_file_group(file_group):
    handle_signals()

    # get the hashed history id
    hashed_history_id = hashed_history_id_from_file(file_group[0])
    
    # add any previously saved history files for this hashed history id
    file_group.extend(history_files_for_hashed_history_id(hashed_history_id))
    
    # load and consolidate all records
    records = load_history(hashed_history_id, file_group)

    # write the consolidated records to a new file
    save_history(hashed_history_id, records)
    
    rewarded_decisions_by_model = assign_rewards_to_decisions(decision_records, reward_records, event_records)
    
    for model, rewarded_decisions in rewarded_decisions_by_model.items():
        upload_rewarded_decisions(model, hashed_history_id, rewarded_decisions)
    
    delete_all(file_group)
    
    
def load_history(hashed_history_id, file_group):
    return
    #     decision_records, reward_records, event_records = load_records(str(f))
    

def update_listeners(listeners, record_timestamp, reward):
    """
    Update the reward property value of each of the given list of records, 
    in place.
    
    Args:
        listeners        : list of dicts
        record_timestamp : A datetime.datetime object
        reward           : int or float
    
    Returns:
        None
    
    Raises:
        TypeError if an unexpected type is received
    """

    if not isinstance(listeners, list):
        raise TypeError("Expecting a list for the 'listeners' arg.")

    if not isinstance(record_timestamp, datetime):
        raise TypeError("Expecting a datetime.datetime timestamp for the 'record_timestamp' arg.")

    if not (isinstance(reward, int) or isinstance(reward, float)):
        raise TypeError("Expecting int, float or bool.")

    try:

        # Loop backwards to be able to remove an item in place
        for i in range(len(listeners)-1, -1, -1):
            listener = listeners[i]
            listener_timestamp = datetime.strptime(listener['timestamp'], DATETIME_FORMAT)
            if listener_timestamp + window < record_timestamp:
                print(f'Deleting listener: {listener_timestamp}, Reward/event: {record_timestamp}')
                del listeners[i]
            else:
                print(f'Adding reward of {float(reward)} to decision.')
                listener['reward'] = listener.get('reward', DEFAULT_REWARD_VALUE) + float(reward)

    except Exception as e:
        raise UpdateListenersError


def assign_rewards_to_decisions(decision_records, reward_records, event_records):
    """
    1) Collect all records of type "decision" in a dictionary.
    2) Assign the rewards of records of type "rewards" to all the "decision" 
    records that match two criteria:
      - reward_key
      - a time window
    3) Assign the value of records of type "event" to all "decision" records
    within a time window.

    Args:
        records: a list of records (dicts) sorted by their "timestamp" property.

    Returns:
        dict whose keys are 'reward_key's and its values are lists of records.
    
    Raises:
        InvalidTypeError: If a record has an invalid type attribute.
    """

    records = []
    records.extend(decision_records)
    records.extend(reward_records)
    records.extend(event_records)
    
    sort_records_by_timestamp(records)

    decision_records_by_reward_key = {}
    for record in records:
        if record.get('type') == 'decision':
            reward_key = record.get('reward_key', DEFAULT_REWARD_KEY)
            listeners = decision_records_by_reward_key.get(reward_key, [])
            decision_records_by_reward_key[reward_key] = listeners
            listeners.append(record)
        
        elif record.get('type') == 'rewards':
            record_timestamp = datetime.strptime(record['timestamp'], DATETIME_FORMAT)
            for reward_key, reward in record['rewards'].items():
                listeners = decision_records_by_reward_key.get(reward_key, [])
                update_listeners(listeners, record_timestamp, reward)
        
        # Event type records get summed to all decisions within the time window regardless of reward_key
        elif record.get('type') == 'event':
            reward = record.get('properties', { 'properties': {} }) \
                           .get('value', DEFAULT_EVENTS_REWARD_VALUE)
            record_timestamp = datetime.strptime(record['timestamp'], DATETIME_FORMAT)
            for reward_key, listeners in decision_records_by_reward_key.items():
                update_listeners(listeners, record_timestamp, reward)
            
        else:
            raise InvalidTypeError
    
    return decision_records

def save_history(hashed_history_id, history_records):
    output_file = history_dir_for_hashed_history_id(hashed_history_id) / f'{hashed_history_id}-{uuid.uuid4()}.jsonl.gz'

    parent_dir = output_file.parent()
    if not parent_dir.exists():
        print(f'creating {str(parent_dir)}')
        parent_dir.mkdir(parents=True, exist_ok=True)
    
    with gzip.open(output_file.absolute(), mode='w') as gzf:
        for record in history_records:
            gzf.write((json.dumps(record) + "\n"))

def upload_rewarded_decisions(model, hashed_history_id, rewarded_decisions):
    # TODO double check model name and hashed_history_id to ensure valid characters
    
    gzipped = io.BytesIO()
    
    with gzip.open(gzipped, mode='w') as gzf:
        for record in rewarded_decisions:
            gzf.write((json.dumps(record) + "\n"))
    
    gzipped.seek(0)
    
    s3_key = rewarded_decisions_s3_key(model, hashed_history_id)
    
    s3client.put_object(Bucket=TRAIN_BUCKET, Body=gzipped, Key=s3_key)

def delete_all(paths):
    for path in paths:
        path.unlink(missing_ok=True)

def identify_incoming_files_to_process(input_dir, node_id, node_count):
    """
    Return a list of Path objects representing files that need to be processed.

    Args:
        input_dir : Path object towards the input folder.
        node_id   : int representing the id of the target node (zero-indexed)
        node_count: int representing the number of total nodes of the cluster
    
    Returns:
        List of Path objects representing files
    """

    files_to_process = []
    file_count = 0
    for f in input_dir.glob('*.jsonl.gz'):
        file_count += 1
        # convert first 16 hex chars (64 bit) to an int
        # the file name starts with a sha-256 hash so the bits will be random
        # check if int mod node_count matches our node
        if (int(f.name[:16], 16) % node_count) == node_id:
            files_to_process.append(f)

    print(f'selected {len(files_to_process)} of {file_count} .jsonl.gz files from {input_dir} to process')
    return files_to_process


def rewarded_decisions_s3_key(model, hashed_history_id):
    return f'rewarded_decisions/{model}/{hashed_history_id[0:2]}/{hashed_history_id[2:4]}/{hashed_history_id}.jsonl.gz'

def hashed_history_id_from_file(file):
    return file.name.split('-')[0]

def history_dir_for_hashed_history_id(hashed_history_id):
    # returns a path like /mnt/histories/1c/aa
    return HISTORIES_PATH / hashed_history_id[0:2] / hashed_history_id[2:4]

def history_files_for_hashed_history_id(hashed_history_id):
    return history_dir_for_hashed_history_id(hashed_history_id).glob(f'{hashed_history_id}-*.jsonl.gz')
        
def group_files_by_hashed_history_id(files):
    sorted_files = sorted(files, key=hashed_history_id_from_file)
    return [list(it) for k, it in groupby(sorted_files, hashed_history_id_from_file)]    

def handle_signals():
    if SIGTERM:
        print(f'Quitting due to SIGTERM signal (node {AWS_BATCH_JOB_ARRAY_INDEX}).')
        sys.exit()


def signal_handler(signalNumber, frame):
    global SIGTERM
    SIGTERM = True
    print(f"SIGTERM received (node {AWS_BATCH_JOB_ARRAY_INDEX}).")
    return


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    worker()
