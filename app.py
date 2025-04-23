# app.py - Implementing OpenAI Agents SDK Workflow (Vector Store Only)

import os
import json
import asyncio
import logging
import time
import uuid
import hashlib
# Import PDF handling utilities
from pymupdf_tools import extract_text_from_pdf
import traceback
import re
import markdown
import html
import ast
from dotenv import load_dotenv, find_dotenv
from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify, send_file
from werkzeug.utils import secure_filename
from openai import (
    OpenAI, APIConnectionError, AuthenticationError, RateLimitError,
    NotFoundError, BadRequestError, APIStatusError
)
from typing import Optional, List, Dict, Any, Union

# Import semantic cache for agent-driven query caching
from semantic_cache import semantic_search_cache

# --- Agent SDK Imports ---
from agents import Agent, Runner, Handoff, RunContextWrapper, function_tool, AgentOutputSchema
from agents.result import RunResult  # Import for extract_final_answer function
from agents.agent_output import AgentOutputSchema as AgentOutputSchemaClass  # Import for custom Runner
# --- CORRECTED & CONFIRMED Tracing Imports ---
from agents.tracing.processor_interface import TracingProcessor # Correct base class path
from agents.tracing.traces import Trace # Correct type hint path
from agents.tracing.spans import Span # Needed for type hints in processor methods
from agents.tracing import add_trace_processor # Correct registration function path
# --- END CORRECTED Imports ---

# --- Enhanced Intent Determination ---
from intent_determination import determine_final_intent, record_intent_determination

# --- DocumentAnalyzerAgent Integration ---
# Import the integration module (functions will be imported later)
import document_analyzer_integration

# Import tools from document_analyzer_agent
from document_analyzer_agent import detect_fields_from_template, analyze_document_for_workflow
from data_models import DocumentAnalysis

# --- Pydantic Models ---
from pydantic import BaseModel, Field, ConfigDict

# --- Load Configuration ---
load_dotenv(find_dotenv(), override=True)

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO if os.getenv('FLASK_DEBUG') != '1' else logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s:%(lineno)d: %(message)s'
)
logger = logging.getLogger(__name__)

# --- App Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY')
if not app.config['SECRET_KEY']:
    logger.warning("FLASK_SECRET_KEY not set. Using temporary key.")
    app.config['SECRET_KEY'] = os.urandom(24)

# --- Constants & Configurable Values ---
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
TEMPLATE_DIR = os.getenv('TEMPLATE_DIR', 'templates/document_templates')
DOCX_OUTPUT_DIR = os.getenv('DOCX_OUTPUT_DIR', 'docx_output')
# Ensure the path is absolute
if not os.path.isabs(DOCX_OUTPUT_DIR):
    DOCX_OUTPUT_DIR = os.path.abspath(DOCX_OUTPUT_DIR)
ALLOWED_EXTENSIONS = {'pdf'}
ALLOWED_TEMPLATE_EXTENSIONS = {'txt', 'md', 'pdf'}
DATABASE_FILE = os.getenv('DATABASE_FILE', 'chat_history.db')
COMPLETION_MODEL = os.getenv('COMPLETION_MODEL', 'gpt-4o-mini')
MAX_COMPLETION_TOKENS = int(os.getenv('MAX_COMPLETION_TOKENS', 8000))
COMPLETION_TEMPERATURE = float(os.getenv('COMPLETION_TEMPERATURE', 0.3))
MAX_SEARCH_RESULTS_TOOL = int(os.getenv('MAX_SEARCH_RESULTS_TOOL', 5)) # Chunks per tool call
SEARCH_RANKER = os.getenv('SEARCH_RANKER', 'auto')
DEFAULT_VS_CACHE_DURATION = int(os.getenv('DEFAULT_VS_CACHE_DURATION', 300))

# Ensure output directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DOCX_OUTPUT_DIR, exist_ok=True)

# --- Setup Flask App ---

# Ensure template directory exists
os.makedirs(TEMPLATE_DIR, exist_ok=True)

# --- Database Setup ---
try:
    from chat_db import ChatHistoryDB
    chat_db = ChatHistoryDB(DATABASE_FILE)
    logger.info(f"ChatHistoryDB initialized with {DATABASE_FILE}")
except Exception as db_init_err:
    logger.error(f"Failed to initialize ChatHistoryDB: {db_init_err}", exc_info=True)
    chat_db = None

# --- OpenAI Client Setup ---
client: Optional[OpenAI] = None
def get_openai_client() -> Optional[OpenAI]:
    global client
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key: logger.error("OPENAI_API_KEY not found."); client = None; return None
    if client is None or client.api_key != api_key:
        try: client = OpenAI(api_key=api_key, timeout=45.0); logger.info("OpenAI client (re)initialized.")
        except Exception as e: logger.error(f"Failed to init OpenAI client: {e}", exc_info=True); client = None; return None
    return client

def get_model_with_fallback(preferred_model=COMPLETION_MODEL):
    """Get the preferred model with fallback to a more stable model if needed."""
    # Allow override via environment variable
    model = os.getenv("OPENAI_MODEL", preferred_model)

    # Define fallback chain
    fallback_models = ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"]

    # If the preferred model is in the fallback chain, start from there
    if model in fallback_models:
        fallback_index = fallback_models.index(model)
        fallback_models = fallback_models[fallback_index:]

    # Return the preferred model and the fallback chain
    return model, fallback_models

# --- Helper Functions ---
def allowed_file(filename): return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Cache Management Functions ---
async def clear_semantic_cache():
    """Clear the semantic search cache"""
    semantic_search_cache.cache.clear()
    semantic_search_cache.timestamps.clear()
    logger.info("Semantic search cache cleared")

async def get_cache_stats():
    """Get statistics about the semantic cache"""
    total_entries = len(semantic_search_cache.cache)
    expired_entries = sum(1 for key, timestamp in semantic_search_cache.timestamps.items()
                         if time.time() - timestamp > semantic_search_cache.ttl)
    return {
        "total_entries": total_entries,
        "active_entries": total_entries - expired_entries,
        "expired_entries": expired_entries,
        "cache_size_limit": semantic_search_cache.max_size,
        "ttl_seconds": semantic_search_cache.ttl,
        "confidence_threshold": semantic_search_cache.confidence_threshold
    }

# --- Vector Store Functions ---
vector_store_cache = {"list": [], "last_updated": 0}
async def get_vector_stores(cache_duration=DEFAULT_VS_CACHE_DURATION):
    # (Keep implementation using cache and session fix)
    global vector_store_cache; now = time.time(); current_client = get_openai_client();
    if not current_client: return []
    if not vector_store_cache["list"] or now - vector_store_cache["last_updated"] > cache_duration:
        logger.info("Refreshing vector stores from OpenAI...")
        try:
            my_vector_stores = await asyncio.to_thread(current_client.vector_stores.list, order="desc", limit=50)
            vector_store_cache["list"] = [{"id": vs.id, "name": vs.name or f"Store ({vs.id[-6:]})"} for vs in my_vector_stores.data]
            vector_store_cache["last_updated"] = now; logger.info(f"Refreshed VS list: {len(vector_store_cache['list'])} VS.")
        except Exception as e: logger.error(f"Error fetching vector stores: {e}", exc_info=True); vector_store_cache["list"] = []
    else: logger.debug("Using cached vector store list.")
    return list(vector_store_cache["list"])

# --- Keep add_files_to_vector_store (Ensure it sets 'metadata') ---
async def add_files_to_vector_store(vector_store_id, file_paths_with_names, all_metadata_dict):
    """
    Uploads files individually, associates them with the vector store,
    and adds attributes provided by the user during upload.
    """
    current_client = get_openai_client()
    if not current_client: return {"status": "error", "message": "OpenAI client error."}
    if not file_paths_with_names: return {"status": "error", "message": "No files provided."}
    success_count = 0; failure_count = 0; total_count = len(file_paths_with_names); upload_results = []

    for temp_path, original_filename in file_paths_with_names:
        file_id = None; vs_file = None; file_stream = None
        try:
            logger.info(f"Processing file '{original_filename}' for VS {vector_store_id}...")

            # --- Get attributes for THIS file from the dictionary passed in ---
            # Use filename as the key used in frontend JS
            file_metadata = all_metadata_dict.get(original_filename, {})
            attributes = {
                "document_type": file_metadata.get("document_type", "general"), # Default if missing
                "language": file_metadata.get("language", "unknown"),       # Default if missing
                "category": file_metadata.get("category") or "",             # Handle optional field
                "original_filename": original_filename,
                "upload_unix_ts": int(time.time()),
                "processed_version": "1.1" # Indicate version using this upload method
            }
            # Remove empty category if needed
            if not attributes["category"]: del attributes["category"]
            logger.info(f"Using attributes for {file_id}: {attributes}")
            # --- End using provided attributes ---

            # --- Upload, Associate, Update (Keep this logic) ---
            file_stream = open(temp_path, "rb"); file_object = await asyncio.to_thread(current_client.files.create, file=file_stream, purpose="assistants")
            file_id = file_object.id; file_stream.close(); file_stream = None; logger.info(f"Uploaded '{original_filename}' as File ID: {file_id}")

            vs_file = await asyncio.to_thread(current_client.vector_stores.files.create_and_poll, vector_store_id=vector_store_id, file_id=file_id)
            vs_file_id = vs_file.id; logger.info(f"Associated File ID {file_id} ({original_filename}), Status: {vs_file.status}")

            if vs_file.status == 'completed':
                try: # Update attributes using the dictionary derived from user input
                    logger.info(f"Attempting update attributes for File ID {file_id}...")
                    await asyncio.to_thread(current_client.vector_stores.files.update, vector_store_id=vector_store_id, file_id=file_id, attributes=attributes)
                    logger.info(f"Attributes update call successful for File ID {file_id}.")
                    success_count += 1; upload_results.append(f"'{original_filename}': OK (Type: {attributes.get('document_type', '?')})")
                except Exception as update_err: logger.error(f"FAILED update attributes {file_id}: {update_err}", exc_info=True); failure_count += 1; upload_results.append(f"'{original_filename}': Added but ATTR UPDATE FAILED")
            else:
                logger.error(f"File {file_id} association failed. Status: {vs_file.status}")
                failure_count += 1
                upload_results.append(f"'{original_filename}': Failed assoc.")
                # Cleanup logic for failed association
                try:
                    logger.warning(f"Cleanup File {file_id}")
                    await asyncio.to_thread(current_client.files.delete, file_id=file_id)
                except Exception as del_err:
                    logger.error(f"Failed cleanup {file_id}: {del_err}")
        except Exception as e:
            logger.error(f"Error processing '{original_filename}': {e}", exc_info=True)
            failure_count += 1
            upload_results.append(f"'{original_filename}': Error")
        finally: # Cleanup
            if file_stream and not file_stream.closed:
                file_stream.close()
            try: os.remove(temp_path); logger.debug(f"Removed temp file: {temp_path}")
            except OSError as e_remove: logger.warning(f"Could not remove temp file {temp_path}: {e_remove}")

    final_message = f"Processed {total_count}. Success: {success_count}, Failed: {failure_count}."; status = "error" if failure_count == total_count else ("warning" if failure_count > 0 else "success")
    if failure_count > 0: final_message += " Details: " + " | ".join(upload_results[-failure_count:])
    return {"status": status, "message": final_message}


# --- Pydantic Models ---
class RetrievalSuccess(BaseModel):
    content: str
    source_filename: str

class RetrievalError(BaseModel):
    error_message: str
    details: Optional[str] = None

# Import the shared data model
from data_models import ExtractedData

class FinalAnswer(BaseModel):
    markdown_response: str

    model_config = ConfigDict(extra='ignore')

    @classmethod
    def model_validate_json(cls, json_data, **kwargs):
        """Custom JSON validation to handle cases where markdown_response is an object"""
        try:
            # Try standard validation first
            return super().model_validate_json(json_data, **kwargs)
        except Exception as e:
            # If that fails, try to handle the case where markdown_response is an object
            try:
                data = json.loads(json_data)
                if isinstance(data.get('markdown_response'), dict):
                    # If markdown_response is a dict, convert it to a string
                    md_resp_dict = data['markdown_response']

                    # Case 1: It has title and content fields
                    title = md_resp_dict.get('title', '')
                    content = md_resp_dict.get('content', '')

                    # Case 2: It has title and type fields (common error pattern)
                    if 'type' in md_resp_dict and not content:
                        # This is the specific error case we're seeing
                        if title:
                            # Use the title as the content
                            data['markdown_response'] = f"# {title}\n\nNo detailed information available about this topic."
                        else:
                            # Generic fallback
                            data['markdown_response'] = "No information available about the knowledge base."
                    elif not content and title:
                        # If there's no content but there is a title, use the title
                        data['markdown_response'] = f"# {title}\n\nNo detailed information available about this topic."
                    elif title and content:
                        # If there's both title and content, format them
                        data['markdown_response'] = f"# {title}\n\n{content}"
                    else:
                        # Convert the entire dict to a string as a last resort
                        try:
                            data['markdown_response'] = json.dumps(md_resp_dict, indent=2)
                        except:
                            data['markdown_response'] = "No information available."

                    # Try validation again with the fixed data
                    return cls.model_validate(data)
                raise e  # Re-raise if we couldn't fix it
            except Exception as fix_error:
                # Log the error during fix attempt
                logger.error(f"Error during FinalAnswer validation fix: {fix_error}")
                # If our fix attempt failed, create a basic valid instance
                return cls(markdown_response="Sorry, there was an error processing the response. Please try again with a different query.")

class DOCXGenerationResult(BaseModel):
    status: str  # "success" or "error"
    file_path: Optional[str]  # Path to the generated file
    file_name: Optional[str]  # Filename for downloading
    message: str  # Success or error message

class AnalysisResult(BaseModel):
    intent: str = Field(default="kb_query", description="The determined intent of the user's query")
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional details about the intent, such as query_topic, temp_file_names, required_fields, etc."
    )

# --- Tool Definitions ---

