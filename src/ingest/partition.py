# Built-in imports
from collections import ChainMap
import itertools
from typing import List
import math

# External imports
from ksuid import Ksuid
import orjson
import pandas as pd
from uuid import uuid4


# Local imports
from config import s3client, TRAIN_BUCKET, PARQUET_FILE_MAX_DECISION_RECORDS, stats
from firehose_record import DECISION_ID_KEY, REWARDS_KEY, REWARD_KEY, DF_SCHEMA
from firehose_record import is_valid_message_id
from utils import is_valid_model_name, json_dumps, list_partitions_after


ISO_8601_BASIC_FORMAT = '%Y%m%dT%H%M%SZ'


class RewardedDecisionPartition:


    def __init__(self, model_name, df, s3_key=None):
        assert is_valid_model_name(model_name)
        # TODO assert df and df.shape[0] > 0 # must have some rows

        self.model_name = model_name
        self.df = df

        """        
        This implementation intentionally only supports a single s3 key to ensure that
        partitions_from_firehose_record_group() only ever associates a single key with each partition.
        See comments in partitions_from_firehose_record_group() for a deeper explanation.
        The tradeoff is that repair() has to do slightly more work with loading, merging,
        and deleting multiple S3 keys.
        """
        self.s3_key = s3_key

        self.sorted = False


    def process(self):

        # load the existing .parquet file (if any) from s3
        self.load()
        
        # remove any invalid rows
        self.filter_valid()

        # sort the combined dataframe and update min/max decision_ids
        self.sort()

        # merge the rewarded decisions together to accumulate the rewards
        self.merge()
        
        # save the consolidated .parquet file to s3
        self.save()

        # delete the old .parquet file (if any) and clean up dataframe RAM
        self.cleanup()


    def load(self):
        """
        Raises:
            ValueError: If invalid records are found
        """
        
        if not self.s3_key:
            stats.increment_rewarded_decision_count(self.model_name, self.df.shape[0])
            # nothing to load, just use the incoming firehose records
            return

        # TODO split load into s3 request and parse.
        try:
            s3_df = pd.read_parquet(f's3://{TRAIN_BUCKET}/{self.s3_key}')
            stats.increment_s3_requests_count('get')
        except IOError as e:
            print(f'non-fatal error reading {self.s3_key} ignoring file, will likely trigger automatic repair (exception: {e})')
            
            # it is critical that the file at self.s3_key is not deleted because its records have not been merged
            self.s3_key = None
            assert not self.s3_key
            
            return

        # TODO: add more validations
        valid_idxs = s3_df.decision_id.apply(is_valid_message_id)
        if not valid_idxs.all():
            unrecoverable_key = f'unrecoverable/{self.s3_key}'
            s3_df.to_parquet(f's3://{TRAIN_BUCKET}/{unrecoverable_key}', compression='ZSTD')
            s3client.delete_object(Bucket=TRAIN_BUCKET, Key=self.s3_key)
            stats.remember_bad_s3_parquet_file(unrecoverable_key)
            stats.increment_s3_requests_count('put-post')
            raise ValueError(f"Invalid records found in '{self.s3_key}'. Moved to s3://{TRAIN_BUCKET}/{unrecoverable_key}'")

        stats.increment_rewarded_decision_count(self.model_name, self.df.shape[0], s3_df.shape[0])
        self.df = pd.concat([self.df, s3_df], ignore_index=True)


    def save(self):
        assert self.sorted

        # split the dataframe into multiple chunks if necessary
        for chunk in split(self.df):
            # generate a unique s3 key for this chunk
            chunk_s3_key = parquet_s3_key(self.model_name, min_decision_id=chunk[DECISION_ID_KEY].iat[0], 
                max_decision_id=chunk[DECISION_ID_KEY].iat[-1], count=chunk.shape[0])
                
            chunk.to_parquet(f's3://{TRAIN_BUCKET}/{chunk_s3_key}', compression='ZSTD')
            stats.increment_s3_requests_count('put-post')

    
    def filter_valid(self):
        # TODO remove any rows with invalid decision_ids, update stats, copy to /unrecoverable (lower priority)
        pass
    
    
    def sort(self):
        self.df.sort_values(DECISION_ID_KEY, inplace=True, ignore_index=True)
        
        self._min_decision_id = self.df[DECISION_ID_KEY].iat[0]
        self._max_decision_id = self.df[DECISION_ID_KEY].iat[-1]

        self.sorted = True
        
    
    @property    
    def min_decision_id(self):
        assert self.sorted
        # use instance variable because it will be accessed after dataframe cleanup
        return self._min_decision_id
        
    
    @property
    def max_decision_id(self):
        assert self.sorted
        # use instance variable because it will be accessed after dataframe cleanup
        return self._max_decision_id


    def merge(self):
        """
        Merge full or partial "rewarded decision records".
        This process is idempotent. It may be safely repeated on 
        duplicate records and performed in any order.
        If fields collide, one will win, but which one is unspecified.  

        """
        
        assert self.sorted

        def merge_rewards(rewards_series):
            """Shallow merge of a list of dicts"""
            rewards_dicts = rewards_series.dropna().apply(lambda x: orjson.loads(x))
            return json_dumps(dict(ChainMap(*rewards_dicts)))

        def sum_rewards(rewards_series):
            """ Sum all the merged rewards values """
            merged_rewards = orjson.loads(merge_rewards(rewards_series))
            return float(sum(merged_rewards.values()))

        def get_first_cell(col_series):
            """Return the first cell of a column """
            
            if col_series.isnull().all():
                first_element = col_series.iloc[0]
            else:
                first_element = col_series.dropna().iloc[0]

                if col_series.name == "count":
                    return first_element.astype("int64")
            return first_element


        non_reward_keys = [key for key in self.df.columns if key not in [REWARD_KEY, REWARDS_KEY]]

        # Create dict of aggregations with cols in the same order as the expected result
        aggregations = { key : pd.NamedAgg(column=key, aggfunc=get_first_cell) for key in non_reward_keys }

        if REWARDS_KEY in self.df.columns:
            aggregations[REWARDS_KEY] = pd.NamedAgg(column="rewards", aggfunc=merge_rewards)
            aggregations[REWARD_KEY]  = pd.NamedAgg(column="rewards", aggfunc=sum_rewards)
        
        """
        Now perform the aggregations. This is how it works:
        
        1) "groupby" creates subsets of the original DF where each subset 
        has rows with the same decision_id.
        
        2) "agg" uses the aggregations dict to create a new row for each 
        subset. The columns will be new and named after each key in the 
        aggregations dict. The cell values of each column will be based on 
        the NamedAgg named tuple, specified in the aggregations dict.
        
        3) These NamedAgg named tuples specify which column of the subset 
        will be passed to the specified aggregation functions.
        
        4) The aggregation functions process the values of the passed column 
        and return a single value, which will be the contents of the cell 
        in a new column for that subset.
        
        Example:
        
        >>> df = pd.DataFrame({
        ...     "A": [1, 1, 2, 2],
        ...     "B": [1, 2, 3, 4],
        ...     "C": [0.362838, 0.227877, 1.267767, -0.562860],
        ... })

        >>> df
           A  B         C
        0  1  1  0.362838
        1  1  2  0.227877
        2  2  3  1.267767
        3  2  4 -0.562860

        >>> df.groupby("A").agg(
        ...     b_min=pd.NamedAgg(column="B", aggfunc="min"),
        ...     c_sum=pd.NamedAgg(column="C", aggfunc="sum")
        ... )
            b_min     c_sum
        A
        1      1  0.590715
        2      3  0.704907
        """

        self.df = self.df.groupby("decision_id").agg(**aggregations).reset_index(drop=True).astype(DF_SCHEMA)
        stats.increment_records_after_merge_count(self.df.shape[0])


    def cleanup(self):
        if self.s3_key:
            # delete the previous .parqet from s3
            # do this last in case there is a problem during processing that needs to be retried
            s3client.delete_object(Bucket=TRAIN_BUCKET, Key=self.s3_key)
            stats.increment_s3_requests_count('delete')

        # reclaim the dataframe memory
        # do not clean up min/max decision_ids since they will need to be used after processing
        # for determining if any of the .parquet files have overlapping decision_ids
        self.df = None
        del self.df
        
    
    @staticmethod
    def partitions_from_firehose_record_group(firehose_record_group):
        """
        High Level Algorithm:
        
        1)  For each decision_id that is being ingested, we need to check if that decision_id is already covered by an existing partition
        within S3.
        
        2)  Each partition file S3 key starts with a prefix of the max KSUID in that partition and contains more characters after that.
        
        3)  S3's list_objects_v2 request returns results in lexicographical order, using the StartAfter option using a prefix key generated
            from the target decision_id, the first result will be the target partition.  If there are no results
            then the max decision_id in all partitions is less than the current one, so there will be no existing partition returned
            and a new partition file will be created.

        4)  Sending a seperate list_objects_v2 request for each decision_id would be extremely slow and expensive.  Due to S3's lexicographically
            sorted list_objects_v2 results, rather than send one list request per decision_id to S3, we just send a few list requests for 
            the range of decision_id prefixes that we’re interested in.  We then use those listing results to partition the decisions by the 
            s3_key that may contain the same decision_ids.
            
        This function assumes a consistent system where there is a maximum of one partition that a decision_id could be found within. Inconsisency 
        is assumed to be a rare event that is handled by the repair() process. This allows for less memory use than loading all possibly matching partitions
        and we would rather repair() fail due to out of memory than the primary ingest process failing.
        """

        model_name = firehose_record_group.model_name

        if DEBUG:
            print(f"Working on the FirehoseRecordGroup of model: '{model_name}'")
        
        rdrs_df = firehose_record_group.to_pandas_df()

        sorted_s3_prefixes = get_sorted_s3_prefixes(rdrs_df, model_name=model_name)

        rdrs_df = rdrs_df.iloc[sorted_s3_prefixes.index]
        rdrs_df.reset_index(drop=True, inplace=True)

        min_decision_id, max_decision_id = \
            (rdrs_df['decision_id'].iloc[0], rdrs_df['decision_id'].iloc[-1])

        start_after_key = parquet_s3_key_prefix(model_name, min_decision_id)
        if DEBUG:
            print(f"{model_name} - Model's decision ids span along: [{min_decision_id} - {max_decision_id}]")

        """
        List the s3 keys.
    
        Since start_after_key is a prefix, it is guaranteed to match any keys which begin with those prefix characters. So the first
        key returned is guaranteed to have a maximum timestamp equal to or greater than the min_decision_id's timestamp
    

        Design Note: Since the ingest process only loads a maximum of one s3 key per partition, we could have implemented an
        early stopping on this list process, but in normal operation we are ingesting to the end of the timeline anyway
        so most list operations would continue to the final s3 key. Thus for simplicity we opt for a simple start_after_key
        semantic.
        """
        s3_keys = list_partitions_after(
            bucket_name=TRAIN_BUCKET,
            key=start_after_key,
            prefix=f'rewarded_decisions/{model_name}/')
        if len(s3_keys) == 0:
            return [RewardedDecisionPartition(model_name, rdrs_df)]

        if DEBUG:
            print(f"{model_name} - Retrieved {len(s3_keys)} Parquet file key(s) from S3")
            print(f"{model_name} - Crafting RewardedDecisionPartitions...")

        map_of_s3_keys_to_rdrs = {}
        for i, s3_key in enumerate(sorted(s3_keys)):

            s3_prefixes = \
                get_sorted_s3_prefixes(rdrs_df, model_name=model_name, reset_index=True)

            append_s3_to_firehose_records = (s3_prefixes < s3_key)

            if not append_s3_to_firehose_records.any():
                continue
            if DEBUG:
                print("{} - This RDP has {:02} (P)RDRs, {:02} unique decision_id(s) and a Parquet S3 key".format(
                    model_name,
                    append_s3_to_firehose_records.sum(),
                    rdrs_df.loc[append_s3_to_firehose_records, "decision_id"].unique().shape[0]
                    ))

            # append selected rows to list
            map_of_s3_keys_to_rdrs[s3_key] = \
                rdrs_df[append_s3_to_firehose_records].reset_index(drop=True)
            # remove appended rows from rdrs_df
            rdrs_df = rdrs_df[~append_s3_to_firehose_records].reset_index(drop=True)

        partitions_s3 = [
            RewardedDecisionPartition(
                model_name=model_name,
                df=df, s3_key=s3k) for s3k, df in map_of_s3_keys_to_rdrs.items()]

        if not rdrs_df.empty:
            if DEBUG:
                print("{} - This RDP has {:02} (P)RDRs, {:02} unique decision_id(s) and no S3 key".format(
                    model_name,
                    rdrs_df.shape[0],
                    rdrs_df.loc[:, "decision_id"].unique().shape[0]
                ))
            partitions_s3.append(RewardedDecisionPartition(model_name=model_name, df=rdrs_df))

        if DEBUG:
            print(f"{model_name} - {len(partitions_s3):02} RDPs were produced out of this FirehoseRecordGroup")
        
        return partitions_s3


