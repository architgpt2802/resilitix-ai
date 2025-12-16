import streamlit as st
import vertexai
from vertexai.generative_models import GenerativeModel, Tool, Part, Content, ChatSession, FunctionDeclaration
import requests
import json
import google.auth.transport.requests
import google.oauth2.id_token
import os
from google.cloud import discoveryengine_v1 as discoveryengine
from keplergl import KeplerGl
import streamlit.components.v1 as components
import pandas as pd

# --- CONFIGURATION ---
PROJECT_ID = "resiliencegenomeai"
LOCATION = "us-central1"
# Your Cloud Run Tool URL (for SQL execution)
TOOL_URL = "https://resilitix-sql-tool-525917099044.us-central1.run.app"

# RAG CONFIGURATION (UPDATED WITH YOUR ID)
RAG_DATA_STORE_ID = "resilitix-rag-data_1765252053186" 

# Page Config
st.set_page_config(page_title="Resilitix AI", page_icon="⚡", layout="wide")

# Initialize Vertex AI
if "vertex_init" not in st.session_state:
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    st.session_state.vertex_init = True

if "map_data" not in st.session_state:
    st.session_state.map_data = None
if "map_config" not in st.session_state:
    # st.session_state.map_config = {}
    st.session_state.map_config = {
    'uiState': {
        'activeSidePanel': None,
        'readOnly': True
    }
}

# --- HELPER: GET AUTH TOKEN ---
def get_id_token(url):
    """
    Generates a Google ID Token to authenticate with the Cloud Run tool.
    """
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, url)

# --- HELPER: LOAD CONFIGURATION ---
def load_config():
    """
    Reads instructions.md and examples.json to build the system prompt.
    """
    # 1. Load Rules from instructions.md
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config", "instructions.md")
        with open(config_path, "r") as f:
            base_instructions = f.read()
    except FileNotFoundError:
        base_instructions = """
        Role: You are an expert Data Analyst and Knowledge Assistant for Resilitix. 
        Your goal is to answer user questions using the appropriate tool.
        """

    # 2. Load Examples from examples.json (SQL examples still useful for context)
    try:
        examples_path = os.path.join(os.path.dirname(__file__), "config", "examples.json")
        with open(examples_path, "r") as f:
            examples_data = json.load(f)
            
        examples_text = "\n\n### SQL Few-Shot Examples:\n"
        for ex in examples_data:
            examples_text += f"User: {ex['question']}\nSQL: {ex['sql']}\n\n"
            
    except FileNotFoundError:
        examples_text = ""

    # 3. Add Orchestration and RAG-Specific Instructions
    orchestration_instruction = """
    Tool Usage Rules:
    1. For questions requiring quantitative data, statistics, or metrics: Use the `query_bigquery` tool.
    2. For questions requiring information from documents, reports, or the knowledge base:** Use the `search_knowledge_base` tool.
    3. For geospatial visualization, use `plot_kepler_map`. The SQL MUST return columns 'hex_id' (string), 'County', 'State', 'Zipcode' columns and optional 'value' column (numeric, optional):** Use the `plot_kepler_map` tool.
    4. Important: While plotting the map, always return hex ids at level 6 (hex_id_l6) unless asked otherwise.
    5. You should first think the plan through regarding which tool are you going to call first, and then downstream tool calls.
    For example: If asked to plot states which were affected by a specific disaster, you can choose to go to knowledgebase to look for the states or answer from you own knowledge, and then go to ploting tool to plot those regions.
    """
    
    return base_instructions + orchestration_instruction + examples_text

# --- TOOL 1: BIGQUERY (Text-to-SQL) ---
def query_bigquery(query: str) -> dict:
    """
    Executes a Standard SQL query against the Resilitix BigQuery dataset.
    """
    print(f"DEBUG: Tool (BigQuery) called with: {query}")
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

# --- TOOL 2: RAG (Document Search) ---
def search_knowledge_base(query: str) -> dict:
    """
    Searches the internal knowledge base and extracts the summary text directly from the API response.
    """
    print(f"DEBUG: Tool (RAG) called with: {query}")
    
    try:
        # FIX: Explicitly set the client options to ensure the correct global endpoint is targeted
        client_options = {"api_endpoint": "discoveryengine.googleapis.com"}
        client = discoveryengine.SearchServiceClient(client_options=client_options)

        serving_config = client.serving_config_path(
            project=PROJECT_ID,
            location="global", 
            data_store=RAG_DATA_STORE_ID,
            serving_config="default_search",
        )
        
        # Configure Search Request to generate a summary
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
        
        # Extract the confirmed working summary field
        summary_text = ""
        if (response.summary and 
            response.summary.summary_with_metadata and 
            response.summary.summary_with_metadata.summary):
            
            summary_text = response.summary.summary_with_metadata.summary
            
        if not summary_text:
            return {"found": False, "message": "Search successful, but no relevant summary text could be generated from documents."}
            
        # Return the summary text as a document for the model to use
        return {"found": True, "documents": [{"summary_text": summary_text}]}

    except Exception as e:
        # Return the error message directly to the model so it can be relayed to the user
        print(f"RAG Search Exception Detail: {e}")
        return {"error": f"RAG Search Exception: {str(e)}"}

