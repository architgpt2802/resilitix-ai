# Use a standard Python image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the main logic file
COPY main.py .

# The command to run the application (Gunicorn serves the Flask app created by functions_framework)
# --bind 0.0.0.0:8080: Binds to the required port and host
# main:execute_bigquery_sql: Tells Gunicorn to serve the function 'execute_bigquery_sql' from 'main.py'
CMD ["functions-framework", "--target=execute_bigquery_sql"]
