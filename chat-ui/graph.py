import vertexai
from vertexai.generative_models import GenerativeModel, Tool, Part, Content, ChatSession, FunctionDeclaration
import requests
import json
import google.auth.transport.requests
import google.oauth2.id_token
import os
import traceback
from pydantic import BaseModel
from google.cloud import discoveryengine_v1 as discoveryengine
from typing import Dict, Any, Literal, TypedDict, Annotated, Sequence, Optional, Union
from langchain_core.runnables import Runnable
from langgraph.types import Command
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, AIMessageChunk, SystemMessage
from langchain.agents import create_agent
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

# Config

PROJECT_ID = "resiliencegenomeai"
LOCATION = "us-central1"
TOOL_URL = "https://resilitix-sql-tool-525917099044.us-central1.run.app"
RAG_DATA_STORE_ID = "resilitix-rag-data_1765252053186" 

vertexai.init(project=PROJECT_ID, location=LOCATION)

# Helper methods

def get_id_token(url: str) -> str:
    """Generates a Google ID Token to authenticate with the Cloud Run tool."""
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, url)

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
    for _ in range(10):
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

                # Send result back to Model (This allows the loop to continue or finish)
                response = chat.send_message(
                    Part.from_function_response(name="run_sql", response={"content": data_result})
                )

        except Exception as e:
            traceback.print_exc()
            final_output = {"error": f"SQL Agent Internal Error: {e}"}
            break

    print(f"    [Agent A: SQL] Final Output: {final_output}")

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
                final_answer = {"text": response.text, "search_summary": search_result["summary"]}
                
    except Exception as e:
        final_answer = {"error": f"RAG Agent Internal Error: {e}"}

    print(f"    [Agent B: RAG] Final Answer: {final_answer}")
        
    return final_answer

def agent_mapping(user_query: str, previous_query: str) -> Dict[str, Any]:
    """
    SPECIALIST C: Mapping Agent.
    """
    print(f"\n  [Agent C: Mapping] Processing Request: '{user_query}'")
    
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
    
    Reference SQL Query: {previous_query}
    
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
    
    print(f"    [Agent C: Mapping] Final Output: {final_output}")

    return final_output

class Result(BaseModel):
    name: str
    query: str
    output:Union[str, Dict[str, Any]]

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    task: Literal[str]
    results: Optional[list[Result]]

def sql_agent(state: AgentState):
    """SQL agent for the task"""
    print("="*10, " Inside SQL Agent ", "="*10)

    user_query = state["task"] or state["messages"][-1].content
    
    output = agent_text_to_sql(user_query)

    if "error" in output:
        return {"messages": [AIMessage(content=f"Error: {output['error']}")]}

    sql_query = output["generated_sql"]
    sql_output = output["execution_result"]

    sql_result = Result(name = "sql_agent", query=sql_query, output=sql_output)
    
    return {
        "messages": [AIMessage(content=f"SQL Query: {sql_query}\nSQL Output:\n{sql_output}")],
        "results": [sql_result],
    }
    
def rag_agent(state: AgentState):
    """RAG agent for the task"""
    print("="*10, " Inside RAG Agent ", "="*10)

    user_query = state["task"] or state["messages"][-1].content
    rag_output = agent_rag(user_query)["text"]

    rag_result = Result(name = "rag_agent", query=user_query, output=rag_output)

    return {
        "messages": [AIMessage(content=f"RAG Query: {user_query}\nRAG Output:\n{rag_output}")],
        "results": [rag_result],
    }

def is_plot_required(state: AgentState) -> bool:
    """
    Decide whether the user query should trigger the Mapping / Plot Agent.
    Returns True if the query implies a geospatial visualization.
    """
    print("=" * 10, " Inside Is Plot Required ", "=" * 10)

    user_query = state.get("task") or state["messages"][-1].content
    q = user_query.lower()

    MAP_KEYWORDS = {
        "map", "heatmap", "geo", "geospatial", "spatial",
        "location", "locations", "region", "regions",
        "area", "areas", "zone", "zones",
        "city", "state", "country", "county",
        "latitude", "longitude", "lat", "lon",
        "h3", "hex", "hexagon", "grid",
        "distribution", "density", "hotspot"
    }

    PLOT_VERBS = {
        "show", "plot", "visualize", "display", "see",
        "compare", "highlight"
    }

    has_map_keyword = any(k in q for k in MAP_KEYWORDS)
    has_plot_verb = any(v in q for v in PLOT_VERBS)

    if has_map_keyword:
        print("Plot required: detected geospatial keyword")
        return "plot_agent"

    if has_plot_verb and any(r in q for r in ["by city", "by region", "by area", "by location", "by hex"]):
        print("Plot required: visualization + regional grouping")
        return "plot_agent"

    return "summarize_agent"

