import asyncio
import json
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from openai import AsyncOpenAI

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


load_dotenv()


MCP_URL = os.environ.get(
    "MCP_URL",
    "http://localhost:8000/mcp",
)

OPENAI_MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-5",
)

client = AsyncOpenAI()

app = FastAPI(
    title="Home Assistant AI",
)


class ChatRequest(BaseModel):
    message: str
