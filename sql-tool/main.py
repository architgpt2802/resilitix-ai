import functions_framework
from google.cloud import bigquery
import json
import os

# Initialize BigQuery Client
client = bigquery.Client()

@functions_framework.http
def execute_bigquery_sql(request):
    # 1. Parse Request
    request_json = request.get_json(silent=True)

    # If no JSON or no 'query' key, return error
    if not request_json or 'query' not in request_json:
        return (json.dumps({"error": "No query provided"}), 400, {'Content-Type': 'application/json'})

    sql_query = request_json['query']
    print(f"Executing SQL: {sql_query}") 

    # 2. Run Query
    try:
        query_job = client.query(sql_query)
        results = query_job.result()

        # Convert rows to dicts
        rows = [dict(row) for row in results]

        # Return JSON
        return (json.dumps({"data": rows}, default=str), 200, {'Content-Type': 'application/json'})

    except Exception as e:
        print(f"BigQuery Error: {str(e)}")
        return (json.dumps({"error": str(e)}), 500, {'Content-Type': 'application/json'})
