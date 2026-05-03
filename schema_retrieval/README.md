# schema_retrieval Demo

字段级 Schema 检索 Demo。

---

## 目录结构

```text
schema_retrieval/
  __init__.py

  # 核心数据结构与 Schema 处理
  objects.py                     # 核心数据结构
  sqlite_loader.py               # SQLite Schema 解析
  document_builder.py            # 字段级检索文档构建
  graph_builder.py               # Schema 子图构建

  # 关键词召回
  tokenizer.py                   # 中英文混合分词
  bm25.py                        # BM25 关键词索引
  retriever.py                   # 关键词召回主流程

  # 向量召回
  keyword_extractor_client.py    # 大模型关键词抽取
  embedding_client.py            # 阿里云 Embedding Client
  vector_index.py                # 内存向量索引

  # 融合与精排
  rrf_fusion_client.py           # RRF 融合
  rerank_client.py               # Rerank 精排

  # Demo
  keyword_recall_demo.py         # 关键词召回 Demo
  vector_recall_demo.py          # 向量召回 Demo
  hybrid_recall_demo.py          # 关键词 + 向量 + RRF 融合 Demo
  hybrid_rerank_schema_demo.py   # 混合召回 + Rerank + SchemaGraph Demo

  README.md
```

---

## 环境变量

不配置环境变量也可以先跑通本地 Mock 流程。

调用阿里云百炼真实模型时配置：

```bash
export DASHSCOPE_API_KEY="你的阿里云百炼APIKey"
```

调用 Rerank 模型时额外配置：

```bash
export DASHSCOPE_WORKSPACE_ID="你的WorkspaceId"
```

---

## 执行命令

### 1. 关键词召回

```bash
python -m schema_retrieval.keyword_recall_demo
```

### 2. 向量召回

```bash
python -m schema_retrieval.vector_recall_demo
```

### 3. 混合召回

```bash
python -m schema_retrieval.hybrid_recall_demo
```

### 4. 混合召回 + Rerank + SchemaGraph

```bash
python -m schema_retrieval.hybrid_rerank_schema_demo
```

---

## 清理缓存

```bash
rm -rf schema_retrieval/__pycache__
```