def plot_agent(state: AgentState):
    """Plot agent for the task"""
    print("="*10, " Inside Plot Agent ", "="*10)

    user_query = state["task"] or state["messages"][-1].content
    previous_query = state["results"][0].query

    mapping_output = agent_mapping(user_query, previous_query)

    plot_query = mapping_output["generated_map_sql"]
    plot_output = mapping_output["map_data_result"]

    plot_result = Result(name = "plot_agent", query=plot_query, output=plot_output)

    return {
        "results": [plot_result],
    }

def summarize_agent(state: AgentState):
    """Summarize agent for the task using a chat model"""
    print("=" * 10, " Inside Summarize Agent ", "=" * 10)

    user_query = state.get("task") or state["messages"][-1].content
    results = state.get("results", [])

    sql_context = "No SQL results available."
    rag_context = "No RAG context available."

    # Extract agent outputs
    for r in results:
        if r.name == "sql_agent":
            sql_context = json.dumps({
                "query": r.query,
                "output": r.output
            }, indent=2)

        elif r.name == "rag_agent":
            rag_context = json.dumps({
                "query": r.query,
                "output": r.output
            }, indent=2)

    system_prompt = """
    You are a information summarizer preparing a concise research report.

    STRICT RULES:
    - Summarize ONLY the information provided.
    - Do NOT invent metrics, trends, or conclusions.
    - If information is missing or inconclusive, say so explicitly.
    - Clearly separate data-driven findings from contextual references.
    - Be concise, factual, and professional.
    """

    user_prompt = f"""
    User Question:
    {user_query}

    SQL Agent Results:
    {sql_context}

    RAG Agent Results:
    {rag_context}

    Task:
    Produce a short, clear summary report answering the user's question
    using ONLY the information above.
    """

    model = GenerativeModel(
        "gemini-2.5-flash",
        system_instruction=system_prompt
    )

    response = model.generate_content(user_prompt)

    ai_message = AIMessage(content=response.text)

    return {
        "messages": [ai_message],
    }

graph = StateGraph(AgentState)

graph.add_node("sql_agent", sql_agent)
graph.add_node("rag_agent", rag_agent)
graph.add_node("plot_agent", plot_agent)
graph.add_node("summarize_agent", summarize_agent)

graph.set_entry_point("sql_agent")

graph.add_edge("sql_agent", "rag_agent")

graph.add_conditional_edges(
    "rag_agent",
    is_plot_required,
    {
        "plot_agent": "plot_agent",
        "summarize_agent": "summarize_agent",
    },
)

graph.add_edge("plot_agent", "summarize_agent")

graph.add_edge("summarize_agent", END)

app = graph.compile()


import json
from langchain_core.messages import HumanMessage

def main(test_query):

    # 2. Initialize the state
    # LangGraph expects the initial state as a dictionary
    initial_input = {
        "task": test_query,
        "messages": [HumanMessage(content=test_query)],
        "results": []
    }

    print(f"\n{'='*20} STARTING MULTI-AGENT RUN {'='*20}")
    print(f"User Query: {test_query}\n")

    # 3. Stream the graph execution
    # This allows you to see the output of each node as it completes
    
    result = app.invoke(initial_input)
    print("="*50)
    print("Final Result: ", result)
    
if __name__ == "__main__":
    # Ensure you have your environment variables set (PROJECT_ID, TOOL_URL, etc.)
    # 1. Define the test query 
    # This query should trigger SQL, RAG, and the Plot Agent
    test_query = "show me the hospitals in the high flood risk areas in brazos county"
    main(test_query)