# import vertexai
# from vertexai.generative_models import GenerativeModel, Tool, Part, Content, ChatSession, FunctionDeclaration
# import requests
# import json
# import google.auth.transport.requests
# import google.oauth2.id_token
# import os
# from google.cloud import discoveryengine_v1 as discoveryengine

# # --- CONFIGURATION (Matches app.py) ---
# PROJECT_ID = "resiliencegenomeai"
# LOCATION = "us-central1"
# TOOL_URL = "https://resilitix-sql-tool-525917099044.us-central1.run.app"
# RAG_DATA_STORE_ID = "resilitix-rag-data_1765252053186" # Used the ID you confirmed earlier

# # Initialize Vertex AI
# vertexai.init(project=PROJECT_ID, location=LOCATION)

# # --- HELPER: GET AUTH TOKEN ---
# def get_id_token(url):
#     auth_req = google.auth.transport.requests.Request()
#     return google.oauth2.id_token.fetch_id_token(auth_req, url)

# # --- HELPER: LOAD CONFIGURATION ---
# def load_config():
#     try:
#         config_path = os.path.join(os.path.dirname(__file__), "config", "instructions.md")
#         with open(config_path, "r") as f:
#             base_instructions = f.read()
#     except FileNotFoundError:
#         base_instructions = "You are a helpful data assistant."

#     try:
#         examples_path = os.path.join(os.path.dirname(__file__), "config", "examples.json")
#         with open(examples_path, "r") as f:
#             examples_data = json.load(f)
#             examples_text = "\n\n### SQL Few-Shot Examples:\n"
#             for ex in examples_data:
#                 examples_text += f"User: {ex['question']}\nSQL: {ex['sql']}\n\n"
#     except FileNotFoundError:
#         examples_text = ""

#     orchestration_instruction = """
#     ## Tool Usage Rules:
#     1. Use `query_bigquery` for quantitative data, statistics, or metrics.
#     2. Use `search_knowledge_base` for qualitative documents, policies, or reports.
#     3. You must choose only ONE tool per turn.
#     """
    
#     return base_instructions + orchestration_instruction + examples_text

# # --- TOOL 1: BIGQUERY ---
# def query_bigquery(query: str) -> dict:
#     print(f"\n[DEBUG] Generating SQL Tool Token...")
#     payload = json.dumps({"query": query})
    
#     try:
#         token = get_id_token(TOOL_URL)
#         headers = {
#             'Content-Type': 'application/json',
#             'Authorization': f'Bearer {token}'
#         }
        
#         print(f"[DEBUG] Sending request to Cloud Run: {TOOL_URL}")
#         response = requests.post(TOOL_URL, data=payload, headers=headers)
        
#         if response.status_code != 200:
#             return {"error": f"HTTP {response.status_code} Error. Raw response: {response.text}"}
            
#         try:
#             return response.json()
#         except json.JSONDecodeError:
#             return {"error": f"Invalid JSON received. Raw content: {response.text}"}

#     except Exception as e:
#         return {"error": f"Connection Exception: {str(e)}"}

# # --- TOOL 2: RAG ---
# def search_knowledge_base(query: str) -> dict:
#     print(f"\n[DEBUG] querying Discovery Engine...")
#     try:
#         # Explicitly set the API endpoint to global to match your previous fix
#         client_options = {"api_endpoint": "discoveryengine.googleapis.com"}
#         client = discoveryengine.SearchServiceClient(client_options=client_options)

#         serving_config = client.serving_config_path(
#             project=PROJECT_ID,
#             location="global", 
#             data_store=RAG_DATA_STORE_ID,
#             serving_config="default_search",
#         )
        
#         request = discoveryengine.SearchRequest(
#             serving_config=serving_config,
#             query=query,
#             page_size=5,
#             content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
#                 summary_spec=discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec(
#                     summary_result_count=5 
#                 )
#             )
#         )
#         response = client.search(request)
        
#         summary_text = ""
#         if (response.summary and 
#             response.summary.summary_with_metadata and 
#             response.summary.summary_with_metadata.summary):
#             summary_text = response.summary.summary_with_metadata.summary
            
