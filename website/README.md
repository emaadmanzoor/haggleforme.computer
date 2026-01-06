# WhichLlama Web Edition

Minimal web port of the WhichLlama mystery-model game. A FastAPI backend orchestrates game state and OpenRouter calls, while a React front end delivers a bright terminal-inspired interface.

## Prerequisites

- Python 3.11+
- Node.js 18+
- Environment variable `OPENROUTER_KEY` available to the backend process

## Backend

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn website.backend.main:app --reload --host 0.0.0.0 --port 8000
```

The backend keeps per-session game state in memory and persists leaderboard stats to `website/backend/leaderboard.json`.

## Frontend

```bash
cd website
npm install
npm run dev
```

The Vite dev server proxies `/api/*` calls to `http://localhost:8000`, so running both processes locally enables the full experience. The production build lives under `website/dist` after `npm run build`.

## Game Flow

- A new session rolls a random username and hidden model from the OpenRouter free tier candidates.
- Each prompt counts as one of five questions and streams the model response back into the terminal.
- Enter `/guess` or press **Guess** to open the model picker — one guess per session.
- The global leaderboard tracks wins, losses, and questions asked across all web players.