@function_tool(strict_mode=False)
async def get_kb_document_content(ctx: RunContextWrapper, document_type: str, query_or_identifier: str) -> Union[RetrievalSuccess, RetrievalError]:
    """Retrieves content from the knowledge base (Vector Store) based on document type and query/identifier."""
    logger.info(f"[Tool Call] get_kb_document_content: type='{document_type}', query='{query_or_identifier[:50]}...'")
    tool_context = ctx.context
    vs_id = tool_context.get("vector_store_id")
    tool_client = tool_context.get("client")
    chat_id = tool_context.get("chat_id")
    if not tool_client or not vs_id:
        return RetrievalError(error_message="Tool config error.")

    # Prepare filter information for cache lookup
    filter_info = {
        'document_type': document_type,
        'included_file_ids': included_file_ids if included_file_ids else []
    }

    # Check semantic cache first
    try:
        cached_result = await semantic_search_cache.find_semantic_match(
            new_query=query_or_identifier,
            vector_store_id=vs_id,
            filters=filter_info,
            chat_id=chat_id or "default"
        )

        if cached_result:
            logger.info(f"[SEMANTIC CACHE HIT] Retrieved semantically equivalent result for query: '{query_or_identifier[:30]}...'")
            # Ensure the cached result is the correct type
            if isinstance(cached_result, dict) and 'content' in cached_result and 'source_filename' in cached_result:
                return RetrievalSuccess(**cached_result)
            return cached_result
    except Exception as cache_err:
        logger.warning(f"Semantic cache lookup failed: {cache_err}. Falling back to direct search.")

    # Get file inclusion settings if chat_id is provided
    included_file_ids = []
    if chat_id and chat_db:
        try:
            chat_files = await asyncio.to_thread(chat_db.get_chat_files, chat_id)
            included_file_ids = [file["file_id"] for file in chat_files if file["included"]]
            if included_file_ids:
                logger.info(f"Using {len(included_file_ids)} included files for chat {chat_id}")
        except Exception as e:
            logger.warning(f"Error getting included files for chat {chat_id}: {e}")

    # First try with a filter for more precise results
    try:
        # Build filters
        filters = []

        # Add document type filter if specified and not a general type
        if document_type and document_type.lower() not in ["kb", "knowledge base", "general", "unknown", "none", ""]:
            logger.info(f"Adding document_type filter for: {document_type}")
            filters.append({"type": "eq", "key": "document_type", "value": document_type})
        else:
            logger.info(f"Not filtering by document_type: '{document_type}' is considered general")

        # Add file filter if we have included files
        if included_file_ids:
            # OpenAI API doesn't support 'in' filter type, so we need to use 'or' with multiple 'eq' filters
            if len(included_file_ids) == 1:
                # If only one file ID, use a simple 'eq' filter
                filters.append({"type": "eq", "key": "id", "value": included_file_ids[0]})
            elif len(included_file_ids) > 1:
                # If multiple file IDs, use 'or' with multiple 'eq' filters
                file_filters = []
                for file_id in included_file_ids:
                    file_filters.append({"type": "eq", "key": "id", "value": file_id})

                # Add the combined OR filter
                if file_filters:
                    filters.append({"type": "or", "filters": file_filters})

        # Create filter object if we have any filters
        filter_obj = None
        if len(filters) > 1:
            # If multiple filters, combine with AND
            filter_obj = {"type": "and", "filters": filters}
        elif len(filters) == 1:
            # If single filter, use it directly
            filter_obj = filters[0]

        # Define search variants to run in parallel
        search_tasks = []
        search_variant_descriptions = []

        # Variant 1: Search with all filters (if we have any)
        if filter_obj:
            search_params = {
                "vector_store_id": vs_id,
                "query": query_or_identifier,
                "filters": filter_obj,
                "max_num_results": MAX_SEARCH_RESULTS_TOOL,
                "ranking_options": {"ranker": SEARCH_RANKER}
            }
            search_tasks.append(asyncio.to_thread(tool_client.vector_stores.search, **search_params))
            search_variant_descriptions.append(f"All filters: {json.dumps(filter_obj, indent=2)}")

        # Variant 2: Search with just file filters (if we have included files)
        if included_file_ids:
            # Create file filter using supported filter types
            if len(included_file_ids) == 1:
                file_filter = {"type": "eq", "key": "id", "value": included_file_ids[0]}
            else:
                file_filters = []
                for file_id in included_file_ids:
                    file_filters.append({"type": "eq", "key": "id", "value": file_id})
                file_filter = {"type": "or", "filters": file_filters}

            # Only add this variant if it's different from the first one
            if not filter_obj or filter_obj != file_filter:
                search_params = {
                    "vector_store_id": vs_id,
                    "query": query_or_identifier,
                    "filters": file_filter,
                    "max_num_results": MAX_SEARCH_RESULTS_TOOL,
                    "ranking_options": {"ranker": SEARCH_RANKER}
                }
                search_tasks.append(asyncio.to_thread(tool_client.vector_stores.search, **search_params))
                search_variant_descriptions.append(f"File-only filter: {json.dumps(file_filter, indent=2)}")

        # Variant 3: Search with just document type filter (if specified and not general)
        if document_type and document_type.lower() not in ["kb", "knowledge base", "general", "unknown", "none", ""]:
            doc_type_filter = {"type": "eq", "key": "document_type", "value": document_type}

            # Only add this variant if it's different from previous ones
            if (not filter_obj or filter_obj != doc_type_filter) and (not included_file_ids or doc_type_filter != file_filter):
                search_params = {
                    "vector_store_id": vs_id,
                    "query": query_or_identifier,
                    "filters": doc_type_filter,
                    "max_num_results": MAX_SEARCH_RESULTS_TOOL,
                    "ranking_options": {"ranker": SEARCH_RANKER}
                }
                search_tasks.append(asyncio.to_thread(tool_client.vector_stores.search, **search_params))
                search_variant_descriptions.append(f"Document-type-only filter: {json.dumps(doc_type_filter, indent=2)}")

        # Variant 4: Search without any filters (semantic fallback)
        search_params = {
            "vector_store_id": vs_id,
            "query": query_or_identifier,
            "max_num_results": MAX_SEARCH_RESULTS_TOOL,
            "ranking_options": {"ranker": SEARCH_RANKER}
        }
        search_tasks.append(asyncio.to_thread(tool_client.vector_stores.search, **search_params))
        search_variant_descriptions.append("No filters (semantic fallback)")

        # Log the search variants
        logger.info(f"[CACHE MISS] Running {len(search_tasks)} parallel search variants for query: '{query_or_identifier[:50]}...'")
        for i, desc in enumerate(search_variant_descriptions):
            logger.info(f"Search variant {i+1}: {desc}")

        # Run all search variants in parallel
        search_results_list = await asyncio.gather(*search_tasks)

        # Process results from all variants
        combined_results = []
        result_counts = []

        for i, results in enumerate(search_results_list):
            if results and results.data:
                count = len(results.data)
                result_counts.append(count)
                logger.info(f"Search variant {i+1} returned {count} results")
                combined_results.extend(results.data)
            else:
                result_counts.append(0)
                logger.info(f"Search variant {i+1} returned no results")

        # If we have any results, use them
        if combined_results:
            # Deduplicate results based on file_id and content hash
            unique_results = {}
            for res in combined_results:
                # Create a unique key for each result based on file_id and content hash
                content_hash = hashlib.md5(''.join(part.text for part in res.content if part.type == 'text').encode()).hexdigest()
                key = f"{res.file_id}_{content_hash}"

                # Only keep the result with the highest score if we have duplicates
                if key not in unique_results or res.score > unique_results[key].score:
                    unique_results[key] = res

            # Convert back to list and sort by score
            deduplicated_results = list(unique_results.values())
            deduplicated_results.sort(key=lambda x: x.score, reverse=True)

            # Limit to MAX_SEARCH_RESULTS_TOOL
            final_results = deduplicated_results[:MAX_SEARCH_RESULTS_TOOL]

            logger.info(f"Combined {sum(result_counts)} results, deduplicated to {len(deduplicated_results)}, returning top {len(final_results)}")

            # Create a dummy search results object with our final results
            search_results = type('obj', (object,), {'data': final_results})
        else:
            # No results from any variant
            search_results = None

        if search_results and search_results.data:
            content = "\n\n".join(re.sub(r'\s+', ' ', part.text).strip() for res in search_results.data for part in res.content if part.type == 'text')
            source_filename = search_results.data[0].filename or f"FileID:{search_results.data[0].file_id[-6:]}"
            logger.info(f"[Tool Result] KB Content Found for query '{query_or_identifier[:30]}...'. Len: {len(content)}")

            # Create result object
            result = RetrievalSuccess(content=content, source_filename=source_filename)

            # Store in semantic cache
            try:
                await semantic_search_cache.set(
                    vector_store_id=vs_id,
                    query=query_or_identifier,
                    filters=filter_info,
                    chat_id=chat_id or "default",
                    document_type=document_type,
                    result=result
                )
                logger.info(f"[CACHE SET] Stored new result in semantic cache")
            except Exception as cache_err:
                logger.warning(f"Failed to store result in semantic cache: {cache_err}")

            return result
        else:
            logger.warning(f"[Tool Result] No KB content found for query: '{query_or_identifier[:50]}...'")
            error_result = RetrievalError(error_message=f"No KB content found for query related to '{document_type}'.")

            # Also cache negative results to avoid repeated failed searches
            try:
                await semantic_search_cache.set(
                    vector_store_id=vs_id,
                    query=query_or_identifier,
                    filters=filter_info,
                    chat_id=chat_id or "default",
                    document_type=document_type,
                    result=error_result
                )
                logger.info(f"[CACHE SET] Stored negative result in semantic cache")
            except Exception as cache_err:
                logger.warning(f"Failed to store negative result in semantic cache: {cache_err}")

            return error_result
    except Exception as e:
        logger.error(f"[Tool Error] KB Search failed for query '{query_or_identifier[:30]}...': {e}", exc_info=True)
        return RetrievalError(error_message=f"KB Search error: {str(e)}")

@function_tool(strict_mode=False)
async def process_temporary_file(ctx: RunContextWrapper, filename: str) -> Union[RetrievalSuccess, RetrievalError]:
    """Reads and returns the text content of a previously uploaded temporary file for use as context."""
    logger.info(f"[Tool Call] process_temporary_file: filename='{filename}'")
    tool_context = ctx.context
    temp_file_info = tool_context.get("temp_file_info")
    if not temp_file_info or temp_file_info.get("filename") != filename:
        return RetrievalError(error_message=f"Temporary file '{filename}' not available.")
    file_path = temp_file_info.get("path")
    if not file_path or not os.path.exists(file_path):
        return RetrievalError(error_message=f"Temporary file path invalid for '{filename}'.")
    try:
        text_content = ""
        file_lower = filename.lower()
        if file_lower.endswith(".pdf"):
            # Use our robust PDF text extraction function
            text_content = extract_text_from_pdf(file_path)
        elif file_lower.endswith((".txt", ".md")):
            with open(file_path, 'r', encoding='utf-8') as f:
                text_content = f.read()
        else:
            return RetrievalError(error_message=f"Unsupported temporary file type: {filename}")
        cleaned_content = re.sub(r'\s+', ' ', text_content).strip()
        logger.info(f"[Tool Result] Processed temporary file '{filename}'. Length: {len(cleaned_content)}")
        return RetrievalSuccess(content=cleaned_content, source_filename=f"Uploaded: {filename}")
    except Exception as e:
        logger.error(f"[Tool Error] Failed read/process temp file '{filename}': {e}", exc_info=True)
        return RetrievalError(error_message=f"Error processing temp file: {str(e)}")

@function_tool(strict_mode=False)
async def retrieve_template_content(ctx: RunContextWrapper, template_name: str) -> Union[RetrievalSuccess, RetrievalError]:
    """Retrieves the text content of a specified document template (txt, md, pdf)."""
    logger.info(f"[Tool Call] retrieve_template_content: template_name='{template_name}'")
    try:
        # Use asyncio.to_thread to run the file operations asynchronously
        async def process_template():
            # Sanitize template_name but preserve extension
            original_filename = secure_filename(template_name)
            base_name, ext = os.path.splitext(original_filename)
            if not ext: ext = ".md" # Default to markdown if no extension provided

            # Check if extension is allowed for templates
            allowed_template_exts = {'.txt', '.md', '.pdf'}
            if ext.lower() not in allowed_template_exts:
                logger.error(f"Attempted to retrieve template with unsupported extension: {original_filename}")
                return RetrievalError(error_message=f"Unsupported template file type '{ext}'. Allowed: {', '.join(allowed_template_exts)}")

            final_filename = f"{base_name}{ext}" # Reconstruct potentially sanitized name
            template_path = os.path.join(TEMPLATE_DIR, final_filename)

            # Security check: Ensure path is still within TEMPLATE_DIR
            if not os.path.exists(template_path) or os.path.commonpath([TEMPLATE_DIR]) != os.path.commonpath([TEMPLATE_DIR, template_path]):
                logger.error(f"[Tool Error] Template file not found or invalid path: {template_path}")
                return RetrievalError(error_message=f"Template '{template_name}' not found.")

            # --- Extract content based on type ---
            content = ""
            logger.info(f"Reading template file: {template_path}")
            if ext.lower() == ".pdf":
                # Use our robust PDF text extraction function
                content = extract_text_from_pdf(template_path)
            elif ext.lower() in [".txt", ".md"]:
                with open(template_path, 'r', encoding='utf-8') as f:
                    content = f.read()

            if not content:
                logger.warning(f"Extracted empty content from template: {final_filename}")
                return RetrievalError(error_message=f"Could not extract content from template '{final_filename}'.")

            logger.info(f"[Tool Result] Retrieved template '{final_filename}'. Length: {len(content)}")
            cleaned_content = re.sub(r'\s+', ' ', content).strip()
            return RetrievalSuccess(content=cleaned_content, source_filename=f"Template: {original_filename}")

        # Run the template processing function asynchronously
        return await process_template()

    except Exception as e:
         logger.error(f"[Tool Error] Error retrieving template '{template_name}': {e}", exc_info=True)
         return RetrievalError(error_message=f"Error retrieving template: {str(e)}")

@function_tool(strict_mode=False)
async def generate_docx_from_markdown(ctx: RunContextWrapper, markdown_content: str, template_name: str) -> DOCXGenerationResult:
    """Converts markdown content into a professionally formatted DOCX file.

    Args:
        markdown_content: The populated markdown content from the template
        template_name: Name of the original template for reference

    Returns:
        Object containing the path to the generated DOCX file and status
    """
    try:
        # Import the docx_generator module
        import docx_generator

        # Generate the DOCX file asynchronously
        file_path, file_name = await asyncio.to_thread(docx_generator.markdown_to_docx, markdown_content, template_name)

        # Return success result
        return DOCXGenerationResult(
            status="success",
            file_path=file_path,
            file_name=file_name,
            message="DOCX file successfully generated"
        )
    except Exception as e:
        logger.error(f"Error generating DOCX: {e}", exc_info=True)
        return DOCXGenerationResult(
            status="error",
            file_path=None,
            file_name=None,
            message=f"Error generating DOCX: {str(e)}"
        )

@function_tool(strict_mode=False)
async def extract_data_for_template(ctx: RunContextWrapper, context_sources: List[str], required_fields: List[str], document_analyses: Optional[List[Dict]] = None) -> ExtractedData:
    """Extracts specific data fields required for a template from provided text context sources."""
    logger.info(f"[Tool Call] extract_data_for_template. Required: {required_fields}. Sources: {len(context_sources)} provided.")

    if document_analyses:
        logger.info(f"Document analyses provided for extraction: {len(document_analyses)}")

    # Call the integrated DocumentAnalyzerAgent implementation
    return await document_analyzer_integration.extract_data_for_template_integrated(ctx, context_sources, required_fields, document_analyses)

# --- Helper Functions for Workflow ---
async def detect_required_fields_from_template(template_content: str, template_name: str) -> List[str]:
    """Dynamically detect required fields from a template based on content analysis."""
    logger.info(f"Attempting to detect required fields from template: {template_name}")

    # Create a context wrapper with the OpenAI client
    ctx = RunContextWrapper({"client": get_openai_client()})

    # Create a combined input object with both template_content and template_name
    combined_input = {"template_content": template_content, "template_name": template_name}

    # Call the detect_fields_from_template tool directly using on_invoke_tool with only ctx and the combined input
    return await detect_fields_from_template.on_invoke_tool(ctx, combined_input)

