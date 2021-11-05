# Built-in imports
import orjson as json
import re
import dateutil
import datetime
import gzip
from ksuid import Ksuid

# Local imports
from config import s3client, FIREHOSE_BUCKET, stats
from utils import utc, is_valid_model_name, is_valid_ksuid
from utils import get_valid_timestamp


MESSAGE_ID_KEY = 'message_id'
DECISION_ID_KEY = 'decision_id'
TIMESTAMP_KEY = 'timestamp'
TYPE_KEY = 'type'
DECISION_TYPE = 'decision'
REWARD_TYPE = 'reward'
MODEL_KEY = 'model'
REWARD_KEY = REWARD_TYPE
REWARDS_KEY = 'rewards'
VARIANT_KEY = 'variant'
GIVENS_KEY = 'givens'
COUNT_KEY = 'count'
SAMPLE_KEY = 'sample'
RUNNERS_UP_KEY = 'runners_up'


class FirehoseRecord:
    # slots are faster and use much less memory than dicts
    __slots__ = [MESSAGE_ID_KEY, TIMESTAMP_KEY, TYPE_KEY, MODEL_KEY, DECISION_ID_KEY, REWARD_KEY, VARIANT_KEY, GIVENS_KEY, COUNT_KEY, RUNNERS_UP_KEY, SAMPLE_KEY]
   
    # throws TypeError if json_record is wrong type
    # throws ValueError if record is invalid
    # throws KeyError if required field is missing
    def __init__(self, json_record):
        #
        # local variables are used in this method rather than self for performance
        #

        assert isinstance(json_record, dict)
        
        message_id = json_record[MESSAGE_ID_KEY]
        if not is_valid_ksuid(message_id):
            raise ValueError('invalid message_id')

        self.message_id = message_id
        
        # parse and validate timestamp
        timestamp = get_valid_timestamp(json_record[TIMESTAMP_KEY])
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=utc)
        
        self.timestamp = timestamp
        
        type_ = json_record[TYPE_KEY]
        if not isinstance(type_, str):
            raise ValueError('invalid type')
            
        self.type = type_
             
        model = json_record[MODEL_KEY]
        if not is_valid_model_name(model):
            raise ValueError('invalid model')
            
        self.model = model

        if self.is_reward_record():
            decision_id = json_record[DECISION_ID_KEY]
            if not is_valid_ksuid(decision_id):
                raise ValueError('invalid decision_id')
            self.decision_id = decision_id

            # parse and validate reward
            reward = json_record[REWARD_KEY]
    
            if not isinstance(reward, (int, float)):
                raise ValueError('invalid reward')
    
            self.reward = reward
            
            
        elif self.is_decision_record():
            # parse variant (all JSON types are valid)
            self.variant = json_record.get(VARIANT_KEY, None)
            
            # parse and validate given
            givens = json_record.get(GIVENS_KEY, None)
            
            if givens is not None and not isinstance(givens, dict):
                raise ValueError('invalid givens')
    
            self.givens = givens
            
            # parse and validate count
            count = json_record[COUNT_KEY]
    
            if not isinstance(count, int) or count < 1:
                raise ValueError('invalid count')
                
            self.count = count
    
            # parse and validate runners up
            runners_up = json_record.get(RUNNERS_UP_KEY, None)
    
            if runners_up is not None:
                if not isinstance(runners_up, list) or len(runners_up) == 0:
                    raise ValueError('invalid runners_up')
    
            self.runners_up = runners_up
    
            # parse and validate sample
            self.sample = json_record.get(SAMPLE_KEY, None)
            
            has_sample = False
            if SAMPLE_KEY in json_record:
                # null is a valid sample so we need a boolean to indicate presence
                has_sample = True
    
            # validate sample pool size
            sample_pool_size = _get_sample_pool_size(count, runners_up)
    
            if sample_pool_size < 0:
                raise ValueError('invalid count or runners_up')
    
            if has_sample:
                if sample_pool_size == 0:
                    raise ValueError('invalid count or runners_up')
            else:
                if sample_pool_size > 0:
                    raise ValueError('missing sample')


    def has_sample(self):
        # init validates that if and only if a sample is present (which can be null), the sample pool size is >= 1
        return self.sample_pool_size() >= 1
    

    def sample_pool_size(self):
        return _get_sample_pool_size(self.count, self.runners_up)
        

    def is_decision_record(self):
        return self.type == DECISION_TYPE
        
        
    def is_reward_record(self):
        return self.type == REWARD_TYPE

        
    def to_rewarded_decision_dict(self):
        """ Return a dict representation of the rewarded decision record """
        
        result = {}
        
        # sorting the json keys may improve compression
        dumps = lambda x: json.dumps(x, option=json.OPT_SORT_KEYS).decode("utf-8")
        
        if self.is_decision_record():
            result[DECISION_ID_KEY] = self.message_id
            result[TIMESTAMP_KEY] = self.timestamp
            result[VARIANT_KEY] = dumps(self.variant)
            
            if self.givens is not None:
                result[GIVENS_KEY] = dumps(self.givens)
                
            if self.count is not None:
                result[COUNT_KEY] = self.count
                
            if self.runners_up is not None:
                result_runners_up = []
                for runner_up in self.runners_up:
                    result_runners_up.append(dumps(runner_up))
                result[RUNNERS_UP_KEY] = result_runners_up
                
            if self.sample is not None:
                result[SAMPLE_KEY] = dumps(self.sample)
                
        elif self.is_reward_record():
            # do NOT copy timestamp for reward record
            result[DECISION_ID_KEY] = self.decision_id
            result[REWARDS_KEY] = { self.message_id: self.reward }
            
        return result


    def __str__(self):
        return f'message_id {self.message_id} type {self.type} model {self.model} decision_id {self.decision_id}' \
        f'reward {self.reward} count {self.count} givens {self.givens} variant {self.variant}' \
        f' runners_up {self.runners_up} sample {self.sample} timestamp {self.timestamp}' 
    