#         if not summary_text:
#             return {"found": False, "message": "Search successful, but no summary text generated."}
            
#         return {"found": True, "documents": [{"summary_text": summary_text}]}

#     except Exception as e:
#         return {"error": f"RAG Search Exception: {str(e)}"}

# # --- MAIN EXECUTION LOOP ---
# def run_cli_chat():
#     print("--- Resilitix CLI Tester Initialized ---")
#     print("Type 'exit' to quit.\n")

#     # Define Tools
#     sql_func = FunctionDeclaration(
#         name="query_bigquery",
#         description="Executes a Standard SQL query against the Resilitix BigQuery dataset.",
#         parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
#     )
    
#     rag_func = FunctionDeclaration(
#         name="search_knowledge_base",
#         description="Searches the internal document knowledge base.",
#         parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
#     )
    
#     combined_tools = Tool(function_declarations=[sql_func, rag_func])
    
#     # Initialize Model
#     model = GenerativeModel(
#         "gemini-2.5-flash",
#         system_instruction=load_config(),
#         tools=[combined_tools],
#     )
#     chat = model.start_chat()

#     while True:
#         try:
#             user_input = input("\nUser: ")
#             if user_input.lower() in ["exit", "quit"]: break

#             # Send message to model
#             response = chat.send_message(user_input)

#             # Handle Tool Calls
#             while response.candidates and response.candidates[0].content.parts[0].function_call:
#                 part = response.candidates[0].content.parts[0]
#                 func_name = part.function_call.name
#                 args = part.function_call.args
                
#                 tool_result = None

#                 if func_name == "query_bigquery":
#                     sql_q = args.get("query", "")
#                     print(f"\n🛠️  [AGENT DECISION] Running SQL: {sql_q}")
                    
#                     tool_result = query_bigquery(sql_q)
                    
#                     # --- RAW DATA INSPECTION FOR MAPS ---
#                     print("\n" + "="*40)
#                     print("📊 RAW DATA RETURNED FROM BIGQUERY:")
#                     # Pretty print the JSON so you can see the table structure
#                     print(json.dumps(tool_result, indent=2))
#                     print("="*40 + "\n")
                
#                 elif func_name == "search_knowledge_base":
#                     rag_q = args.get("query", "")
#                     print(f"\n📚 [AGENT DECISION] RAG Search: {rag_q}")
#                     tool_result = search_knowledge_base(rag_q)
#                     # print(json.dumps(tool_result, indent=2)) # Uncomment to see raw RAG data

#                 # Send Result back to Model
#                 response = chat.send_message(
#                     Part.from_function_response(
#                         name=func_name,
#                         response={"content": tool_result}
#                     )
#                 )

#             # Final Answer
#             print(f"\n🤖 Agent: {response.text}")

#         except Exception as e:
#             print(f"❌ Error: {e}")

# if __name__ == "__main__":
#     run_cli_chat()





import vertexai
from vertexai.generative_models import GenerativeModel, Tool, Part, Content, ChatSession, FunctionDeclaration
import requests
import json
import google.auth.transport.requests
import google.oauth2.id_token
import os
from google.cloud import discoveryengine_v1 as discoveryengine
from typing import Dict, Any

# --- GLOBAL CONTEXT STORE (Simulating st.session_state for CLI) ---
_CONTEXT_STORE = {
    "last_sql_context": None
}

# --- CONFIGURATION ---
PROJECT_ID = "resiliencegenomeai"
LOCATION = "us-central1"
# The Cloud Run endpoint for SQL execution
TOOL_URL = "https://resilitix-sql-tool-525917099044.us-central1.run.app"
# Discovery Engine Data Store ID
RAG_DATA_STORE_ID = "resilitix-rag-data_1765252053186" 

# Initialize Vertex AI
vertexai.init(project=PROJECT_ID, location=LOCATION)

# --- LOW-LEVEL HELPERS (The "Hands" - Raw External API Calls) ---

def get_id_token(url: str) -> str:
    """Generates a Google ID Token to authenticate with the Cloud Run tool."""
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, url)