def extract_final_answer(run_result: RunResult) -> str:
    """Extracts markdown response from FinalAnswer Pydantic model in RunResult,
       handling potential errors or unexpected output types."""
    try:
        final_output = run_result.final_output

        # Case 1: We got a FinalAnswer instance
        if isinstance(final_output, FinalAnswer):
            return final_output.markdown_response

        # Case 2: We got a dictionary that might contain markdown_response
        elif isinstance(final_output, dict) and 'markdown_response' in final_output:
            md_resp = final_output['markdown_response']

            # If markdown_response is a string, use it directly
            if isinstance(md_resp, str):
                logger.warning(f"Got dict with markdown_response string. Extracting directly.")
                return md_resp

            # If markdown_response is a dict, handle it specially
            elif isinstance(md_resp, dict):
                logger.warning(f"Got nested markdown_response structure. Attempting to extract content.")

                # Try to extract title and content
                title = md_resp.get('title', '')
                content = md_resp.get('content', '')

                # Handle the case where it has title and type fields (common error pattern)
                if 'type' in md_resp and not content:
                    if title:
                        return f"# {title}\n\nNo detailed information available about this topic."
                    else:
                        return "No information available about the knowledge base."
                elif title and not content:
                    return f"# {title}\n\nNo detailed information available about this topic."
                elif title and content:
                    return f"# {title}\n\n{content}"
                else:
                    # Try to convert the entire nested dict to a string representation
                    try:
                        return f"Information about the knowledge base:\n\n{json.dumps(md_resp, indent=2)}"
                    except:
                        return "No information available about the knowledge base."
            else:
                # If markdown_response is neither string nor dict, convert to string
                logger.warning(f"Got unexpected markdown_response type: {type(md_resp)}. Converting to string.")
                try:
                    return str(md_resp)
                except:
                    return "Error: Could not convert response to string format."

        # Case 3: We got a string that might be the direct response
        elif isinstance(final_output, str):
            logger.warning(f"Got string instead of FinalAnswer model. Using directly.")
            return final_output

        # Case 4: Try to extract from raw response text
        elif hasattr(run_result, 'raw_response') and run_result.raw_response:
            logger.warning(f"Attempting to extract from raw_response.")
            try:
                # Try to parse the raw response as JSON
                raw_text = run_result.raw_response
                if isinstance(raw_text, str):
                    # Look for JSON object in the raw text
                    start_idx = raw_text.find('{')
                    end_idx = raw_text.rfind('}')
                    if start_idx >= 0 and end_idx > start_idx:
                        json_str = raw_text[start_idx:end_idx+1]
                        data = json.loads(json_str)
                        if 'markdown_response' in data:
                            md_resp = data['markdown_response']
                            if isinstance(md_resp, str):
                                return md_resp
                            elif isinstance(md_resp, dict):
                                title = md_resp.get('title', '')
                                if title:
                                    return f"# {title}\n\nNo detailed information available about this topic."
                return raw_text  # Use the raw text as a fallback
            except Exception as raw_err:
                logger.error(f"Error extracting from raw_response: {raw_err}")
                # Continue to fallback methods

        # Case 5: Fallback to the last assistant message
        logger.error(f"Workflow step expected FinalAnswer Pydantic model, but received type: {type(final_output)}. Output: {final_output}")
        # Try to gracefully fallback to the last assistant message content
        history_list = run_result.to_input_list()
        # Find the last message in the history list generated by an assistant
        last_msg = next((msg for msg in reversed(history_list) if msg.get('role') == 'assistant'), None)
        if last_msg and isinstance(last_msg.get('content'), str):
            logger.warning("Falling back to last assistant message content due to unexpected final output type.")
            # Add a warning to the user that the format might be off
            return f"*(Warning: Output format unexpected. Displaying last raw message)*\n\n{last_msg.get('content')}"
        else:
            # If no suitable fallback message found
            return "Error: Could not generate final answer in the expected format."
    except Exception as e:
        # Catch any errors during extraction
        logger.error(f"Error during answer extraction: {e}", exc_info=True)
        return f"Sorry, an error occurred while processing your request: {str(e)}\n\nPlease try again with a different query."

async def run_standard_agent_rag(user_query: str, history: List[Dict[str, str]], workflow_context: Dict, vs_id: Optional[str] = None) -> str:
    """Implements a simplified RAG workflow using the final_synthesizer_agent.
    This is used as a fallback when no template or temporary files are specified.

    Args:
        user_query: The user's query
        history: The conversation history
        workflow_context: The workflow context containing any necessary information
        vs_id: Optional vector store ID (can be extracted from workflow_context if needed)
    """
    logger.info(f"Running Standard RAG workflow for query: '{user_query[:50]}...'")
    try:
        # First, retrieve relevant content from the knowledge base
        kb_content = ""
        if vs_id:
            logger.info(f"Retrieving KB content for query: '{user_query[:50]}...'")
            kb_context = workflow_context.copy()

            # Create a more specific query and document type based on context
            kb_query = user_query
            document_type = "general"  # Default document type

            # Get document analyses from the workflow context if available
            workflow_document_analyses = workflow_context.get("document_analyses", [])

            # Check if we have document analyses to inform our query
            if workflow_document_analyses:
                # Extract document types from analyses
                doc_types = [analysis.doc_type for analysis in workflow_document_analyses if analysis.confidence > 0.7]

                if doc_types:
                    # Use the most common document type with high confidence
                    from collections import Counter
                    most_common_type = Counter(doc_types).most_common(1)[0][0]
                    document_type = most_common_type
                    logger.info(f"Using document type from analysis: {document_type}")

                    # Enhance query with key sections if available
                    key_sections = []
                    for analysis in workflow_document_analyses:
                        if analysis.key_sections:
                            key_sections.extend(analysis.key_sections)

                    if key_sections:
                        # Use up to 3 key sections to enhance the query
                        top_sections = key_sections[:3]
                        kb_query = f"{user_query} related to {', '.join(top_sections)}"
                        logger.info(f"Enhanced query with key sections: {kb_query}")

            # Special case for labor code queries
            if "code de travail" in user_query.lower() or "labor code" in user_query.lower() or "travail" in user_query.lower():
                kb_query = f"Moroccan Labor Code information related to: {user_query}"
                document_type = "code de travail"  # More specific document type

            # Get KB content using the minimal data gathering agent
            kb_data_raw = await Runner.run(
                data_gathering_agent_minimal,
                input=f"Get KB content about '{document_type}' related to: {kb_query}",
                context=kb_context
            )
            kb_data = kb_data_raw.final_output

            # If first attempt fails, try a more general search
            if isinstance(kb_data, RetrievalError):
                logger.info(f"First KB retrieval attempt failed. Trying more general search.")
                # Get KB content using the minimal data gathering agent with a more general document type
                kb_data_raw = await Runner.run(
                    data_gathering_agent_minimal,
                    input=f"Get KB content about 'general' related to: {kb_query}",
                    context=kb_context
                )
                kb_data = kb_data_raw.final_output

            if isinstance(kb_data, RetrievalSuccess):
                kb_content = kb_data.content
                logger.info(f"Retrieved KB content for query. Length: {len(kb_content)}")
            elif isinstance(kb_data, RetrievalError):
                logger.warning(f"KB retrieval failed: {kb_data.error_message}")
                # Provide a clear message that the information couldn't be found
                kb_content = """
                I couldn't find specific information about this topic in the knowledge base.
                The search functionality encountered an error or no relevant content was found.

                Please try:
                - A different query
                - Checking if the relevant files are selected in the knowledge base
                - Consulting official sources for accurate information

                I can only provide information that exists in the knowledge base and will not generate fabricated content.
                """
                logger.info("Using no-results message instead of fabricating information")

        # Create a prompt that includes the query and KB content
        prompt = f"""Answer the following question using ONLY the knowledge base content provided below.\n\nQuestion: {user_query}\n\nIMPORTANT: If the knowledge base content does not contain information to answer this question, clearly state this limitation. DO NOT fabricate or make up information that is not in the provided content. Accuracy is more important than helpfulness."""

        if kb_content:
            prompt += f"\n\nRelevant Knowledge Base Content:\n{kb_content}"

        synthesis_messages = history + [{
            "role": "user",
            "content": prompt
        }]

        # Log the model being used
        model, _ = get_model_with_fallback()
        logger.info(f"Using model: {model} for final synthesis")

        # Run the final synthesizer agent with the query and KB content
        try:
            final_synth_raw = await Runner.run(final_synthesizer_agent, input=synthesis_messages, context=workflow_context)

            # Since we removed the output_type, the result might be a string directly
            if isinstance(final_synth_raw.final_output, str):
                logger.info("Got string response directly from agent")
                return final_synth_raw.final_output

            # Otherwise, try to extract the final answer using our helper function
            final_markdown_response = extract_final_answer(final_synth_raw)
            return final_markdown_response
        except Exception as agent_error:
            # Check if this is the specific error pattern we're seeing
            error_str = str(agent_error)
            if "markdown_response" in error_str and "title" in error_str and "type" in error_str:
                logger.warning(f"Caught specific error pattern with markdown_response as object. Handling directly.")

                # Try to extract the JSON from the error message
                try:
                    # Find JSON object in the error message
                    start_idx = error_str.find('{')
                    end_idx = error_str.rfind('}')
                    if start_idx >= 0 and end_idx > start_idx:
                        json_str = error_str[start_idx:end_idx+1]
                        data = json.loads(json_str)

                        # Check if it has the specific pattern we're looking for
                        if isinstance(data.get('markdown_response'), dict):
                            md_resp_dict = data['markdown_response']
                            title = md_resp_dict.get('title', '')

                            if title:
                                # Create a reasonable response based on the title
                                if "knowledge base" in title.lower() or "knowledgebase" in title.lower():
                                    return f"# {title}\n\nThe knowledge base is a collection of documents that can be searched for information. You can ask questions about specific topics, and I'll try to find relevant information from the documents in the knowledge base.\n\nCurrently, I don't have detailed information about what specific documents are in the knowledge base. You can try asking questions about specific topics to see if there's relevant information available."
                                else:
                                    return f"# {title}\n\nI don't have detailed information about this topic in the knowledge base. Please try asking a more specific question or check if the relevant documents are included in the knowledge base."
                except Exception as extract_error:
                    logger.error(f"Error extracting from error message: {extract_error}")

                # Fallback response if extraction fails
                return "I don't have detailed information about the knowledge base contents. You can try asking specific questions about topics you're interested in, and I'll search for relevant information in the available documents."
            else:
                # Re-raise if it's not the specific error pattern we're handling
                raise
    except Exception as e:
        logger.error(f"Standard RAG workflow failed: {e}", exc_info=True)
        return f"Sorry, an error occurred during processing: {html.escape(str(e))}"

# --- Agent Definitions ---
# Define the minimal Data Gathering Agent
data_gathering_agent_minimal = Agent(
    name="DataGatheringAgentMinimal",
    instructions="""Use the provided tools to gather specific data or document content.
    Call the *single* most appropriate tool based on the request.
    Once the tool call is complete, stop and provide the tool's output.""",
    tools=[retrieve_template_content, process_temporary_file, get_kb_document_content],
    model=COMPLETION_MODEL,
    tool_use_behavior="stop_on_first_tool",
    output_type=Union[RetrievalSuccess, RetrievalError]
)

query_analyzer_agent = Agent(
    name="QueryAnalyzerAgent",
    instructions="""Analyze the user's query, available templates, and temporary files to determine the true intent with high accuracy.

    Possible intents:
    - `kb_query`: User wants information from the knowledge base only
    - `temp_context_query`: User wants information based on temporary files only
    - `kb_query_with_temp_context`: User wants information that combines knowledge base and temporary files
    - `populate_template`: User wants to fill a template with data
    - `analyze_template`: User wants analysis or comparison related to a template, not just filling it

    Guidelines for sophisticated intent determination:
    1. Deeply analyze the semantic meaning of the user's query, not just keywords
    2. Consider the context of any selected template or uploaded files
    3. Understand that templates can be used for both population AND analysis
    4. Recognize that questions about documents may need both KB and temporary file context
    5. Identify when the user is continuing a previous conversation thread

    IMPORTANT: The presence of a selected template in the UI does NOT automatically mean the intent is `populate_template`.
    The user's query is the primary factor in determining intent.

    Examples with nuanced reasoning:
    - "Generate a contract for Omar" → `populate_template` (explicit request to create a document)
    - "How many articles are in the labor code?" → `kb_query` (seeking factual information from KB)
    - "Compare this template with the labor code" → `analyze_template` (requesting comparative analysis)
    - "What does this document say about working hours?" → `temp_context_query` (asking about uploaded content)
    - "Is this contract compliant with labor regulations?" → `kb_query_with_temp_context` (needs both KB and document)
    - "What fields are required in this template?" → `analyze_template` (asking about template structure)
    - "Can you extract the employee details from this document?" → `temp_context_query` (extraction from uploaded file)

    For `populate_template` intent:
    - Include a comprehensive list of required fields based on template type and content
    - Consider what fields would be logically needed even if not explicitly mentioned
    - For employment contracts: employee_name, employer_name, start_date, salary, job_title, etc.
    - For invoices: client_name, invoice_date, due_date, items, total_amount, tax_rate, etc.

    For `kb_query_with_temp_context` intent:
    - Include both the specific query topic and the relevant temporary files
    - Explain why both knowledge base and document context are needed

    For `analyze_template` intent:
    - Specify whether knowledge base lookup is needed for the analysis
    - Indicate whether temporary files should be considered in the analysis
    - Include what aspects of the template should be analyzed

    Output a JSON object with the following structure:
    {
        "intent": "one of the intents listed above",
        "confidence": 0.0-1.0, // How confident you are in this intent determination
        "details": {
            // Intent-specific details as described above
            // Include reasoning for your determination
        }
    }
    """,
    model=COMPLETION_MODEL
    # No output_type to avoid schema validation issues
)
# Define the minimal DOCX Generation Agent
docx_generation_agent_minimal = Agent(
    name="DOCXGenerationAgentMinimal",
    instructions="""Use the generate_docx_from_markdown tool to convert the provided markdown content into a DOCX file.
    Once the tool call is complete, stop and provide the tool's output.""",
    tools=[generate_docx_from_markdown],
    model=COMPLETION_MODEL,
    tool_use_behavior="stop_on_first_tool",
    output_type=DOCXGenerationResult
)

