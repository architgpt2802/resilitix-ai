import vertexai
from vertexai.generative_models import GenerativeModel, Tool, Part, Content, ChatSession, FunctionDeclaration
import requests
import json
import google.auth.transport.requests
import google.oauth2.id_token
import os
from google.cloud import discoveryengine_v1 as discoveryengine

# --- CONFIGURATION (Matches app.py) ---
PROJECT_ID = "resiliencegenomeai"
LOCATION = "us-central1"
TOOL_URL = "https://resilitix-sql-tool-525917099044.us-central1.run.app"
RAG_DATA_STORE_ID = "resilitix-rag-data_1765252053186" # Used the ID you confirmed earlier

# Initialize Vertex AI
vertexai.init(project=PROJECT_ID, location=LOCATION)

# --- HELPER: GET AUTH TOKEN ---
def get_id_token(url):
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, url)

# --- HELPER: LOAD CONFIGURATION ---
def load_config():
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config", "instructions.md")
        with open(config_path, "r") as f:
            base_instructions = f.read()
    except FileNotFoundError:
        base_instructions = "You are a helpful data assistant."

    try:
        examples_path = os.path.join(os.path.dirname(__file__), "config", "examples.json")
        with open(examples_path, "r") as f:
            examples_data = json.load(f)
            examples_text = "\n\n### SQL Few-Shot Examples:\n"
            for ex in examples_data:
                examples_text += f"User: {ex['question']}\nSQL: {ex['sql']}\n\n"
    except FileNotFoundError:
        examples_text = ""

    orchestration_instruction = """
    ## Tool Usage Rules:
    1. Use `query_bigquery` for quantitative data, statistics, or metrics.
    2. Use `search_knowledge_base` for qualitative documents, policies, or reports.
    3. You must choose only ONE tool per turn.
    """
    
    return base_instructions + orchestration_instruction + examples_text

# --- TOOL 1: BIGQUERY ---
def query_bigquery(query: str) -> dict:
    print(f"\n[DEBUG] Generating SQL Tool Token...")
    payload = json.dumps({"query": query})
    
    try:
        token = get_id_token(TOOL_URL)
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}'
        }
        
        print(f"[DEBUG] Sending request to Cloud Run: {TOOL_URL}")
        response = requests.post(TOOL_URL, data=payload, headers=headers)
        
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code} Error. Raw response: {response.text}"}
            
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON received. Raw content: {response.text}"}

    except Exception as e:
        return {"error": f"Connection Exception: {str(e)}"}

# --- TOOL 2: RAG ---
def search_knowledge_base(query: str) -> dict:
    print(f"\n[DEBUG] querying Discovery Engine...")
    try:
        # Explicitly set the API endpoint to global to match your previous fix
        client_options = {"api_endpoint": "discoveryengine.googleapis.com"}
        client = discoveryengine.SearchServiceClient(client_options=client_options)

        serving_config = client.serving_config_path(
            project=PROJECT_ID,
            location="global", 
            data_store=RAG_DATA_STORE_ID,
            serving_config="default_search",
        )
        
        request = discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=query,
            page_size=5,
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                summary_spec=discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec(
                    summary_result_count=5 
                )
            )
        )
        response = client.search(request)
        
        summary_text = ""
        if (response.summary and 
            response.summary.summary_with_metadata and 
            response.summary.summary_with_metadata.summary):
            summary_text = response.summary.summary_with_metadata.summary
            
        if not summary_text:
            return {"found": False, "message": "Search successful, but no summary text generated."}
            
        return {"found": True, "documents": [{"summary_text": summary_text}]}

    except Exception as e:
        return {"error": f"RAG Search Exception: {str(e)}"}

# --- MAIN EXECUTION LOOP ---
def run_cli_chat():
    print("--- Resilitix CLI Tester Initialized ---")
    print("Type 'exit' to quit.\n")

    # Define Tools
    sql_func = FunctionDeclaration(
        name="query_bigquery",
        description="Executes a Standard SQL query against the Resilitix BigQuery dataset.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    
    rag_func = FunctionDeclaration(
        name="search_knowledge_base",
        description="Searches the internal document knowledge base.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    
    combined_tools = Tool(function_declarations=[sql_func, rag_func])
    
    # Initialize Model
    model = GenerativeModel(
        "gemini-2.5-flash",
        system_instruction=load_config(),
        tools=[combined_tools],
    )
    chat = model.start_chat()

    while True:
        try:
            user_input = input("\nUser: ")
            if user_input.lower() in ["exit", "quit"]: break

            # Send message to model
            response = chat.send_message(user_input)

            # Handle Tool Calls
            while response.candidates and response.candidates[0].content.parts[0].function_call:
                part = response.candidates[0].content.parts[0]
                func_name = part.function_call.name
                args = part.function_call.args
                
                tool_result = None

                if func_name == "query_bigquery":
                    sql_q = args.get("query", "")
                    print(f"\n🛠️  [AGENT DECISION] Running SQL: {sql_q}")
                    
                    tool_result = query_bigquery(sql_q)
                    
                    # --- RAW DATA INSPECTION FOR MAPS ---
                    print("\n" + "="*40)
                    print("📊 RAW DATA RETURNED FROM BIGQUERY (For Map Logic):")
                    # Pretty print the JSON so you can see the table structure
                    print(json.dumps(tool_result, indent=2))
                    print("="*40 + "\n")
                
                elif func_name == "search_knowledge_base":
                    rag_q = args.get("query", "")
                    print(f"\n📚 [AGENT DECISION] RAG Search: {rag_q}")
                    tool_result = search_knowledge_base(rag_q)
                    # print(json.dumps(tool_result, indent=2)) # Uncomment to see raw RAG data

                # Send Result back to Model
                response = chat.send_message(
                    Part.from_function_response(
                        name=func_name,
                        response={"content": tool_result}
                    )
                )

            # Final Answer
            print(f"\n🤖 Agent: {response.text}")

        except Exception as e:
            print(f"❌ Error: {e}")

if __name__ == "__main__":
    run_cli_chat()