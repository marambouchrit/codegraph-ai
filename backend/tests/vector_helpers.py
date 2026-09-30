"""Helpers for the vector tests: a tiny embedding model and an in-memory Qdrant.

`HashingEmbeddings` stands in for the real model so tests need no download: each
word (split on camelCase and snake_case) is hashed into one of `dimension`
buckets, and the vector is normalized. Texts sharing words get close vectors,
which is enough to test search, filters and ranking deterministically. The real
model is covered by the optional `pytest -m embeddings` tests.

The vector store is the real `QdrantVectorStore`, on qdrant-client's local
in-memory mode (`QdrantClient(":memory:")`): Qdrant's own implementation of
collections, filters, upserts and search, without a server.
"""

import hashlib
import math
import re
from collections.abc import Sequence

from qdrant_client import QdrantClient

from app.core.errors import EmbeddingModelError
from app.rag.embeddings import EmbeddingProvider, Vector
from app.rag.vector_store import QdrantVectorStore

WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def words(text: str) -> list[str]:
    return [word.lower() for word in WORD.findall(text)]


class HashingEmbeddings(EmbeddingProvider):
    def __init__(self, dimension: int = 256, model_name: str = "test/hashing") -> None:
        self._dimension = dimension
        self._model_name = model_name
        self.documents_embedded = 0
        self.queries_embedded = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        self.documents_embedded += len(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> Vector:
        self.queries_embedded += 1
        return self._vector(text)

    def _vector(self, text: str) -> Vector:
        vector = [0.0] * self._dimension
        for word in words(text):
            bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % (self._dimension - 1)
            vector[bucket + 1] += 1.0
        if not any(vector):
            vector[0] = 1.0  # no words: a fixed direction (a zero vector has no angle)
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


class FailingEmbeddings(HashingEmbeddings):
    """Loads fine, then fails to embed documents after `fail_after` of them."""

    def __init__(self, fail_after: int = 0) -> None:
        super().__init__()
        self.fail_after = fail_after

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        if self.documents_embedded + len(texts) > self.fail_after:
            raise EmbeddingModelError("The embedding model failed to embed the text.")
        return super().embed_documents(texts)


def memory_store(collection: str = "test_chunks") -> QdrantVectorStore:
    return QdrantVectorStore(QdrantClient(":memory:"), collection)


# A small project for end-to-end tests: authentication code, and unrelated code.
AUTH_PROJECT: dict[str, str] = {
    "app/auth/service.py": '''"""User authentication: password check and JWT tokens."""
import hashlib

import jwt

SECRET_KEY = "change-me"


class AuthService:
    """Authenticates users with a username and a password."""

    def __init__(self, database):
        self.database = database

    def login(self, username, password):
        """Check the password of the user and return an access token."""
        user = self.database.find_user(username)
        if user is None or not verify_password(password, user.password_hash):
            raise PermissionError("invalid credentials")
        return self.create_token(user)

    def create_token(self, user):
        """Create a signed JWT access token for the authenticated user."""
        return jwt.encode({"sub": user.id}, SECRET_KEY, algorithm="HS256")


def verify_password(password, password_hash):
    """Compare a password with its stored hash."""
    return hashlib.sha256(password.encode()).hexdigest() == password_hash
''',
    "app/db/database.py": '''class Database:
    """Stores users in memory."""

    def __init__(self):
        self.users = {}

    def find_user(self, username):
        return self.users.get(username)

    def save_user(self, user):
        self.users[user.username] = user
''',
    "app/reports/charts.py": '''def render_bar_chart(values, width=40):
    """Draw a horizontal bar chart of monthly sales figures as text."""
    peak = max(values) or 1
    return "\\n".join("#" * int(width * value / peak) for value in values)


def average(values):
    return sum(values) / len(values)
''',
    "web/src/cart.ts": '''export interface CartItem {
  sku: string;
  quantity: number;
  unitPrice: number;
}

export class ShoppingCart {
  private items: CartItem[] = [];

  addItem(item: CartItem): void {
    this.items.push(item);
  }

  totalPrice(): number {
    return this.items.reduce((sum, item) => sum + item.quantity * item.unitPrice, 0);
  }
}
''',
}