# Define the minimal Document Analyzer Agent
template_analyzer_agent_minimal = Agent(
    name="TemplateAnalyzerAgentMinimal",
    instructions="""Use the analyze_document_for_workflow tool to analyze the document structure and content.
    Provide a detailed analysis of the document type, structure, and key sections.
    Once the tool call is complete, stop and provide the tool's output.""",
    tools=[analyze_document_for_workflow],
    model=COMPLETION_MODEL,
    tool_use_behavior="stop_on_first_tool",
    output_type=DocumentAnalysis
)
data_extractor_agent = Agent(
    name="DataExtractorAgent",
    instructions="""Extract structured data from the provided context sources.
    You will receive a JSON object with 'context_sources' (array of text) and 'required_fields' (array of field names).

    IMPORTANT GUIDELINES FOR DATA EXTRACTION:
    1. Carefully analyze all context sources to find the required fields
    2. Pay special attention to any KEY: field | VALUE: value patterns in the context
    3. Look for standard patterns like "field: value" or "field - value"
    4. For each required field, extract the most accurate value from the context
    5. If a field is not found in the context, return null for that field
    6. For dates, extract them in their original format
    7. For names, extract the full name as provided
    8. For addresses or locations, extract the complete address
    9. For numerical values, extract them with their units if provided

    Use the extract_data_for_template tool to extract the required fields from the context sources.
    IMPORTANT: After calling the tool, STOP IMMEDIATELY. Do not add any additional text or explanation.

    EXAMPLES OF WHAT TO LOOK FOR:
    - Direct statements: "The employee name is John Smith"
    - Form fields: "Name: John Smith"
    - Formatted data: "KEY: employee_name | VALUE: John Smith"
    - Contextual information: "This contract is between ABC Company and John Smith"

    Be thorough and extract as much information as possible from the provided context.
    """,
    tools=[extract_data_for_template],
    model=COMPLETION_MODEL,
    # Remove output_type to avoid schema validation issues
    tool_use_behavior="stop_on_first_tool"  # Keep tool_use_behavior
)
template_populator_agent = Agent(
    name="TemplatePopulatorAgent",
    instructions="""Efficiently populate templates with provided data. Focus on speed and accuracy.

    CORE TASK:
    1. Fill template placeholders with matching data values
    2. Use [MISSING: field_name] for missing fields
    3. Maintain original template structure
    4. If compliance is mentioned, add BRIEF notes only where critical

    TEMPLATE POPULATION PROCESS:
    - Identify placeholders: [field], {field}, <field>, {{field}}
    - Match with data fields (case-insensitive)
    - Replace placeholders with corresponding values
    - Format dates as DD/MM/YYYY unless template indicates otherwise
    - Format currency with appropriate symbols

    EFFICIENCY GUIDELINES:
    - Focus ONLY on explicit placeholders in the template
    - Do NOT add lengthy explanations or analysis
    - Keep compliance notes brief and targeted
    - Skip non-essential formatting
    - Omit "Additional Information" section unless explicitly requested

    OUTPUT:
    - Return populated template as Markdown
    - Do not include explanations about your process
    """,
    model=COMPLETION_MODEL,
    output_type=FinalAnswer
)
final_synthesizer_agent = Agent(
    name="FinalSynthesizerAgent",
    instructions="""Synthesize final answer from query & context (KB or temp file).

    IMPORTANT RULES:
    1. NEVER fabricate information that is not in the provided context
    2. If the context doesn't contain information to answer the query, clearly state this limitation
    3. Do not try to be helpful by making up information - accuracy is more important than helpfulness
    4. Only provide information that is explicitly supported by the context
    5. If asked about a specific country, language, or document that isn't in the context, clearly state that this information is not available

    Follow this structure:
    1. Task understanding: Briefly restate what you're being asked to do
    2. Context summary: Summarize what information is available in the context
    3. Reasoning: Analyze how the context relates to the query
    4. Final answer: Provide a clear, direct answer based ONLY on the context
    5. Limitations: Explicitly state what information was not available in the context

    Format your response in Markdown.

    IMPORTANT: Your response MUST be a simple string, not a complex object. Do not return a JSON object with title and type fields.
    """,
    model=COMPLETION_MODEL,
    # Remove output_type to avoid schema validation issues
)

