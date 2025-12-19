# Separate Gemini Model for each Tool

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
TOOL_URL = "https://resilitix-sql-tool-525917099044.us-central1.run.app"
RAG_DATA_STORE_ID = "resilitix-rag-data_1765252053186" 

# Initialize Vertex AI
vertexai.init(project=PROJECT_ID, location=LOCATION)

# --- HELPER: GET AUTH TOKEN ---
def get_id_token(url: str) -> str:
    """Generates a Google ID Token to authenticate with the Cloud Run tool."""
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, url)

# --- HELPER: LOAD CONFIGURATION ---
def load_config():
    """Reads instructions.md and examples.json to build the context string."""
    # 1. Load Rules
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config", "instructions.md")
        with open(config_path, "r") as f:
            base_instructions = f.read()
    except FileNotFoundError:
        base_instructions = "You are a helpful data assistant."

    # 2. Load Examples
    try:
        examples_path = os.path.join(os.path.dirname(__file__), "config", "examples.json")
        with open(examples_path, "r") as f:
            examples_data = json.load(f)
            examples_text = "\n\n### SQL Few-Shot Examples:\n"
            for ex in examples_data:
                examples_text += f"User: {ex['question']}\nSQL: {ex['sql']}\n\n"
    except FileNotFoundError:
        examples_text = ""
    
    return base_instructions + "\n" + examples_text

# --- LOW-LEVEL HELPERS ---
def execute_bigquery_request(query: str) -> Dict[str, Any]:
    """Raw helper to hit the Cloud Run SQL Tool and get data."""
    print(f"    [Execution] Sending SQL to Cloud Run: {query[:80]}...")
    payload = json.dumps({"query": query})
    try:
        token = get_id_token(TOOL_URL)
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
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
    """Raw helper to hit Vertex AI Search."""
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
             
        return {"summary": summary, "found": bool(summary)}
    except Exception as e:
        return {"error": str(e)}

# --- AGENT SPECIALISTS ---