def _get_sample_pool_size(count, runners_up):
    sample_pool_size = count - 1 - (len(runners_up) if runners_up else 0)  # subtract chosen variant and runners up from count
    assert sample_pool_size >= 0
    return sample_pool_size


class FirehoseRecordGroup:
    
    
    def __init__(self, model_name, records):
        assert(is_valid_model_name(model_name))
        self.model_name = model_name
        self.records = records
        

    def to_rewarded_decision_dicts(self):
        assert(self.records)
        return list(map(lambda x: x.to_rewarded_decision_dict(), self.records))


    @staticmethod
    def load_groups(s3_key):
        assert(s3_key)
        """
        Load records from a gzipped jsonlines file
        """
        
        records_by_model = {}
        invalid_records = []
        
        print(f'loading s3://{FIREHOSE_BUCKET}/{s3_key}')
    
        # download and parse the firehose file
        s3obj = s3client.get_object(Bucket=FIREHOSE_BUCKET, Key=s3_key)['Body']
        with gzip.GzipFile(fileobj=s3obj) as gzf:
            for line in gzf.readlines():
    
                try:
                    record = FirehoseRecord(json.loads(line))
                    model = record.model
                    
                    if model not in records_by_model:
                        records_by_model[model] = []
                    
                    records_by_model[model].append(record)
                    
                except Exception as e:
                    stats.add_parse_exception(e)
                    invalid_records.append(line)
                    continue
    
        if len(invalid_records):
            print(f'skipped {len(invalid_records)} invalid records')
            # TODO write invalid records to /uncrecoverable
    
        print(f'loaded {sum(map(len, records_by_model.values()))} records from firehose')
        
        results = []
        for model, records in records_by_model.items():
            results.append(FirehoseRecordGroup(model, records))
            
        return results