# --- Complex Workflow Orchestration ---
async def run_complex_rag_workflow(user_query: str, vs_id: str, history: List[Dict[str, str]],
                                   temp_files_info: Optional[List[Dict]] = None,
                                   template_to_populate: Optional[str] = None,
                                   chat_id: Optional[str] = None):
    """Orchestrates interaction between agents for complex RAG, including template population."""
    current_client = get_openai_client()
    if not current_client: raise ValueError("Client missing.")
    workflow_context = {"vector_store_id": vs_id, "client": current_client, "temp_files_info": temp_files_info or [], "history": history, "chat_id": chat_id}
    # Add the current query to the context
    workflow_context["current_query"] = user_query
    final_markdown_response = "Error: Workflow failed."
    logger.info(f"Running Workflow. Query: '{user_query[:50]}...', TempFiles: {len(temp_files_info or [])}, Template: {template_to_populate}")

    try:
        # 1. Determine Initial Intent (will be refined by QueryAnalyzerAgent)
        intent = "kb_query"; details = {"query": user_query} # Default

        # Run the QueryAnalyzerAgent to determine the true intent
        logger.info("Running QueryAnalyzerAgent to determine true intent")
        analyzer_input = {
            "user_query": user_query,
            "template_name": template_to_populate if template_to_populate else None,
            "has_temp_files": bool(temp_files_info),
            "temp_file_names": [f['filename'] for f in temp_files_info] if temp_files_info else []
        }
        # Log the model being used
        model, _ = get_model_with_fallback()
        logger.info(f"Using model: {model} for query analysis")
        analyzer_result = await Runner.run(query_analyzer_agent, input=json.dumps(analyzer_input), context=workflow_context)

        # Log the raw analyzer result
        logger.info(f"QueryAnalyzerAgent raw result: {analyzer_result.final_output}")

        # Parse the analyzer's output
        try:
            # If the output is a string, try to parse it as JSON
            if isinstance(analyzer_result.final_output, str):
                # Check if the output is wrapped in markdown code blocks
                output_str = analyzer_result.final_output.strip()

                # Extract JSON from markdown code blocks if present
                if output_str.startswith('```') and '```' in output_str[3:]:
                    # Check if it's a JSON code block
                    if output_str.startswith('```json'):
                        # Find the end of the opening code block marker
                        start_idx = output_str.find('\n', 3) + 1
                        if start_idx <= 0:  # No newline found after opening marker
                            start_idx = output_str.find('{')
                    else:
                        # For other code blocks, just look for the opening brace
                        start_idx = output_str.find('{')

                    # Find the start of the closing code block marker
                    end_idx = output_str.rfind('```')

                    # Extract the JSON content
                    if start_idx > 0 and end_idx > start_idx:
                        json_str = output_str[start_idx:end_idx].strip()
                        # Remove any trailing whitespace or newlines
                        json_str = json_str.rstrip()
                        logger.info(f"Extracted JSON from markdown: {json_str[:100]}...")
                    else:
                        # Try to find JSON directly
                        start_idx = output_str.find('{')
                        end_idx = output_str.rfind('}')
                        if start_idx >= 0 and end_idx > start_idx:
                            json_str = output_str[start_idx:end_idx+1]
                            logger.info(f"Extracted JSON by braces: {json_str[:100]}...")
                        else:
                            json_str = output_str  # Fallback to the original string
                else:
                    # If not in code blocks, try to find JSON object directly
                    start_idx = output_str.find('{')
                    end_idx = output_str.rfind('}')
                    if start_idx >= 0 and end_idx > start_idx:
                        json_str = output_str[start_idx:end_idx+1]
                        logger.info(f"Extracted JSON by braces: {json_str[:100]}...")
                    else:
                        json_str = output_str  # Fallback to the original string

                # Try to parse the JSON
                try:
                    # Clean the JSON string to handle potential issues
                    # Remove any trailing commas before closing braces/brackets
                    clean_json_str = re.sub(r',\s*([\]\}])', r'\1', json_str)
                    # Remove any control characters that might cause parsing issues
                    clean_json_str = re.sub(r'[\x00-\x1F\x7F]', '', clean_json_str)

                    analysis = json.loads(clean_json_str)
                    intent = analysis.get("intent", "kb_query")
                    details = analysis.get("details", {})
                    logger.info(f"Successfully parsed JSON: {intent}, {details}")
                except json.JSONDecodeError as json_err:
                    # Try to find JSON object in the string
                    logger.warning(f"JSON parse error: {json_err}. Trying to extract JSON object.")
                    # Log the problematic JSON string for debugging
                    logger.warning(f"Problematic JSON string: {json_str[:200]}")

                    # Try a more aggressive approach to extract valid JSON
                    start_idx = output_str.find('{')
                    end_idx = output_str.rfind('}')
                    if start_idx >= 0 and end_idx > start_idx:
                        json_str = output_str[start_idx:end_idx+1]
                        try:
                            # Apply the same cleaning as above
                            clean_json_str = re.sub(r',\s*([\]\}])', r'\1', json_str)
                            clean_json_str = re.sub(r'[\x00-\x1F\x7F]', '', clean_json_str)

                            analysis = json.loads(clean_json_str)
                            intent = analysis.get("intent", "kb_query")
                            details = analysis.get("details", {})
                            logger.info(f"Successfully parsed extracted JSON: {intent}, {details}")
                        except json.JSONDecodeError as inner_err:
                            logger.error(f"Failed to parse extracted JSON: {inner_err}. JSON: {json_str[:100]}...")
                            # Try one more approach - use regex to find a valid JSON object
                            try:
                                json_pattern = re.compile(r'\{[^\{\}]*((\{[^\{\}]*\})|[^\{\}])*\}')
                                match = json_pattern.search(output_str)
                                if match:
                                    potential_json = match.group(0)
                                    analysis = json.loads(potential_json)
                                    intent = analysis.get("intent", "kb_query")
                                    details = analysis.get("details", {})
                                    logger.info(f"Successfully parsed regex-extracted JSON: {intent}, {details}")
                                else:
                                    intent = "kb_query"  # Default to kb_query
                                    details = {"query": user_query}
                            except Exception:
                                intent = "kb_query"  # Default to kb_query
                                details = {"query": user_query}
                    else:
                        logger.error(f"Could not find JSON object in string: {output_str[:100]}...")
                        intent = "kb_query"  # Default to kb_query
                        details = {"query": user_query}
            # If the output is already a dict, use it directly
            elif isinstance(analyzer_result.final_output, dict):
                analysis = analyzer_result.final_output
                intent = analysis.get("intent", "kb_query")
                details = analysis.get("details", {})
            # If the output is an AnalysisResult object, use its attributes
            elif hasattr(analyzer_result.final_output, "intent") and hasattr(analyzer_result.final_output, "details"):
                intent = analyzer_result.final_output.intent
                details = analyzer_result.final_output.details
            else:
                logger.warning(f"Unexpected analyzer output type: {type(analyzer_result.final_output)}")
                intent = "kb_query"  # Default to kb_query
                details = {"query": user_query}
        except Exception as e:
            logger.error(f"Error parsing analyzer output: {e}")
            intent = "kb_query"  # Default to kb_query
            details = {"query": user_query}

        # Add the original query to the details if not already present
        if "query" not in details:
            details["query"] = user_query

        # Log the determined intent and details
        logger.info(f"Intent determined by analyzer: {intent}")
        logger.info(f"Details determined by analyzer: {details}")

        # Enhanced intent determination using our sophisticated LLM-based approach
        logger.info("Using enhanced intent determination with LLM capabilities")

        # Prepare tasks for parallel execution
        parallel_tasks = []
        template_task = None
        temp_file_tasks = []

        # Add template retrieval task if a template is selected
        template_content = None
        if template_to_populate and template_to_populate.strip():
            logger.info(f"Preparing to retrieve template: {template_to_populate}")
            template_task = Runner.run(
                data_gathering_agent_minimal,
                input=f"Retrieve template content named '{template_to_populate}'.",
                context=workflow_context
            )
            parallel_tasks.append(template_task)

        # Add temporary file processing and analysis tasks
        document_analyses = []
        if temp_files_info:
            logger.info(f"Preparing to analyze {len(temp_files_info)} temporary files in parallel")
            for temp_file in temp_files_info:
                # Create a task for processing the temporary file
                temp_context = workflow_context.copy()
                temp_context["temp_file_info"] = temp_file
                temp_file_task = Runner.run(
                    data_gathering_agent_minimal,
                    input=f"Process temporary file: {temp_file['filename']}",
                    context=temp_context
                )
                temp_file_tasks.append((temp_file, temp_file_task))
                parallel_tasks.append(temp_file_task)

        # Execute all tasks in parallel
        if parallel_tasks:
            logger.info(f"Executing {len(parallel_tasks)} tasks in parallel")
            # Store the results of asyncio.gather in a variable
            gathered_results = await asyncio.gather(*parallel_tasks)

            # Create a mapping of tasks to their results
            task_results = {}
            for i, task in enumerate(parallel_tasks):
                task_results[task] = gathered_results[i]

            # Process template result if available
            if template_task:
                try:
                    # Get the result from the task_results mapping
                    template_res_raw = task_results[template_task]
                    template_data = template_res_raw.final_output

                    if isinstance(template_data, RetrievalSuccess):
                        template_content = template_data.content
                        logger.info(f"Retrieved template content for intent analysis. Length: {len(template_content)}")
                    else:
                        logger.warning(f"Could not retrieve template content for intent analysis: {template_data.error_message if isinstance(template_data, RetrievalError) else 'Unknown error'}")
                except Exception as e:
                    logger.error(f"Error retrieving template content for intent analysis: {e}")

            # Process temporary file results and create analysis tasks
            analysis_tasks = []
            for temp_file, temp_file_task in temp_file_tasks:
                try:
                    # Get the result from the task_results mapping
                    temp_data_raw = task_results[temp_file_task]
                    temp_data = temp_data_raw.final_output

                    if isinstance(temp_data, RetrievalSuccess):
                        # Create a task for analyzing the document structure
                        logger.info(f"Preparing document analysis for: {temp_file['filename']}")
                        analysis_task = Runner.run(
                            template_analyzer_agent_minimal,
                            input=json.dumps({
                                "document_content": temp_data.content,
                                "document_name": temp_file['filename']
                            }),
                            context=workflow_context
                        )
                        analysis_tasks.append((temp_file, analysis_task))
                    else:
                        logger.warning(f"Could not process temp file {temp_file['filename']}: {temp_data.error_message if isinstance(temp_data, RetrievalError) else 'Unknown error'}")
                except Exception as e:
                    logger.error(f"Error processing temporary file {temp_file['filename']}: {e}", exc_info=True)

            # Execute all analysis tasks in parallel
            if analysis_tasks:
                logger.info(f"Executing {len(analysis_tasks)} document analysis tasks in parallel")
                analysis_parallel_tasks = [task for _, task in analysis_tasks]
                analysis_results = await asyncio.gather(*analysis_parallel_tasks)

                # Create a mapping of analysis tasks to their results
                analysis_task_results = {}
                for i, task in enumerate(analysis_parallel_tasks):
                    analysis_task_results[task] = analysis_results[i]

                # Process analysis results
                for i, (temp_file, analysis_task) in enumerate(analysis_tasks):
                    try:
                        # Get the result from the analysis_task_results mapping
                        analysis_raw = analysis_task_results[analysis_task]
                        analysis = analysis_raw.final_output

                        if isinstance(analysis, DocumentAnalysis):
                            document_analyses.append(analysis)
                            logger.info(f"Document analysis successful for {temp_file['filename']}: {analysis.doc_type} (confidence: {analysis.confidence})")
                        else:
                            logger.warning(f"Document analysis returned unexpected type: {type(analysis)}")
                    except Exception as e:
                        logger.error(f"Error analyzing document {temp_file['filename']}: {e}", exc_info=True)

        # Add document analyses to workflow context for use by other functions
        if document_analyses:
            workflow_context["document_analyses"] = document_analyses
            logger.info(f"Added {len(document_analyses)} document analyses to workflow context")

        # Determine the final intent using our sophisticated approach
        final_intent, final_details = await determine_final_intent(
            analyzer_intent=intent,
            analyzer_details=details,
            user_query=user_query,
            template_name=template_to_populate if template_to_populate else None,
            template_content=template_content,
            temp_files_info=temp_files_info,
            history=history,
            client=current_client,
            model=model,
            document_analyses=document_analyses
        )

        # Record the intent determination for analysis
        await record_intent_determination(
            user_query=user_query,
            determined_intent=final_intent,
            analyzer_intent=intent,
            intent_scores={},  # We'll populate this in a future update
            final_workflow=None  # This will be set after workflow execution
        )

        # Update intent and details with the final determination
        intent = final_intent
        details = final_details

        logger.info(f"Final intent determination: {intent}")
        logger.info(f"Final intent details: {details}")

        # Check if we need clarification before proceeding
        if details.get("needs_clarification", False) and details.get("clarification_message"):
            logger.info(f"Low confidence intent detection. Asking for clarification.")
            clarification_message = details["clarification_message"]
            confidence = details.get("confidence", 0.0)

            # Return the clarification message to the user
            return {
                "markdown_response": f"{clarification_message}\n\n(Confidence: {confidence:.2f})",
                "intent": intent,
                "details": details
            }

        # If we're proceeding with template population, prepare for it
        if intent == "populate_template" and template_to_populate:
            logger.info(f"Template population confirmed for: '{template_to_populate}'")
            # Get the template content if we haven't already
            if not template_content:
                # Get the template content using the minimal data gathering agent
                template_res_raw = await Runner.run(
                    data_gathering_agent_minimal,
                    input=f"Retrieve template content named '{template_to_populate}'.",
                    context=workflow_context
                )
                template_data = template_res_raw.final_output

                if not isinstance(template_data, RetrievalSuccess):
                    raise ValueError(f"Template Retrieval Failed: {template_data.error_message if isinstance(template_data, RetrievalError) else 'Unknown error'}")

                template_content = template_data.content

            template_name = template_to_populate

            # Use the required fields from the intent determination
            required_fields = details.get("required_fields", [])

            # If no required fields were provided, detect them from the template
            if not required_fields:
                logger.info(f"No required fields provided, detecting from template: {template_name}")
                # Call detect_required_fields_from_template helper function
                required_fields = await detect_required_fields_from_template(template_content, template_name)

            # Normalize field names (convert to lowercase and replace spaces with underscores)
            normalized_fields = []
            for field in required_fields:
                if isinstance(field, str):
                    # Convert to lowercase and replace spaces with underscores
                    normalized_field = field.lower().replace(" ", "_")
                    normalized_fields.append(normalized_field)
                else:
                    # If not a string, just add it as is (shouldn't happen, but just in case)
                    normalized_fields.append(field)

            logger.info(f"Using fields for template '{template_name}': {normalized_fields}")
            details = {"template_name": template_to_populate, "required_fields": normalized_fields}
            required_fields = normalized_fields

        # 2. Execute based on Intent
        if intent == "populate_template":
            logger.info("Executing Template Population Workflow...")
            template_name = details.get("template_name")
            required_fields = details.get("required_fields", [])
            if not template_name:
                # If template name is missing but user mentioned a template, provide a helpful error message
                if "template" in user_query.lower() or "modèle" in user_query.lower():
                    return "I noticed you mentioned using a template, but no template was selected. Please select a template from the dropdown menu and try again."
                else:
                    raise ValueError("Missing template name for template population workflow.")

            if not required_fields:
                raise ValueError("Missing required fields for template population.")

            # a. Get Template Content directly via retrieve_template_content tool
            logger.info(f"Gathering template: {template_name}")
            # Get the template content using the minimal data gathering agent
            template_res_raw = await Runner.run(
                data_gathering_agent_minimal,
                input=f"Retrieve template content named '{template_name}'.",
                context=workflow_context
            )
            template_data = template_res_raw.final_output
            if isinstance(template_data, RetrievalError): raise Exception(f"Template Retrieval Failed: {template_data.error_message}")
            if not isinstance(template_data, RetrievalSuccess): raise TypeError(f"Expected RetrievalSuccess for template, got {type(template_data)}")
            template_content = template_data.content

            # b. Gather Context Sources for Extraction (Last User Message + All Temp Files + Relevant KB Content)
            # Only include the last user message from history to reduce noise
            last_user_message = next((msg.get('content') for msg in reversed(history) if msg.get('role') == 'user'), '')
            context_sources_text = [f"User Request: {user_query}", f"Previous User Message: {last_user_message}"] if last_user_message != user_query else [f"User Request: {user_query}"]

            # Add template name and required fields as context
            context_sources_text.append(f"Template Being Populated: {template_name}")
            context_sources_text.append(f"Required Fields: {', '.join(required_fields)}")

            # Process temporary files
            if temp_files_info:
                for temp_file in temp_files_info:
                    logger.info(f"Gathering temporary file content: {temp_file['filename']}")
                    # Process temporary file using the minimal data gathering agent
                    # Add temp file info to the context
                    temp_context = workflow_context.copy()
                    temp_context["temp_file_info"] = temp_file
                    temp_data_raw = await Runner.run(
                        data_gathering_agent_minimal,
                        input=f"Process temporary file: {temp_file['filename']}",
                        context=temp_context
                    )
                    temp_data = temp_data_raw.final_output
                    if isinstance(temp_data, RetrievalSuccess):
                        # Format the content more clearly to help extraction
                        # Use the original content directly without any preprocessing
                        enhanced_content = temp_data.content

                        # Log the content for debugging
                        logger.info(f"Using semantic content from {temp_file['filename']}: {enhanced_content[:500]}...")
                        context_sources_text.append(f"\n\n### Document Content from: {temp_file['filename']}\n{enhanced_content}")
                    else:
                        logger.warning(f"Could not process temp file {temp_file['filename']}: {temp_data.error_message if isinstance(temp_data, RetrievalError) else 'Unknown error'}")

            # Fetch relevant KB content for data extraction if needed
            # This helps with extracting data that might be in the knowledge base but not in the temp files
            try:
                # Determine if we need KB content based on required fields
                needs_kb_content = any(field in ["employer_name", "job_title", "salary", "contract_duration", "probation_period", "notice_period"] for field in required_fields)

                if needs_kb_content:
                    logger.info("Fetching relevant KB content to assist with data extraction")
                    kb_context = workflow_context.copy()

                    # Create a targeted query based on required fields
                    field_query = " ".join(required_fields)
                    kb_query = f"Information about {field_query} in employment contracts or documents"

                    # Get KB content using the minimal data gathering agent
                    kb_data_raw = await Runner.run(
                        data_gathering_agent_minimal,
                        input=f"Get KB content about 'labor_code' related to: {kb_query}",
                        context=kb_context
                    )
                    kb_data = kb_data_raw.final_output

                    if isinstance(kb_data, RetrievalSuccess):
                        kb_content = kb_data.content
                        logger.info(f"Retrieved KB content for data extraction. Length: {len(kb_content)}")
                        context_sources_text.append(f"\n\n### Relevant Knowledge Base Information:\n{kb_content}")
                    elif isinstance(kb_data, RetrievalError):
                        logger.warning(f"KB retrieval for data extraction failed: {kb_data.error_message}")
                        # Add some basic information that might help with extraction
                        context_sources_text.append("\n\n### Relevant Background Information:\n" +
                                               "- Standard employment contracts in Morocco typically include employee name, job title, salary, " +
                                               "start date, work location, and contract duration.\n" +
                                               "- Moroccan Labor Code specifies probation periods of 3 months for executives, " +
                                               "1.5 months for employees, and 15 days for workers.\n" +
                                               "- Standard notice periods are 1 month for executives, 15 days for employees, and 8 days for workers.")
            except Exception as kb_err:
                logger.error(f"Error fetching KB content for data extraction: {kb_err}")
                # Continue without KB content

            # c. Extract Data via DataExtractorAgent (Agent has NO output_type)
            logger.info(f"Requesting data extraction for fields: {required_fields}")

            # Prepare document analyses for extraction if available
            extraction_doc_analyses = None
            if document_analyses:
                # Convert DocumentAnalysis objects to dictionaries for the tool
                extraction_doc_analyses = []
                for analysis in document_analyses:
                    analysis_dict = {
                        "doc_type": analysis.doc_type,
                        "confidence": analysis.confidence,
                        "key_sections": analysis.key_sections,
                        "language": analysis.language,
                        "metadata": analysis.metadata
                    }
                    extraction_doc_analyses.append(analysis_dict)
                logger.info(f"Passing {len(extraction_doc_analyses)} document analyses to extraction")

            # Create input with document analyses if available
            extractor_input = {
                "context_sources": context_sources_text,
                "required_fields": required_fields,
                "document_analyses": extraction_doc_analyses
            }

            # Pass context in case the tool implementation needs it in the future
            extractor_agent_run_result = await Runner.run(data_extractor_agent, input=json.dumps(extractor_input), context=workflow_context)

            # --- DIRECT EXTRACTION from new_items ---
            # Since we're using tool_use_behavior="stop_on_first_tool", the tool output should be in new_items
            logger.info("Searching for tool output in new_items")

            # Initialize with default values in case we can't find the tool output
            extracted_data_obj = None

            # Log all new_items for debugging
            logger.debug(f"Number of new_items: {len(extractor_agent_run_result.new_items)}")
            for i, item in enumerate(extractor_agent_run_result.new_items):
                logger.debug(f"Item {i} type: {type(item)}")
                if hasattr(item, '__dict__'):
                    logger.debug(f"Item {i} attributes: {item.__dict__}")

            # Try to find the tool output in new_items
            tool_output_item = None
            for item in extractor_agent_run_result.new_items:
                # Log detailed information about each item for debugging
                logger.info(f"Examining item: {item}")
                if hasattr(item, '__dict__'):
                    logger.info(f"Item attributes: {item.__dict__}")

                # Check for different attribute names that might contain the tool name
                if hasattr(item, 'tool_name') and item.tool_name == "extract_data_for_template":
                    tool_output_item = item
                    logger.info(f"Found tool output item with tool_name 'extract_data_for_template'")
                    break
                elif hasattr(item, 'name') and item.name == "extract_data_for_template":
                    tool_output_item = item
                    logger.info(f"Found tool output item with name 'extract_data_for_template'")
                    break
                elif hasattr(item, 'function_name') and item.function_name == "extract_data_for_template":
                    tool_output_item = item
                    logger.info(f"Found tool output item with function_name 'extract_data_for_template'")
                    break
                # Check for any item that has an output attribute that looks like ExtractedData
                elif hasattr(item, 'output') and hasattr(item.output, 'data') and hasattr(item.output, 'status'):
                    tool_output_item = item
                    logger.info(f"Found tool output item with output that looks like ExtractedData")
                    break

            # If we found the tool output, use it
            if tool_output_item and hasattr(tool_output_item, 'output'):
                logger.info(f"Using tool output: {tool_output_item.output}")
                if isinstance(tool_output_item.output, ExtractedData):
                    extracted_data_obj = tool_output_item.output
                else:
                    logger.warning(f"Tool output is not ExtractedData: {type(tool_output_item.output)}")
                    # Try to convert it to ExtractedData
                    try:
                        if isinstance(tool_output_item.output, dict):
                            extracted_data_obj = ExtractedData(**tool_output_item.output)
                        else:
                            logger.warning(f"Cannot convert tool output to ExtractedData: {tool_output_item.output}")
                    except Exception as e:
                        logger.error(f"Error converting tool output to ExtractedData: {e}")

            # If we couldn't find or use the tool output, try to extract from the final_output
            if not extracted_data_obj:
                logger.warning("Could not find or use tool output in new_items, trying final_output")
                try:
                    # The final_output might be a string containing the extracted data
                    final_output = extractor_agent_run_result.final_output
                    logger.info(f"Final output type: {type(final_output)}, value: {final_output}")

                    # If it's already an ExtractedData object, use it directly
                    if isinstance(final_output, ExtractedData):
                        extracted_data_obj = final_output
                        logger.info("Found ExtractedData object in final_output")
                    # If it's a dict that looks like ExtractedData, convert it
                    elif isinstance(final_output, dict) and ('data' in final_output or 'status' in final_output):
                        extracted_data_obj = ExtractedData(**final_output)
                        logger.info("Converted dict to ExtractedData object")
                    # If it's a string, try to parse it as JSON
                    elif isinstance(final_output, str):
                        # Try to extract JSON from the string (it might be wrapped in text)
                        json_start = final_output.find('{')
                        json_end = final_output.rfind('}')
                        if json_start >= 0 and json_end > json_start:
                            json_str = final_output[json_start:json_end+1]
                            try:
                                # Try to parse as JSON
                                data_dict = json.loads(json_str)
                                if isinstance(data_dict, dict) and 'data' in data_dict:
                                    extracted_data_obj = ExtractedData(**data_dict)
                                    logger.info("Parsed JSON with data field from final_output")
                                elif isinstance(data_dict, dict):
                                    extracted_data_obj = ExtractedData(data=data_dict, status="success")
                                    logger.info("Parsed JSON and created ExtractedData from final_output")
                            except json.JSONDecodeError as json_err:
                                logger.warning(f"Failed to parse JSON from final_output: {json_err}")
                        else:
                            logger.warning("No JSON object found in final_output string")
                except Exception as e:
                    logger.error(f"Error extracting data from final_output: {e}")

            # If extraction failed, use agent-based recovery
            if not extracted_data_obj or not isinstance(extracted_data_obj, ExtractedData):
                logger.warning("Initial extraction failed, using agent-based recovery")

                recovery_agent = Agent(
                    name="ExtractionRecoveryAgent",
                    instructions="""You are an expert at recovering from failed extractions.
                    When initial extraction fails, you:
                    1. Analyze why it failed
                    2. Use alternative semantic approaches
                    3. Look for information in unexpected places
                    4. Never use patterns or rules - only semantic understanding

                    Provide the extracted data in a structured format.""",
                    model="gpt-4o"
                )

                recovery_prompt = f"""
                Extraction failed for fields: {required_fields}
                Available documents:
                {json.dumps([f['filename'] for f in temp_files_info], indent=2)}

                Document contents:
                {context_sources_text}

                Extract the required information using semantic understanding only.
                """

                recovery_result = await Runner.run(recovery_agent, input=recovery_prompt)

                # Parse recovery result into ExtractedData
                if isinstance(recovery_result.final_output, dict):
                    extracted_data_obj = ExtractedData(
                        data=recovery_result.final_output,
                        status="success"
                    )
                else:
                    # Fallback to empty data if recovery fails
                    extracted_data_obj = ExtractedData(
                        data={field: None for field in required_fields},
                        status="error",
                        error_message="Recovery failed"
                    )

            logger.info(f"Using extracted data: {extracted_data_obj}")
            # --- End Direct Extraction ---

            # Check the Pydantic object created from parsing
            if isinstance(extracted_data_obj, ExtractedData) and extracted_data_obj.status == "success":
                extracted_data = extracted_data_obj.data
                logger.info(f"Data extraction successful: {extracted_data}")
                if not extracted_data:
                    logger.warning("Data extraction returned no data fields.")
                if not isinstance(extracted_data, dict):
                    logger.error(f"Extracted data is not a dictionary: {type(extracted_data)}")
                    extracted_data = {}  # Default to empty dict
            else:  # Handle errors reported by the tool OR failure to parse
                err_msg = extracted_data_obj.error_message if isinstance(extracted_data_obj, ExtractedData) else 'Could not find/parse valid tool result.'
                logger.error(f"Data Extraction Failed overall: {err_msg}")
                # Proceed with empty data for populator
                extracted_data = {}
            # --- End Robust Checking ---

            # Check if we need to fetch KB content for template population
            kb_content = ""
            if "code de travail" in user_query.lower() or "labor code" in user_query.lower() or "compliance" in user_query.lower():
                logger.info("Fetching relevant KB content for template population based on user query")
                kb_context = workflow_context.copy()

                # Create a more targeted query based on specific fields mentioned in the user query
                # Extract key terms from the user query to make the KB search more focused
                query_terms = []
                if "salary" in user_query.lower() or "compensation" in user_query.lower() or "payment" in user_query.lower():
                    query_terms.append("salary requirements")
                if "hours" in user_query.lower() or "time" in user_query.lower() or "schedule" in user_query.lower():
                    query_terms.append("working hours")
                if "notice" in user_query.lower() or "termination" in user_query.lower():
                    query_terms.append("notice period")
                if "probation" in user_query.lower() or "trial" in user_query.lower():
                    query_terms.append("probation period")

                # If no specific terms found, use a more general but still focused query
                if not query_terms:
                    # Limit to just a few most important fields instead of all fields
                    important_fields = [field for field in required_fields if field in [
                        "employee_name", "employer_name", "job_title", "salary",
                        "start_date", "probation_period", "notice_period"
                    ]][:3]  # Limit to top 3 important fields

                    if important_fields:
                        kb_query = f"Key legal requirements for {', '.join(important_fields)} in employment contracts"
                    else:
                        kb_query = "Essential legal requirements for employment contracts"
                else:
                    kb_query = f"Legal requirements for {', '.join(query_terms)}"

                logger.info(f"Using optimized KB query: {kb_query}")

                # Get KB content using the minimal data gathering agent with a limit on results
                kb_data_raw = await Runner.run(
                    data_gathering_agent_minimal,
                    input=f"Get KB content about 'labor_code' related to: {kb_query}. Return ONLY the most relevant 2-3 paragraphs.",
                    context=kb_context
                )
                kb_data = kb_data_raw.final_output

                if isinstance(kb_data, RetrievalSuccess):
                    # Extract only the most relevant sentences (up to 1000 characters)
                    full_content = kb_data.content
                    if len(full_content) > 1000:
                        # Split into sentences and take the most relevant ones
                        sentences = re.split(r'(?<=[.!?]) +', full_content)
                        # Prioritize sentences that mention terms from the query
                        relevant_sentences = []
                        for sentence in sentences:
                            if any(term in sentence.lower() for term in kb_query.lower().split()):
                                relevant_sentences.append(sentence)

                        # If we have relevant sentences, use them; otherwise take the first few
                        if relevant_sentences:
                            kb_content = " ".join(relevant_sentences[:5])  # Take up to 5 relevant sentences
                        else:
                            kb_content = " ".join(sentences[:3])  # Take first 3 sentences as fallback
                    else:
                        kb_content = full_content

                    logger.info(f"Retrieved and optimized KB content for template population. Original length: {len(full_content)}, Optimized length: {len(kb_content)}")
                elif isinstance(kb_data, str):
                    # Also limit string content
                    if len(kb_data) > 1000:
                        sentences = re.split(r'(?<=[.!?]) +', kb_data)
                        kb_content = " ".join(sentences[:3])
                    else:
                        kb_content = kb_data
                    logger.info(f"Retrieved KB content as string. Original length: {len(kb_data)}, Used length: {len(kb_content)}")
                elif isinstance(kb_data, RetrievalError):
                    # If we can't find content in the KB, provide a minimal generic message
                    logger.warning(f"KB retrieval failed: {kb_data.error_message}. Using minimal fallback information.")
                    kb_content = "No specific legal requirements found in the knowledge base. Proceeding with standard template population."
                else:
                    logger.warning(f"Unexpected KB data type: {type(kb_data)}")

            # d. Populate Template via TemplatePopulationAgent
            logger.info(f"Populating template '{template_name}' with extracted data: {json.dumps(extracted_data, indent=2)}")
            populator_input_dict = {
                "template": template_content,
                "data": extracted_data,
                "user_query": user_query,  # Include the user's original query
                "kb_content": kb_content  # Include any KB content we retrieved
            }
            # Input for populator includes the template, data, user query, and KB content
            populator_res_raw = await Runner.run(template_populator_agent, input=json.dumps(populator_input_dict))
            populated_markdown = extract_final_answer(populator_res_raw)

            # e. Generate DOCX file from populated markdown
            try:
                logger.info(f"Generating DOCX file for template '{template_name}'")
                # Generate DOCX using the minimal DOCX generation agent
                docx_result = await Runner.run(
                    docx_generation_agent_minimal,
                    input=json.dumps({
                        "markdown_content": populated_markdown,
                        "template_name": template_name
                    }),
                    context=workflow_context
                )
                docx_generation_result = docx_result.final_output

                # Add download link to response if successful
                if docx_generation_result and hasattr(docx_generation_result, 'status') and docx_generation_result.status == "success":
                    download_link = f"/download_docx/{docx_generation_result.file_name}"
                    final_markdown_response = populated_markdown + "\n\n---\n\n" + f"[Download as DOCX]({download_link})"
                    logger.info(f"DOCX generation successful. Download link: {download_link}")
                else:
                    # Fall back to text-only if DOCX generation fails
                    error_message = getattr(docx_generation_result, 'message', 'Unknown error') if docx_generation_result else 'Failed to generate DOCX'
                    final_markdown_response = populated_markdown + "\n\n---\n\n" + "DOCX generation failed. Using text version only."
                    logger.warning(f"DOCX generation failed: {error_message}")
            except Exception as docx_err:
                logger.error(f"Error during DOCX generation: {docx_err}", exc_info=True)
                final_markdown_response = populated_markdown + "\n\n---\n\n" + "DOCX generation failed. Using text version only."

        elif intent == "kb_query_with_temp_context":
            # New workflow for queries that need both KB and temporary file context
            logger.info("Executing KB Query with Temp Context Workflow...")

            # 1. Get the query topic from the analyzer's details
            query_topic = details.get("query_topic", user_query)
            logger.info(f"Query topic: {query_topic}")

            # 2. Get the temporary file names from the analyzer's details
            temp_file_names = details.get("temp_file_names", [])
            if not temp_file_names and temp_files_info:
                temp_file_names = [f['filename'] for f in temp_files_info]
            logger.info(f"Temporary file names: {temp_file_names}")

            # 3. Process temporary files
            temp_contexts = []
            for temp_file in temp_files_info:
                if temp_file['filename'] in temp_file_names: # Check if file is relevant
                    logger.info(f"Processing temporary file: {temp_file['filename']}")
                    # Process temporary file using the minimal data gathering agent
                    # Add temp file info to the context
                    temp_context = workflow_context.copy()
                    temp_context["temp_file_info"] = temp_file
                    temp_data_raw = await Runner.run(
                        data_gathering_agent_minimal,
                        input=f"Process temporary file: {temp_file['filename']}",
                        context=temp_context
                    )
                    temp_data = temp_data_raw.final_output
                    if isinstance(temp_data, RetrievalSuccess):
                        temp_contexts.append(f"### Context from Uploaded: {temp_file['filename']}\n{temp_data.content}")
                    else:
                        temp_contexts.append(f"### Error processing {temp_file['filename']}:\n{temp_data.error_message if isinstance(temp_data, RetrievalError) else 'Unknown Error'}")

            # 4. Get KB content
            logger.info(f"Getting KB content for query: {query_topic}")
            kb_context = workflow_context.copy()
            # Get KB content using the minimal data gathering agent
            kb_data_raw = await Runner.run(
                data_gathering_agent_minimal,
                input=f"Get KB content about 'general' related to: {query_topic}",
                context=kb_context
            )
            kb_data = kb_data_raw.final_output

            # 5. Combine contexts
            combined_context = []
            if isinstance(kb_data, RetrievalSuccess):
                combined_context.append(f"### Knowledge Base Content:\n{kb_data.content}")
            elif isinstance(kb_data, str):
                combined_context.append(f"### Knowledge Base Content:\n{kb_data}")
            else:
                logger.warning(f"Unexpected KB data type: {type(kb_data)}")

            combined_context.extend(temp_contexts)
            combined_context_str = "\n\n".join(combined_context)

            # 6. Run the final synthesizer
            logger.info("Running final synthesizer with combined context")
            synthesis_messages = history + [{"role": "user", "content": f"Answer based on the following document context(s).\n\n{combined_context_str}\n\n### Query:\n{user_query}"}]
            final_synth_raw = await Runner.run(final_synthesizer_agent, input=synthesis_messages)
            final_markdown_response = extract_final_answer(final_synth_raw)

        elif intent == "analyze_template":
            # New workflow for analyzing templates rather than just filling them
            logger.info("Executing Template Analysis Workflow...")

            # 1. Get the template content
            template_name = details.get("template_name", template_to_populate)
            if not template_name:
                raise ValueError("Template name missing for analysis")

            logger.info(f"Gathering template for analysis: {template_name}")
            # Get the template content using the minimal data gathering agent
            template_res_raw = await Runner.run(
                data_gathering_agent_minimal,
                input=f"Retrieve template content named '{template_name}'.",
                context=workflow_context
            )
            template_data = template_res_raw.final_output

            if not isinstance(template_data, RetrievalSuccess):
                raise ValueError(f"Template Retrieval Failed: {template_data.error_message if isinstance(template_data, RetrievalError) else 'Unknown error'}")

            template_content = template_data.content

            # 2. Get KB content if needed
            query_topic = details.get("query_topic", user_query)
            kb_content = ""
            if details.get("needs_kb_lookup", True):  # Default to True for template analysis
                logger.info(f"Getting KB content for template analysis: {query_topic}")
                kb_context = workflow_context.copy()

                # Create a query based on the template and user query
                kb_query = query_topic

                # If the query mentions labor code, focus on that
                if "code de travail" in user_query.lower() or "labor code" in user_query.lower() or "travail" in user_query.lower():
                    kb_query = "Labor Code relevant to employment contracts and legal requirements"
                # If the query mentions invoices or billing, focus on that
                elif "invoice" in user_query.lower() or "facture" in user_query.lower() or "billing" in user_query.lower():
                    kb_query = "Information about invoices, billing, and payment requirements"
                # Otherwise, create a general query based on the template name
                else:
                    kb_query = f"Information about {template_name} templates and requirements"

                # Get KB content using the minimal data gathering agent
                kb_data_raw = await Runner.run(
                    data_gathering_agent_minimal,
                    input=f"Get KB content about 'general' related to: {kb_query}",
                    context=kb_context
                )
                kb_data = kb_data_raw.final_output

                if isinstance(kb_data, RetrievalSuccess):
                    kb_content = kb_data.content
                    logger.info(f"Retrieved KB content for template analysis. Length: {len(kb_content)}")
                elif isinstance(kb_data, str):
                    kb_content = kb_data
                    logger.info(f"Retrieved KB content as string. Length: {len(kb_content)}")
                elif isinstance(kb_data, RetrievalError):
                    # Provide generic fallback content if KB retrieval fails
                    logger.warning(f"KB retrieval for template analysis failed: {kb_data.error_message}")
                    kb_content = """
                    General Document Information:
                    - This is a generic fallback message because no specific information was found in the knowledge base
                    - The system will proceed with template analysis using only the template content
                    - For more specific information, please upload relevant documents or provide more details in your query
                    """

            # 3. Process temporary files if needed
            temp_content = ""
            if details.get("needs_temp_files", False) and temp_files_info:
                temp_contexts = []
                for temp_file in temp_files_info:
                    logger.info(f"Processing temporary file for template analysis: {temp_file['filename']}")
                    # Process temporary file using the minimal data gathering agent
                    # Add temp file info to the context
                    temp_context = workflow_context.copy()
                    temp_context["temp_file_info"] = temp_file
                    temp_data_raw = await Runner.run(
                        data_gathering_agent_minimal,
                        input=f"Process temporary file: {temp_file['filename']}",
                        context=temp_context
                    )
                    temp_data = temp_data_raw.final_output
                    if isinstance(temp_data, RetrievalSuccess):
                        temp_contexts.append(f"### Context from Uploaded: {temp_file['filename']}\n{temp_data.content}")
                temp_content = "\n\n".join(temp_contexts)

            # 4. Run the template analyzer agent
            logger.info("Running template analyzer with template content and context")

            # Create a more detailed prompt for the analyzer based on the query type
            analyzer_prompt = f"""Analyze the following template and provide insights based on the user's query.\n\n
            ### Template: {template_name}\n\n{template_content}\n\n"""

            if kb_content:
                analyzer_prompt += f"\n\n### Relevant Knowledge Base Information:\n\n{kb_content}\n\n"

            if temp_content:
                analyzer_prompt += f"\n\n### Uploaded Document Context:\n\n{temp_content}\n\n"

            analyzer_prompt += f"\n\n### User Query:\n{user_query}\n\n"

            # Add special instructions for comparison queries
            if "comparatif" in user_query.lower() or "compare" in user_query.lower() or "comparison" in user_query.lower() or "tableau" in user_query.lower() or "table" in user_query.lower():
                analyzer_prompt += """\nPlease provide a detailed comparison in table format between the template and any relevant requirements or standards.
                For each article or section of the template:
                1. Identify the corresponding requirement or standard
                2. Assess compliance (Compliant, Partially Compliant, Non-Compliant, or Not Specified)
                3. Provide recommendations for improvement if needed

                Format your response as a markdown table with these columns:
                | Article/Section | Template Content | Requirement/Standard | Compliance Status | Recommendation |
                """

            # Use the final synthesizer agent instead of template populator for analysis
            synthesis_messages = history + [{"role": "user", "content": analyzer_prompt}]
            analyzer_res_raw = await Runner.run(final_synthesizer_agent, input=synthesis_messages, context=workflow_context)
            final_markdown_response = extract_final_answer(analyzer_res_raw)

        elif intent == "temp_context_query":
            logger.info("Executing Temporary Context Query Workflow...")
            temp_filenames = details.get("temp_filenames", [])
            query_about_temp = details.get("query", user_query)
            if not temp_filenames or not temp_files_info: raise ValueError("Temp filename missing or file unavailable.")

            # a. Gather content from all temp files
            temp_contexts = []
            for temp_file in temp_files_info:
                 if temp_file['filename'] in temp_filenames: # Check if file is relevant
                    logger.info(f"Gathering temporary file content: {temp_file['filename']}")
                    # Process temporary file using the minimal data gathering agent
                    # Add temp file info to the context
                    temp_context = workflow_context.copy()
                    temp_context["temp_file_info"] = temp_file
                    temp_data_raw = await Runner.run(
                        data_gathering_agent_minimal,
                        input=f"Process temporary file: {temp_file['filename']}",
                        context=temp_context
                    )
                    temp_data = temp_data_raw.final_output
                    if isinstance(temp_data, RetrievalSuccess): temp_contexts.append(f"### Context from Uploaded: {temp_file['filename']}\n{temp_data.content}")
                    else: temp_contexts.append(f"### Error processing {temp_file['filename']}:\n{temp_data.error_message if isinstance(temp_data, RetrievalError) else 'Unknown Error'}")

            # b. Synthesize using combined temp context
            combined_temp_context = "\n\n".join(temp_contexts)
            synthesis_messages = history + [{"role": "user", "content": f"Answer based ONLY on the following document context(s).\n\n{combined_temp_context}\n\n### Query:\n{query_about_temp}"}]
            final_synth_raw = await Runner.run(final_synthesizer_agent, input=synthesis_messages)
            final_markdown_response = extract_final_answer(final_synth_raw)

        else: # Default KB RAG
            logger.info("Executing Standard KB RAG Workflow...")
            query_to_run = details.get("query", user_query)
            # This calls the simplified Planner -> Synthesizer flow
            final_markdown_response = await run_standard_agent_rag(query_to_run, history, workflow_context, vs_id)

    except Exception as workflow_err:
        logger.error(f"Complex Agent workflow failed for VS {vs_id}: {workflow_err}", exc_info=True)
        final_markdown_response = f"Sorry, an error occurred during processing: {html.escape(str(workflow_err))}"

    return final_markdown_response