def execute_bigquery_request(query: str) -> Dict[str, Any]:
    """Raw helper to hit the Cloud Run SQL Tool and get data."""
    print(f"    [Execution] Sending SQL to Cloud Run: {query[:80]}...")
    payload = json.dumps({"query": query})
    try:
        token = get_id_token(TOOL_URL)
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}'
        }
        response = requests.post(TOOL_URL, data=payload, headers=headers)
        
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code} Error. Raw response: {response.text}"}
        
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON received. Raw content: {response.text}"}
    except Exception as e:
        return {"error": f"Connection Exception: {str(e)}"}

def execute_rag_search(query: str) -> Dict[str, Any]:
    """Raw helper to hit Vertex AI Search and extract summary/chunks."""
    try:
        client_options = {"api_endpoint": "discoveryengine.googleapis.com"}
        client = discoveryengine.SearchServiceClient(client_options=client_options)

        serving_config = client.serving_config_path(
            project=PROJECT_ID, location="global", data_store=RAG_DATA_STORE_ID, serving_config="default_search",
        )
        
        request = discoveryengine.SearchRequest(
            serving_config=serving_config, query=query, page_size=5,
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                summary_spec=discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec(summary_result_count=5)
            )
        )
        response = client.search(request)
        
        summary = ""
        if response.summary and response.summary.summary_with_metadata:
             summary = response.summary.summary_with_metadata.summary
             
        # In a real app, you might extract chunks here too. For testing, we just use the summary field.
        
        return {"summary": summary, "found": bool(summary)}
    except Exception as e:
        return {"error": str(e)}

# --- AGENT SPECIALISTS (The Sub-Agents, each with its own LLM session) ---