def agent_text_to_sql(user_query: str) -> Dict[str, Any]:
    """
    SPECIALIST A: Data Analyst Agent (Text-to-SQL).
    """
    print(f"\n  [Agent A: SQL] Processing Request: '{user_query}'")
    
    sql_func = FunctionDeclaration(
        name="run_sql",
        description="Executes a Standard SQL query on the BigQuery dataset.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    sql_tool = Tool(function_declarations=[sql_func])

    # Load shared config (Schema/Examples)
    shared_config = load_config()

    system_prompt = f"""
    You are a SQL Expert for the Resilitix BigQuery data.
    
    {shared_config}
    
    1. Convert the user's request into a single Standard SQL query.
    2. Use the `run_sql` tool to execute it.
    3. If the query fails, analyze the error and try again.
    4. Return the exact JSON output from the `run_sql` tool call.
    """

    model = GenerativeModel("gemini-2.5-flash", system_instruction=system_prompt, tools=[sql_tool])
    chat = model.start_chat()
    
    response = chat.send_message(user_query)
    
    final_output = {"error": "SQL Agent could not process request."}
    
    # --- RETRY LOOP FOR SQL AGENT ---
    # We loop up to 5 times to allow the model to fix bad SQL or refine results
    for _ in range(5):
        try:
            # Check if we have a response content
            if not response.candidates:
                break

            part = response.candidates[0].content.parts[0]

            # If it's just text (no tool call), we assume the agent is done or asking for clarification
            if not part.function_call:
                break

            # Handle Tool Call
            fn = part.function_call
            if fn.name == "run_sql":
                sql_q = fn.args["query"]
                print(f"    [Agent A: SQL] Generated SQL: {sql_q}")
                
                # Execute SQL
                data_result = execute_bigquery_request(sql_q)
                
                # Prepare Result
                full_result = {
                    "request": user_query,
                    "generated_sql": sql_q,
                    "execution_result": data_result
                }
                
                # Store context globally for the Map Agent to use later
                _CONTEXT_STORE["last_sql_context"] = full_result
                final_output = full_result

                # Send result back to Model (This allows the loop to continue or finish)
                response = chat.send_message(
                    Part.from_function_response(name="run_sql", response={"content": data_result})
                )

        except Exception as e:
            final_output = {"error": f"SQL Agent Internal Error: {e}"}
            break

    return final_output

def agent_rag(user_query: str) -> Dict[str, Any]:
    """
    SPECIALIST B: RAG Agent (Document Search).
    """
    print(f"\n  [Agent B: RAG] Processing Request: '{user_query}'")
    
    search_func = FunctionDeclaration(
        name="search_knowledge_base",
        description="Search the document knowledge base for contextual information and facts.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    rag_tool = Tool(function_declarations=[search_func])

    system_prompt = """
    You are a Knowledge Librarian. 
    1. Search the knowledge base for the user's query using `search_knowledge_base`.
    2. Answer strictly based on the search results. Return the final, concise answer only.
    """
    
    model = GenerativeModel("gemini-2.5-flash", system_instruction=system_prompt, tools=[rag_tool])
    chat = model.start_chat()
    
    response = chat.send_message(user_query)
    final_answer = {"text": "RAG Agent found no information."}

    try:
        # Simple single-turn loop for RAG usually suffices
        while response.candidates[0].content.parts[0].function_call:
            fn = response.candidates[0].content.parts[0].function_call
            if fn.name == "search_knowledge_base":
                q = fn.args["query"]
                print(f"    [Agent B: RAG] Search Query: {q}")
                
                search_result = execute_rag_search(q)
                
                response = chat.send_message(
                    Part.from_function_response(name="search_knowledge_base", response={"content": search_result})
                )
                final_answer = {"text": response.text, "search_summary": search_result}
                
    except Exception as e:
        final_answer = {"error": f"RAG Agent Internal Error: {e}"}
        
    return final_answer

def agent_mapping(user_query: str) -> Dict[str, Any]:
    """
    SPECIALIST C: Mapping Agent.
    """
    print(f"\n  [Agent C: Mapping] Processing Request: '{user_query}'")
    
    context = _CONTEXT_STORE.get("last_sql_context", {})
    context_str = json.dumps(context)
    
    # Load shared config so map agent knows table schemas too
    shared_config = load_config()

    sql_func = FunctionDeclaration(
        name="run_map_sql",
        description="Executes SQL. Must return the 'hex_id' (H3 index) and a 'value' column.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    map_tool = Tool(function_declarations=[sql_func])

    system_prompt = f"""
    You are a Geospatial Visualization Expert.
    
    {shared_config}
    
    Previous Data Context: {context_str}
    
    1. Analyze the user query and the context to generate a NEW SQL query.
    2. The query MUST include a column named 'hex_id' from the hex_county_state_zip_crosswalk table with column name 'hex_id_l6'(H3 Index at level 6) and a numeric column named 'value'.
    3. Execute the query using `run_map_sql`.
    4. Return the exact JSON output. Do NOT summarize or chat.
    """

    model = GenerativeModel("gemini-2.5-flash", system_instruction=system_prompt, tools=[map_tool])
    chat = model.start_chat()
    
    response = chat.send_message(user_query)
    final_output = {"error": "Mapping Agent could not process request."}

    # Retry loop for Mapping Agent as well (in case SQL fails)
    for _ in range(5):
        try:
            if not response.candidates: break
            part = response.candidates[0].content.parts[0]
            if not part.function_call: break

            fn = part.function_call
            if fn.name == "run_map_sql":
                sql_q = fn.args["query"]
                print(f"    [Agent C: Mapping] Generated SQL: {sql_q}")
                
                data_result = execute_bigquery_request(sql_q)
                
                full_result = {
                    "request": user_query,
                    "context_used": "SQL context summary...",
                    "generated_map_sql": sql_q,
                    "map_data_result": data_result
                }

                response = chat.send_message(
                    Part.from_function_response(name="run_map_sql", response={"content": data_result})
                )
                final_output = full_result
                
        except Exception as e:
            final_output = {"error": f"Mapping Agent Internal Error: {e}"}
            break

    return final_output

# --- THE ORCHESTRATOR ---

def orchestrator_dispatch(user_prompt: str) -> str:
    print("\n--- Orchestrator Start ---")
    
    tools = Tool(function_declarations=[
        FunctionDeclaration(
            name="call_sql_agent",
            description="Delegate to Data Analyst for structured data, counts, or table queries.",
            parameters={"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}
        ),
        FunctionDeclaration(
            name="call_rag_agent",
            description="Delegate to Researcher for text documents, policies, or general knowledge.",
            parameters={"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}
        ),
        FunctionDeclaration(
            name="call_map_agent",
            description="Delegate to Cartographer to visualize data on a map.",
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
    final_response = "I'm not sure how to handle that request."
    
    try:
        # Loop for Orchestrator to potentially call multiple agents if needed (though instruction says once)
        while response.candidates[0].content.parts[0].function_call:
            fn = response.candidates[0].content.parts[0].function_call
            func_name = fn.name
            arg_request = fn.args["request"]
            
            print(f"\n  [Orchestrator] Delegating to: {func_name} with query: '{arg_request}'")
            
            agent_output = {}
            if func_name == "call_sql_agent":
                agent_output = agent_text_to_sql(arg_request)
            elif func_name == "call_rag_agent":
                agent_output = agent_rag(arg_request)
            elif func_name == "call_map_agent":
                agent_output = agent_mapping(arg_request)
            
            print(f"\n  [Orchestrator] Received result. Synthesizing...")
            
            response = chat.send_message(
                Part.from_function_response(name=func_name, response={"agent_output": agent_output})
            )
            final_response = response.text
        else:
            final_response = response.text
            
    except Exception as e:
        final_response = f"❌ Orchestrator Error: {e}"

    print("--- Orchestrator End ---")
    return final_response

# --- MAIN EXECUTION LOOP ---
def run_cli_chat():
    print("=====================================================================")
    print("--- Resilitix Multi-Agent CLI Tester Initialized ---")
    print("Type 'exit' to quit.")
    print("=====================================================================")

    while True:
        try:
            user_input = input("\nUser: ")
            if user_input.lower() in ["exit", "quit"]: 
                print("\n Context Store: ", _CONTEXT_STORE["last_sql_context"])
                break

            _CONTEXT_STORE["last_sql_context"] = None
            final_answer = orchestrator_dispatch(user_input)
            print(f"\n🤖 Final Answer: {final_answer}")

        except Exception as e:
            print(f"❌ Critical Error: {e}")

if __name__ == "__main__":
    run_cli_chat()