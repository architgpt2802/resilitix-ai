import streamlit as st
import vertexai
from vertexai.generative_models import GenerativeModel, Tool, Part, Content, ChatSession, FunctionDeclaration
import requests
import json
import google.auth.transport.requests
import google.oauth2.id_token
import os

# --- CONFIGURATION ---
PROJECT_ID = "resiliencegenomeai"
LOCATION = "us-central1"
# Your Cloud Run Tool URL
TOOL_URL = "https://resilitix-sql-tool-525917099044.us-central1.run.app"

# Page Config
st.set_page_config(page_title="Resilitix AI", page_icon="⚡", layout="wide")
st.title("⚡ Resilitix Data Assistant")

# Initialize Vertex AI
if "vertex_init" not in st.session_state:
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    st.session_state.vertex_init = True

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
        # We look in the local config folder
        config_path = os.path.join(os.path.dirname(__file__), "config", "instructions.md")
        with open(config_path, "r") as f:
            base_instructions = f.read()
    except FileNotFoundError:
        # Fallback if file is missing
        base_instructions = """
        Role: You are an expert Data Analyst for Resilitix. 
        You answer user questions by querying the BigQuery dataset data_library.
        """

    # 2. Load Examples from examples.json
    try:
        examples_path = os.path.join(os.path.dirname(__file__), "config", "examples.json")
        with open(examples_path, "r") as f:
            examples_data = json.load(f)
            
        examples_text = "\n\n### Few-Shot Examples:\n"
        for ex in examples_data:
            examples_text += f"User: {ex['question']}\nSQL: {ex['sql']}\n\n"
            
    except FileNotFoundError:
        examples_text = ""

    # Combine them
    return base_instructions + examples_text

# --- 1. DEFINE THE TOOL FUNCTION ---
def query_bigquery(query: str) -> dict:
    """
    Executes a Standard SQL query against the Resilitix BigQuery dataset.
    """
    print(f"DEBUG: Tool called with: {query}")
    payload = json.dumps({"query": query})
    
    try:
        # 1. Get Token
        try:
            token = get_id_token(TOOL_URL)
        except Exception as e:
            return {"error": f"Token Generation Failed: {str(e)}"}

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}'
        }
        
        # 2. Send Request
        response = requests.post(TOOL_URL, data=payload, headers=headers)
        
        # 3. DEBUGGING: Check for non-200 status immediately
        if response.status_code != 200:
            # Return the RAW TEXT so we can see the HTML error message
            return {"error": f"HTTP {response.status_code} Error. Raw response: {response.text}"}
            
        # 4. Parse JSON
        try:
            return response.json()
        except json.JSONDecodeError:
            # If it's 200 OK but not JSON, show what it is
            return {"error": f"Invalid JSON received. Raw content: {response.text}"}

    except Exception as e:
        return {"error": f"Connection Exception: {str(e)}"}

# --- 2. INITIALIZE SESSION STATE ---
if "chat_session" not in st.session_state:
    
    # --- BULLETPROOF TOOL DEFINITION ---
    sql_func = FunctionDeclaration(
        name="query_bigquery",
        description="Executes a Standard SQL query against the Resilitix BigQuery dataset.",
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
    
    bq_tool = Tool(
        function_declarations=[sql_func]
    )
    
    # --- NEW: LOAD CONFIGURATION ---
    full_system_instruction = load_config()

    # Initialize the Model (Gemini 2.5 Flash)
    model = GenerativeModel(
        "gemini-2.5-flash",
        system_instruction=full_system_instruction, # <--- Updated to use loaded config
        tools=[bq_tool],
    )
    st.session_state.chat_session = model.start_chat()
    st.session_state.messages = []

# --- 3. CHAT INTERFACE ---

# Display Chat History
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Input Handling
if prompt := st.chat_input("Ask about wildfire risk, population, or tables..."):
    # 1. Display User Message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 2. Generate Response (The Loop)
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                # Send message to model
                response = st.session_state.chat_session.send_message(prompt)
                
                # --- MANUAL TOOL LOOP ---
                for _ in range(5):
                    try:
                        part = response.candidates[0].content.parts[0]
                    except:
                        break

                    if not part.function_call:
                        break # It's text, we are done

                    # It IS a function call
                    func_name = part.function_call.name
                    args = part.function_call.args
                    
                    if func_name == "query_bigquery":
                        sql_q = args.get("query", "")
                        st.caption(f"🛠️ Running SQL: `{sql_q}`") 
                        
                        # Execute Tool (Now Authenticated!)
                        tool_result = query_bigquery(sql_q)
                        
                        # Send Result back to Model
                        response = st.session_state.chat_session.send_message(
                            Part.from_function_response(
                                name="query_bigquery",
                                response={"content": tool_result}
                            )
                        )
                
                # Display Final Text
                final_text = response.text
                st.markdown(final_text)
                st.session_state.messages.append({"role": "assistant", "content": final_text})
                
            except Exception as e:
                st.error(f"Error: {e}")