# --- Modified Chat API Route ---
@app.route('/chat/<chat_id>', methods=['POST'])
async def chat_api(chat_id):
    start_time = time.time()
    if not chat_db: return jsonify({"error": "Database service not available."}), 500

    # Handle both JSON and form data
    user_message = ""
    template_to_populate = None
    temp_files_info = []

    # Check content type to determine how to parse the request
    if request.content_type and 'multipart/form-data' in request.content_type:
        # Handle form data with possible file uploads
        user_message = request.form.get('message', '').strip()
        template_to_populate = request.form.get('template_to_populate')

        # Debug logging
        logger.info(f"Received POST form data - template_to_populate: '{template_to_populate}' (Type: {type(template_to_populate)})")
        logger.info(f"All form data keys: {list(request.form.keys())}")
        logger.info(f"All form data values: {dict(request.form)}")

        # Check if template_to_populate is empty string
        if template_to_populate == '':
            logger.info("template_to_populate is an empty string, setting to None")
            template_to_populate = None

        # Process temporary files if present
        if 'temporary_files[]' in request.files:
            temp_files = request.files.getlist('temporary_files[]')
            for temp_file in temp_files:
                if temp_file and temp_file.filename:
                    # Save the temporary file
                    original_filename = secure_filename(temp_file.filename)
                    temp_filename = str(uuid.uuid4()) + "_" + original_filename
                    temp_path = os.path.join(UPLOAD_FOLDER, temp_filename)
                    try:
                        temp_file.save(temp_path)
                        logger.info(f"Saved temporary file for chat: {original_filename}")
                        temp_files_info.append({
                            'filename': original_filename,
                            'path': temp_path
                        })
                    except Exception as e:
                        logger.error(f"Failed to save temporary file {original_filename}: {e}")
    else:
        # Handle JSON data
        try:
            data = request.get_json()
            if data:
                user_message = data.get('message', '').strip()
                template_to_populate = data.get('template_to_populate')

                # Debug logging
                logger.info(f"Received JSON data - template_to_populate: '{template_to_populate}' (Type: {type(template_to_populate)})")
                logger.info(f"All JSON data keys: {list(data.keys())}")
                logger.info(f"All JSON data values: {data}")

                # Check if template_to_populate is empty string
                if template_to_populate == '':
                    logger.info("template_to_populate is an empty string, setting to None")
                    template_to_populate = None
        except Exception as json_err:
            logger.error(f"Error parsing JSON request: {json_err}")
            return jsonify({"error": "Invalid request format."}), 400

    # Validate message
    if not user_message and not temp_files_info:
        return jsonify({"error": "Message is empty and no files provided."}), 400

    # Get chat details and prepare history
    try:
        chat_details = await asyncio.to_thread(chat_db.get_chat_details, chat_id)
        if not chat_details: return jsonify({"error": "Chat not found."}), 404
        vector_store_id = chat_details.get('vector_store_id')
        if not vector_store_id: logger.error(f"Chat {chat_id} missing VS ID."); return jsonify({"error": "Chat KB link missing."}), 400

        # Add user message to database
        display_message = user_message
        if not display_message and temp_files_info:
            display_message = f"[Uploaded {len(temp_files_info)} file(s): {', '.join(f['filename'] for f in temp_files_info)}]"
        await asyncio.to_thread(chat_db.add_message, chat_id, 'user', display_message)

        # Get message history for context
        message_history_db = await asyncio.to_thread(chat_db.get_messages, chat_id, limit=10)
        history_for_workflow = [{"role": msg["role"], "content": msg["content"]} for msg in message_history_db if msg["role"] != 'user'][-6:]
    except Exception as db_err:
        logger.error(f"DB error pre-processing {chat_id}: {db_err}", exc_info=True)
        # Clean up any temporary files
        for temp_file in temp_files_info:
            try:
                if os.path.exists(temp_file['path']):
                    os.remove(temp_file['path'])
            except Exception as cleanup_err:
                logger.error(f"Failed to clean up temp file: {cleanup_err}")
        return jsonify({"error": "Database error."}), 500

    logger.info(f"Agent Chat API Start: ChatID={chat_id}, VSID={vector_store_id}, Query='{user_message[:50]}...', Files: {len(temp_files_info)}")
    response_content_html = "<p>An error occurred processing your request.</p>" # Default

    try:
        # --- Run the NEW Agent Workflow ---
        final_markdown_response = await run_complex_rag_workflow(
            user_query=user_message,
            vs_id=vector_store_id,
            history=history_for_workflow,
            temp_files_info=temp_files_info,
            template_to_populate=template_to_populate,
            chat_id=chat_id
        )
        # --- Convert Final Markdown to HTML ---
        try: response_content_html = markdown.markdown(final_markdown_response, extensions=['fenced_code', 'tables', 'nl2br'])
        except Exception as md_err: logger.error(f"Final Markdown conversion failed: {md_err}"); response_content_html = f"<p>Error formatting response.</p><pre>{html.escape(final_markdown_response)}</pre>"
        # --- Save Assistant Response ---
        await asyncio.to_thread(chat_db.add_message, chat_id, 'assistant', response_content_html)

    except Exception as workflow_err:
        logger.error(f"Agent workflow failed for chat {chat_id}: {workflow_err}", exc_info=True)
        response_content_html = f"<p>Sorry, an error occurred: {html.escape(str(workflow_err))}</p>"
        try: await asyncio.to_thread(chat_db.add_message, chat_id, 'assistant', response_content_html)
        except Exception as db_final_err: logger.error(f"Failed save final error msg to DB {chat_id}: {db_final_err}")
    finally:
        # Clean up temporary files
        for temp_file in temp_files_info:
            try:
                if os.path.exists(temp_file['path']):
                    os.remove(temp_file['path'])
                    logger.debug(f"Cleaned up temporary file: {temp_file['filename']}")
            except Exception as cleanup_err:
                logger.error(f"Failed to clean up temp file: {cleanup_err}")

    logger.info(f"Agent Chat API End: ChatID={chat_id}. Total time: {time.time() - start_time:.2f}s")
    # Return empty sources for agent workflow for now
    return jsonify({"response": response_content_html, "retrieved_sources": []})