def get_sorted_s3_prefixes(df, model_name, reset_index=False):
    """ Get s3 prefixes based on decision_ids from DF of records """

    s3_prefixes = df['decision_id'].apply(
        lambda x: parquet_s3_key_prefix(model_name=model_name, max_decision_id=x)).copy()

    if not reset_index:
        return s3_prefixes.sort_values()

    return s3_prefixes.sort_values().reset_index(drop=True)


def get_min_decision_id(partitions):
    min_per_partitions = list(map(lambda x: x.min_decision_id, partitions))
    min_decision_id = min([None] if not min_per_partitions else min_per_partitions)

    return min_decision_id


def get_unique_overlapping_keys(single_overlap_keys):
    return set(itertools.chain(*single_overlap_keys.values()))


def get_all_overlaps(keys_to_repair):
    """
    Given a list of S3 keys, where each one has:

        - the min timestamp of the records in its corresponding Parquet file
        - the max timestamp of the records in its corresponding Parquet file
    
    Create one `IntervalDict`s [1] for each S3 key: a closed `Interval`
    [2] acts as a key and a list with the S3 key acts as the value.

        -> This `Interval` is an object representing the interval between:
            - the min timestamp of the records in its corresponding Parquet file
            - the max timestamp of the records in its corresponding Parquet file
            
        -> The list which contains the S3 key is for storing the S3 keys that 
           are part of this interval (in this case, only one)
    
    This `IntervalDict` will later be merged with other `IntervalDict` 
    (if both have overlapping `Interval`s) and a new `IntervalDict` 
    will be created, with an updated `Interval` and a list with all the
    combined s3 keys of the original `IntervalDicts`.

    Return a list of `IntervalDict`s covering all the timestamp range of 
    the given S3 keys. Some or all of these `IntervalDict`s may be 
    created out of the merge of multiple `IntervalDicts`, so such 
    merged objects will contain multiple S3 keys.
    
    [1] https://github.com/AlexandreDecan/portion#map-intervals-to-data
    [2] https://github.com/AlexandreDecan/portion#interval-creation
    """
    
    # Create list of "Interval" objects
    train_s3_intervals = []
    for key in keys_to_repair:
        maxts_key, mints_key = key.split('/')[-1].split('-')[:2]
        interval = P.IntervalDict({P.closed(mints_key, maxts_key): [key]})
        train_s3_intervals.append(interval)

    # Modified from:
    # https://www.csestack.org/merge-overlapping-intervals/
    train_s3_intervals.sort(key = lambda x: x.domain().lower)
    overlaps = [train_s3_intervals[0]]
    for i in range(1, len(train_s3_intervals)):

        pop_element = overlaps.pop()
        next_element = train_s3_intervals[i]

        if pop_element.domain().overlaps(next_element.domain()):
            new_element = pop_element.combine(next_element, how=lambda a, b: a + b)
            overlaps.append(new_element)
        else:
            overlaps.append(pop_element)
            overlaps.append(next_element)

    return overlaps