def agent_text_to_sql(user_query: str) -> Dict[str, Any]:
    """
    SPECIALIST A: Data Analyst Agent (Text-to-SQL).
    Takes NL query, generates SQL, executes, and returns SQL/Data.
    """
    print(f"\n  [Agent A: SQL] Processing Request: '{user_query}'")
    
    # 1. Define Tool for this specific Agent's LLM session
    sql_func = FunctionDeclaration(
        name="run_sql",
        description="Executes a Standard SQL query on the BigQuery dataset.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    sql_tool = Tool(function_declarations=[sql_func])

    # 2. Agent Prompt (Focuses the model on its task)
    system_prompt = """
    You are a SQL Expert for the Resilitix BigQuery data.
    1. Your only job is to convert the user's request into a single Standard SQL query.
    2. Use the `run_sql` tool to execute it.
    3. Return the exact JSON output from the `run_sql` tool call, including the generated SQL query and the fetched data. Do NOT summarize or chat.
    """

    # 3. Independent Model Session
    model = GenerativeModel("gemini-2.5-flash", system_instruction=system_prompt, tools=[sql_tool])
    chat = model.start_chat()
    
    response = chat.send_message(user_query)
    
    final_output = {"error": "SQL Agent could not process request."}
    
    try:
        # Check for tool call from the Agent's model
        if response.candidates[0].content.parts[0].function_call:
            fn = response.candidates[0].content.parts[0].function_call
            if fn.name == "run_sql":
                sql_q = fn.args["query"]
                print(f"    [Agent A: SQL] Generated SQL: {sql_q}")
                
                # Execute the SQL using the low-level helper
                data_result = execute_bigquery_request(sql_q)
                
                # Add metadata before sending back to the Orchestrator
                full_result = {
                    "request": user_query,
                    "generated_sql": sql_q,
                    "execution_result": data_result
                }
                
                # CRITICAL: Store the data for the Mapping Agent
                _CONTEXT_STORE["last_sql_context"] = full_result
                
                # Feed the result back to the Agent's model to close the loop (although we don't use its text output here)
                chat.send_message(
                    Part.from_function_response(name="run_sql", response={"content": data_result})
                )
                
                final_output = full_result

    except Exception as e:
        final_output = {"error": f"SQL Agent Internal Error: {e}"}

    return final_output

def agent_rag(user_query: str) -> Dict[str, Any]:
    """
    SPECIALIST B: RAG Agent (Document Search).
    Searches knowledge base and returns fetched chunks/summary.
    """
    print(f"\n  [Agent B: RAG] Processing Request: '{user_query}'")
    
    # 1. Define Tool for this specific Agent's LLM session
    search_func = FunctionDeclaration(
        name="search_knowledge_base",
        description="Search the document knowledge base for contextual information and facts.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    rag_tool = Tool(function_declarations=[search_func])

    # 2. Agent Prompt
    system_prompt = """
    You are a Knowledge Librarian. 
    1. Search the knowledge base for the user's query using `search_knowledge_base`.
    2. Answer strictly based on the search results. Return the final, concise answer only.
    """
    
    # 3. Independent Model Session
    model = GenerativeModel("gemini-2.5-flash", system_instruction=system_prompt, tools=[rag_tool])
    chat = model.start_chat()
    
    response = chat.send_message(user_query)
    
    final_answer = {"text": "RAG Agent found no information."}

    try:
        # Check for tool call from the Agent's model
        if response.candidates[0].content.parts[0].function_call:
            fn = response.candidates[0].content.parts[0].function_call
            if fn.name == "search_knowledge_base":
                q = fn.args["query"]
                print(f"    [Agent B: RAG] Search Query: {q}")
                
                # Execute the RAG search
                search_result = execute_rag_search(q)
                
                # Feed back to the Agent to generate the final text answer
                response = chat.send_message(
                    Part.from_function_response(name="search_knowledge_base", response={"content": search_result})
                )
                final_answer = {"text": response.text, "search_summary": search_result}
                
    except Exception as e:
        final_answer = {"error": f"RAG Agent Internal Error: {e}"}
        
    return final_answer

def agent_mapping(user_query: str) -> Dict[str, Any]:
    """
    SPECIALIST C: Mapping Agent (Forced Geospatial SQL Generation).
    Generates map-specific SQL and returns the data for plotting (no plotting here).
    """
    print(f"\n  [Agent C: Mapping] Processing Request: '{user_query}'")
    
    # Get context from the SQL Agent (if available)
    context = _CONTEXT_STORE.get("last_sql_context", {})
    context_str = json.dumps(context)

    # 1. Define Tool for this specific Agent's LLM session
    sql_func = FunctionDeclaration(
        name="run_map_sql",
        description="Executes SQL. Must return the 'hex_id' (H3 index) and a 'value' column.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    map_tool = Tool(function_declarations=[sql_func])

    # 2. Prompt (Focuses the model on its task using context)
    system_prompt = f"""
    You are a Geospatial Visualization Expert.
    Your goal is to generate a SQL query to fetch data for mapping.
    Previous Data Context (if available): {context_str}
    
    1. Analyze the user query and the context to generate a NEW SQL query.
    2. The query MUST include a column named 'hex_id' (H3 Index at level 6) and a numeric column named 'value'.
    3. Execute the query using `run_map_sql`.
    4. Return the exact JSON output from the `run_map_sql` tool call, including the generated SQL query and the fetched data. Do NOT summarize or chat.
    """

    # 3. Independent Model Session
    model = GenerativeModel("gemini-2.5-flash", system_instruction=system_prompt, tools=[map_tool])
    chat = model.start_chat()
    
    response = chat.send_message(user_query)
    
    final_output = {"error": "Mapping Agent could not process request."}

    try:
        # Check for tool call from the Agent's model
        if response.candidates[0].content.parts[0].function_call:
            fn = response.candidates[0].content.parts[0].function_call
            if fn.name == "run_map_sql":
                sql_q = fn.args["query"]
                print(f"    [Agent C: Mapping] Generated SQL: {sql_q}")
                
                # Execute the map-specific SQL
                data_result = execute_bigquery_request(sql_q)
                
                # Add metadata before returning
                full_result = {
                    "request": user_query,
                    "context_used": "SQL context summary...", # In a real app, summarize context
                    "generated_map_sql": sql_q,
                    "map_data_result": data_result
                }

                # Feed back to Agent's model to close the loop
                chat.send_message(
                    Part.from_function_response(name="run_map_sql", response={"content": data_result})
                )
                
                final_output = full_result
                
    except Exception as e:
        final_output = {"error": f"Mapping Agent Internal Error: {e}"}

    return final_output

# --- THE ORCHESTRATOR (The Brain) ---

def orchestrator_dispatch(user_prompt: str) -> str:
    """
    The main model session. Decides which agent to call and summarizes the result.
    """
    print("\n--- Orchestrator Start ---")
    
    # Define Tools (These call the Python functions above, delegating to the agents)
    tools = Tool(function_declarations=[
        FunctionDeclaration(
            name="call_sql_agent",
            description="Delegate to Data Analyst for structured data, counts, or table queries (e.g., 'What is the average risk score for Texas?').",
            parameters={"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}
        ),
        FunctionDeclaration(
            name="call_rag_agent",
            description="Delegate to Researcher for text documents, policies, or general knowledge (e.g., 'What is the official policy on wildfires?').",
            parameters={"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}
        ),
        FunctionDeclaration(
            name="call_map_agent",
            description="Delegate to Cartographer to visualize data on a map (e.g., 'Show me the population density in California').",
            parameters={"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}
        )
    ])

    model = GenerativeModel(
        "gemini-2.5-flash",
        system_instruction="You are the Orchestrator. Analyze the user request and call the appropriate Specialist Agent once. Then, synthesize the agent's output into a natural, conversational response for the user.",
        tools=[tools]
    )
    
    chat = model.start_chat()
    response = chat.send_message(user_prompt)
    
    final_response = "I'm not sure how to handle that request. Please try asking a question about data, documents, or maps."
    
    try:
        # Orchestrator Tool Call Check
        if response.candidates[0].content.parts[0].function_call:
            fn = response.candidates[0].content.parts[0].function_call
            func_name = fn.name
            arg_request = fn.args["request"]
            
            print(f"\n  [Orchestrator] Delegating to: {func_name} with query: '{arg_request}'")
            
            # Call the appropriate Python agent function
            agent_output = {}
            if func_name == "call_sql_agent":
                agent_output = agent_text_to_sql(arg_request)
            elif func_name == "call_rag_agent":
                agent_output = agent_rag(arg_request)
            elif func_name == "call_map_agent":
                agent_output = agent_mapping(arg_request)
            
            print(f"\n  [Orchestrator] Received result from {func_name}. Synthesizing response...")
            
            # Send Agent Result (the raw structured data) back to Orchestrator to synthesize
            response = chat.send_message(
                Part.from_function_response(name=func_name, response={"agent_output": agent_output})
            )
            final_response = response.text
        else:
            # If the orchestrator didn't call a tool, it answers directly
            final_response = response.text
            
    except Exception as e:
        final_response = f"❌ Orchestrator Error during tool execution: {e}"

    print("--- Orchestrator End ---")
    return final_response

# --- MAIN EXECUTION LOOP ---
def run_cli_chat():
    print("=====================================================================")
    print("--- Resilitix Multi-Agent CLI Tester Initialized ---")
    print("Agents: SQL, RAG, Mapping (No plotting, returns JSON data for test)")
    print("Type 'exit' to quit.\n")
    print("Example Queries:")
    print("1. Find the top 5 most populated counties in California.")
    print("2. What are the key points in the Wildfire Mitigation Strategy document?")
    print("3. Show me a map of hospital locations in Texas.")
    print("=====================================================================")

    while True:
        try:
            user_input = input("\nUser: ")
            if user_input.lower() in ["exit", "quit"]: break

            # Clear context store for a fresh turn
            _CONTEXT_STORE["last_sql_context"] = None

            # Orchestrator handles the request
            final_answer = orchestrator_dispatch(user_input)

            # Final Answer
            print(f"\n🤖 Final Answer: {final_answer}")

        except Exception as e:
            print(f"❌ Critical Error in CLI Loop: {e}")

if __name__ == "__main__":
    run_cli_chat()