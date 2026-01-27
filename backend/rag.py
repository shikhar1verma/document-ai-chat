"""Retrieval and QA utilities."""

import logging
import os
from typing import List, Optional

import psycopg
from psycopg import Connection
from dotenv import load_dotenv
from langchain_google_genai import GoogleGenerativeAIEmbeddings, GoogleGenerativeAI
from pgvector.psycopg import register_vector

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")

def _get_connection() -> Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg.connect(DATABASE_URL)
    register_vector(conn)
    return conn


def retrieve(question: str, upload_id: str, top_k: int = 5) -> List[str]:
    """Retrieve top-k relevant text chunks from the database."""
    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=GEMINI_API_KEY)
    query_vec = embeddings.embed_query(question, output_dimensionality=768)
    conn = _get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT chunk_text
                    FROM documents
                    WHERE upload_id = %s
                    ORDER BY chunk_embedding <-> (%s::vector(768))
                    LIMIT %s
                    """,
                    (upload_id, query_vec, top_k),
                )
                rows = cur.fetchall()
                return [row[0] for row in rows]
    finally:
        conn.close()


def answer_question(question: str, upload_id: str) -> str:
    """Answer a question using retrieved context."""
    chunks = retrieve(question, upload_id)
    context = "\n".join(chunks)
    llm = GoogleGenerativeAI(
        model="gemini-flash-latest",
        google_api_key=GEMINI_API_KEY)
    prompt = (
        "You are a contract-savvy assistant. Use ONLY the following excerpts to answer:\n"
        f"{context}\n\nQuestion: {question}"
    )
    logging.info("Generating answer...")
    return llm(prompt)
