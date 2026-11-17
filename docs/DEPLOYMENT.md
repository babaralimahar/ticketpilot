# Deployment

Target: **Render free web services** (Docker) + **Neon Postgres**, the same as P1/P2.

> Status: **live.** Agent API https://ticketpilot-api-lcf9.onrender.com and mock shop
> https://ticketpilot-e331.onrender.com (Render free, Singapore), one Neon free database. The shop
> was seeded from a local machine. `SHOP_WAKE_S=90` makes the agent wait for the sleeping shop.

TicketPilot needs two services from the same image: the **API** (`ticketpilot.main`) and the
**mock shop** (`shopsim.app`) that the agent's tools call. In a real deployment, shopsim is
replaced by the store's actual order/refund API (Shopify, Stripe, an internal OMS); only
`ticketpilot/adapters/shop.py` changes.

## Steps

1. **Push to GitHub.** From `D:\AI Projects\ticketpilot`, add a remote and push with tags.
2. **Neon.** Create a project and copy the **direct** connection string (pooling off: the
   LangGraph checkpointer uses psycopg with `prepare_threshold=0`, but direct is simplest).
3. **Migrate and seed from your machine:**
   ```powershell
   $env:DATABASE_URL="<neon url>"; $env:SHOPSIM_DATABASE_URL="<neon url>"
   uv run alembic upgrade head
   uv run python -m shopsim.seed
   ```
   The LangGraph checkpoint tables are created automatically on first run.
4. **Render, service 1: `ticketpilot-shop`.** New → Web Service → Docker, start command
   `uvicorn shopsim.app:create_app --factory --host 0.0.0.0 --port $PORT`, env:
   `SHOPSIM_DATABASE_URL` (Neon URL), `SHOPSIM_SERVICE_TOKEN` (random).
5. **Render, service 2: `ticketpilot`.** Default Dockerfile command. Environment:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | the Neon URL |
   | `ENV` | `prod` |
   | `LLM_PROVIDER` | `openai_compat` |
   | `LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` |
   | `LLM_MODEL` | `gemini-3.1-flash-lite` |
   | `LLM_API_KEY` | your Gemini key |
   | `SHOPSIM_URL` | the shop service URL |
   | `SHOPSIM_TOKEN` | same value as `SHOPSIM_SERVICE_TOKEN` |
   | `BUSINESS_DATE` | `2026-11-10` (the synthetic shop's "today"; unset for a real shop) |
   | `WORKER_MODE` | `off` |

6. **Keys.** Create API keys: `uv run python -m app.cli create-key --name agent1 --role clerk`,
   `--role approver` for supervisors, `--role admin` for the kill switch.
7. **Verify.** `/readyz` returns 200; sign in at `/ui` with a clerk key, submit the sample
   ticket, then approve a >USD 50 refund as approver at `/ui/approvals`.

## Production notes

- **Free-tier sleep:** a sleeping Render service drops in-flight background runs. That is safe
  (checkpoints are durable and writes are idempotent) but the run stays `running`; a real
  deployment adds a sweeper that resumes stale runs from their last checkpoint.
- **Provider data retention:** ticket text and order facts go to the model provider. Use a paid
  tier with no training on your data before handling real customers.
- **Rate limits:** the Gemini free tier rate-limits hard (HTTP 429). The client retries with
  backoff; a run that still cannot reach the model ends `failed`, never half-done.