def plot_kepler_map(query: str) -> dict:
    """
    Queries BigQuery to retrieve geospatial data (hexID and value) 
    and returns the data as a list of dictionaries, suitable for KeplerGL.
    """
    print(f"\n[DEBUG] Generating SQL for KeplerGL data: {query}")
    
    bigquery_result = query_bigquery(query)

    if "error" in bigquery_result:
        return {"error": f"Error retrieving data for map: {bigquery_result['error']}"}

    raw_data = bigquery_result.get("data", [])
    
    if not raw_data:
        return {"error": "Query executed successfully but returned 0 records."}

    # 2. Process Data for Kepler
    try:
        # Convert list of dicts to Pandas DataFrame
        df = pd.DataFrame(raw_data)
        
        # Validation: Ensure hex_id exists for mapping
        if 'hex_id' not in df.columns:
             # Try to find a column that looks like a hex_id if named differently
            return {"error": "The SQL result must contain a column named 'hex_id' for the map to render."}

        # 3. Update Session State
        # This stores the dataframe so the UI column can render it on the next run
        st.session_state.map_data = df
        
        # 4. Return Success Message to LLM
        return {
            "status": "success",
            "rows_retrieved": len(df),
            "message": "Data successfully loaded into the dashboard map. Tell the user the map has been updated."
        }
        
    except Exception as e:
        return {"error": f"Data processing error: {str(e)}"}

# --- 2. INITIALIZE SESSION STATE & TOOLS ---
if "chat_session" not in st.session_state:
    
    # --- TOOL 1: SQL Definition ---
    sql_func = FunctionDeclaration(
        name="query_bigquery",
        description="Executes a Standard SQL query against the Resilitix BigQuery dataset. Use this for data analysis and metric questions.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The Standard SQL query to execute. Must begin with SELECT."
                }
            },
            "required": ["query"]
        }
    )
    
    # --- TOOL 2: RAG Definition ---
    rag_func = FunctionDeclaration(
        name="search_knowledge_base",
        description="Searches the internal document knowledge base for contextual information and facts. Use this for document-based questions.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to find relevant document context."
                }
            },
            "required": ["query"]
        }
    )

    # --- TOOL 3: PLOT Definition ---
    plot_func = FunctionDeclaration(
        name="plot_kepler_map",
        description="Retrieves geospatial data for plotting on a Kepler GL map. The LLM must ensure the SQL output contains a 'hex_id' column and a numeric 'value' column.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The Standard SQL query to execute. Must begin with SELECT."
                }
            },
            "required": ["query"]
        }
    )
    
    combined_tools = Tool(
        function_declarations=[sql_func, rag_func, plot_func]
    )
    
    full_system_instruction = load_config()

    model = GenerativeModel(
        "gemini-2.5-pro",
        system_instruction=full_system_instruction, 
        tools=[combined_tools],
    )
    st.session_state.chat_session = model.start_chat()
    st.session_state.messages = []

# --- LAYOUT DEFINITION ---
col1, col2, col3 = st.columns([3, 0.5, 6.5])

# --- RIGHT COLUMN: MAP RENDERER ---
with col3:
    try:
        if st.session_state.map_data is not None:
            map_ = KeplerGl(height=750, data={"resilience_layer": st.session_state.map_data})
        else:
            # Empty Default Map
            map_ = KeplerGl()
            
        html_map = map_._repr_html_(center_map=True)
        components.html(html_map, height=750)
    except Exception as e:
        st.error(f"Error rendering map: {e}")


# --- LEFT COLUMN: CHAT INTERFACE ---
with col1:
    st.title("EmergenCITY AI")
    # Display History
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    # Input Handling
    if prompt := st.chat_input():
        
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    # Send to Vertex AI
                    response = st.session_state.chat_session.send_message(prompt)
                    
                    should_rerun = False # Flag to trigger UI update

                    # --- TOOL ORCHESTRATOR LOOP ---
                    # We loop to handle potential multiple tool calls or chained reasoning
                    while response.candidates[0].content.parts[0].function_call:
                        
                        part = response.candidates[0].content.parts[0]
                        func_name = part.function_call.name
                        args = part.function_call.args
                        
                        tool_result = None

                        if func_name == "query_bigquery":
                            sql_q = args.get("query", "")
                            st.caption(f"🛠️ SQL: `{sql_q}`") 
                            tool_result = query_bigquery(sql_q)
                        
                        elif func_name == "search_knowledge_base":
                            rag_q = args.get("query", "")
                            st.caption(f"📚 RAG: `{rag_q}`")
                            tool_result = search_knowledge_base(rag_q)
                        
                        elif func_name == "plot_kepler_map":
                            plot_q = args.get("query", "")
                            st.caption(f"🗺️ Plotting: `{plot_q}`")
                            tool_result = plot_kepler_map(plot_q)
                            
                            # CRITICAL: If plotting succeeded, we must rerun to update the Right Column
                            if "status" in tool_result and tool_result["status"] == "success":
                                should_rerun = True

                        else:
                            tool_result = {"error": f"Unknown function: {func_name}"}

                        # Send Tool Response back to Gemini
                        response = st.session_state.chat_session.send_message(
                            Part.from_function_response(
                                name=func_name,
                                response={"content": tool_result}
                            )
                        )

                    # Display Final Text
                    final_text = response.text
                    st.markdown(final_text)
                    st.session_state.messages.append({"role": "assistant", "content": final_text})

                    # Trigger the map update if needed
                    if should_rerun:
                        st.rerun()
                    
                except Exception as e:
                    st.error(f"Error: {e}")