# --- End chat_api ---

# --- Define Custom Trace Processor (Using Correct Base Class & Methods) ---
class PrintTraceProcessor(TracingProcessor): # Inherit from TracingProcessor
    """A simple trace processor that prints trace details using logger."""

    def on_trace_start(self, trace: Trace) -> None:
        logger.debug(f"[TRACE START] ID: {trace.trace_id}, Workflow: {trace.workflow_name}, Start: {trace.start_time}")

    def on_trace_end(self, trace: Trace) -> None:
        duration = (trace.end_time - trace.start_time).total_seconds() if trace.end_time and trace.start_time else "N/A"
        error_msg = f", Error: {trace.error}" if trace.error else ""
        logger.debug(f"[TRACE END] ID: {trace.trace_id}, Workflow: {trace.workflow_name}, Duration: {duration}s{error_msg}")

    def on_span_start(self, span: Span[Any]) -> None:
        # Limit input log length
        input_str = str(span.input)[:200] + ('...' if len(str(span.input)) > 200 else '') if span.input else "None"
        logger.debug(f"  [SPAN START] ID: {span.span_id}, Parent: {span.parent_id}, Name: {span.name}, Type: {span.type}, Input: {input_str}")

    def on_span_end(self, span: Span[Any]) -> None:
        duration = (span.end_time - span.start_time).total_seconds() if span.end_time and span.start_time else "N/A"
        error_msg = f", Error: {span.error}" if span.error else ""
        # Limit output log length
        output_str = str(span.output)[:200] + ('...' if len(str(span.output)) > 200 else '') if span.output else "None"
        logger.debug(f"  [SPAN END] ID: {span.span_id}, Name: {span.name}, Duration: {duration}s, Output: {output_str}{error_msg}")

    def shutdown(self) -> None:
        # Called when the application using the SDK is shutting down gracefully.
        logger.debug("[TRACE SHUTDOWN] Trace processor shutting down.")
        # Add any cleanup logic if needed (e.g., flushing buffers to external systems)

    def force_flush(self) -> None:
        # Called to ensure all buffered traces are processed immediately.
        logger.debug("[TRACE FLUSH] Force flush requested.")
        # Add logic to flush buffers if your processor batches data.

# --- End Custom Trace Processor Definition ---

# --- Rename/Delete Chat Routes (Keep as before) ---
@app.route('/rename_chat/<chat_id>', methods=['POST'])
async def rename_chat_route(chat_id):
    if not chat_db:
        flash("DB error.", "error")
        return redirect(url_for('index'))
    new_title = request.form.get('new_title', '').strip()
    if not new_title:
        flash("New title empty.", "error")
    else:
        try:
            success = await asyncio.to_thread(chat_db.rename_chat, chat_id, new_title)
            if success:
                flash(f"Chat renamed.", "success")
            else:
                flash("Rename failed.", "error")
        except Exception as e:
            logger.error(f"Error renaming chat {chat_id}: {e}", exc_info=True)
            flash("Internal error.", "error")
    return redirect(url_for('chat_view', chat_id=chat_id))

@app.route('/delete_chat/<chat_id>', methods=['POST'])
async def delete_chat_route(chat_id):
    if not chat_db:
        flash("DB error.", "error")
        return redirect(url_for('index'))
    try:
        success = await asyncio.to_thread(chat_db.delete_chat, chat_id)
        if success:
            flash("Chat deleted.", "success")
        else:
            flash("Delete failed.", "warning")
    except Exception as e:
        logger.error(f"Error deleting chat {chat_id}: {e}", exc_info=True)
        flash("Internal error.", "error")
    return redirect(url_for('index'))

# --- Flask Routes ---

@app.route('/', methods=['GET'])
async def index():
    # --- Make sure this full implementation is present ---
    db_status_ok = chat_db is not None
    recent_chats = []
    vector_stores = [] # Initialize to prevent potential errors if DB/API fails
    if db_status_ok:
        try: recent_chats = await asyncio.to_thread(chat_db.get_chats, limit=30)
        except Exception as e: logger.error(f"Error fetching recent chats: {e}"); flash("Error loading chats.", "error"); db_status_ok = False
    else: flash("DB error. Chat history disabled.", "error")
    try:
        vector_stores = await get_vector_stores()
    except Exception as e:
        logger.error(f"Error fetching VS list for index: {e}"); flash("Error loading KBs.", "error")

    # Check session for newly created VS
    new_vs_info = session.pop('new_vs_info', None)
    if new_vs_info:
        found = any(vs['id'] == new_vs_info['id'] for vs in vector_stores)
        if not found:
            logger.warning(f"Injecting recently created VS '{new_vs_info['name']}' into list.")
            vector_stores.insert(0, new_vs_info)

    # Render the template, passing None for chat-specific variables for the index page
    return render_template('chat_ui.html',
                           chats=recent_chats,
                           vector_stores=vector_stores,
                           db_status_ok=db_status_ok,
                           current_chat_id=None, # Explicitly None for index
                           current_chat_title='Agent RAG Chat', # Default title
                           current_chat_messages=None, # Explicitly None
                           current_vector_store_id=None) # Explicitly None
    # --- End of index function implementation ---

@app.route('/new_chat', methods=['POST'])
async def new_chat_route():
     # --- Ensure full implementation is here ---
     if not chat_db: flash("DB error.", "error"); return redirect(url_for('index'))
     vector_store_id = request.form.get('vector_store_id', '').strip()
     if not vector_store_id: flash("Select KB.", "error"); return redirect(url_for('index'))
     vs_list = await get_vector_stores(); vs_name = next((vs['name'] for vs in vs_list if vs['id'] == vector_store_id), vector_store_id[-6:])
     try:
        chat_id = await asyncio.to_thread(chat_db.create_chat, vector_store_id, f"Chat: {vs_name}")
        if chat_id: logger.info(f"Created chat {chat_id} for VS {vector_store_id}"); return redirect(url_for('chat_view', chat_id=chat_id))
        else: flash("Failed chat create.", "error"); return redirect(url_for('index'))
     except Exception as e: logger.error(f"Error creating chat in DB: {e}", exc_info=True); flash("Error creating chat.", "error"); return redirect(url_for('index'))
    # --- End of new_chat_route implementation ---

@app.route('/chat_view/<chat_id>', methods=['GET'])
async def chat_view(chat_id):
     # --- Ensure full implementation is here ---
     if not chat_db: flash("DB error.", "error"); return redirect(url_for('index'))
     try:
         chat_details = await asyncio.to_thread(chat_db.get_chat_details, chat_id);
         if not chat_details: flash("Chat not found.", "error"); return redirect(url_for('index'))
         messages = await asyncio.to_thread(chat_db.get_messages, chat_id, limit=200)
         vector_stores = await get_vector_stores(); recent_chats = await asyncio.to_thread(chat_db.get_chats, limit=30)
         return render_template('chat_ui.html', chats=recent_chats, vector_stores=vector_stores,
                                current_chat_id=chat_id, current_chat_title=chat_details.get('title', 'Chat'),
                                current_chat_messages=messages, current_vector_store_id=chat_details.get('vector_store_id'),
                                db_status_ok=True)
     except Exception as e: logger.error(f"Error loading chat view {chat_id}: {e}", exc_info=True); flash("Error loading chat.", "error"); return redirect(url_for('index'))
    # --- End of chat_view implementation ---

# --- DOCX Download Route ---
@app.route('/download_docx/<filename>', methods=['GET'])
def download_docx(filename):
    """Download a generated DOCX file."""
    try:
        # Validate filename to prevent directory traversal attacks
        if not re.match(r'^[\w\-_.]+\.docx$', filename):
            logger.error(f"Invalid DOCX filename requested: {filename}")
            return "Invalid filename", 400

        # Get the file path
        file_path = os.path.join(DOCX_OUTPUT_DIR, filename)

        # Check if the file exists
        if not os.path.exists(file_path):
            logger.error(f"DOCX file not found: {file_path}")
            return "File not found", 404

        # Return the file as an attachment
        return send_file(file_path, as_attachment=True, download_name=filename)
    except Exception as e:
        logger.error(f"Error downloading DOCX file: {e}", exc_info=True)
        return "Error downloading file", 500

# --- File Management API Routes ---
@app.route('/test_file_management', methods=['GET'])
def test_file_management():
    """Test route for file management functionality"""
    return render_template('test_file_management.html')