def repair_overlapping_keys(model_name: str, partitions: List[RewardedDecisionPartition]):
    """
    Detect parquet files which contain decision ids from overlapping 
    time periods and fix them.
    
    The min timestamp is encoded into the file name so that a 
    lexicographically ordered listing can determine if two parquet 
    files have overlapping decision_id ranges, which they should not. 
    
    If overlapping ranges are detected they should be repaired by 
    loading the overlapping parquet files, consolidating them, 
    optionally splitting, then saving. This process should lead to 
    eventually consistency.
    """
    
    for partition in partitions:
        assert partition.model_name == model_name
        
    min_decision_id = get_min_decision_id(partitions)

    """
    List the s3 keys.
    
    Since start_after_key is a prefix, it is guaranteed to match any keys which begin with those prefix characters. So the first
    key returned is guaranteed to have a maximum timestamp equal to or greater than the min_decision_id's timestamp
    
    Design Note: Since the repair process only needs to operate on a bounded range of keys, we could have implemented an
    early stopping on this list process, but in normal operation we are ingesting to the end of the timeline anyway
    so most list operations would continue to the final s3 key. Thus for simplicity we opt for a simple start_after_key
    semantic.
    """
    train_s3_keys = list_partitions_after(
        bucket_name=TRAIN_BUCKET,
        key=parquet_s3_key_prefix(model_name, min_decision_id),
        prefix=f'rewarded_decisions/{model_name}/')

    # if there are no files in s3 yet there is nothing to fix
    if len(train_s3_keys) <= 1:
        return

    train_s3_keys.reverse()

    assert train_s3_keys[0] > train_s3_keys[-1]

    overlaps = get_all_overlaps(keys_to_repair=train_s3_keys)

    # If there are overlapping ranges, load parquet files, consolidate them, save them
    for overlap in overlaps:
        keys = get_unique_overlapping_keys(overlap)
        if len(keys) > 1:
            if DEBUG:
                print(f"{model_name} - Found {len(keys)} overlapping S3 keys")
            stats.increment_counts_of_set_of_overlapping_s3_keys(len(keys))

            dfs = []
            for s3_key in keys:
                dfs.append(pd.read_parquet(f's3://{TRAIN_BUCKET}/{s3_key}'))
                stats.increment_s3_requests_count('get')

            df = pd.concat(dfs, ignore_index=True)
            RDP = RewardedDecisionPartition(model_name, df=df)
            RDP.process()

            response = s3client.delete_objects(
                Bucket=TRAIN_BUCKET,
                Delete={
                    'Objects': [{'Key': s3_key} for s3_key in keys],
                },
            )
            stats.increment_s3_requests_count('delete')

    return


