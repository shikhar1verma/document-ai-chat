"""PDF ingestion + progress helpers for Document-AI Chat."""

import logging, os, re, uuid, tempfile, shutil
from typing import List

import psycopg
from psycopg import Connection
from dotenv import load_dotenv
from langchain_community.document_loaders import PyPDFLoader
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from pgvector.psycopg import register_vector

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")  # keep TAB/LF/CR


def _sanitize(text: str) -> str:
    """Strip control chars that Postgres TEXT cannot store."""
    return _CONTROL_CHARS_RE.sub("", text)


def _get_connection() -> Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg.connect(DATABASE_URL)
    register_vector(conn)
    return conn


def create_upload(upload_id: str, filename: str) -> None:
    """Insert placeholder row so UI can start polling."""
    conn = _get_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO uploads (upload_id, filename) VALUES (%s, %s)",
                (upload_id, filename),
            )
    finally:
        conn.close()


def update_progress(upload_id: str, message: str) -> None:
    """Overwrite progress string."""
    conn = _get_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE uploads SET progress = %s WHERE upload_id = %s",
                (message, upload_id),
            )
    finally:
        conn.close()

def ingest_file(file_path: str, filename: str, upload_id: str) -> None:
    """
    Heavy-weight background task.
    • Parses PDF  → splits chunks → embeds → stores vectors.
    • Emits progress text at coarse milestones.
    """
    update_progress(upload_id, "Ingestion started")
    logging.info("Loading PDF: %s", file_path)

    # 1. Read PDF
    update_progress(upload_id, "10% – parsing PDF")
    loader = PyPDFLoader(file_path)
    documents = loader.load()

    # 2. Chunk
    splitter = RecursiveCharacterTextSplitter(chunk_size=5000, chunk_overlap=400)
    chunks = splitter.split_documents(documents)
    logging.info("Chunks created: %d", len(chunks))
    update_progress(upload_id, "40% – splitting into chunks")

    # 3. Embeddings
    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=GEMINI_API_KEY,
    )
    texts: List[str] = [_sanitize(doc.page_content) for doc in chunks]
    update_progress(upload_id, "60% – generating embeddings")
    vectors = embeddings.embed_documents(texts, output_dimensionality=768)

    # 4. Insert rows
    conn = None
    total = len(texts)
    try:
        conn = _get_connection()
        with conn, conn.cursor() as cur:
            for idx, (text, vector) in enumerate(zip(texts, vectors), start=1):
                cur.execute(
                    """
                    INSERT INTO documents (upload_id, chunk_text, chunk_embedding)
                    VALUES (%s, %s, %s)
                    """,
                    (upload_id, text, vector),
                )
                # 25 / 50 / 75 %
                if idx in {int(total * 0.25), int(total * 0.50), int(total * 0.75)}:
                    pct = 60 + int(idx / total * 30)
                    update_progress(upload_id, f"{pct}% – indexing chunks")
    except Exception as exc:
        update_progress(upload_id, f"Error: {exc}")
        raise
    finally:
        if conn:
            conn.close()
        # clean tmp
        try:
            os.remove(file_path)
        except OSError:
            pass

    update_progress(upload_id, "100% – ingestion complete. Ready for chat ✔")
    logging.info("Ingestion complete for %s", filename)