@app.route('/api/vector_stores', methods=['GET'])
async def vector_stores_api():
    """Get all vector stores"""
    try:
        # Get the OpenAI client
        client = get_openai_client()
        if not client:
            return jsonify({"error": "OpenAI client not available"}), 500

        # Get all vector stores
        vs_response = await asyncio.to_thread(
            client.vector_stores.list
        )

        # Format the response
        vector_stores = []
        for vs in vs_response.data:
            vector_stores.append({
                "id": vs.id,
                "name": vs.name,
                "created_at": vs.created_at
            })

        return jsonify({"vector_stores": vector_stores})
    except Exception as e:
        logger.error(f"Error getting vector stores: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/kb_files/<vector_store_id>', methods=['GET'])
async def kb_files_api(vector_store_id):
    """Get files in a knowledge base"""
    try:
        # Get the OpenAI client
        client = get_openai_client()
        if not client:
            return jsonify({"error": "OpenAI client not available"}), 500

        # Get files in the vector store
        files_response = await asyncio.to_thread(
            client.vector_stores.files.list,
            vector_store_id=vector_store_id
        )

        # Format the response
        files = []
        for file in files_response.data:
            # Debug log to see the actual structure
            logger.info(f"Vector store file object attributes: {dir(file)}")

            # Get the file ID - it might be 'id' instead of 'file_id'
            file_id = getattr(file, 'id', None)

            # If we can't find the ID directly, try to extract it from other attributes
            if not file_id:
                # Try to get it from the object representation
                file_str = str(file)
                logger.info(f"File object string representation: {file_str}")

                # Extract ID using regex if possible
                import re
                id_match = re.search(r'id=[\'\"](.+?)[\'\"]', file_str)
                if id_match:
                    file_id = id_match.group(1)
                    logger.info(f"Extracted file ID from string: {file_id}")

            # If we still don't have an ID, use a placeholder
            if not file_id:
                logger.warning(f"Could not determine file ID for file: {file}")
                file_id = f"unknown_{len(files)}"

            # Try to get file details if we have an ID
            file_details = None
            try:
                file_details = await asyncio.to_thread(
                    client.files.retrieve,
                    file_id=file_id
                )
            except Exception as e:
                logger.warning(f"Could not retrieve file details for ID {file_id}: {e}")

            # Build the file info with available data
            file_info = {
                "id": file_id,
                "status": getattr(file, 'status', 'unknown'),
                "metadata": getattr(file, 'metadata', {})
            }

            # Add file details if available
            if file_details:
                file_info.update({
                    "filename": file_details.filename,
                    "created_at": file_details.created_at
                })
            else:
                # Use fallback values
                file_info.update({
                    "filename": getattr(file, 'filename', f"File {len(files) + 1}"),
                    "created_at": getattr(file, 'created_at', None)
                })

            files.append(file_info)

        return jsonify({"files": files})
    except Exception as e:
        logger.error(f"Error getting KB files: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/chat_files/<chat_id>', methods=['GET'])
async def get_chat_files_api(chat_id):
    """Get file inclusion settings for a chat"""
    try:
        if not chat_db:
            return jsonify({"error": "Database not available"}), 500

        # Get chat details to verify it exists
        chat_details = await asyncio.to_thread(chat_db.get_chat_details, chat_id)
        if not chat_details:
            return jsonify({"error": "Chat not found"}), 404

        # Get file inclusion settings
        chat_files = await asyncio.to_thread(chat_db.get_chat_files, chat_id)

        # Format the response
        included_file_ids = [file["file_id"] for file in chat_files if file["included"]]
        excluded_file_ids = [file["file_id"] for file in chat_files if not file["included"]]

        return jsonify({
            "included_file_ids": included_file_ids,
            "excluded_file_ids": excluded_file_ids
        })
    except Exception as e:
        logger.error(f"Error getting chat files: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/chats', methods=['POST'])
async def create_chat_api():
    """Create a new chat"""
    try:
        if not chat_db:
            return jsonify({"error": "Database not available"}), 500

        # Get request data
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400

        title = data.get("title", "New Chat")
        vector_store_id = data.get("vector_store_id")

        if not vector_store_id:
            return jsonify({"error": "Vector store ID is required"}), 400

        # Create the chat
        chat_id = await asyncio.to_thread(chat_db.create_chat, title, vector_store_id)

        return jsonify({
            "status": "success",
            "message": "Chat created successfully",
            "chat_id": chat_id
        })
    except Exception as e:
        logger.error(f"Error creating chat: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/chat_files/<chat_id>', methods=['POST'])
async def set_chat_files_api(chat_id):
    """Set file inclusion settings for a chat"""
    try:
        if not chat_db:
            return jsonify({"error": "Database not available"}), 500

        # Get chat details to verify it exists
        chat_details = await asyncio.to_thread(chat_db.get_chat_details, chat_id)
        if not chat_details:
            return jsonify({"error": "Chat not found"}), 404

        # Get request data
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400

        included_file_ids = data.get("included_file_ids", [])
        excluded_file_ids = data.get("excluded_file_ids", [])

        # Update file inclusion settings
        if included_file_ids:
            await asyncio.to_thread(chat_db.set_chat_files, chat_id, included_file_ids, True)
        if excluded_file_ids:
            await asyncio.to_thread(chat_db.set_chat_files, chat_id, excluded_file_ids, False)

        return jsonify({"status": "success", "message": "File inclusion settings updated"})
    except Exception as e:
        logger.error(f"Error setting chat files: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/chats/<chat_id>/messages', methods=['POST'])
async def send_chat_message_api(chat_id):
    """Send a message to a chat"""
    try:
        if not chat_db:
            return jsonify({"error": "Database not available"}), 500

        # Get chat details to verify it exists
        chat_details = await asyncio.to_thread(chat_db.get_chat_details, chat_id)
        if not chat_details:
            return jsonify({"error": "Chat not found"}), 404

        # Get request data
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400

        user_message = data.get("message")
        if not user_message:
            return jsonify({"error": "Message is required"}), 400

        # Get chat history
        chat_history = await asyncio.to_thread(chat_db.get_chat_messages, chat_id)
        history_for_workflow = []
        for msg in chat_history:
            history_for_workflow.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        # Get vector store ID from chat details
        vector_store_id = chat_details["vector_store_id"]

        # Run the workflow
        final_markdown_response = await run_complex_rag_workflow(
            user_query=user_message,
            vs_id=vector_store_id,
            history=history_for_workflow,
            temp_files_info=None,
            template_to_populate=None,
            chat_id=chat_id
        )

        # Save the messages to the database
        await asyncio.to_thread(chat_db.add_message, chat_id, "user", user_message)
        await asyncio.to_thread(chat_db.add_message, chat_id, "assistant", final_markdown_response)

        return jsonify({
            "status": "success",
            "message": "Message sent successfully",
            "response": final_markdown_response
        })
    except Exception as e:
        logger.error(f"Error sending message: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# --- Template Management Routes ---
@app.route('/upload_template', methods=['POST'])
async def upload_template_route():
    """Handle template file uploads and save them to the template directory."""
    # Check if the file is in the request - handle both 'file' and 'template_files[]' field names
    file = None
    if 'file' in request.files:
        file = request.files['file']
    elif 'template_files[]' in request.files:
        file = request.files['template_files[]']
    else:
        logger.error("No file part in the request")
        return jsonify({"status": "error", "message": "No file part"}), 400

    if file.filename == '':
        logger.error("No file selected")
        return jsonify({"status": "error", "message": "No file selected"}), 400

    # Get metadata - handle both naming conventions
    title = request.form.get('title', '').strip() or request.form.get('template_title', '').strip()
    # Get description but don't use it yet - will be used in future implementation
    _ = request.form.get('description', '').strip() or request.form.get('template_description', '').strip()

    if not title:
        logger.error("No title provided for template")
        return jsonify({"status": "error", "message": "Template title is required"}), 400

    # Check file extension
    filename = secure_filename(file.filename)
    file_ext = os.path.splitext(filename)[1].lower().lstrip('.')

    if file_ext not in ALLOWED_TEMPLATE_EXTENSIONS:
        logger.error(f"Invalid file extension: {file_ext}")
        return jsonify({"status": "error", "message": f"Invalid file extension. Allowed: {', '.join(ALLOWED_TEMPLATE_EXTENSIONS)}"}), 400

    try:
        # Create a filename based on the title
        safe_title = secure_filename(title)
        template_filename = f"{safe_title}.{file_ext}"
        template_path = os.path.join(TEMPLATE_DIR, template_filename)

        # Save the file
        file.save(template_path)
        logger.info(f"Template saved: {template_path}")

        # For PDF files, we might want to extract text for preview/search
        template_text = ""
        if file_ext == 'pdf':
            try:
                # Use our robust PDF text extraction function
                template_text = extract_text_from_pdf(template_path)
                logger.info(f"Extracted {len(template_text)} characters of text from PDF template")
            except Exception as pdf_err:
                logger.warning(f"Could not extract text from PDF template: {pdf_err}")

        # Save metadata (optional - could be stored in a database or JSON file)
        # For now, we'll just return success
        return jsonify({
            "status": "success",
            "message": "Template uploaded successfully",
            "filename": template_filename,
            "title": title,
            "type": file_ext
        })

    except Exception as e:
        logger.error(f"Error saving template: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Error saving template: {str(e)}"}), 500

@app.route('/list_templates', methods=['GET'])
async def list_templates_route():
    """List all available templates with their metadata."""
    try:
        templates = []
        for filename in os.listdir(TEMPLATE_DIR):
            file_path = os.path.join(TEMPLATE_DIR, filename)
            if os.path.isfile(file_path):
                file_ext = os.path.splitext(filename)[1].lower().lstrip('.')
                if file_ext in ALLOWED_TEMPLATE_EXTENSIONS:
                    # For now, we'll just use the filename as the title
                    # In a more complete implementation, you'd load metadata from a database
                    title = os.path.splitext(filename)[0].replace('_', ' ').title()
                    templates.append({
                        "filename": filename,
                        "title": title,
                        "description": "",  # Would come from metadata in a full implementation
                        "type": file_ext
                    })

        return jsonify(templates)
    except Exception as e:
        logger.error(f"Error listing templates: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Error listing templates: {str(e)}"}), 500

# --- Cache Management Routes ---
@app.route('/cache/clear', methods=['POST'])
async def clear_cache_route():
    """Clear the semantic search cache"""
    try:
        await clear_semantic_cache()
        return jsonify({"status": "success", "message": "Semantic cache cleared"})
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/cache/stats', methods=['GET'])
async def cache_stats_route():
    """Get cache statistics"""
    try:
        stats = await get_cache_stats()
        return jsonify(stats)
    except Exception as e:
        logger.error(f"Error getting cache stats: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- Other Routes ---
@app.route('/create_vector_store', methods=['POST'])
async def create_vector_store_route():
    global vector_store_cache
    current_client = get_openai_client()
    if not current_client:
        flash("OpenAI client error.", "error")
        return redirect(url_for('index'))

    store_name = request.form.get('store_name', '').strip()
    if not store_name:
        flash("KB name cannot be empty.", "error")
        return redirect(request.referrer or url_for('index'))

    logger.info(f"Attempting to create VS: '{store_name}'")
    try:
        vs = await asyncio.to_thread(current_client.vector_stores.create, name=store_name)
        logger.info(f"Created VS ID: {vs.id}, Name: '{store_name}'")
        new_vs_details = {"id": vs.id, "name": vs.name or f"Store ({vs.id[-6:]})"}
        session['new_vs_info'] = new_vs_details
        logger.info(f"Stored new VS info in session: {new_vs_details}")
        vector_store_cache["list"] = []
        vector_store_cache["last_updated"] = 0
        logger.info("VS cache invalidated after creation.")
        flash(f"KB '{store_name}' created!", "success")

    except (AuthenticationError, APIStatusError) as api_err:
        logger.error(f"Auth/API Error creating VS: {api_err}")
        flash(f"API Error creating KB: {api_err}", "error")
    except Exception as e:
        logger.error(f"Error creating VS: {e}", exc_info=True)
        flash(f"Error creating KB: {str(e)}", "error")

    return redirect(request.referrer or url_for('index'))

@app.route('/upload_to_store', methods=['POST'])
async def upload_to_store_route():
    vector_store_id = request.form.get('vector_store_id')
    if not vector_store_id: flash("Please select KB.", "error"); return redirect(request.referrer or url_for('index'))

    uploaded_files = request.files.getlist('pdf_files[]') # Get the list of FileStorage objects
    if not uploaded_files or all(f.filename == '' for f in uploaded_files): flash("No files selected.", "warning"); return redirect(request.referrer or url_for('index'))

    # --- Parse the metadata JSON string ---
    metadata_json_str = request.form.get('metadata', '{}') # Get the JSON string, default to empty obj
    all_file_metadata = {}
    try:
        all_file_metadata = json.loads(metadata_json_str)
        logger.info(f"Received metadata for files: {all_file_metadata}")
    except json.JSONDecodeError:
        logger.error("Failed to parse metadata JSON from form.")
        flash("Error processing file metadata.", "error")
        return redirect(request.referrer or url_for('index'))
    # --- End Metadata Parsing ---

    saved_files_info = []; upload_errors = False
    # Save files temporarily, keep track of original filename for metadata lookup
    for file in uploaded_files:
        if file and allowed_file(file.filename):
            original_filename = secure_filename(file.filename)
            # Ensure original filename exists in metadata keys if that's how we map
            if original_filename not in all_file_metadata:
                logger.warning(f"Metadata not found for filename '{original_filename}'. Using defaults.")
                # Optionally add a default entry to all_file_metadata here if needed later
                # all_file_metadata[original_filename] = {} # Or skip the file?

            temp_filename = str(uuid.uuid4()) + "_" + original_filename
            temp_path = os.path.join(UPLOAD_FOLDER, temp_filename)
            try: file.save(temp_path); saved_files_info.append((temp_path, original_filename)); logger.info(f"Saved temp file '{original_filename}'.")
            except Exception as e: logger.error(f"Failed save {original_filename}: {e}"); flash(f"Error saving '{original_filename}'.", "error"); upload_errors = True
        elif file.filename != '': flash(f"'{file.filename}' type not allowed.", "error"); upload_errors = True

    if upload_errors: # If saving failed, cleanup and redirect
        for temp_path, _ in saved_files_info:
            try: os.remove(temp_path)
            except OSError as e: logger.warning(f"Could not remove temp file {temp_path}: {e}")
        return redirect(request.referrer or url_for('index'))
    if not saved_files_info: flash("No valid files were processed.", "warning"); return redirect(request.referrer or url_for('index'))

    # --- Call the modified function, passing the metadata dictionary ---
    upload_result = await add_files_to_vector_store(vector_store_id, saved_files_info, all_file_metadata)
    flash(upload_result["message"], upload_result["status"])
    # Temporary files are cleaned up inside add_files_to_vector_store now
    return redirect(request.referrer or url_for('index')) # Redirect back

# --- Custom Runner with Non-Strict Schema Validation ---
class NonStrictRunner(Runner):
    """Custom Runner that uses non-strict schema validation for all agents."""

    @classmethod
    def _get_output_schema(cls, agent: Agent) -> AgentOutputSchemaClass | None:
        """Override to use non-strict schema validation."""
        if agent.output_type is None or agent.output_type is str:
            return None

        return AgentOutputSchemaClass(agent.output_type, strict_json_schema=False)

# Replace the standard Runner with our custom one
Runner = NonStrictRunner

# --- Import Async Utilities ---
from async_utils import setup_async_for_flask

# --- Main Execution Block ---
if __name__ == '__main__':
    logger.info("Starting Flask application with Agent SDK integration...")

    # Set up asyncio for Flask
    setup_async_for_flask()

    if os.getenv('FLASK_DEBUG') == '1':
        try:
            # Now check for the correctly imported TracingProcessor
            ActualTraceProcessorBase = globals().get('TracingProcessor')
            CustomProcessor = globals().get('PrintTraceProcessor')
            # Ensure base class was imported and custom class inherits from it
            if ActualTraceProcessorBase and CustomProcessor and ActualTraceProcessorBase != object and issubclass(CustomProcessor, ActualTraceProcessorBase):
                 add_trace_processor(CustomProcessor()) # Register instance
                 logger.info("Added PrintTraceProcessor for agent debugging (set logging to DEBUG).")
            else:
                 logger.warning("Trace processor components not correctly defined/imported, skipping registration.")
        except Exception as trace_reg_err:
             logger.error(f"Error during Trace Processor registration: {trace_reg_err}", exc_info=True)

    use_debug = os.getenv('FLASK_DEBUG', '0') == '1'
    logger.info(f"Flask debug mode: {use_debug}")
    app.run(debug=use_debug, host='0.0.0.0', port=5001)