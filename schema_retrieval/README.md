# schema_retrieval

Column-level schema retrieval, with runnable demos.

---

## Layout

```text
schema_retrieval/
  __init__.py

  # Core structures and schema handling
  objects.py                     # Core data structures
  sqlite_loader.py               # SQLite schema parsing
  document_builder.py            # Column-level document construction
  graph_builder.py               # Schema subgraph construction

  # Keyword retrieval
  tokenizer.py                   # Mixed Chinese/English tokenizer
  bm25.py                        # BM25 keyword index
  retriever.py                   # Keyword retrieval entry point

  # Vector retrieval
  keyword_extractor_client.py    # Model-driven keyword extraction
  embedding_client.py            # Embedding client
  vector_index.py                # In-memory vector index

  # Fusion and reranking
  rrf_fusion_client.py           # RRF fusion
  rerank_client.py               # Reranking

  # Demos
  keyword_recall_demo.py         # Keyword retrieval
  vector_recall_demo.py          # Vector retrieval
  hybrid_recall_demo.py          # Keyword + vector + RRF fusion
  hybrid_rerank_schema_demo.py   # Hybrid retrieval + rerank + SchemaGraph

  README.md
```

---

## Environment variables

The demos run end to end against a local mock with nothing configured.

To call a real model:

```bash
export DASHSCOPE_API_KEY="your-api-key"
```

Reranking needs one more:

```bash
export DASHSCOPE_WORKSPACE_ID="your-workspace-id"
```

---

## Commands

### 1. Keyword retrieval

```bash
python -m schema_retrieval.keyword_recall_demo
```

### 2. Vector retrieval

```bash
python -m schema_retrieval.vector_recall_demo
```

### 3. Hybrid retrieval

```bash
python -m schema_retrieval.hybrid_recall_demo
```

### 4. Hybrid retrieval + rerank + SchemaGraph

```bash
python -m schema_retrieval.hybrid_rerank_schema_demo
```

---

## Clearing caches

```bash
rm -rf schema_retrieval/__pycache__
```