def split(df, max_row_count=PARQUET_FILE_MAX_DECISION_RECORDS):
    
    chunk_count = math.ceil(df.shape[0] / max_row_count)
    
    # adapted from https://stackoverflow.com/a/2135920/2590111 to split into roughly equal size chunks
    def split_roughly_equal(df, n):
        k, m = divmod(df.shape[0], n)
        return (df.iloc[i*k+min(i, m):(i+1)*k+min(i+1, m)] for i in range(n))

    return split_roughly_equal(df, chunk_count)


def parquet_s3_key_prefix(model_name, max_decision_id):
    max_timestamp = Ksuid.from_base62(max_decision_id).datetime.strftime(ISO_8601_BASIC_FORMAT)
    
    yyyy = max_timestamp[0:4]
    mm = max_timestamp[4:6]
    dd = max_timestamp[6:8]
    
    # The max timestamp is encoded first in the path so that a lexicographically sorted
    # search of file names starting at the prefix of the target decision_id will provide
    # the .parquet that should contain that decision_id, if it exists
    return f'rewarded_decisions/{model_name}/parquet/{yyyy}/{mm}/{dd}/{max_timestamp}'
    
    
def parquet_s3_key(model_name, min_decision_id, max_decision_id, count):
    min_timestamp = Ksuid.from_base62(min_decision_id).datetime.strftime(ISO_8601_BASIC_FORMAT)
    
    #
    # The min timestamp is encoded into the file name so that a lexicographically ordered listing
    # can determine if two parquet files have overlapping decision_id ranges, which they should not.
    # If overlapping ranges are detected they should be repaired by loading the overlapping parquet
    # files, consolidating them, optionally splitting, then saving.  This process should lead to
    # eventually consistency.
    #
    # The final UUID4 is simply to give the file a random name. For now, the characters following
    # the last dash should be considered an opaque string of random characters
    #
    return f'{parquet_s3_key_prefix(model_name, max_decision_id)}-{min_timestamp}-{count}-{uuid4()}.parquet'


def list_partition_s3_keys(model_name):
    keys = list_s3_keys(bucket_name=bucket_name, prefix=prefix)
    return keys if not valid_keys_only else [k for k in keys if is_valid_rewarded_decisions_s3_key(k)]
