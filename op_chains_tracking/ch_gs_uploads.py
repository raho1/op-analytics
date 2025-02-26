#!/usr/bin/env python
# coding: utf-8

# In[1]:


# ! pipenv run jupyter nbconvert --to python ch_gs_uploads.ipynb


# In[ ]:


print('start ch uploads')
#Clickhouse db w/ Goldsky
# https://clickhouse.com/docs/en/integrations/python

import requests as r
import pandas as pd
import clickhouse_connect as cc
import os

import sys
sys.path.append("../helper_functions")
import duneapi_utils as d
import pandas_utils as p
import clickhouse_utils as ch
import csv_utils as cu
import google_bq_utils as bqu
import opstack_metadata_utils as ops
import goldsky_db_utils as gsb
sys.path.pop()

import time


# In[3]:


client = ch.connect_to_clickhouse_db() #Default is OPLabs DB
# client.close()

table_name = 'daily_aggegate_l2_chain_usage_goldsky'


# In[4]:


# bqu.delete_bq_table('api_table_uploads',table_name)


# In[5]:


days_chunk_size = 7 #30 #90
# days_chunk_size = 1
max_execution_secs = 5000

run_whole_history = False #True


# In[5]:


# # Get Chain List
chain_configs = ops.get_superchain_metadata_by_data_source('oplabs') # OPLabs db

# # if we want to filter for backfills
# chain_configs = chain_configs[chain_configs['blockchain'].isin(['xterio','kroma'])]
# chain_configs = chain_configs[chain_configs['blockchain'].isin(['automata'])]

# chain_configs


# In[6]:


sql_directory = "inputs/sql/"

query_name = "ch_template_alltime_chain_activity_v2"


# In[7]:


# Set the memory limit using the settings parameter
settings = {
    'max_memory_usage': 200000000000  # 200GB in bytes
}


# In[ ]:


import pandas as pd
from datetime import datetime, timedelta

unified_dfs = []
today = datetime.now().date()
current_date = today


