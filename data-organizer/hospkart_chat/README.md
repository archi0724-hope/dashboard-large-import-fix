# HospKart BD AI Chatbot

Production-style local assistant for business development workflows:
- Semantic product search over catalog
- Quotation generation with GST and discount support
- Multi-turn context tracking
- JSON and PDF quotation export
- CLI, Flask API, and browser chat UI
- Optional Ollama-powered response formatting

## Architecture
Designed as:
1. Data ingestion (`data/hospkart_products.csv`)
2. Embedding generation (`sentence-transformers`)
3. Vector retrieval (`ChromaDB`)
4. Agent orchestration (`hospkart_agent.py`)
5. Output formatting + export (`exports/*.json`, `exports/*.pdf`)

## Quick Start
```bash
python setup.py
python quickstart.py
```
`setup.py` automatically creates `.env` from `.env.example` on first run.

For API:
```bash
python app.py
```
Then call:
```bash
curl -X POST http://localhost:5000/chat -H "Content-Type: application/json" -d "{\"query\":\"Quote for 10 hospital beds\"}"
```

Or open browser:
- [http://localhost:5000](http://localhost:5000)
- API endpoint: `POST /api/chat` (also supports `POST /chat`)
- Health endpoint: `GET /health`
- Runtime config endpoint: `GET /debug/config`
- Model sanity endpoint: `GET /model-test`

Example:
```bash
curl "http://localhost:5000/model-test?prompt=Create%20a%20quotation%20summary%20for%2020%20beds"
```

## Ollama (Optional)
Install and run Ollama locally, then:
```bash
set USE_OLLAMA=true
set OLLAMA_MODEL=llama3.2
```
If Ollama/model is unavailable, the app falls back to template-based responses.

Main env keys:
- `USE_OLLAMA`
- `OLLAMA_MODEL`
- `EMBEDDING_MODEL`
- `VECTOR_DB_PATH`
- `FLASK_HOST`
- `FLASK_PORT`

## Dataset Cleaning + Fine-tune Pipeline
If your source dataset is wide/messy, run:

```bash
python scripts/preprocess_dataset.py --input "data/raw_products.xlsx" --output "data/hospkart_products.cleaned.csv"
```

If column names are very custom, pass explicit map:
```bash
python scripts/preprocess_dataset.py --input "data/raw.csv" --mapping "{\"product_name\":\"Item Name\",\"price\":\"MRP\",\"vendor_name\":\"Supplier\"}"
```

Then generate fine-tune JSONL:
```bash
python scripts/generate_finetune_data.py --input "data/hospkart_products.cleaned.csv" --output "data/training_data.jsonl"
```

Optional Ollama model build (if you have a `Modelfile`):
```bash
ollama create hospkart-bd -f Modelfile
```

Use this custom model in app:
```bash
set OLLAMA_MODEL=hospkart-bd
set USE_OLLAMA=true
```

### One-command live dataset pipeline
For your current file `data/live_data_export.csv`, run:
```bash
python scripts/train_and_index.py --input "data/live_data_export.csv"
```
This command will:
- clean and normalize source data
- update runtime catalog at `data/hospkart_products.csv`
- regenerate `data/training_data.jsonl`
- rebuild Chroma vector index used by the agent

Optional (also rebuild Ollama model):
```bash
python scripts/train_and_index.py --input "data/live_data_export.csv" --rebuild-ollama --ollama-model hospkart-bd
```

## Required CSV Schema
`id,product_name,category,manufacturer,vendor_name,price,specs,warranty,stock`

## Deploy (same behavior as local)

This repo includes:
- `Procfile` for web process startup
- `scripts/bootstrap_index.py` to auto-create/refresh Chroma index before app start (when not in lightweight mode)

### Deploy steps
1. Add environment variables from `.env.example` in your hosting dashboard.
2. Ensure `PRODUCT_CSV_PATH` points to a valid catalog CSV in deployed repo.
3. Use build command:
   ```bash
   pip install -r requirements.txt
   ```
4. Use start command (or let `Procfile` run automatically):
   ```bash
   gunicorn app:app --workers 1 --bind 0.0.0.0:${PORT:-5000}
   ```

### Low-memory hosting (512MB)
- Set `LIGHTWEIGHT_MODE=true` to skip loading sentence-transformers/Chroma in web runtime.
- In this mode, product matching uses CSV keyword scoring (lower memory, slightly lower semantic quality).
- If you later upgrade memory plan, set `LIGHTWEIGHT_MODE=false` and run:
  ```bash
  python scripts/bootstrap_index.py
  ```

### Persistent storage recommendation
- If your host supports persistent disk, set `VECTOR_DB_PATH` there (example `/data/chroma_db`).
- If persistent disk is not available, index will rebuild on start (works, but slower startup).
