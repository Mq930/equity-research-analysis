import os
import json
import warnings
import asyncio
from pathlib import Path
from typing import List

import requests
from bs4 import BeautifulSoup

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_community.vectorstores import FAISS
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langchain_text_splitters import RecursiveCharacterTextSplitter

warnings.filterwarnings("ignore")
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set. Add it to backend/.env")

# -------------------- App Setup -------------------- #
app = FastAPI(title="Equity Research AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your actual frontend origin in production
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- Globals (single-process, in-memory state) -------------------- #
llm = ChatGroq(api_key=GROQ_API_KEY, model="llama-3.1-8b-instant", streaming=True)

_embeddings = None

def get_embeddings():
    global _embeddings

    if _embeddings is None:
        _embeddings = GoogleGenerativeAIEmbeddings(
            model="models/text-embedding-004",
            google_api_key=os.getenv("GOOGLE_API_KEY")
        )

    return _embeddings

vectorstore = None  # FAISS index, built after /api/process-urls

ANALYST_SYSTEM_PROMPT = (
    "You are a Senior Equity Research Analyst specializing in financial analysis, market strategy, and fundamental company valuation. "
    "Your communication style is professional, objective, concise, and structured like an institutional research report.\n\n"
    "OPERATIONAL RULES:\n"
    "1. If context documents are provided below, base your financial insights, metrics, and takeaways strictly on that context.\n"
    "2. If NO context documents are available (or the user asks for analysis without processing sources first), state professionally "
    "that you require research material to conduct an accurate evaluation, and explicitly ask the user to input and process relevant URLs.\n"
    "3. Keep responses structured using professional terminology (e.g., Valuation, Growth Drivers, Risks, Financial Highlights).\n\n"
    "Retrieved Context:\n{context}"
)

GENERAL_SYSTEM_PROMPT = (
    "You are a Senior Equity Research Analyst specializing in financial analysis, market strategy, and fundamental company valuation. "
    "Your communication style is professional, objective, concise, and structured like an institutional research report.\n\n"
    "No source documents have been processed yet. State professionally that you require research material to conduct an "
    "accurate evaluation, and ask the user to submit and process relevant URLs before proceeding, unless the question is "
    "general financial knowledge you can answer responsibly without sources."
)


# -------------------- Schemas -------------------- #
class ProcessRequest(BaseModel):
    urls: List[str]


class ChatRequest(BaseModel):
    message: str


# -------------------- Helpers -------------------- #
def fetch_and_clean_url(url: str) -> str:
    """Fetch a URL and extract readable text. Raises a descriptive error on failure."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise ValueError(f"Could not fetch '{url}': {e}")

    soup = BeautifulSoup(resp.text, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)

    if not text or len(text) < 50:
        raise ValueError(f"No readable text extracted from '{url}'.")

    return text


# -------------------- Routes -------------------- #
@app.get("/api/health")
def health():
    return {"status": "ok", "vectorstore_ready": vectorstore is not None}


@app.post("/api/process-urls")
def process_urls(req: ProcessRequest):
    global vectorstore

    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="Please provide at least one valid URL.")

    docs = []
    errors = []

    for url in urls:
        try:
            text = fetch_and_clean_url(url)
            docs.append(Document(page_content=text, metadata={"source": url}))
        except ValueError as e:
            errors.append(str(e))

    if not docs:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to process any URLs. Errors: {'; '.join(errors)}",
        )

    try:
        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        split_docs = splitter.split_documents(docs)

        embeddings = get_embeddings()
        vectorstore = FAISS.from_documents(split_docs, embeddings)

        result = {
            "status": "success",
            "chunks_indexed": len(split_docs),
            "urls": [d.metadata["source"] for d in docs],
        }
        if errors:
            result["warnings"] = errors
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error indexing documents: {str(e)}")


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    async def event_stream():
        sources_found = set()
        try:
            if vectorstore is not None:
                prompt = ChatPromptTemplate.from_messages([
                    ("system", ANALYST_SYSTEM_PROMPT),
                    ("human", "{input}"),
                ])
                question_answer_chain = create_stuff_documents_chain(llm, prompt)
                rag_chain = create_retrieval_chain(vectorstore.as_retriever(), question_answer_chain)

                for chunk in rag_chain.stream({"input": req.message}):
                    if "answer" in chunk and chunk["answer"]:
                        yield f"data: {json.dumps({'type': 'token', 'content': chunk['answer']})}\n\n"
                        await asyncio.sleep(0)
                    if "context" in chunk:
                        for doc in chunk["context"]:
                            src = doc.metadata.get("source")
                            if src:
                                sources_found.add(src)

                yield f"data: {json.dumps({'type': 'sources', 'content': list(sources_found)})}\n\n"
            else:
                prompt = ChatPromptTemplate.from_messages([
                    ("system", GENERAL_SYSTEM_PROMPT),
                    ("human", "{input}"),
                ])
                chain = prompt | llm
                for chunk in chain.stream({"input": req.message}):
                    token = getattr(chunk, "content", "") or ""
                    if token:
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                        await asyncio.sleep(0)

            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# Frontend is deployed separately on Vercel; this backend is API-only.