for index, chain in chain_configs.iterrows():
    chain_schema = chain['blockchain']
    display_name = chain['display_name']
    block_time_sec = chain['block_time_sec']
    chain_id = chain['mainnet_chain_id']

    # Get Max Date for this chain
    # Check BQ
    # sql = f'''
    # SELECT MAX(dt) AS max_dt
    # FROM `api_table_uploads.daily_aggegate_l2_chain_usage_goldsky`
    # WHERE (chain_id = '{chain_id}' AND chain_id IS NOT NULL)
    #         OR (chain = '{display_name}')
    # '''
    # Check for any gaps in data
    sql = f'''
            WITH date_data AS (
            SELECT dt
            FROM `api_table_uploads.daily_aggegate_l2_chain_usage_goldsky`
            WHERE (chain_id = '{chain_id}' AND chain_id IS NOT NULL)
                OR (chain = '{display_name}')
                GROUP BY 1
            )
            SELECT MIN(d1.dt) AS max_dt
            FROM date_data d1
            LEFT JOIN date_data d2 ON d2.dt = DATE_ADD(d1.dt, INTERVAL 1 DAY)
            WHERE d2.dt IS NULL
                '''
    bq_df = bqu.run_query_to_df(sql)
    query_start_date = bq_df['max_dt'].iloc[0]
    print(query_start_date)
    # Check CH
    sql = f'''
            '''

    if pd.isna(query_start_date) | run_whole_history:
        try:
            # Get Start Date for the Chain
            print(f'{chain_schema} is new, getting first block date')
            firstblock_sql = f'''SELECT timestamp AS first_dt FROM {chain_schema}_blocks WHERE number = 1 AND is_deleted = 0'''
            firstblock_df = client.query_df(firstblock_sql)
            query_start_date = pd.to_datetime(firstblock_df['first_dt'].iloc[0]).date()
        except Exception as e:
            print('no first block date, skipping')
            break
    else:
        print(f'{chain_schema} exists, latest aggregation date')
        query_start_date = query_start_date.date() - timedelta(days=days_chunk_size)

    # Calculate the number of days to process
    days_to_process = (current_date - query_start_date).days
    print(f"{chain_schema} - To process: {days_to_process} days, from {query_start_date} to {current_date} \n")
    # Process data in chunks
    for chunk_start in range(days_to_process, 0, -days_chunk_size):
        chunk_end = max(chunk_start - days_chunk_size, 0)
        
        chunk_start_date = max( current_date - timedelta(days=chunk_start), query_start_date)
        chunk_end_date = current_date - timedelta(days=chunk_end-1)
        
        # Read the SQL query from file
        with open(os.path.join(sql_directory, f"{query_name}.sql"), "r") as file:
            query = file.read()
        print(f"{query_name} - {chain_schema} - Processing {chunk_start_date} to {chunk_end_date}")

        # Pass in Params to the query
        query = query.replace("@chain_db_name@", chain_schema)
        query = query.replace("@start_date@", chunk_start_date.strftime('%Y-%m-%d'))
        query = query.replace("@end_date@", chunk_end_date.strftime('%Y-%m-%d'))
        query = query.replace("@block_time_sec@", str(block_time_sec))
        query = query.replace("@max_execution_secs@", str(max_execution_secs))

        query = gsb.process_goldsky_sql(query)

        # # Save the query
        # output_folder = os.path.join("outputs", "sql")
        # os.makedirs(output_folder, exist_ok=True)
        # filename = f"{chain_schema}_query.sql"
        # file_path = os.path.join(output_folder, filename)
        # with open(file_path, 'w') as file:
        #     file.write(query)

        # Start the timer
        start_time = time.time()
        # Execute the query
        result_df = client.query_df(query, settings=settings)
        # End the timer
        end_time = time.time()
        execution_time = end_time - start_time
        print(f"{query_name} - {chain_schema} - Completed in {execution_time}")

        if not result_df.empty:
            result_df['chain_raw'] = result_df['chain']
            result_df['chain'] = display_name
            result_df['chain_id'] = result_df['chain_id'].astype('string')
            result_df = result_df[ result_df['dt'] < pd.to_datetime(today)]
            # bqu.append_and_upsert_df_to_bq_table(result_df, table_name, unique_keys=['dt', 'chain', 'network'])
            unified_dfs.append(result_df)
        else:
            print('dataframe is empty')

concat_df = pd.concat(unified_dfs)


# In[ ]:


# Ensure No Duplicates

# Sort the DataFrame by the two columns in descending order
write_df = concat_df.sort_values(['num_blocks', 'num_raw_txs'], ascending=[False, False])

# Create a rank column using the dense method
write_df['rank'] = write_df.groupby(['num_blocks', 'num_raw_txs']).ngroup().rank(method='dense', ascending=True)
    # Select the top-ranked row for each group (dt, chain, network)
write_df = write_df.groupby(['dt', 'chain', 'network']).first().reset_index()
# Drop the rank column
write_df = write_df.drop(columns=['rank'])

write_df = write_df.drop_duplicates() #remove date overlap
# display(write_df.sort_values(by=['dt']))
# write_df.to_csv('outputs/debug_automata.csv')
# BQ Upload
bqu.append_and_upsert_df_to_bq_table(write_df, table_name, unique_keys=['dt', 'chain_id', 'network'])
#CSV Upload
csv_chain_name = display_name.lower().replace(' ', '_').replace('-', '_')
# write_df.to_csv(f"outputs/chain_data/{query_name}_{csv_chain_name}.csv", index=False)


# In[ ]:


# Copy to Dune
print('upload bq to dune')
sql = '''
SELECT *
FROM `api_table_uploads.daily_aggegate_l2_chain_usage_goldsky`
'''
bq_df = bqu.run_query_to_df(sql)

dune_table_name = 'ch_template_alltime_chain_activity'
d.write_dune_api_from_pandas(bq_df, dune_table_name,table_description = dune_table_name)

