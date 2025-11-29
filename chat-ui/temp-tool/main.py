import functions_framework
from google.cloud import bigquery
import json
import os

# Initialize BigQuery Client
client = bigquery.Client()

@functions_framework.http
def execute_bigquery_sql(request):
    request_json = request.get_json(silent=True)
    if not request_json or 'query' not in request_json:
        return (json.dumps({"error": "No query provided"}), 400)

    sql_query = request_json['query']
    print(f"Executing SQL: {sql_query}") # Log for debugging

    try:
        # Run the query
        query_job = client.query(sql_query)
        results = query_job.result()
        rows = [dict(row) for row in results]
        # Convert date/time objects to strings
        return json.dumps({"data": rows}, default=str)

    except Exception as e:
        print(f"BigQuery Error: {str(e)}")
        return (json.dumps({"error": str(e)}), 500)
